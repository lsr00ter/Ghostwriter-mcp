"""Tests for the MCP tool layer.

The decorated functions are still plain callables (MCPServer's ``@tool``
returns the original function), so they can be invoked directly.
"""

import json
import os
import unittest
from unittest import mock

import httpx2
from mcp.server.mcpserver.exceptions import ToolError

import ghostwriter_api as gw
import main

ENV = {
    "GHOSTWRITER_GRAPHQL_URL": "https://gw.test/v1/graphql",
    "GHOSTWRITER_API_TOKEN": "test-token",
}


class ToolTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self._env = mock.patch.dict(os.environ, ENV, clear=True)
        self._env.start()
        gw.set_transport(None)

    async def asyncTearDown(self):
        await gw.close_client()
        gw.set_transport(None)
        self._env.stop()

    def queue(self, *payloads, status_code=200):
        responses = list(payloads)

        def handler(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            payload = responses.pop(0) if responses else {}
            return httpx2.Response(status_code, json=payload)

        gw.set_transport(httpx2.MockTransport(handler))


class TestToolErrorHandling(ToolTestCase):
    async def test_failure_raises_tool_error(self):
        self.queue({"errors": [{"message": "nope"}]})
        with self.assertRaises(ToolError):
            await main.search_ghostwriter_findings("x")

    async def test_config_failure_raises_tool_error(self):
        with mock.patch.dict(
            os.environ, {"GHOSTWRITER_GRAPHQL_URL": "", "GHOSTWRITER_URL": ""}, clear=False
        ):
            with self.assertRaises(ToolError):
                await main.search_ghostwriter_findings("x")

    async def test_missing_row_raises_tool_error(self):
        self.queue({"data": {"insert_project_one": None}})
        with self.assertRaises(ToolError):
            await main.create_ghostwriter_project(1, "code", 1)


class TestSearchTools(ToolTestCase):
    async def test_null_severity_and_description_are_safe(self):
        self.queue(
            {
                "data": {
                    "finding": [
                        {"id": 1, "title": "X", "description": None, "severity": None}
                    ]
                }
            }
        )
        result = await main.search_ghostwriter_findings("x")
        self.assertEqual(result[0]["severity"], "Unknown")
        self.assertEqual(result[0]["description"], "")

    async def test_long_description_is_truncated(self):
        self.queue(
            {
                "data": {
                    "finding": [
                        {
                            "id": 1,
                            "title": "X",
                            "description": "a" * 250,
                            "severity": {"severity": "High"},
                        }
                    ]
                }
            }
        )
        result = await main.search_ghostwriter_findings("x")
        self.assertEqual(len(result[0]["description"]), 103)
        self.assertTrue(result[0]["description"].endswith("..."))

    async def test_empty_result_returns_empty_list(self):
        self.queue({"data": {"finding": None}})
        self.assertEqual(await main.search_ghostwriter_findings("x"), [])


class TestCreateTools(ToolTestCase):
    async def test_create_client_uses_shortName_key(self):
        self.queue(
            {
                "data": {
                    "insert_client_one": {
                        "id": 10,
                        "name": "Acme",
                        "codename": "ACME",
                        "shortName": "AC",
                        "address": None,
                        "description": None,
                    }
                }
            }
        )
        result = await main.create_ghostwriter_client("Acme", "AC", "ACME")
        self.assertEqual(result["shortName"], "AC")
        self.assertEqual(result["id"], 10)
        self.assertIn("clientId", result["_workflow_note"])

    async def test_create_project_returns_camel_case_dates(self):
        self.queue(
            {
                "data": {
                    "insert_project_one": {
                        "id": 11,
                        "codename": "ACME",
                        "startDate": "2026-01-01",
                        "endDate": "2026-01-31",
                    }
                }
            }
        )
        result = await main.create_ghostwriter_project(10, "ACME", 2)
        # search_ghostwriter_projects also reports camelCase dates, so the two
        # tools must not disagree about the shape of the same entity.
        self.assertEqual(result["startDate"], "2026-01-01")
        self.assertEqual(result["endDate"], "2026-01-31")

    async def test_create_finding_accepts_every_optional_argument(self):
        """The full MCP surface for this tool must stay callable.

        A rename here once left the tool passing a keyword the API function did
        not accept, which only surfaced in a live call.
        """
        self.queue(
            {
                "data": {
                    "insert_finding_one": {
                        "id": 12,
                        "title": "SQLi",
                        "description": "desc",
                    }
                }
            }
        )
        result = await main.create_ghostwriter_finding(
            "SQLi",
            "desc",
            findingTypeId=4,
            severityId=2,
            cvssScore=5.0,
            cvssVector="CVSS:3.1/AV:N",
            replication_steps="1. do a thing",
            extraFields={"custom": 1},
        )
        self.assertEqual(result["id"], 12)
        obj = json.loads(self.requests[0].content)["variables"]["object"]
        self.assertEqual(obj["extraFields"], {"custom": 1})
        self.assertEqual(obj["cvssScore"], 5.0)
        self.assertEqual(obj["findingTypeId"], 4)
        self.assertEqual(obj["severityId"], 2)


class TestAttachTool(ToolTestCase):
    async def test_attach_by_title_searches_then_attaches(self):
        self.queue(
            {"data": {"finding": [{"id": 42, "title": "SQLi"}]}},
            {"data": {"attachFinding": {"id": 99}}},
        )
        result = await main.attach_finding_to_report("SQLi", 7)
        self.assertEqual(result, {"reportedFindingId": 99, "usedFindingId": 42})
        # second request is the attach mutation
        self.assertIn("attachFinding", json.loads(self.requests[1].content)["query"])

    async def test_attach_by_title_without_match_raises(self):
        self.queue({"data": {"finding": []}})
        with self.assertRaises(ToolError):
            await main.attach_finding_to_report("missing", 7)

    async def test_attach_by_id_skips_search(self):
        self.queue({"data": {"attachFinding": {"id": 5}}})
        result = await main.attach_finding_to_report(42, 7)
        self.assertEqual(result["usedFindingId"], 42)
        self.assertEqual(len(self.requests), 1)


class TestWorkflowTool(ToolTestCase):
    async def test_explain_workflow_returns_steps(self):
        result = await main.explain_workflow()
        self.assertIn("workflow_options", result)
        self.assertTrue(result["workflow_options"]["create_everything_new"])

    async def test_workflow_sequence_is_complete_and_ordered(self):
        """Every dependency the chain needs must be named in the walkthrough.

        projectTypeId is required by create_ghostwriter_project unless the env
        default is set, so a walkthrough that omits it produces a step that
        fails for anyone following it literally.
        """
        result = await main.explain_workflow()
        steps = result["workflow_options"]["create_everything_new"]
        tools = [step["tool"] for step in steps]
        self.assertEqual(tools[0], "list_ghostwriter_lookups")
        self.assertEqual(
            tools[1:],
            [
                "generate_ghostwriter_codename",
                "create_ghostwriter_client",
                "create_ghostwriter_project",
                "create_ghostwriter_report",
                "create_ghostwriter_finding",
                "attach_finding_to_report",
                "update_report_finding",
            ],
        )
        project_step = next(s for s in steps if s["tool"] == "create_ghostwriter_project")
        self.assertIn("projectTypeId", project_step["requires"])
        attach_step = next(s for s in steps if s["tool"] == "attach_finding_to_report")
        self.assertIn("finding", attach_step["requires"])


class TestLookupTool(ToolTestCase):
    async def test_lookups_are_flattened_to_name_keys(self):
        self.queue(
            {
                "data": {
                    "projectType": [{"id": 2, "projectType": "Penetration Test"}],
                    "findingType": [{"id": 4, "findingType": "Web"}],
                    "findingSeverity": [{"id": 2, "severity": "Low"}],
                }
            }
        )
        result = await main.list_ghostwriter_lookups()
        self.assertEqual(result["projectTypes"], [{"id": 2, "name": "Penetration Test"}])
        self.assertEqual(result["findingTypes"], [{"id": 4, "name": "Web"}])
        self.assertEqual(result["severities"], [{"id": 2, "name": "Low"}])

    async def test_missing_lookup_tables_yield_empty_lists(self):
        self.queue({"data": {}})
        result = await main.list_ghostwriter_lookups()
        self.assertEqual(result, {"projectTypes": [], "findingTypes": [], "severities": []})


class TestUpdateTool(ToolTestCase):
    async def test_tool_exposes_every_updatable_column(self):
        """The tool surface must not lag behind reportedFinding_set_input.

        Only two of the eighteen settable columns were reachable at first, so an
        attached finding could not be tailored to the engagement. Editable
        content columns are pinned here; plumbing columns are deliberately out.
        """
        import inspect

        params = set(inspect.signature(main.update_report_finding_tool).parameters)
        expected = {
            "reportedFindingId",
            "replicationSteps",
            "affectedEntities",
            "title",
            "description",
            "impact",
            "mitigation",
            "references",
            "findingGuidance",
            "hostDetectionTechniques",
            "networkDetectionTechniques",
            "severityId",
            "findingTypeId",
            "cvssScore",
            "cvssVector",
            "complete",
            "position",
        }
        self.assertEqual(params, expected)

    async def test_zero_rows_raises_tool_error(self):
        """A silent no-op must not be reported as a successful update."""
        self.queue({"data": {"update_reportedFinding": {"affected_rows": 0, "returning": []}}})
        with self.assertRaises(ToolError):
            await main.update_report_finding_tool(999999, replicationSteps="steps")

    async def test_successful_update_returns_the_row(self):
        self.queue(
            {
                "data": {
                    "update_reportedFinding": {
                        "affected_rows": 1,
                        "returning": [{"id": 6, "replication_steps": "x", "affectedEntities": "h"}],
                    }
                }
            }
        )
        result = await main.update_report_finding_tool(
            6, replicationSteps="x", affectedEntities="h"
        )
        self.assertEqual(result["affected_rows"], 1)
        self.assertEqual(result["returning"][0]["replication_steps"], "x")
        self.assertEqual(result["returning"][0]["affectedEntities"], "h")

    async def test_rerating_sends_only_the_rating_fields(self):
        self.queue(
            {
                "data": {
                    "update_reportedFinding": {
                        "affected_rows": 1,
                        "returning": [{"id": 8, "severityId": 5, "cvssScore": 9.8}],
                    }
                }
            }
        )
        await main.update_report_finding_tool(8, severityId=5, cvssScore=9.8)
        query = json.loads(self.requests[0].content)["query"]
        self.assertIn("severityId: $severityId", query)
        self.assertIn("cvssScore: $cvssScore", query)
        self.assertNotIn("replication_steps", query)


class TestWriteToolOutputs(ToolTestCase):
    async def test_project_output_includes_new_fields(self):
        self.queue(
            {
                "data": {
                    "insert_project_one": {
                        "id": 9,
                        "codename": "CODE",
                        "clientId": 3,
                        "projectTypeId": 2,
                        "startDate": "2026-01-01",
                        "endDate": "2026-01-31",
                        "description": "scope",
                        "slackChannel": "#chan",
                        "timezone": None,
                    }
                }
            }
        )
        result = await main.create_ghostwriter_project(
            3, "CODE", 2, description="scope", slackChannel="#chan"
        )
        self.assertEqual(result["description"], "scope")
        self.assertEqual(result["slackChannel"], "#chan")
        self.assertEqual(result["projectTypeId"], 2)
        # null must not leak through as None
        self.assertEqual(result["timezone"], "")

    async def test_finding_output_includes_narrative_fields(self):
        self.queue(
            {
                "data": {
                    "insert_finding_one": {
                        "id": 7,
                        "title": "T",
                        "description": "D",
                        "impact": "I",
                        "mitigation": "M",
                        "references": None,
                        "cvssScore": 7.5,
                    }
                }
            }
        )
        result = await main.create_ghostwriter_finding(
            "T", "D", impact="I", mitigation="M", cvssScore=7.5
        )
        self.assertEqual(result["impact"], "I")
        self.assertEqual(result["mitigation"], "M")
        self.assertEqual(result["references"], "")
        self.assertEqual(result["cvssScore"], 7.5)

    async def test_client_output_includes_timezone(self):
        self.queue(
            {
                "data": {
                    "insert_client_one": {
                        "id": 4,
                        "name": "Acme",
                        "codename": "ACME",
                        "shortName": "AC",
                        "timezone": "UTC",
                    }
                }
            }
        )
        result = await main.create_ghostwriter_client("Acme", "AC", "ACME", timezone="UTC")
        self.assertEqual(result["timezone"], "UTC")


class TestDeleteTools(ToolTestCase):
    """Deletes are irreversible, so the guards matter more than the happy path."""

    async def test_delete_requires_confirmation(self):
        for tool, kwargs in (
            (main.delete_ghostwriter_report_finding, {"reportedFindingId": 9}),
            (main.delete_ghostwriter_report, {"reportId": 7}),
            (main.delete_ghostwriter_project, {"projectId": 6}),
            (main.delete_ghostwriter_client, {"clientId": 6}),
            (main.delete_ghostwriter_finding, {"findingId": 7}),
        ):
            with self.subTest(tool=tool.__name__):
                with self.assertRaises(ToolError):
                    await tool(**kwargs)
                # nothing may reach the network without confirmation
                self.assertEqual(self.requests, [])

    async def test_delete_rejects_mismatched_confirmation(self):
        with self.assertRaises(ToolError):
            await main.delete_ghostwriter_client(clientId=6, confirmId=7)
        self.assertEqual(self.requests, [])

    async def test_delete_uses_by_pk_and_returns_the_row(self):
        self.queue(
            {"data": {"delete_client_by_pk": {"id": 6, "name": "ZZ", "codename": "CODE"}}}
        )
        result = await main.delete_ghostwriter_client(clientId=6, confirmId=6)
        self.assertEqual(result["deleted"], True)
        self.assertEqual(result["entity"], "client")
        self.assertEqual(result["id"], 6)
        body = json.loads(self.requests[0].content)
        # _by_pk targets exactly one row; a where filter could match more
        self.assertIn("delete_client_by_pk(id: $id)", body["query"])
        self.assertNotIn("where", body["query"])
        self.assertEqual(body["variables"], {"id": 6})

    async def test_deleting_a_missing_row_raises(self):
        """Hasura returns null rather than an error when the id does not exist."""
        self.queue({"data": {"delete_report_by_pk": None}})
        with self.assertRaises(ToolError):
            await main.delete_ghostwriter_report(reportId=999999, confirmId=999999)

    async def test_every_delete_tool_targets_its_own_entity(self):
        cases = [
            (main.delete_ghostwriter_report_finding, "reportedFindingId", 9,
             "delete_reportedFinding_by_pk"),
            (main.delete_ghostwriter_report, "reportId", 7, "delete_report_by_pk"),
            (main.delete_ghostwriter_project, "projectId", 6, "delete_project_by_pk"),
            (main.delete_ghostwriter_client, "clientId", 6, "delete_client_by_pk"),
            (main.delete_ghostwriter_finding, "findingId", 7, "delete_finding_by_pk"),
        ]
        for tool, arg, value, field in cases:
            with self.subTest(tool=tool.__name__):
                self.requests.clear()
                self.queue({"data": {field: {"id": value}}})
                await tool(**{arg: value, "confirmId": value})
                self.assertIn(field, json.loads(self.requests[0].content)["query"])


if __name__ == "__main__":
    unittest.main()
