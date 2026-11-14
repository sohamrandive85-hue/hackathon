"""ContinuAI backend. Run: python app.py"""
import json, os, sqlite3, subprocess, time

# Lightweight .env loader without external dependencies
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

from flask import Flask, jsonify, request, send_from_directory, session
from analyzer import analyze, load_commits
from auth import auth_bp
from token_store import get_token_for_repo

try:
    from rbac import rbac_bp, get_user_allowed_roles
except ImportError:
    rbac_bp = None
    get_user_allowed_roles = lambda uk: None

try:
    from integrations import (
        get_integrations_status,
        run_all_integrations,
        send_slack_handover,
        sync_jira_tasks,
        sync_notion_tasks,
    )
except ImportError:
    get_integrations_status = lambda: {}
    run_all_integrations = lambda hid, data, save_log_fn=None: None
    send_slack_handover = lambda hid, data, dry=False: (False, "Not configured")
    sync_jira_tasks = lambda hid, data, dry=False: (False, "Not configured")
    sync_notion_tasks = lambda hid, data, dry=False: (False, "Not configured")

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "continuai-default-secret-key-for-dev")

base_url = os.environ.get("BASE_URL", "http://localhost:5000")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = base_url.startswith("https")

app.register_blueprint(auth_bp)
if rbac_bp:
    app.register_blueprint(rbac_bp)

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "continuai.db")


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


with db() as _c:
    _c.executescript("""
    create table if not exists h(id integer primary key, source text, departing text, status text, created text, data text);
    create table if not exists log(id integer primary key, hid integer, ts text, actor text, kind text, detail text, snap text);
    """)

now = lambda: time.strftime("%Y-%m-%d %H:%M:%S")


def get(hid):
    with db() as c:
        row = c.execute("select * from h where id=?", (hid,)).fetchone()
    if not row:
        raise LookupError("Handover not found.")
    return dict(row, data=json.loads(row["data"]))


def save(hid, data, actor, kind, detail, status=None):
    with db() as c:
        c.execute("update h set data=?, status=coalesce(?, status) where id=?", (json.dumps(data), status, hid))
        c.execute("insert into log(hid,ts,actor,kind,detail,snap) values(?,?,?,?,?,?)",
                  (hid, now(), actor, kind, detail, json.dumps(data) if kind == "version" else None))


@app.errorhandler(LookupError)
def _nf(e):
    return jsonify(error=str(e)), 404


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.post("/api/analyze")
def api_analyze():
    j = request.get_json(force=True)
    src, dep = (j.get("source") or "").strip(), (j.get("departing") or "").strip()
    if not src:
        return jsonify(error="Enter a repository to analyze."), 400

    user_key = session.get("user_key")
    token, provider = get_token_for_repo(src, user_key=user_key)

    try:
        commits, base = load_commits(src, token=token, provider=provider)
        if not commits:
            raise ValueError("No commits found in that repository.")
        data = analyze(commits, base, dep)
    except FileNotFoundError:
        return jsonify(error="Git is not installed or not on your PATH."), 400
    except (ValueError, subprocess.TimeoutExpired) as e:
        msg = str(e)
        if any(term in msg.lower() for term in ("authentication failed", "repository not found", "could not read from remote", "terminal prompts disabled")):
            prov_label = "GitLab" if "gitlab" in src.lower() else "GitHub"
            return jsonify(error=f"Sign in with {prov_label} to read this repository."), 401
        return jsonify(error=msg if isinstance(e, ValueError) else "Cloning timed out."), 400

    with db() as c:
        hid = c.execute("insert into h(source,departing,status,created,data) values(?,?,?,?,?)",
                        (src, dep, "draft", now(), json.dumps(data))).lastrowid
    save(hid, data, "System", "event", f"Analyzed {data['summary']['commits']} commits from {src}")
    return jsonify(id=hid)


@app.get("/api/h")
def api_list():
    with db() as c:
        rows = c.execute("select id,source,status,created from h order by id desc limit 8").fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/h/<int:hid>")
def api_get(hid):
    h = get(hid)
    return jsonify(source=h["source"], status=h["status"], departing=h["departing"], data=h["data"])


@app.post("/api/h/<int:hid>/item")
def api_item(hid):
    j = request.get_json(force=True)
    h = get(hid)
    if h["status"] == "published":
        return jsonify(error="This handover is published and can no longer be edited."), 409
    item = next((i for i in h["data"]["items"] if i["id"] == j.get("item_id")), None)
    if not item:
        return jsonify(error="Item not found."), 404
    act, who = j.get("action"), (j.get("actor") or "Reviewer")[:60]
    if act in ("approve", "reject", "reset"):
        item["status"] = {"approve": "approved", "reject": "rejected", "reset": "pending"}[act]
        note = {"approve": "Approved", "reject": "Rejected", "reset": "Reset to pending"}[act] + f": {item['title']}"
    elif act == "edit":
        for f in ("title", "why", "alternative"):
            if f in j:
                item[f] = str(j[f])[:600]
        item["edited"] = True
        note = f"Edited: {item['title']}"
    else:
        return jsonify(error="Unknown action."), 400
    save(hid, h["data"], who, "version", note)
    return jsonify(h["data"])


@app.post("/api/h/<int:hid>/publish")
def api_publish(hid):
    h = get(hid)
    n = sum(i["status"] == "approved" for i in h["data"]["items"])
    if not n:
        return jsonify(error="Approve at least one item before publishing."), 400
    who = (request.get_json(force=True).get("actor") or "Reviewer")[:60]
    save(hid, h["data"], who, "event", f"Published handover with {n} approved items", status="published")

    # Integrations run after publish without failing publish
    def log_integration(h_id, actor, kind, detail):
        with db() as c:
            c.execute("insert into log(hid,ts,actor,kind,detail) values(?,?,?,?,?)",
                      (h_id, now(), actor, kind, detail))

    run_all_integrations(hid, h["data"], save_log_fn=log_integration)
    # Save any added keys (jira_key, notion_page_id)
    save(hid, h["data"], "System", "version", "Saved external integration references")
    return jsonify(ok=True)


@app.get("/api/h/<int:hid>/view")
def api_view(hid):
    h = get(hid)
    if h["status"] != "published":
        return jsonify(error="Publish the handover to see role views."), 409
    role = request.args.get("role", "lead")

    # Enforce RBAC for authenticated users (anonymous and sample remain open)
    user_key = session.get("user_key")
    if user_key:
        allowed = get_user_allowed_roles(user_key)
        if allowed is not None and "lead" not in allowed and role not in allowed:
            return jsonify(error=f"Access denied: your team policies do not grant access to the '{role}' view."), 403

    d = h["data"]
    items = [i for i in d["items"] if i["status"] == "approved" and (role == "lead" or role in i["roles"])]
    nodes, edges = d["graph"]["nodes"], d["graph"]["edges"]
    if role != "lead":
        mine = {n["id"] for n in nodes if n["group"] == role}
        edges = [e for e in edges if e["from"] in mine or e["to"] in mine]
        keep = {e["from"] for e in edges} | {e["to"] for e in edges} | mine
        nodes = [n for n in nodes if n["id"] in keep]
    return jsonify(items=items, graph=dict(nodes=nodes, edges=edges))


@app.post("/api/h/<int:hid>/ack")
def api_ack(hid):
    j = request.get_json(force=True)
    name = (j.get("name") or "").strip()[:60]
    if not name:
        return jsonify(error="Enter your name to confirm receipt."), 400
    h = get(hid)
    save(hid, h["data"], name, "event", f"Received the {j.get('role', 'lead')} handover")
    return jsonify(ok=True)


@app.get("/api/h/<int:hid>/log")
def api_log(hid):
    with db() as c:
        rows = c.execute("select ts,actor,kind,detail from log where hid=? order by id desc", (hid,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/integrations")
def api_get_integrations():
    return jsonify(get_integrations_status())


@app.post("/api/h/<int:hid>/integrations/<name>")
def api_retry_integration(hid, name):
    h = get(hid)
    name = name.lower()
    if name == "slack":
        ok, msg = send_slack_handover(hid, h["data"])
    elif name == "jira":
        ok, msg = sync_jira_tasks(hid, h["data"])
    elif name == "notion":
        ok, msg = sync_notion_tasks(hid, h["data"])
    else:
        return jsonify(error="Unknown integration."), 400

    save(hid, h["data"], "Reviewer", "integration", f"{name.capitalize()} retry: {msg}")
    return jsonify(ok=ok, message=msg)


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)), debug=False)
