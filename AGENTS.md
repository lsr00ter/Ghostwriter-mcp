# AGENTS.md

Instructions for coding agents working in this repository.

## What this is

A stdio MCP server exposing a slice of Ghostwriter through its GraphQL/Hasura API.
`main.py` holds the tool definitions and `ghostwriter_api.py` the GraphQL client;
each tool's docstring is its user-facing documentation. Keep that flat layout — the
checked-in MCP configs point at those two module paths.

`.env` holds a live API token. Never print it and never commit it.

## Before you commit

```bash
./.venv/bin/python -m unittest discover -s tests   # tests
./.venv/bin/ruff check .                           # lint
```

Both must be clean. Neither needs the submodule checked out.

Target Python 3.10: backslash escapes inside f-string expressions are a syntax error
before 3.12.

Match the **live** schema, not the upstream `DOCS/schema.graphql`, which drifts —
the live instance mixes casing (`cvssScore` and `extraFields` are camelCase;
`replication_steps` and `last_update` are snake_case). Verify a field exists before
relying on it.

## Skills

`skills/ghostwriter/SKILL.md` is the authority on the platform model and on using
the tools; read it before touching the live instance. The sibling skills in
`vendor/ghostwriter-skills/` cover templates, report readiness, and executive
summaries.

Never edit anything under `vendor/` — it is a pinned git submodule and must stay
pristine so an upstream bump cannot conflict. It is excluded from ruff for that
reason. Never hand-manage the skill links either: `./scripts/sync-skills.sh` owns
them and prunes stale ones. The README documents the discovery directories.

## Where facts live

These docs were deduplicated on purpose: each fact has one home and the others point
at it. Keep it that way.

| Fact | Home |
| ----- | ---- |
| What the tools mean and how the platform behaves | `skills/ghostwriter/SKILL.md` |
| Install, configuration, connecting a client, repo layout | `README.md` |
| Rules for changing this repository | this file |
| Always-on safety and the workflow, at runtime | `main.py`'s `SERVER_INSTRUCTIONS` and `explain_workflow` |

The skill ships on its own, so it states platform behaviour in full on purpose. This
file is always read next to the README, so prefer a pointer over a paraphrase.

## Using the tools against the live instance

These tools talk to a real Ghostwriter.

- Never delete a row you did not create without asking the user first: deletion is
  permanent.
- Anything created for testing must be obvious and disposable: prefix names with
  `ZZ MCP ... (safe to delete)` and use a short name like `ZZTEST`.
