---
name: ghostwriter
description: |
  Write penetration test and red team reports in Ghostwriter through its MCP server. Use this skill when the user mentions Ghostwriter, asks to create or look up a client, project, report, or finding for an engagement, wants to attach findings to a report, needs to tailor a finding for a specific engagement, or says things like "add this finding to the report", "set up a new engagement in Ghostwriter", "what findings are on this report", "document this vulnerability", or "generate the report". Covers the tool surface, the platform model behind it, and the traps that silently produce wrong reports.
---

# Ghostwriter

Ghostwriter is SpecterOps' engagement management and report-writing platform. Its
data model is a strict hierarchy, and the MCP server exposes a slice of it over
GraphQL (Hasura).

```
client ──> project ──> report ──> reportedFinding
                                       ^
                      finding (library) ┘ copied on attach
```

Everything starts with a client. You cannot create a project without a client, a
report without a project, or attach a finding without a report. The tools return
the id the next step needs, so hold onto each one.

## Sibling skills

This skill covers the data model and the MCP tools. Sibling skills cover the work around
them: creating and reviewing the Word/PPTX templates a report is generated from, checking a
report is ready to generate, and drafting an executive summary. Reach for those instead of
reimplementing them here. They are installed alongside this one, so if you can read this file
the others are in the same directory.

## Before you start

Call `list_ghostwriter_lookups` first. It returns `projectTypes`, `findingTypes`,
and `severities` as `{id, name}`. **These ids are seeded per deployment and differ
between installations** — a stock install numbers project types Red Team=1,
Penetration Test=2, but yours may not. Never hardcode them and never guess. There
is no other way to discover them through the tools.

Then search before creating, so you extend existing records rather than making
duplicates: `search_ghostwriter_clients`, `search_ghostwriter_projects`,
`search_ghostwriter_reports`, `search_ghostwriter_findings`. All take a single
`search_term` and match substrings.

## The critical rule: library findings vs report findings

This is the single most important thing to understand, and the wiki flags it as its
main warning.

A **library finding** (`finding` table) is a reusable template shared by everyone.
`attach_finding_to_report` **copies** it into the report as a **reportedFinding**,
which gets its own id. From then on they are independent records.

- Editing the **library** entry changes it for every report that uses it. Only do
  this when improving the reusable template for the whole team.
- Editing the **report copy** affects one report. This is where all
  engagement-specific work belongs.

So: attach the template, then tailor the copy. Never edit the library to say
something about one engagement.

`attach_finding_to_report` returns `{reportedFindingId, usedFindingId}`. Pass
**`reportedFindingId`** to `update_report_finding`. Passing a library finding id
there is the most common mistake; it is rejected rather than silently applied.

## Creating an engagement

```
list_ghostwriter_lookups                    -> ids you will need
generate_ghostwriter_codename               -> codename
create_ghostwriter_client(name, shortName, codename)      -> clientId
create_ghostwriter_project(clientId, codename,
                           projectTypeId)   -> projectId
create_ghostwriter_report(title, projectId) -> reportId
attach_finding_to_report(finding, reportId) -> reportedFindingId
update_report_finding(reportedFindingId, ...)
```

Notes that save a failed call:

- `create_ghostwriter_project` **requires `projectTypeId`** unless the deployment
  sets `GHOSTWRITER_DEFAULT_PROJECT_TYPE_ID`. This is why you call lookups first.
- Codenames come from `generate_ghostwriter_codename` and are Ghostwriter's
  convention for referring to an engagement without naming the client. Use one.
- Dates are ISO `YYYY-MM-DD`. `startDate` defaults to today and `endDate` defaults
  to `startDate`, which is rarely what a real engagement wants — pass both.
- `attach_finding_to_report`'s `finding` takes either a library id (exact) or a
  title string (searched). Prefer the id when you have it; a title that matches
  nothing is an error, not a no-op.

## Writing good findings

`create_ghostwriter_finding` builds a library template. Populate the fields a
report actually needs, not just title and description:

| Field | Holds |
| --- | --- |
| `title`, `description` | what it is |
| `impact` | consequences if exploited |
| `mitigation` | how to fix it |
| `replication_steps` | how to reproduce it |
| `references` | advisories, CWE, standards |
| `hostDetectionTechniques`, `networkDetectionTechniques` | how a defender spots it |
| `findingGuidance` | **internal only, never rendered into the report** — notes to the operator about how to use this template |
| `severityId`, `findingTypeId` | from lookups |
| `cvssScore` (0.0-10.0), `cvssVector` | scoring |

Then on the report copy via `update_report_finding`: `affectedEntities` (the hosts
or users actually affected — the library has no such field), plus
`replicationSteps` as observed, and a re-rating through `severityId` / `cvssScore`
if engagement context changes the risk. `complete` marks it reviewed and
`position` orders it within its severity group.

`update_report_finding` **replaces** values rather than appending, and `""` clears
a field. Only the fields you pass change. Updating an id that does not exist is an
error, not a silent no-op.

## Rich text is Jinja2 — this will bite you

Ghostwriter renders `description`, `impact`, `mitigation`, and the other rich text
fields **through Jinja2 at report generation time**, and they accept HTML.

- HTML is expected: `<p>`, `<ul><li>`, `<code>`. Plain text works but renders as one
  block.
- **`{{ ... }}` and `{% ... %}` are template syntax, not literal text.** Writing
  `{{ user }}` in a description means "substitute the user variable here". If a
  finding legitimately needs literal braces — sample payloads, template injection
  findings, Jinja/Handlebars exploit strings — they must be escaped or the document
  will fail to generate or silently mangle the payload. Use `{{ "{{" }}` for a
  one-off, or a `{% raw %}` block for a larger sample.
- This matters most for exactly the findings where it is easiest to forget: SSTI,
  template injection, and anything quoting a config file.

Useful in this context: `{{ report.title }}`, `{{ project.codename }}`, and on a
finding's rich text fields, `{{ finding }}` refers to the finding itself.

## Reading existing data

- `get_ghostwriter_client_by_id`, `get_ghostwriter_project_by_id`,
  `get_ghostwriter_report_by_id` — direct lookups by id.
- `list_report_finding(reportId)` — ids and titles of findings on a report. Use it
  to get a `reportedFindingId` before updating.
- To walk **up** from a report: `get_ghostwriter_report_by_id` gives `projectId`,
  then `get_ghostwriter_project_by_id` gives `clientId` plus resolved
  `clientName` and `clientCodename`.
- `explain_workflow` returns the server's own step-by-step walkthrough, including
  the use-existing-entities path. Useful as a self-check if a sequence fails.

## Deleting

Five delete tools, narrowest first:

| Tool | Removes | Also takes with it |
| --- | --- | --- |
| `delete_ghostwriter_report_finding` | one report's copy of a finding | nothing (library template survives) |
| `delete_ghostwriter_report` | a report | its findings, evidence, observations |
| `delete_ghostwriter_project` | a project | its reports, and their findings |
| `delete_ghostwriter_client` | a client | its projects, reports, and findings |
| `delete_ghostwriter_finding` | a findings-library template | nothing (existing report copies survive) |

Each requires `confirmId` matching the id, because deletion is permanent and
Ghostwriter has no undo:

```
delete_ghostwriter_client(clientId=6, confirmId=6)
```

Work bottom-up — report findings, reports, projects, clients, then library
entries — so you can see what you are removing instead of triggering a cascade
blind. Prefer deleting the narrowest thing that solves the problem, and confirm
with the user before deleting anything you did not create yourself.

## What the tools do not cover

The MCP server is a slice of Ghostwriter. These exist in the platform and the
GraphQL API but have no tool, so say so rather than pretending:

- **Generating the actual deliverable.** The `generateReport(id:)` mutation returns
  `docxUrl`, `pptxUrl`, `xlsxUrl`, and base64 `reportData`. Writing findings is not
  the same as producing the document; direct the user to the Ghostwriter UI or the
  API for that final step.
- **Observations** — a slimmed-down parallel to findings (`title`, `description`
  only) for reporting things the target does *well*. Same library-vs-copy rule.
- **Evidence files** — screenshots and artifacts attached to findings, referenced
  in rich text.
- **Infrastructure** — domain and server checkouts, which have their own
  `checkoutDomain` / `checkoutServer` actions with overlap and availability checks.
- **Points of contact**, oplogs, project completion and the 90-day archive
  countdown, report templates, collaborative editing, tags.
- **Bulk import.** The library accepts a CSV whose headers are snake_case
  (`replication_steps`, `host_detection_techniques`, `finding_guidance`,
  `finding_type`) — note these differ from the camelCase used here. Import matches
  on `title` and **updates** an existing entry rather than duplicating it.

## Gotchas worth remembering

- **Field naming is genuinely inconsistent** across this deployment's schema:
  `cvssScore` and `extraFields` are camelCase, `replication_steps` and
  `last_update` are snake_case. Tool parameters are named after the GraphQL field
  they feed, so follow the tool signature rather than guessing a convention.
- **`extraFields`** holds deployment-specific custom fields (Ghostwriter 4.1+) as
  a JSON object. Assigning replaces the whole object. Only use keys the deployment
  actually defines.
- Tools raise real errors instead of returning empty success, so read failure
  messages — they name the offending field or id.
- The findings library is a shared source of truth. Search it before creating a
  new template; an engagement usually needs an existing template attached and
  tailored, not a new library entry.
