# ContinuAI

Project knowledge handover from a Git repository. Connect, reconstruct, verify, review, hand over.

## Run (VS Code terminal)

    python -m venv .venv
    .venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
    pip install -r requirements.txt
    python app.py

Open http://localhost:5000 and click "Try the sample project". Git must be installed to analyze real repositories.

Optional: set `ANTHROPIC_API_KEY` to use Claude for decision extraction. Without it, a rule-based extractor runs. Both tag each claim as confirmed or inferred. Deploy with `gunicorn app:app`.

## Workflow
1. Connect: GitHub URL, `owner/repo`, or local path (read via `git log`).
2. Reconstruct: contribution map, module co-change graph, single-owner risks.
3. Verify: decisions, reasons and rejected alternatives linked to commits; unsupported claims marked inferred; reverts surfaced as conflicts.
4. Review: approve, reject or edit each item. Every change is versioned.
5. Handover: publish approved items only, view by role, confirm receipt. All actions appear in Activity.

## Round 2 notes
- Changes from the plan: dependency graph uses module co-change (static import analysis moves to Round 3); ingestion uses git clone instead of the GitHub API, so PRs and issues are not yet read.
- Suggested Round 3 improvements: GitHub PR and issue ingestion, import/AST dependency analysis, GitHub-team-based access control.

Layout: `app.py` (API, SQLite), `analyzer.py` (analysis), `static/index.html` (UI).

## Round 3 setup

ContinuAI Round 3 adds a pluggable multi-provider LLM engine (Gemini, Anthropic, OpenAI), Python AST structural diff analysis, 3-tier graded confidence scoring, GitHub/GitLab OAuth 2.0 with Fernet-encrypted token storage, RBAC policy mapping from GitHub teams, and post-publish Slack, Jira, and Notion integrations.

### 1. Environment Configuration (.env)
Copy `.env.example` to `.env` and configure:
- **LLM Engine**: `LLM_PROVIDER` (`gemini`, `anthropic`, `openai`, or `rule-based`), with corresponding `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY`. Model names default to `gemini-2.5-flash`, `claude-3-5-sonnet-20241022`, or `gpt-4o-mini` (or override via `GEMINI_MODEL`, `ANTHROPIC_MODEL`, `OPENAI_MODEL`).
- **Security & Encryption**: `FLASK_SECRET_KEY` (session signing hex) and `TOKEN_ENCRYPTION_KEY` (Fernet base64 key).
- **OAuth 2.0 (GitHub / GitLab)**: `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET`. (Optional: `GITLAB_CLIENT_ID`, `GITLAB_CLIENT_SECRET`, `GITLAB_BASE`).
- **Web App**: `BASE_URL` (e.g. `http://localhost:5000`) and `PORT` (default: 5000).
- **Integrations (Optional)**:
  - `SLACK_WEBHOOK_URL`: Slack Incoming Webhook URL.
  - `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY`, `JIRA_ISSUE_TYPE` (default: Epic).
  - `NOTION_TOKEN`, `NOTION_DATABASE_ID`, `NOTION_TITLE_PROP` (default: Name).
  - `INTEGRATIONS_DRY_RUN=1`: Simulates external API calls without dispatching real requests.

### 2. GitHub OAuth App Setup
1. In GitHub, go to **Settings > Developer settings > OAuth Apps > New OAuth App**.
2. Set **Application name** to `ContinuAI`.
3. Set **Homepage URL** to your `BASE_URL` (e.g. `http://localhost:5000`).
4. Set **Authorization callback URL** to:
   ```
   http://localhost:5000/auth/github/callback
   ```
   *(Or `<BASE_URL>/auth/github/callback` in production).*
5. Save the generated **Client ID** and **Client Secret** into your `.env` file.

### 3. Running the Test Suite
```bash
pytest -v
```
All unit and integration tests run with mocked external calls (zero API credits consumed).

## Deploy on Render
1. Push this folder to a GitHub repository (public, so judges can open it).
2. Render dashboard: New > Blueprint, select the repo. `render.yaml` sets everything up.
3. Or New > Web Service with build command `pip install -r requirements.txt` and start command `gunicorn app:app --workers 1 --threads 4 --timeout 120`.
4. The free plan sleeps after inactivity (first load takes about 50 seconds) and resets the SQLite file on redeploy. Open the link once before judging.
