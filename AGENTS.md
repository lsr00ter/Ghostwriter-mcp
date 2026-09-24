# AGENTS.md

Instructions for coding agents working in this repository.

## What this is

A stdio MCP server that exposes a slice of Ghostwriter (SpecterOps' engagement
management and reporting platform) through its GraphQL/Hasura API, so an agent can
create and read clients, projects, reports, and findings.

- `main.py` — the MCP tool surface. Tool docstrings are the user-facing
  documentation for each tool.
- `ghostwriter_api.py` — the GraphQL client. All requests go through it.
- `tests/` — `unittest` with mocked HTTP. They must pass with no network and no
  live Ghostwriter.
- `.env` — connection settings and a **live API token** (gitignored). Never print
  its contents, never commit it, and never read the token into output.

## Working on this repo

```bash
./.venv/bin/python -m unittest discover -s tests   # tests
./.venv/bin/ruff check .                           # lint
```

Both must be clean before you commit. Target Python 3.10 — note that backslash
escapes inside f-string expressions are a syntax error before 3.12.

Prefer the existing flat layout (`main.py` + `ghostwriter_api.py`): the Pi MCP
config points at those module paths, so a package restructure breaks it.

Match the **live** schema, not the upstream `DOCS/schema.graphql`, which drifts.
Naming in the live instance is genuinely inconsistent (`cvssScore` and
`extraFields` are camelCase; `replication_steps` and `last_update` are snake_case).
Verify a field exists before relying on it.

## Skills

Two sets, both linked into every project-local discovery directory:

- `skills/ghostwriter/SKILL.md` — this repo's own skill: the data model, the
  library-versus-report-copy rule, the MCP tools, and the platform traps.
- `vendor/ghostwriter-skills/` — SpecterOps' upstream collection (template
  creation and review, report readiness, executive summary), vendored as a git
  submodule. **Never edit files under `vendor/`**; it must stay pristine so
  upstream bumps do not conflict. It is excluded from ruff for that reason.

`.agents/skills/`, `.claude/skills/`, `.codex/skills/`, and `.pi/skills/` hold
symlinks to those sources. They are generated, not hand-written:

```bash
./scripts/sync-skills.sh          # pinned commit vs upstream main
./scripts/sync-skills.sh init     # after a clone without --recursive
./scripts/sync-skills.sh preview  # incoming upstream commits
./scripts/sync-skills.sh update   # bump the submodule and relink
```

Do not add or remove skill links by hand — `link` owns them and will prune links
pointing into the skill sources whose skill no longer exists. Links to skills you
add yourself elsewhere in those directories are left alone.

## Connecting an MCP client

- Claude Code: `.mcp.json` (project scope, picked up automatically).
- Codex: `.codex/config.toml`. Codex has no project-local discovery, so run it as
  `CODEX_HOME="$PWD/.codex" codex`. `codex mcp add` cannot record `cwd`, which is
  what makes `python-dotenv` find `.env` — it must be set in the TOML.

## Safety rules when using the tools

These tools talk to a real Ghostwriter instance.

- **Do not delete anything you did not create** without asking the user first.
  Deletes are permanent, take a `confirmId`, and cascade (a client takes its
  projects, reports, and findings with it).
- Anything created for testing must be obvious and disposable: prefix names with
  `ZZ MCP ... (safe to delete)` and use a short name like `ZZTEST`.
- `list_ghostwriter_lookups` first. Lookup ids (`projectTypeId`, `severityId`,
  `findingTypeId`) are seeded per deployment, so they cannot be hardcoded.
- Rich text fields are rendered through Jinja2 at report generation. Literal
  `{{ }}` must be escaped, or the document will not generate — this matters most
  for template-injection findings.
- Attaching a library finding **copies** it. Edit the copy for engagement work;
  editing the library entry changes every report that uses it.
