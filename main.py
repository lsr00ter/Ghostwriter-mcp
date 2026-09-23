"""Ghostwriter MCP server.

Exposes Ghostwriter's report-management workflow as MCP tools. Tool failures
are raised as ``ToolError`` so MCP clients see ``isError: true`` instead of a
successful result that merely contains an ``error`` key.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

import ghostwriter_api as gw
from ghostwriter_api import (
    GhostwriterError,
    add_finding_to_report,
    create_client,
    create_finding,
    create_project,
    create_report,
    generate_codename,
    get_client_by_id,
    get_project_by_id,
    get_report_by_id,
    list_report_findings,
    search_clients,
    search_findings,
    search_projects,
    search_reports,
    update_report_finding,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("ghostwriter_mcp")

SERVER_INSTRUCTIONS = """
Ghostwriter MCP server for penetration-testing report management.

WORKFLOW DEPENDENCIES (each step returns the ID needed by the next):
1. generate_ghostwriter_codename            -> codename
2. create_ghostwriter_client                -> clientId
3. create_ghostwriter_project  (clientId)   -> projectId
4. create_ghostwriter_report   (projectId)  -> reportId
5. attach_finding_to_report    (reportId)   -> reportedFindingId
6. update_report_finding       (reportedFindingId)

Search before creating to avoid duplicates (search_ghostwriter_clients,
search_ghostwriter_projects, search_ghostwriter_reports). Call explain_workflow
for a full walkthrough, including how to trace an existing report back to its
project and client.
"""

# MCP tool annotations. Read-only tools advertise readOnlyHint; write tools
# distinguish destructive (replaces content) from additive changes.
READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)
DESTRUCTIVE_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True
)


@contextlib.asynccontextmanager
async def lifespan(_server: MCPServer):
    """Release the shared HTTP client when the server shuts down."""
    try:
        yield {}
    finally:
        await gw.close_client()


def _server_version() -> str:
    """Report the installed package version, or a marker for a source checkout."""
    try:
        return _package_version("ghostwriter-mcp")
    except PackageNotFoundError:
        return "0.0.0+source"


server = MCPServer(
    "GhostwriterMCP",
    title="Ghostwriter",
    version=_server_version(),
    instructions=SERVER_INSTRUCTIONS.strip(),
    lifespan=lifespan,
)


def _tool_error(tool_name: str, exc: Exception) -> ToolError:
    """Log a failure and convert it into an MCP protocol error."""
    if isinstance(exc, GhostwriterError):
        logger.warning("%s: %s", tool_name, exc)
    else:
        logger.exception("%s: unexpected error", tool_name)
    return ToolError(str(exc))


def _truncate(text: Any, length: int = 100) -> str:
    value = "" if text is None else str(text)
    return value if len(value) <= length else f"{value[:length]}..."


@server.tool(
    name="search_ghostwriter_findings",
    title="Search findings",
    description="Search for Ghostwriter findings by title. Returns findingId values.",
    annotations=READ_ONLY,
)
async def search_ghostwriter_findings(
    search_term: str | None = None,
) -> list[dict[str, Any]]:
    try:
        results = await search_findings(search_term=search_term)
        findings = (results.get("data") or {}).get("finding") or []
        return [
            {
                "id": f.get("id"),
                "title": f.get("title") or "",
                "severity": (f.get("severity") or {}).get("severity") or "Unknown",
                "description": _truncate(f.get("description")),
            }
            for f in findings
        ]
    except Exception as exc:  # noqa: BLE001 - converted to a protocol error
        raise _tool_error("search_ghostwriter_findings", exc) from exc


@server.tool(
    name="search_ghostwriter_reports",
    title="Search reports",
    description="""Search Ghostwriter reports by title.

    USE CASE: Find existing reports to work with, or check if a report already exists.
    SEARCH BY: Report title (partial matches supported)
    RETURNS: List of reports with their IDs and projectIds

    Example searches:
    - search_ghostwriter_reports("Q4 Pentest") -> finds "Q4 Pentest Report", "Q4 Pentest Final", etc.
    - search_ghostwriter_reports("Web App") -> finds all reports with "Web App" in the title
    """,
    annotations=READ_ONLY,
)
async def search_ghostwriter_reports(
    search_term: str | None = None,
) -> list[dict[str, Any]]:
    try:
        results = await search_reports(search_term=search_term)
        reports = (results.get("data") or {}).get("report") or []
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "projectId": r["projectId"],
                "_workflow_note": f"Use id={r['id']} as reportId for attach_finding_to_report",
            }
            for r in reports
        ]
    except Exception as exc:  # noqa: BLE001
        raise _tool_error("search_ghostwriter_reports", exc) from exc


@server.tool(
    name="search_ghostwriter_clients",
    title="Search clients",
    description="""Search for existing Ghostwriter clients by name, codename, or shortName.

    USE CASE: Before creating a new client, search to see if it already exists.
    SEARCH BY: Client name, codename, or shortName (partial matches supported)
    RETURNS: List of clients with their IDs - use the 'id' field as clientId in create_ghostwriter_project

    Example searches:
    - search_ghostwriter_clients("Acme") -> finds "Acme Corp", "Acme Industries", etc.
    - search_ghostwriter_clients("ACME2024") -> finds client with codename "ACME2024"
    """,
    annotations=READ_ONLY,
)
async def search_ghostwriter_clients(
    search_term: str | None = None,
) -> list[dict[str, Any]]:
    try:
        results = await search_clients(search_term=search_term)
        clients = (results.get("data") or {}).get("client") or []
        return [
            {
                "id": c["id"],
                "name": c["name"],
                "codename": c["codename"],
                "shortName": c.get("shortName", ""),
                "address": c.get("address", ""),
                "description": c.get("description", ""),
                "_workflow_note": f"Use id={c['id']} as clientId for create_ghostwriter_project",
            }
            for c in clients
        ]
    except Exception as exc:  # noqa: BLE001
        raise _tool_error("search_ghostwriter_clients", exc) from exc


@server.tool(
    name="search_ghostwriter_projects",
    title="Search projects",
    description="""Search for existing Ghostwriter projects by codename, client info, or ID.

    USE CASE: Before creating a new project, search to see if it already exists.
    SEARCH BY: Project codename, client name, or client codename (partial matches supported)
    RETURNS: List of projects with their IDs - use the 'id' field as projectId in create_ghostwriter_report

    Example searches:
    - search_ghostwriter_projects("REDTEAM2024") -> finds project with codename "REDTEAM2024"
    - search_ghostwriter_projects("Acme") -> finds projects for clients named "Acme Corp", etc.
    """,
    annotations=READ_ONLY,
)
async def search_ghostwriter_projects(
    search_term: str | None = None,
) -> list[dict[str, Any]]:
    try:
        results = await search_projects(search_term=search_term)
        projects = (results.get("data") or {}).get("project") or []
        return [
            {
                "id": p["id"],
                "codename": p["codename"],
                "clientId": p["clientId"],
                "projectType": (p.get("projectType") or {}).get("projectType", "Unknown"),
                "startDate": p.get("startDate", ""),
                "endDate": p.get("endDate", ""),
                "description": p.get("description", ""),
                "clientName": (p.get("client") or {}).get("name", ""),
                "clientCodename": (p.get("client") or {}).get("codename", ""),
                "_workflow_note": f"Use id={p['id']} as projectId for create_ghostwriter_report",
            }
            for p in projects
        ]
    except Exception as exc:  # noqa: BLE001
        raise _tool_error("search_ghostwriter_projects", exc) from exc


@server.tool(
    name="get_ghostwriter_client_by_id",
    title="Get client by ID",
    description="Fetch a Ghostwriter client directly by ID. Returns full client details.",
    annotations=READ_ONLY,
)
async def get_ghostwriter_client_by_id_tool(clientId: int) -> list[dict[str, Any]]:
    try:
        results = await get_client_by_id(clientId)
        clients = (results.get("data") or {}).get("client") or []
        return [
            {
                "id": x["id"],
                "name": x["name"],
                "codename": x["codename"],
                "shortName": x.get("shortName", ""),
                "address": x.get("address", ""),
                "description": x.get("description", ""),
            }
            for x in clients
        ]
    except Exception as exc:  # noqa: BLE001
        raise _tool_error("get_ghostwriter_client_by_id", exc) from exc


@server.tool(
    name="get_ghostwriter_project_by_id",
    title="Get project by ID",
    description="Fetch a Ghostwriter project directly by ID. Returns project details.",
    annotations=READ_ONLY,
)
async def get_ghostwriter_project_by_id_tool(projectId: int) -> list[dict[str, Any]]:
    try:
        result = await get_project_by_id(projectId)
        projects = (result.get("data") or {}).get("project") or []
        return [
            {
                "id": w["id"],
                "codename": w["codename"],
                "clientId": w["clientId"],
                "projectType": (w.get("projectType") or {}).get("projectType", "Unknown"),
                "startDate": w.get("startDate", ""),
                "endDate": w.get("endDate", ""),
                "description": w.get("description", ""),
                "clientName": (w.get("client") or {}).get("name", ""),
                "clientCodename": (w.get("client") or {}).get("codename", ""),
                "_workflow_note": f"Use id={w['id']} as projectId for create_ghostwriter_report",
            }
            for w in projects
        ]
    except Exception as exc:  # noqa: BLE001
        raise _tool_error("get_ghostwriter_project_by_id", exc) from exc


@server.tool(
    name="get_ghostwriter_report_by_id",
    title="Get report by ID",
    description="Fetch a Ghostwriter report directly by ID. Returns report details.",
    annotations=READ_ONLY,
)
async def get_ghostwriter_report_by_id_tool(reportId: int) -> list[dict[str, Any]]:
    try:
        result = await get_report_by_id(reportId)
        reports = (result.get("data") or {}).get("report") or []
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "projectId": r["projectId"],
                "last_update": r.get("last_update", ""),
            }
            for r in reports
        ]
    except Exception as exc:  # noqa: BLE001
        raise _tool_error("get_ghostwriter_report_by_id", exc) from exc


@server.tool(
    name="generate_ghostwriter_codename",
    title="Generate codename",
    description="""Generate a codename for a new project.

    NOTE: This is typically used before creating a client or project to get a unique codename.""",
    annotations=WRITE,
)
async def generate_ghostwriter_codename() -> dict[str, str]:
    try:
        result = await generate_codename()
        return {"codename": (result.get("data") or {})["generateCodename"]["codename"]}
    except Exception as exc:  # noqa: BLE001
        raise _tool_error("generate_ghostwriter_codename", exc) from exc


@server.tool(
    name="create_ghostwriter_client",
    title="Create client",
    description="""Create a new Ghostwriter client using name, short name, and codename.

    DEPENDENCY: STEP 1 in the workflow (if the client doesn't exist).
    RECOMMENDED: First use search_ghostwriter_clients to check if the client already exists.
    RETURNS: clientId (required for create_ghostwriter_project)

    REQUIRED PARAMETERS:
    - name: Full client name (e.g., "Acme Corporation")
    - shortName: Abbreviated name (e.g., "Acme")
    - codename: Unique identifier (e.g., "ACME2024")

    OPTIONAL PARAMETERS (can be omitted):
    - address: Client's physical address
    - description: Additional notes about the client
    """,
    annotations=WRITE,
)
async def create_ghostwriter_client(
    name: str,
    shortName: str,
    codename: str,
    address: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    try:
        client_data = await create_client(
            name, shortName, codename, address, description
        )

        if not client_data:
            raise GhostwriterError("Failed to create client - no data returned")

        result = {
            "id": client_data["id"],
            "name": client_data["name"],
            "shortName": client_data.get("shortName", ""),
            "codename": client_data["codename"],
            "address": client_data.get("address", ""),
            "description": client_data.get("description", ""),
            "_workflow_note": "Save this 'id' as clientId for create_ghostwriter_project",
        }

        logger.info("Client created with ID: %s", result["id"])
        return result
    except Exception as exc:  # noqa: BLE001
        raise _tool_error("create_ghostwriter_client", exc) from exc


@server.tool(
    name="create_ghostwriter_project",
    title="Create project",
    description="""Create a new Ghostwriter project.

    DEPENDENCY: STEP 2 in the workflow (if the project doesn't exist).
    RECOMMENDED: First use search_ghostwriter_projects to check if the project already exists.
    REQUIRES: clientId from create_ghostwriter_client OR search_ghostwriter_clients
    RETURNS: projectId (required for create_ghostwriter_report)

    Parameters:
    - 'clientId': from create_ghostwriter_client or search_ghostwriter_clients
    - 'projectTypeId' defaults to GHOSTWRITER_DEFAULT_PROJECT_TYPE_ID. IDs are
      deployment-specific: query the `projectType` table to list them (a stock
      install ships Red Team, Penetration Test, Phishing Assessment and
      Web Application Assessment).
    - 'startDate' and 'endDate' are ISO dates (YYYY-MM-DD) and default to today
    """,
    annotations=WRITE,
)
async def create_ghostwriter_project(
    clientId: int,
    codename: str,
    projectTypeId: int | None = None,
    startDate: str | None = None,
    endDate: str | None = None,
) -> dict[str, Any]:
    try:
        result = await create_project(
            clientId, codename, projectTypeId, startDate, endDate
        )
        project = (result.get("data") or {}).get("insert_project_one")
        if not project:
            raise GhostwriterError("Failed to create project - no data returned")

        response = {
            "id": project["id"],
            "codename": project["codename"],
            "startDate": project["startDate"],
            "endDate": project["endDate"],
            "_workflow_note": "Save this 'id' as projectId for create_ghostwriter_report",
        }

        logger.info("Project created with ID: %s", response["id"])
        return response
    except Exception as exc:  # noqa: BLE001
        raise _tool_error("create_ghostwriter_project", exc) from exc


@server.tool(
    name="create_ghostwriter_report",
    title="Create report",
    description="""Create a new Ghostwriter report linked to a project.

    DEPENDENCY: STEP 3 in the workflow (if the report doesn't exist).
    RECOMMENDED: First use search_ghostwriter_reports to check if the report already exists.
    REQUIRES: projectId from create_ghostwriter_project OR search_ghostwriter_projects
    RETURNS: reportId (required for attach_finding_to_report)

    Parameters:
    - 'projectId': from create_ghostwriter_project or search_ghostwriter_projects
    - 'last_update': ISO date the report was last updated (defaults to today)
    """,
    annotations=WRITE,
)
async def create_ghostwriter_report(
    title: str,
    projectId: int,
    last_update: str | None = None,
) -> dict[str, Any]:
    try:
        result = await create_report(title, projectId, last_update)
        report = (result.get("data") or {}).get("insert_report_one")
        if not report:
            raise GhostwriterError("Failed to create report - no data returned")

        response = {
            "id": report["id"],
            "title": report["title"],
            "projectId": report["projectId"],
            "last_update": report["last_update"],
            "_workflow_note": "Save this 'id' as reportId for attach_finding_to_report",
        }

        logger.info("Report created with ID: %s", response["id"])
        return response
    except Exception as exc:  # noqa: BLE001
        raise _tool_error("create_ghostwriter_report", exc) from exc


@server.tool(
    name="create_ghostwriter_finding",
    title="Create finding",
    description=(
        "Create a new finding in the Ghostwriter findings library. "
        "Severity falls back to GHOSTWRITER_DEFAULT_SEVERITY_ID when omitted. "
        "'extraFields' maps to Ghostwriter's user-defined Extra Fields (v4.1+). "
        "Library findings have no 'affectedEntities' field - set that on the "
        "report finding via update_report_finding instead."
    ),
    annotations=WRITE,
)
async def create_ghostwriter_finding(
    title: str,
    description: str,
    findingTypeId: int | None = None,
    severityId: int | None = None,
    cvssScore: float | None = None,
    cvssVector: str | None = None,
    replication_steps: str | None = None,
    extraFields: dict | None = None,
) -> dict[str, Any]:
    try:
        result = await create_finding(
            title=title,
            description=description,
            findingTypeId=findingTypeId,
            severityId=severityId,
            cvssScore=cvssScore,
            cvssVector=cvssVector,
            replication_steps=replication_steps,
            extra_fields=extraFields,
        )

        if not result:
            raise GhostwriterError("Failed to create finding - no data returned")

        return {
            "id": result.get("id"),
            "title": result.get("title"),
            "description": result.get("description", ""),
        }
    except Exception as exc:  # noqa: BLE001
        raise _tool_error("create_ghostwriter_finding", exc) from exc


@server.tool(
    name="attach_finding_to_report",
    title="Attach finding to report",
    description="""Attach a finding from the library to a report.

    DEPENDENCY: STEP 4 in the workflow.
    REQUIRES: reportId from create_ghostwriter_report OR search_ghostwriter_reports

    Parameters:
    - 'finding': Either a finding ID (int) or a title (str) to search for
    - 'reportId': from create_ghostwriter_report or search_ghostwriter_reports
    """,
    annotations=WRITE,
)
async def attach_finding_to_report(
    finding: int | str, reportId: int
) -> dict[str, Any]:
    try:
        if isinstance(finding, str):
            search_results = await search_findings(finding)
            matches = (search_results.get("data") or {}).get("finding") or []
            if not matches:
                raise GhostwriterError(f"No finding found with title like: '{finding}'")
            findingId = matches[0]["id"]
        else:
            findingId = int(finding)

        result = await add_finding_to_report(findingId, reportId)
        attached = (result.get("data") or {}).get("attachFinding")
        if not attached:
            raise GhostwriterError("Failed to attach finding - no data returned")
        return {
            "reportedFindingId": attached["id"],
            "usedFindingId": findingId,
        }
    except Exception as exc:  # noqa: BLE001
        raise _tool_error("attach_finding_to_report", exc) from exc


@server.tool(
    name="list_report_finding",
    title="List report findings",
    description="List only the IDs and titles of findings attached to a report.",
    annotations=READ_ONLY,
)
async def list_report_finding_titles_tool(reportId: int) -> list[dict[str, Any]]:
    try:
        results = await list_report_findings(reportId)
        findings = (results.get("data") or {}).get("reportedFinding") or []
        return [{"id": f["id"], "title": f["title"]} for f in findings]
    except Exception as exc:  # noqa: BLE001
        raise _tool_error("list_report_finding", exc) from exc


@server.tool(
    name="update_report_finding",
    title="Update report finding",
    description="""Update the replication steps and/or affected entities of a reported finding.

    Note: This replaces the current text, it does not append to it.

    DEPENDENCY: STEP 5 in the workflow.
    REQUIRES: reportedFindingId from attach_finding_to_report.

    Parameters:
    - 'reportedFindingId': the reportedFindingId returned by attach_finding_to_report
      (the report-specific row, NOT the findings-library id)
    - 'replicationSteps': how to reproduce the finding (optional)
    - 'affectedEntities': assets or hosts affected by the finding (optional)
    """,
    annotations=DESTRUCTIVE_WRITE,
)
async def update_report_finding_tool(
    reportedFindingId: int,
    replicationSteps: str | None = None,
    affectedEntities: str | None = None,
) -> dict[str, Any]:
    try:
        result = await update_report_finding(
            reportedFindingId=int(reportedFindingId),
            replicationSteps=replicationSteps,
            affectedEntities=affectedEntities,
        )
        updated = (result.get("data") or {}).get("update_reportedFinding")
        if not updated:
            raise GhostwriterError("Failed to update reported finding - no data returned")
        return updated
    except Exception as exc:  # noqa: BLE001
        raise _tool_error("update_report_finding", exc) from exc


@server.tool(
    name="explain_workflow",
    title="Explain workflow",
    description=(
        "Explains the complete workflow for creating a new penetration testing "
        "report in Ghostwriter, including how to use existing entities."
    ),
    annotations=READ_ONLY,
)
async def explain_workflow() -> dict[str, Any]:
    return {
        "workflow_options": {
            "create_everything_new": [
                {
                    "step": 1,
                    "tool": "generate_ghostwriter_codename",
                    "purpose": "Generate a unique codename",
                    "returns": "codename (string)",
                },
                {
                    "step": 2,
                    "tool": "create_ghostwriter_client",
                    "purpose": "Create client organization",
                    "requires": "codename from step 1",
                    "returns": "clientId (integer) - SAVE THIS!",
                },
                {
                    "step": 3,
                    "tool": "create_ghostwriter_project",
                    "purpose": "Create project under client",
                    "requires": "clientId from step 2",
                    "returns": "projectId (integer) - SAVE THIS!",
                },
                {
                    "step": 4,
                    "tool": "create_ghostwriter_report",
                    "purpose": "Create report under project",
                    "requires": "projectId from step 3",
                    "returns": "reportId (integer) - SAVE THIS!",
                },
                {
                    "step": 5,
                    "tool": "attach_finding_to_report",
                    "purpose": "Add findings to the report",
                    "requires": "reportId from step 4",
                },
            ],
            "use_existing_entities": [
                {
                    "step": "1a",
                    "tool": "search_ghostwriter_clients",
                    "purpose": "Check if client already exists",
                    "returns": "clientId if found, otherwise create new client",
                },
                {
                    "step": "2a",
                    "tool": "search_ghostwriter_projects",
                    "purpose": "Check if project already exists",
                    "requires": "clientId from step 1a",
                    "returns": "projectId if found, otherwise create new project",
                },
                {
                    "step": "3a",
                    "tool": "search_ghostwriter_reports",
                    "purpose": "Check if report already exists",
                    "requires": "projectId from step 2a",
                    "returns": "reportId if found, otherwise create new report",
                },
                {
                    "step": "4a",
                    "tool": "attach_finding_to_report",
                    "purpose": "Add findings to existing or new report",
                    "requires": "reportId from step 3a",
                },
                {
                    "step": "traceback-1",
                    "tool": "get_ghostwriter_report_by_id",
                    "purpose": "Given a reportId, retrieve its projectId (to trace back to the project).",
                },
                {
                    "step": "traceback-2",
                    "tool": "get_ghostwriter_project_by_id",
                    "purpose": "Given a projectId, retrieve its clientId (to trace back to the client).",
                },
                {
                    "step": "traceback-3",
                    "tool": "get_ghostwriter_client_by_id",
                    "purpose": "Given a clientId, retrieve full client details (verify correct client).",
                },
            ],
        },
        "best_practices": [
            "Always search first before creating to avoid duplicates",
            "Each step depends on the ID returned from the previous step",
            "Save the 'id' field from each response to use in the next step",
            "You can mix search and create operations as needed",
            "Use search_ghostwriter_findings to find existing findings to attach",
        ],
        "common_scenarios": {
            "new_client_existing_project": "Search for project, if found use its clientId",
            "existing_client_new_project": "Search for client, use its ID to create project",
            "add_findings_to_existing_report": "Search for report, use its ID to attach findings",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ghostwriter MCP Server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport mode: 'stdio' for local MCP clients (Claude Desktop, VS Code), "
        "'sse' for HTTP-based clients",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind when using SSE transport",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8009,
        help="Port to bind when using SSE transport",
    )
    args = parser.parse_args()

    if args.transport == "sse":
        logger.info("Starting Ghostwriter MCP server (SSE) on %s:%s", args.host, args.port)
        server.run(transport="sse", host=args.host, port=args.port)
    else:
        logger.info("Starting Ghostwriter MCP server (stdio)")
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
