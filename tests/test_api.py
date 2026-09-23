"""Tests for the low-level Ghostwriter API client.

Uses ``httpx2.MockTransport`` so no network access or Ghostwriter instance is
required. Run with ``python -m unittest discover -s tests -v``.
"""

import json
import os
import re
import unittest
from pathlib import Path
from unittest import mock

import httpx2

import ghostwriter_api as gw

ENV = {
    "GHOSTWRITER_GRAPHQL_URL": "https://gw.test/v1/graphql",
    "GHOSTWRITER_API_TOKEN": "test-token",
    "GHOSTWRITER_PAGINATION_LIMIT": "7",
}


class ApiTestCase(unittest.IsolatedAsyncioTestCase):
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
        """Queue JSON payloads returned in order for each request made."""
        responses = list(payloads)

        def handler(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            payload = responses.pop(0) if responses else {}
            return httpx2.Response(status_code, json=payload)

        gw.set_transport(httpx2.MockTransport(handler))

    def queue_raw(self, body: str, status_code=200, headers=None):
        def handler(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            return httpx2.Response(status_code, text=body, headers=headers or {})

        gw.set_transport(httpx2.MockTransport(handler))

    def last_body(self) -> dict:
        return json.loads(self.requests[-1].content)


class TestTransport(ApiTestCase):
    async def test_sends_bearer_token_and_json_body(self):
        self.queue({"data": {"ok": True}})
        result = await gw.search_findings("acme")
        self.assertEqual(result, {"data": {"ok": True}})
        request = self.requests[0]
        self.assertEqual(request.url, httpx2.URL(ENV["GHOSTWRITER_GRAPHQL_URL"]))
        self.assertEqual(request.headers["authorization"], "Bearer test-token")
        self.assertEqual(request.headers["content-type"], "application/json")

    async def test_omits_authorization_without_token(self):
        with mock.patch.dict(
            os.environ, {"GHOSTWRITER_API_TOKEN": ""}, clear=False
        ):
            self.queue({"data": {"finding": []}})
            await gw.search_findings("x")
        self.assertNotIn("authorization", self.requests[0].headers)

    async def test_missing_url_raises_config_error(self):
        with mock.patch.dict(os.environ, {"GHOSTWRITER_GRAPHQL_URL": "", "GHOSTWRITER_URL": ""}, clear=False):
            with self.assertRaises(gw.GhostwriterConfigError):
                await gw.search_findings("x")

    async def test_graphql_errors_raise(self):
        self.queue({"errors": [{"message": "boom"}]})
        with self.assertRaises(gw.GhostwriterGraphQLError):
            await gw.search_findings("x")

    async def test_http_error_raises_with_status(self):
        self.queue_raw("nope", status_code=500)
        with self.assertRaises(gw.GhostwriterHTTPError) as ctx:
            await gw.search_findings("x")
        self.assertIn("500", str(ctx.exception))

    async def test_invalid_json_raises(self):
        self.queue_raw("<html>not json</html>")
        with self.assertRaises(gw.GhostwriterError):
            await gw.search_findings("x")


class TestTlsInsecure(ApiTestCase):
    def test_disabled_by_default(self):
        self.assertFalse(gw.Settings.from_env({}).tls_insecure)

    async def test_default_builds_a_verifying_transport(self):
        with mock.patch.object(gw.httpx2, "AsyncHTTPTransport") as transport:
            with mock.patch.object(gw.httpx2, "AsyncClient"):
                gw._get_client()
        self.assertIs(transport.call_args.kwargs["verify"], True)

    def test_truthy_values_enable_it(self):
        for raw in ("1", "true", "TRUE", "Yes", "on"):
            with self.subTest(raw=raw):
                self.assertTrue(
                    gw.Settings.from_env({"GHOSTWRITER_TLS_INSECURE": raw}).tls_insecure
                )

    def test_falsy_values_keep_it_off(self):
        for raw in ("0", "false", "No", "off"):
            with self.subTest(raw=raw):
                self.assertFalse(
                    gw.Settings.from_env({"GHOSTWRITER_TLS_INSECURE": raw}).tls_insecure
                )

    def test_unrecognised_value_is_a_config_error(self):
        with self.assertRaises(gw.GhostwriterConfigError):
            gw.Settings.from_env({"GHOSTWRITER_TLS_INSECURE": "maybe"})

    async def test_flag_disables_verification_and_warns_loudly(self):
        with mock.patch.dict(os.environ, {"GHOSTWRITER_TLS_INSECURE": "1"}):
            with mock.patch.object(gw.httpx2, "AsyncHTTPTransport") as transport:
                with mock.patch.object(gw.httpx2, "AsyncClient"):
                    with self.assertLogs("ghostwriter_api", level="WARNING") as logs:
                        gw._get_client()
        self.assertIs(transport.call_args.kwargs["verify"], False)
        self.assertEqual(transport.call_args.kwargs["retries"], gw._TRANSPORT_RETRIES)
        self.assertIn("GHOSTWRITER_TLS_INSECURE", "\n".join(logs.output))


class TestPagination(ApiTestCase):
    async def test_search_uses_configured_limit(self):
        self.queue({"data": {"finding": []}})
        await gw.search_findings("acme")
        body = self.last_body()
        self.assertEqual(body["variables"]["limit"], 7)
        self.assertEqual(body["variables"]["term"], "%acme%")

    async def test_search_term_is_wrapped_and_blank_matches_all(self):
        self.queue({"data": {"finding": []}})
        await gw.search_findings(None)
        self.assertEqual(self.last_body()["variables"]["term"], "%%")

    async def test_explicit_limit_overrides_default(self):
        self.queue({"data": {"finding": []}})
        await gw.search_findings("acme", limit=3)
        self.assertEqual(self.last_body()["variables"]["limit"], 3)

    async def test_every_search_applies_a_limit(self):
        queries = [
            gw.search_findings("a"),
            gw.search_reports("a"),
            gw.search_clients("a"),
            gw.search_projects("a"),
            gw.list_report_findings(5),
        ]
        for coro in queries:
            self.queue({"data": {}})
            await coro
            self.assertEqual(self.last_body()["variables"]["limit"], 7)


class TestSearchNormalization(ApiTestCase):
    async def test_clients_null_fields_become_empty_strings(self):
        self.queue(
            {
                "data": {
                    "client": [
                        {"id": 1, "name": "Acme", "codename": None, "shortName": None,
                         "address": None, "description": None}
                    ]
                }
            }
        )
        result = await gw.search_clients("acme")
        client = result["data"]["client"][0]
        self.assertEqual(client["codename"], "")
        self.assertEqual(client["shortName"], "")
        self.assertEqual(client["address"], "")
        self.assertEqual(client["description"], "")

    async def test_projects_fill_missing_nested_objects(self):
        self.queue({"data": {"project": [{"id": 1, "codename": "X", "clientId": 2}]}})
        result = await gw.search_projects("x")
        project = result["data"]["project"][0]
        self.assertEqual(project["projectType"], {"projectType": "Unknown"})
        self.assertEqual(project["client"], {"name": "", "codename": ""})


class TestValidation(ApiTestCase):
    async def test_create_client_requires_non_empty_fields(self):
        with self.assertRaises(gw.GhostwriterValidationError):
            await gw.create_client("", "short", "code")
        with self.assertRaises(gw.GhostwriterValidationError):
            await gw.create_client("name", "   ", "code")

    async def test_get_by_id_rejects_non_positive(self):
        with self.assertRaises(gw.GhostwriterValidationError):
            await gw.get_client_by_id(0)
        with self.assertRaises(gw.GhostwriterValidationError):
            await gw.get_report_by_id("abc")

    async def test_create_project_rejects_bad_date(self):
        with self.assertRaises(gw.GhostwriterValidationError):
            await gw.create_project(1, "code", 1, startDate="yesterday")

    async def test_project_type_defaults_from_env(self):
        with mock.patch.dict(
            os.environ, {"GHOSTWRITER_DEFAULT_PROJECT_TYPE_ID": "3"}, clear=False
        ):
            self.queue({"data": {"insert_project_one": {"id": 1}}})
            await gw.create_project(1, "code")
        self.assertEqual(
            self.last_body()["variables"]["object"]["projectTypeId"], 3
        )

    async def test_project_type_required_without_default(self):
        with self.assertRaises(gw.GhostwriterValidationError):
            await gw.create_project(1, "code")

    async def test_cvss_score_range_enforced(self):
        with self.assertRaises(gw.GhostwriterValidationError):
            await gw.create_finding("t", "d", cvssScore=11)

    async def test_update_report_finding_requires_a_field(self):
        with self.assertRaises(gw.GhostwriterValidationError):
            await gw.update_report_finding(1)


class TestWriteObjects(ApiTestCase):
    async def test_create_client_object_shape(self):
        self.queue({"data": {"insert_client_one": {"id": 1, "name": "Acme"}}})
        await gw.create_client("Acme", "AC", "ACME")
        obj = self.last_body()["variables"]["object"]
        self.assertEqual(obj["shortName"], "AC")
        self.assertNotIn("address", obj)
        self.assertNotIn("description", obj)

    async def test_create_client_nests_extra_fields(self):
        self.queue({"data": {"insert_client_one": {"id": 1}}})
        await gw.create_client("Acme", "AC", "ACME", extra_fields={"custom": 1})
        obj = self.last_body()["variables"]["object"]
        self.assertEqual(obj["extraFields"], {"custom": 1})
        self.assertNotIn("custom", obj)

    async def test_create_project_defaults_dates_to_today(self):
        import datetime as dt

        self.queue({"data": {"insert_project_one": {"id": 1}}})
        await gw.create_project(1, "code", 1)
        obj = self.last_body()["variables"]["object"]
        today = dt.date.today().isoformat()
        self.assertEqual(obj["startDate"], today)
        self.assertEqual(obj["endDate"], today)

    async def test_create_report_defaults_last_update(self):
        import datetime as dt

        self.queue({"data": {"insert_report_one": {"id": 1}}})
        await gw.create_report("Title", 1)
        obj = self.last_body()["variables"]["object"]
        self.assertEqual(obj["last_update"], dt.date.today().isoformat())

    async def test_update_report_finding_only_includes_given_fields(self):
        self.queue({"data": {"update_reportedFinding": {"affected_rows": 1}}})
        await gw.update_report_finding(5, affectedEntities="host-1")
        request = self.last_body()
        self.assertIn("affectedEntities: $affectedEntities", request["query"])
        self.assertNotIn("replication_steps:", request["query"])
        self.assertEqual(request["variables"]["affectedEntities"], "host-1")
        self.assertEqual(request["variables"]["reportedFindingId"], 5)

    async def test_create_finding_uses_schema_field_names(self):
        self.queue({"data": {"insert_finding_one": {"id": 7}}})
        await gw.create_finding(
            "Title", "Desc", cvssScore=7.5, cvssVector="CVSS:3.1/AV:N"
        )
        obj = self.last_body()["variables"]["object"]
        self.assertEqual(obj["cvssScore"], 7.5)
        self.assertEqual(obj["cvssVector"], "CVSS:3.1/AV:N")
        self.assertNotIn("cvss_score", obj)
        self.assertNotIn("cvss_vector", obj)
        self.assertNotIn("affectedEntities", obj)


class TestSchemaConformance(ApiTestCase):
    """Guard payload field names against the pinned Ghostwriter schema.

    ``tests/schema_fixtures.json`` was generated by introspecting the target
    deployment's Hasura schema (``__schema`` types and mutation args).
    Regenerate it when targeting a different Ghostwriter release.
    """

    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(
            Path(__file__).with_name("schema_fixtures.json").read_text()
        )

    def assert_keys_valid(self, keys, input_name):
        valid = set(self.fixture["inputs"][input_name])
        invalid = sorted(k for k in keys if k not in valid)
        self.assertEqual(invalid, [], f"{input_name} has no such fields: {invalid}")

    async def test_create_client_payload(self):
        self.queue({"data": {"insert_client_one": {"id": 1}}})
        await gw.create_client(
            "Acme", "AC", "ACME", address="a", description="n", extra_fields={"x": 1}
        )
        self.assert_keys_valid(
            self.last_body()["variables"]["object"], "client_insert_input"
        )

    async def test_create_project_payload(self):
        self.queue({"data": {"insert_project_one": {"id": 1}}})
        await gw.create_project(1, "code", 1, extra_fields={"x": 1})
        self.assert_keys_valid(
            self.last_body()["variables"]["object"], "project_insert_input"
        )

    async def test_create_report_payload(self):
        self.queue({"data": {"insert_report_one": {"id": 1}}})
        await gw.create_report("T", 1)
        self.assert_keys_valid(
            self.last_body()["variables"]["object"], "report_insert_input"
        )

    async def test_create_finding_payload(self):
        self.queue({"data": {"insert_finding_one": {"id": 1}}})
        await gw.create_finding(
            "T",
            "D",
            findingTypeId=1,
            severityId=1,
            cvssScore=5.0,
            cvssVector="v",
            replication_steps="s",
            extra_fields={"x": 1},
        )
        self.assert_keys_valid(
            self.last_body()["variables"]["object"], "finding_insert_input"
        )

    async def test_update_report_finding_set_keys(self):
        self.queue({"data": {"update_reportedFinding": {"affected_rows": 1}}})
        await gw.update_report_finding(5, replicationSteps="a", affectedEntities="b")
        query = self.last_body()["query"]
        set_block = re.search(r"_set:\s*\{(.*?)\}", query, re.S).group(1)
        keys = re.findall(r"(\w+)\s*:", set_block)
        self.assert_keys_valid(keys, "reportedFinding_set_input")


if __name__ == "__main__":
    unittest.main()
