# ContinuAI - Project Notes (Round 3)

ContinuAI is an AI-powered project knowledge handover platform that reconstructs ownership, dependencies, and architectural decisions from a Git repository to generate structured handover documentation.

---

## 1. Architecture Overview
- **Backend**: Python / Flask (`app.py`), lightweight REST endpoints.
- **Intelligence Engine**: `analyzer.py` handles ingestion, module co-change analysis (NetworkX), decision extraction, and risk detection.
- **Frontend**: Single-page vanilla HTML/CSS/JavaScript in `static/index.html`. No build steps or frontend frameworks needed.
- **Database**: SQLite (`continuai.db`) accessed via Python's built-in `sqlite3`.
  - `h`: `(id, source, departing, status, created, data)`
  - `log`: `(id, hid, ts, actor, kind, detail, snap)`

---

## 2. File Map
- `app.py`: Flask API entry point. Routes:
  - `POST /api/analyze`: Ingests source, triggers analysis, stores draft in DB.
  - `GET /api/h`: Lists recent handovers.
  - `GET /api/h/<id>`: Returns full handover data.
  - `POST /api/h/<id>/item`: Approve / reject / edit individual handover items.
  - `POST /api/h/<id>/publish`: Publishes handover when items are approved.
  - `GET /api/h/<id>/view?role=<role>`: Role-specific view (filters approved items & module subgraph).
  - `POST /api/h/<id>/ack`: Logs confirmation of receipt by incoming engineer.
  - `GET /api/h/<id>/log`: Version history & audit log.
- `analyzer.py`:
  - `load_commits(source)`: Clones bare repo or parses demo data.
  - `file_role(path)`: Maps file paths to roles (`infra`, `docs`, `ml`, `frontend`, `backend`, `general`).
  - `analyze(commits, base, departing)`: Aggregates contributions, generates NetworkX graph, extracts decisions/tasks/risks/dependencies, and assigns confidence.
  - `demo_commits()`: Built-in 15 commits for immediate offline demo testing.
- `static/index.html`: Responsive single-page UI with visual graph rendering and role switcher.
- `requirements.txt`: Minimal runtime dependencies (`flask`, `networkx`, `requests`, `gunicorn`, `pytest`).

---

## 3. How to Run
```bash
# In hackathon-main/hackathon-main:
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```
Open `http://localhost:5000` in browser. Click "Try the sample project" for instant demo without cloning.

---

## 4. Tier 1 Architecture Additions
- `llm/`:
  - `providers.py`: `BaseLLMProvider` interface and concrete implementations for:
    - Gemini (`gemini-2.5-flash` or `GEMINI_MODEL`) via Google REST API
    - Anthropic (`claude-3-5-sonnet` or `ANTHROPIC_MODEL`) via Anthropic Messages API
    - OpenAI (`gpt-4o-mini` or `OPENAI_MODEL`) via OpenAI Chat Completions API
    - Fallback: Offline Rule-Based Extractor
  - `engine.py`: Provider selector (`LLM_PROVIDER`), retry logic, structured JSON prompt generation, and error resilience.
- `ast_analyzer.py`: Python AST diff analyzer extracting functions, classes, changed signatures, and import diffs from patches/blobs.
- Confidence Scoring: Graded `0.0 - 1.0` blending text evidence (rationale strength) and structural AST signal. Backward compatible: retains `confidence` string ("confirmed", "likely", "inferred") and exposes `confidence_score`.
