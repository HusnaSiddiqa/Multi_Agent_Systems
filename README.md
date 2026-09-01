# Multi-Agent Analytics Assistant

This app is an AI analytics assistant for an Oncology (APO) team. Users ask natural-language
questions about claims metrics, NPS / share, and market dynamics, and answers with a
data table, a plain-English insight, and optional PowerPoint / Excel exports.

Under the hood it is a **multi-agent system**: a router picks one of three answering
paths, each backed by its own agent, with a Databricks Genie supervisor as the catch-all
fallback.

---

## Answering paths

| Path           | Triggered when                                                        | Agent / flow                                                                                     |
| -------------- | -------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| **Click**      | User picks a typeahead suggestion (a known template)                 | Fetch template by `example_id` → extract slots → render & run SQL → generate insight            |
| **Semantic**   | Typed question matches a template (vector score ≥ `SCORE_THRESHOLD`) | Resolve context → mask → vector retrieve → LLM judge → slot extract → run SQL → generate insight |
| **Supervisor** | No template match (score `< 0.55`, or the LLM judge rejects)          | Routed to a Databricks Genie supervisor endpoint (chat mode), with trace/telemetry extraction   |

The client's environment currently exposes Genie **Chat Mode** only; Agent Mode (multi-step
reasoning, tool use, iterative SQL) is reached via direct Genie Space links in
`org_config.local.yaml`.

---

## Processing steps

These are streamed to the UI progress panel as each stage completes:

| Step                   | What it does                                                              |
| ---------------------- | ----------------------------------------------------------------------- |
| **Resolve Context**    | Rewrites a follow-up into a standalone question using thread history (skipped on the 1st turn) |
| **Mask**               | Strips specific drug / time / line-of-therapy values, leaving a generic question shape |
| **Retrieve**           | Databricks Vector Search finds the closest SQL template                  |
| **Judge**              | An LLM verifies the retrieved template can actually answer the question  |
| **Slot Extraction**    | Pulls parameters (drug, quarter/wave, line of therapy, …) from the question |
| **SQL Execution**      | Renders the template with slots and runs it on a SQL Warehouse           |
| **Insight Generation** | LLM summarizes the result set in plain English                           |

Latest-quarter resolution and repeated-query results are cached in-process (5-minute TTL).

---

## Features

- **Typeahead suggestions** — keyword-scored matches against the pre-built question set
- **Multi-turn conversations** — follow-ups are resolved against thread history
- **Chat history** — every thread is persisted to Delta and resumable from the sidebar
- **Export to PowerPoint** — native editable chart slide generated from any result
- **Download Excel** — full result set as `.xlsx`
- **Narrative deck** — download the latest auto-generated narrative PowerPoint from a UC Volume
- **Feedback** — thumbs up/down + comments captured per answer

---

## Architecture

```
                 ┌──────────────────────────────┐
  React SPA  ───▶│  Flask API (app.py)          │
  (frontend/)    │  /api/ask  → session_id      │
                 │  /api/progress/<id> (poll)   │
                 └───────────────┬──────────────┘
                                 │ ThreadPoolExecutor
                                 ▼
                    ┌────────────────────────┐
                    │  Router (initial_path) │
                    └───┬──────────┬─────────┘
             click /    │ semantic │        \ no match
                        ▼          ▼         ▼
        ┌───────────────────────────┐   ┌──────────────────────┐
        │ sql_template_agent.py     │   │ supervisor_agent.py  │
        │  • exact / vector match   │   │  • call Genie endpt  │
        │  • LLM judge              │   │  • parse output      │
        │  • slot extraction        │   │  • extract trace /   │
        │  • SQL render + execute   │   │    genie space calls │
        │  • insight generation     │   └──────────────────────┘
        └────────────┬──────────────┘
                     ▼
        ┌───────────────────────────┐     ┌────────────────────────┐
        │ ppt_agent/  (on demand)   │     │ conversation.py        │
        │  profiler → llm_chooser → │     │  chat history (Delta)  │
        │  chart_builder (pptx)     │     │  threads / messages    │
        └───────────────────────────┘     └────────────────────────┘

  Databricks: SQL Warehouse · Vector Search index · Genie supervisor endpoint ·
              LLM serving endpoint · UC Volume (narrative deck) · Delta chat-history table
```

---

## Project structure

```
app.py                         # Flask API: routes, session store, progress streaming
config.py                      # Env-var configuration
conversation.py                # Chat history + threads (Delta table CRUD)
suggestions.py                 # Typeahead: load template examples, keyword scoring
sql_template_agent.py          # Click + Semantic paths (mask, retrieve, judge, slots, SQL, insight)
supervisor_agent.py            # Genie supervisor fallback + trace extraction
feedback_module.py             # Thumbs up/down + comments persistence
scripts/render_app_yaml.py     # Render app.yaml from app.yaml.example + org_config.local.yaml

ppt_agent/                     # Chart-slide generation (native editable PPTX)
  agent.py                     #   orchestrator: profile → choose → render, returns bytes
  profiler.py                  #   infers column types / cardinality
  llm_chooser.py               #   LLM picks chart type + column→channel mapping; deterministic fallback
  registry.py                  #   chart-type registry (single source of truth)
  chart_builder.py             #   python-pptx renderer

utils/
  llm_client.py                # LLM serving client
  sql_execution.py             # SQL Warehouse execution, PAT/OAuth token helpers
  format_data_to_statement_response.py

frontend/                      # React + Vite + TypeScript SPA (@databricks/appkit-ui, recharts, tailwind)
system_files/                  # Prompts (masking, slot extraction, judge, insight) + example configs
```

---

## API

| Method | Route                              | Purpose                                          |
| ------ | ---------------------------------- | ----------------------------------------------- |
| GET    | `/api/suggestions?q=`             | Typeahead suggestions                            |
| POST   | `/api/ask`                        | Submit a question → returns `session_id`         |
| GET    | `/api/progress/<session_id>`      | Poll step progress + final result               |
| POST   | `/api/generate-ppt`              | Chart slide (PPTX) from a result                 |
| POST   | `/api/download-excel`            | Full dataset as `.xlsx`                          |
| GET    | `/api/download-narrative-deck`   | Latest auto-generated narrative deck (UC Volume) |
| POST   | `/api/feedback`                  | Rating / comment on an answer                    |
| POST   | `/api/thread/new`               | Start a new chat thread                          |
| GET    | `/api/history`                  | List the user's threads                          |
| GET    | `/api/thread/<thread_id>/messages` | Messages in a thread                          |
| POST   | `/api/admin/refresh-templates`  | Reload template examples cache                   |
| GET    | `/api/health`, `/api/debug`     | Health / diagnostics                            |

User identity comes from the `X-Forwarded-Email` / `X-Forwarded-User` headers injected by
Databricks Apps.

---

## Configuration

All settings are environment variables (see [`config.py`](config.py) and
[`app.yaml.example`](app.yaml.example)):

| Variable                                        | Purpose                                        |
| ---------------------------------------------- | -------------------------------------------- |
| `DATABRICKS_HOSTNAME` / `WORKSPACE_HOST`       | Workspace host                                |
| `SQL_WAREHOUSE_HTTP_PATH`                      | SQL Warehouse for template + history queries  |
| `LLM_MODEL`                                    | LLM serving endpoint (e.g. `databricks-claude-sonnet-4-5`) |
| `VECTOR_INDEX_NAME`                            | Vector Search index of SQL templates          |
| `SCORE_THRESHOLD` / `UPPER_SCORE_THRESHOLD`    | Template match cutoffs (default `0.55` / `0.95`) |
| `SUPERVISOR_ENDPOINT_NAME` / `_URL`           | Genie supervisor serving endpoint             |
| `SUPERVISOR_EXPERIMENT_ID`                     | MLflow experiment for supervisor traces       |
| `SUGGESTION_QUESTIONS_TABLE`                   | Template examples table (typeahead + latest quarter) |
| `CHAT_HISTORY_TABLE`                           | Delta table for threads / messages            |
| `NARRATIVE_DECK_VOLUME_PATH`                   | UC Volume path for the narrative deck          |
| `MAX_SUGGESTIONS` / `MIN_CHARS_FOR_SUGGESTIONS`| Typeahead behaviour                            |

**Secrets are never committed.** `app.yaml` and `system_files/org_config.local.yaml` are
git-ignored; the repo ships only `*.example` templates.

---

## Local development

```bash
# Backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# export the env vars above, then:
python app.py                     # serves API + built frontend on PORT (default 8000)

# Frontend (separate terminal, for hot reload)
cd frontend
npm install
npm run dev
```

---

## Deployment (Databricks Apps)

`app.yaml` is generated, not committed:

1. Copy `system_files/org_config.example.yaml` → `system_files/org_config.local.yaml` and fill in real values.
2. Run `python scripts/render_app_yaml.py` to render `app.yaml` from `app.yaml.example` + `org_config.local.yaml`.
3. Deploy the app — Databricks Apps runs the manifest command: build the frontend, install
   `requirements.txt`, then `python app.py`.

---

