"""Async client for the Ghostwriter GraphQL (Hasura) API.

This module owns all network access to Ghostwriter and provides:

* lazy, environment-driven configuration via :func:`get_settings`;
* a shared ``httpx2.AsyncClient`` (connection pooling + safe connection retries);
* typed exceptions so MCP tools can surface failures as protocol errors;
* input validation for identifiers, dates and text fields;
* a bounded result size for every list query.

TLS certificate verification is enabled by default and should stay that way: a
self-signed certificate belongs in the system trust store (or in a CA bundle).
As a last-resort escape hatch for hosts whose certificate has no usable
``subjectAltName``, ``GHOSTWRITER_TLS_INSECURE=1`` disables verification and
logs a loud warning. See ``GHOSTWRITER_TLS_INSECURE`` in ``.env.example``.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx2
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_PAGINATION_LIMIT = 50
_BOOL_TRUE = frozenset({"1", "true", "yes", "on"})
_BOOL_FALSE = frozenset({"0", "false", "no", "off"})
# Retries apply to connection-establishment failures only (never to a request
# that already reached the server), so mutations cannot be duplicated.
_TRANSPORT_RETRIES = 2


class GhostwriterError(RuntimeError):
    """Base class for all Ghostwriter client failures."""


class GhostwriterConfigError(GhostwriterError):
    """The client is misconfigured (for example a missing GraphQL URL)."""


class GhostwriterValidationError(GhostwriterError):
    """A caller supplied an invalid argument."""


class GhostwriterHTTPError(GhostwriterError):
    """Ghostwriter returned a non-2xx HTTP response."""


class GhostwriterGraphQLError(GhostwriterError):
    """Ghostwriter returned a GraphQL ``errors`` payload."""


class GhostwriterNotFoundError(GhostwriterError):
    """A mutation matched no rows, so it silently changed nothing."""


@dataclass(frozen=True)
class Settings:
    """Resolved runtime configuration."""

    graphql_url: str
    api_token: str | None
    request_timeout: float
    pagination_limit: int
    default_project_type_id: int | None
    default_severity_id: int | None
    tls_insecure: bool

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        url = (
            env.get("GHOSTWRITER_GRAPHQL_URL") or env.get("GHOSTWRITER_URL") or ""
        ).strip()
        token = (env.get("GHOSTWRITER_API_TOKEN") or "").strip() or None
        return cls(
            graphql_url=url,
            api_token=token,
            request_timeout=_parse_float(
                env.get("GHOSTWRITER_REQUEST_TIMEOUT"), DEFAULT_TIMEOUT_SECONDS
            ),
            pagination_limit=_parse_int(
                env.get("GHOSTWRITER_PAGINATION_LIMIT"), DEFAULT_PAGINATION_LIMIT
            ),
            default_project_type_id=_parse_optional_int(
                env.get("GHOSTWRITER_DEFAULT_PROJECT_TYPE_ID")
            ),
            default_severity_id=_parse_optional_int(
                env.get("GHOSTWRITER_DEFAULT_SEVERITY_ID")
            ),
            tls_insecure=_parse_bool(env.get("GHOSTWRITER_TLS_INSECURE")),
        )


def _parse_float(raw: str | None, default: float) -> float:
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise GhostwriterConfigError(
            f"GHOSTWRITER_REQUEST_TIMEOUT must be a number, got {raw!r}"
        ) from exc


def _parse_int(raw: str | None, default: int) -> int:
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise GhostwriterConfigError(
            f"Expected an integer environment value, got {raw!r}"
        ) from exc


def _parse_bool(raw: str | None, *, default: bool = False) -> bool:
    """Parse a boolean flag, accepting the usual truthy/falsy spellings."""
    if raw in (None, ""):
        return default
    value = raw.strip().lower()
    if value in _BOOL_TRUE:
        return True
    if value in _BOOL_FALSE:
        return False
    raise GhostwriterConfigError(
        "Expected a boolean environment value "
        f"(1/0, true/false, yes/no, on/off), got {raw!r}"
    )


def _parse_optional_int(raw: str | None) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise GhostwriterConfigError(
            f"Expected an integer environment value, got {raw!r}"
        ) from exc


def get_settings() -> Settings:
    """Read configuration from the environment, failing early if it is unusable."""
    settings = Settings.from_env()
    if not settings.graphql_url:
        raise GhostwriterConfigError(
            "GHOSTWRITER_GRAPHQL_URL (or GHOSTWRITER_URL) is not set in the environment"
        )
    return settings


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #
_transport: httpx2.AsyncBaseTransport | None = None
_client: httpx2.AsyncClient | None = None


def _get_client() -> httpx2.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        settings = get_settings()
        if _transport is not None:
            transport = _transport  # test/embedding transport owns its own TLS setup
        else:
            # ``verify`` belongs on the transport: an explicit ``transport=`` wins
            # over the equivalent ``AsyncClient`` argument, so setting it there
            # would silently do nothing.
            transport = httpx2.AsyncHTTPTransport(
                retries=_TRANSPORT_RETRIES, verify=not settings.tls_insecure
            )
        if settings.tls_insecure:
            logger.warning(
                "GHOSTWRITER_TLS_INSECURE is enabled: TLS certificate verification "
                "is DISABLED for %s. Traffic stays encrypted, but the server is no "
                "longer authenticated, so a man-in-the-middle cannot be detected. "
                "Give the server certificate a valid subjectAltName and unset this "
                "flag.",
                settings.graphql_url,
            )
        _client = httpx2.AsyncClient(
            transport=transport,
            timeout=httpx2.Timeout(settings.request_timeout),
        )
    return _client


async def close_client() -> None:
    """Close the shared client; safe to call when no client exists."""
    global _client
    client, _client = _client, None
    if client is not None and not client.is_closed:
        await client.aclose()


def set_transport(transport: httpx2.AsyncBaseTransport | None) -> None:
    """Testing/embedding hook: replace the HTTP transport and drop the cached client."""
    global _transport, _client
    _transport = transport
    _client = None


async def _post(
    query: str,
    variables: dict[str, Any] | None = None,
    timeout: float | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """POST a GraphQL document to Ghostwriter and return the decoded payload."""
    settings = get_settings()

    headers = {"Content-Type": "application/json"}
    if settings.api_token:
        headers["Authorization"] = f"Bearer {settings.api_token}"
    if extra_headers:
        headers.update(extra_headers)

    client = _get_client()
    request_timeout = settings.request_timeout if timeout is None else timeout

    try:
        response = await client.post(
            settings.graphql_url,
            headers=headers,
            json={"query": query, "variables": variables or {}},
            timeout=request_timeout,
        )
    except httpx2.RequestError as exc:
        raise GhostwriterError(
            f"Network error calling Ghostwriter GraphQL: {exc}"
        ) from exc

    if response.is_error:
        raise GhostwriterHTTPError(
            f"Ghostwriter HTTP error {response.status_code}: {response.text}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise GhostwriterError(
            f"Invalid JSON response from Ghostwriter: {response.text[:500]}"
        ) from exc

    if isinstance(payload, dict) and payload.get("errors"):
        raise GhostwriterGraphQLError(f"GraphQL errors: {payload['errors']}")

    return payload


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def _require_text(value: Any, field: str) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        raise GhostwriterValidationError(f"{field} must be a non-empty string")
    return text


def _require_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise GhostwriterValidationError(f"{field} must be an integer, got {value!r}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise GhostwriterValidationError(
            f"{field} must be an integer, got {value!r}"
        ) from exc
    if parsed <= 0:
        raise GhostwriterValidationError(f"{field} must be > 0, got {parsed}")
    return parsed


def _normalize_date(value: Any, field: str, default: str | None = None) -> str | None:
    """Validate an ISO date/datetime string, falling back to ``default``."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    text = str(value).strip()
    try:
        dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise GhostwriterValidationError(
            f"{field} must be an ISO date (YYYY-MM-DD), got {value!r}"
        ) from exc
    return text


def _today() -> str:
    return dt.date.today().isoformat()


def _like(search_term: str | None) -> str:
    text = "" if search_term is None else str(search_term).strip()
    return f"%{text}%" if text else "%%"


def _page_limit(limit: int | None = None) -> int:
    if limit is None:
        return get_settings().pagination_limit
    return _require_positive_int(limit, "limit")


def _first_row(result: dict[str, Any], key: str) -> dict[str, Any] | None:
    data = result.get("data") or {}
    return data.get(key)


# --------------------------------------------------------------------------- #
# Read operations
# --------------------------------------------------------------------------- #
async def search_findings(search_term: str | None = None, limit: int | None = None):
    """Search the findings library by title."""
    query = """
    query ($term: String!, $limit: Int!) {
      finding(where: {title: {_ilike: $term}}, limit: $limit) {
        id
        title
        description
        severity {
          severity
        }
      }
    }
    """
    variables = {"term": _like(search_term), "limit": _page_limit(limit)}
    return await _post(query, variables)


async def search_reports(search_term: str | None = None, limit: int | None = None):
    """Search for reports by title."""
    query = """
    query ($term: String!, $limit: Int!) {
      report(where: {title: {_ilike: $term}}, limit: $limit) {
        id
        title
        projectId
      }
    }
    """
    variables = {"term": _like(search_term), "limit": _page_limit(limit)}
    return await _post(query, variables)


async def search_clients(search_term: str | None = None, limit: int | None = None):
    """Search for clients by name, codename, or shortName."""
    query = """
    query ($term: String!, $limit: Int!) {
      client(where: {
        _or: [
          {name: {_ilike: $term}},
          {codename: {_ilike: $term}},
          {shortName: {_ilike: $term}}
        ]
      }, limit: $limit) {
        id
        name
        shortName
        codename
        address
        description
      }
    }
    """
    variables = {"term": _like(search_term), "limit": _page_limit(limit)}
    result = await _post(query, variables)

    clients = (result.get("data") or {}).get("client") or []
    for client in clients:
        client["name"] = client.get("name") or ""
        client["codename"] = client.get("codename") or ""
        client["address"] = client.get("address") or ""
        client["description"] = client.get("description") or ""
        client["shortName"] = client.get("shortName") or ""
    return result


async def search_projects(search_term: str | None = None, limit: int | None = None):
    """Search for projects by codename or related client info."""
    query = """
    query ($term: String!, $limit: Int!) {
      project(where: {
        _or: [
          {codename: {_ilike: $term}},
          {client: {name: {_ilike: $term}}},
          {client: {codename: {_ilike: $term}}}
        ]
      }, limit: $limit) {
        id
        codename
        clientId
        startDate
        endDate
        description
        projectType {
          projectType
        }
        client {
          name
          codename
        }
      }
    }
    """
    variables = {"term": _like(search_term), "limit": _page_limit(limit)}
    result = await _post(query, variables)

    projects = (result.get("data") or {}).get("project") or []
    for project in projects:
        project["description"] = project.get("description") or ""
        project["startDate"] = project.get("startDate") or ""
        project["endDate"] = project.get("endDate") or ""
        if not project.get("projectType"):
            project["projectType"] = {"projectType": "Unknown"}
        if not project.get("client"):
            project["client"] = {"name": "", "codename": ""}
    return result


async def get_client_by_id(client_id: int):
    """Get a specific client by ID."""
    query = """
    query ($clientId: bigint!) {
      client(where: {id: {_eq: $clientId}}) {
        id
        name
        shortName
        codename
        address
        description
      }
    }
    """
    variables = {"clientId": _require_positive_int(client_id, "client_id")}
    return await _post(query, variables)


async def get_project_by_id(project_id: int):
    """Get a specific project by ID."""
    query = """
    query ($projectId: bigint!) {
      project(where: {id: {_eq: $projectId}}) {
        id
        codename
        clientId
        startDate
        endDate
        description
        projectType {
          projectType
        }
        client {
          name
          codename
        }
      }
    }
    """
    variables = {"projectId": _require_positive_int(project_id, "project_id")}
    return await _post(query, variables)


async def get_report_by_id(report_id: int):
    """Get a specific report by ID."""
    query = """
    query ($reportId: bigint!) {
      report(where: {id: {_eq: $reportId}}) {
        id
        title
        projectId
        last_update
      }
    }
    """
    variables = {"reportId": _require_positive_int(report_id, "report_id")}
    return await _post(query, variables)


async def list_report_findings(report_id: int, limit: int | None = None):
    """List findings attached to a report."""
    query = """
    query ($reportId: bigint!, $limit: Int!) {
      reportedFinding(where: {reportId: {_eq: $reportId}}, limit: $limit) {
        id
        title
      }
    }
    """
    variables = {
        "reportId": _require_positive_int(report_id, "report_id"),
        "limit": _page_limit(limit),
    }
    return await _post(query, variables)


async def generate_codename():
    """Ask Ghostwriter for a unique project codename."""
    query = """
    mutation {
      generateCodename {
        codename
      }
    }
    """
    return await _post(query)


# --------------------------------------------------------------------------- #
# Write operations
# --------------------------------------------------------------------------- #
async def create_client(
    name: str,
    short_name: str,
    codename: str,
    address: str | None = None,
    description: str | None = None,
    extra_fields: dict[str, Any] | None = None,
):
    """Create a client and return the inserted row."""
    obj: dict[str, Any] = {
        "name": _require_text(name, "name"),
        "shortName": _require_text(short_name, "short_name"),
        "codename": _require_text(codename, "codename"),
    }
    if address is not None:
        obj["address"] = address
    if description is not None:
        obj["description"] = description
    if extra_fields:
        obj["extraFields"] = extra_fields

    query = """
    mutation CreateClient($object: client_insert_input!) {
      insert_client_one(object: $object) {
        id
        name
        codename
        shortName
        address
        description
      }
    }
    """
    result = await _post(query, {"object": obj})
    return _first_row(result, "insert_client_one")


async def create_project(
    clientId: int,
    codename: str,
    projectTypeId: int | None = None,
    startDate: str | None = None,
    endDate: str | None = None,
    extra_fields: dict[str, Any] | None = None,
):
    """Create a project under a client and return the inserted row.

    ``projectTypeId`` falls back to ``GHOSTWRITER_DEFAULT_PROJECT_TYPE_ID`` when
    omitted; it is an error if neither is provided.
    """
    if projectTypeId is None:
        projectTypeId = get_settings().default_project_type_id
    if projectTypeId is None:
        raise GhostwriterValidationError(
            "projectTypeId is required (or set GHOSTWRITER_DEFAULT_PROJECT_TYPE_ID)"
        )

    start = _normalize_date(startDate, "startDate", default=_today())
    end = _normalize_date(endDate, "endDate", default=start)
    obj: dict[str, Any] = {
        "clientId": _require_positive_int(clientId, "clientId"),
        "projectTypeId": _require_positive_int(projectTypeId, "projectTypeId"),
        "codename": _require_text(codename, "codename"),
        "startDate": start,
        "endDate": end,
    }
    if extra_fields:
        obj["extraFields"] = extra_fields

    query = """
    mutation CreateProject($object: project_insert_input!) {
      insert_project_one(object: $object) {
        id
        codename
        startDate
        endDate
      }
    }
    """
    return await _post(query, {"object": obj})


async def create_report(title: str, projectId: int, last_update: str | None = None):
    """Create a report under a project and return the inserted row."""
    obj: dict[str, Any] = {
        "title": _require_text(title, "title"),
        "projectId": _require_positive_int(projectId, "projectId"),
        "last_update": _normalize_date(last_update, "last_update", default=_today()),
    }

    query = """
    mutation CreateReport($object: report_insert_input!) {
      insert_report_one(object: $object) {
        id
        title
        projectId
        last_update
      }
    }
    """
    return await _post(query, {"object": obj})


async def create_finding(
    title: str,
    description: str,
    findingTypeId: int | None = None,
    severityId: int | None = None,
    cvssScore: float | None = None,
    cvssVector: str | None = None,
    replication_steps: str | None = None,
    extra_fields: dict[str, Any] | None = None,
):
    """Create a finding in the Ghostwriter findings library.

    Field names mirror this deployment's ``finding_insert_input`` (note
    ``cvssScore`` / ``cvssVector``). The library has no ``affectedEntities``
    field; that belongs to a *reported* finding (see
    :func:`update_report_finding`).
    """
    obj: dict[str, Any] = {
        "title": _require_text(title, "title"),
        "description": _require_text(description, "description"),
    }
    if findingTypeId is not None:
        obj["findingTypeId"] = _require_positive_int(findingTypeId, "findingTypeId")
    if severityId is None:
        severityId = get_settings().default_severity_id
    if severityId is not None:
        obj["severityId"] = _require_positive_int(severityId, "severityId")
    if cvssScore is not None:
        try:
            score = float(cvssScore)
        except (TypeError, ValueError) as exc:
            raise GhostwriterValidationError(
                f"cvssScore must be a number, got {cvssScore!r}"
            ) from exc
        if not 0.0 <= score <= 10.0:
            raise GhostwriterValidationError(
                f"cvssScore must be between 0.0 and 10.0, got {score}"
            )
        obj["cvssScore"] = score
    if cvssVector:
        obj["cvssVector"] = cvssVector
    if replication_steps is not None:
        obj["replication_steps"] = replication_steps
    if extra_fields:
        obj["extraFields"] = extra_fields

    query = """
    mutation CreateFinding($object: finding_insert_input!) {
      insert_finding_one(object: $object) {
        id
        title
        description
      }
    }
    """
    result = await _post(query, {"object": obj})
    return _first_row(result, "insert_finding_one")


async def add_finding_to_report(findingId: int, reportId: int):
    """Attach a library finding to a report (creates a reportedFinding row)."""
    query = """
    mutation attachFinding($findingId: Int!, $reportId: Int!) {
      attachFinding(findingId: $findingId, reportId: $reportId) {
        id
      }
    }
    """
    variables = {
        "findingId": _require_positive_int(findingId, "findingId"),
        "reportId": _require_positive_int(reportId, "reportId"),
    }
    return await _post(query, variables)


async def update_report_finding(
    reportedFindingId: int,
    replicationSteps: str | None = None,
    affectedEntities: str | None = None,
):
    """Update replication steps and/or affected entities of a reported finding.

    ``reportedFindingId`` is the ``id`` of the ``reportedFinding`` row returned
    by ``attachFinding`` -- not a findings-library id. The provided values
    replace the existing text; they are not appended.
    """
    variables: dict[str, Any] = {
        "reportedFindingId": _require_positive_int(
            reportedFindingId, "reportedFindingId"
        )
    }
    set_fields = []
    if replicationSteps is not None:
        set_fields.append("replication_steps: $replicationSteps")
        variables["replicationSteps"] = replicationSteps
    if affectedEntities is not None:
        set_fields.append("affectedEntities: $affectedEntities")
        variables["affectedEntities"] = affectedEntities

    if not set_fields:
        raise GhostwriterValidationError(
            "At least one of replicationSteps or affectedEntities must be provided."
        )

    # Field names are hardcoded above; only their presence varies.
    query = f"""
    mutation updateReportedFinding($reportedFindingId: bigint!, $replicationSteps: String, $affectedEntities: String) {{
      update_reportedFinding(
        where: {{id: {{_eq: $reportedFindingId}}}},
        _set: {{ {", ".join(set_fields)} }}
      ) {{
        affected_rows
        returning {{
          id
          replication_steps
          affectedEntities
        }}
      }}
    }}
    """
    result = await _post(query, variables)

    # Hasura treats "no rows matched" as success, so a typo'd id would look like a
    # successful update. ``_set`` always changes the row, so a matched id reports
    # affected_rows >= 1.
    updated = (result.get("data") or {}).get("update_reportedFinding") or {}
    if not updated.get("affected_rows"):
        raise GhostwriterNotFoundError(
            f"No reported finding with id {variables['reportedFindingId']}."
        )
    return result


async def list_lookups():
    """Return the lookup tables a caller needs to pick valid ids.

    ``projectTypeId``, ``findingTypeId`` and ``severityId`` are seeded per
    deployment, so their ids cannot be hardcoded.
    """
    query = """
    query GhostwriterLookups {
      projectType(order_by: {id: asc}) {
        id
        projectType
      }
      findingType(order_by: {id: asc}) {
        id
        findingType
      }
      findingSeverity(order_by: {id: asc}) {
        id
        severity
      }
    }
    """
    return await _post(query)
