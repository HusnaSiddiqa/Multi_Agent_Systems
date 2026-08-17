# C3PO v3.1 — ONC Analytics Assistant

## Overview
C3PO is an AI-powered analytics assistant for the client's Oncology APO team. It answers natural language questions about claims metrics, NPS share, market dynamics, and more.

## How It Works

### Three Answering Paths

| Path | When | How |
|------|------|-----|
| Click Path | User clicks a suggestion | Pre-built SQL template executes instantly |
| Semantic Path | Typed question matches a template | Mask, vector search, slot extract, SQL, insight |
| Supervisor Path | No template match (score < 0.55 or LLM verifier reject) | Routed to Databricks Genie (chat mode) |

## Features

| Feature | Description |
|---------|-------------|
| Typeahead Suggestions | Start typing to see matching pre-built questions |
| Export to PowerPoint | Generate a chart slide from any result |
| Download Excel | Exports full dataset as .xlsx  |
| Narrative Deck | Download the latest auto-generated narrative PowerPoint deck |
| Multi-turn Conversations | Follow-up questions use conversation context |
| Chat History | All threads are saved and resumable from the sidebar |

## Databricks Genie Spaces

Direct links to the Genie Agent Mode spaces in Databricks are organization-specific and are not committed to this repo. See `system_files/org_config.local.yaml` (gitignored — copy from `system_files/org_config.example.yaml`) for the real links.

**Note:** C3PO currently uses Genie Chat Mode via the supervisor endpoint (MAS API). The Genie Agent Mode API is not yet available in the client's environment. Until it becomes available, if you need Agent Mode capabilities (multi-step reasoning, tool use, iterative SQL refinement), use the Genie Space links in `org_config.local.yaml` to navigate directly to Agent Mode in the Databricks UI.

### Processing Steps (visible in progress panel)

| Step | What it does |
|------|-------------|
| Resolve Context | Rewrites follow-up questions using conversation history (skipped for 1st question) |
| Mask | Removes specific drug/time/LOT values, leaving a generic question shape |
| Retrieve | Vector search finds the closest template |
| Judge | LLM confirms whether the template can answer the question |
| Slot Extraction | Extracts parameters (drug, time period, line of therapy, etc.) |
| SQL Execution | Renders and runs the SQL query |
| Insight Generation | LLM summarizes the result in plain English |


## Deployment

`app.yaml` (the Databricks Apps manifest) contains real, organization-specific values and is gitignored — it is never committed. To (re)generate it locally:

1. Copy `system_files/org_config.example.yaml` to `system_files/org_config.local.yaml` and fill in the real values (also gitignored).
2. Run `python scripts/render_app_yaml.py` to render `app.yaml` from `app.yaml.example` + `org_config.local.yaml`.
3. Deploy as usual — Databricks Apps reads the generated `app.yaml`.

---
Built by the Infigen team.
