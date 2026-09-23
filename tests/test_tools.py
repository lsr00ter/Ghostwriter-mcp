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
                        "note": None,
                    }
                }
            }
        )
        result = await main.create_ghostwriter_client("Acme", "AC", "ACME")
        self.assertEqual(result["shortName"], "AC")
        self.assertEqual(result["id"], 10)
        self.assertIn("clientId", result["_workflow_note"])


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


if __name__ == "__main__":
    unittest.main()
