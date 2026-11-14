"""Repository analysis: commits -> contribution map, dependency graph, decisions, open work."""
import collections, hashlib, itertools, json, os, re, shutil, subprocess, tempfile, base64
import networkx as nx
from ast_analyzer import analyze_commit_ast, format_ast_summary
from llm.engine import extract_decisions_hybrid

# ---------------------------------------------------------------- ingestion
def _git(args, env=None):
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    r = subprocess.run(["git"] + args, capture_output=True, text=True, timeout=180, env=merged_env)
    if r.returncode:
        err = r.stderr.strip()[-300:] or "git command failed"
        # Scrub any potential tokens or credentials from error text
        err = re.sub(r"Authorization:[^\s]+", "Authorization: [REDACTED]", err, flags=re.I)
        err = re.sub(r"Basic\s+[a-zA-Z0-9+/=]+", "Basic [REDACTED]", err, flags=re.I)
        err = re.sub(r"([a-zA-Z0-9_]{24,})", "[REDACTED]", err)
        raise ValueError(err)
    return r.stdout


def load_commits(source, token=None, provider=None):
    """Return (commits, base_url). Source: 'demo', GitHub URL, owner/repo, or a local path.
    Authenticates remote clones using git extraHeader subprocess ENV."""
    src = source.strip()
    if src.lower() == "demo":
        return demo_commits(), ""
    if not os.path.isdir(src) and re.match(r"^[\w.-]+/[\w.-]+$", src):
        src = "https://github.com/" + src

    git_env = {}
    if token and re.match(r"^(https?://)", src):
        prov = (provider or ("gitlab" if "gitlab" in src.lower() else "github")).lower()
        auth_str = f"oauth2:{token}" if prov == "gitlab" else f"x-access-token:{token}"
        b64_auth = base64.b64encode(auth_str.encode()).decode()
        git_env = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {b64_auth}",
        }

    tmp = None
    try:
        if re.match(r"^(https?://|git@)", src):
            tmp = tempfile.mkdtemp()
            _git(["clone", "--bare", "--depth", "50", src, tmp], env=git_env)
            path = tmp
        elif os.path.isdir(src):
            path = src
        else:
            raise ValueError("Enter a GitHub URL, owner/repo, a local repository path, or 'demo'.")
        out = _git(["-C", path, "log", "-n", "50", "--name-only",
                    "--pretty=format:%x1d%H%x1f%an%x1f%aI%x1f%s%x1f%b%x1e"], env=git_env)
        
        commits = []
        for chunk in out.split("\x1d")[1:]:
            head, _, files = chunk.partition("\x1e")
            sha, au, dt, subj, body = head.split("\x1f", 4)
            file_list = [f for f in files.split("\n") if f.strip()]
            ast_info = analyze_commit_ast(path, sha, file_list)
            commits.append(dict(
                sha=sha, author=au, date=dt[:10], subject=subj, body=body.strip(),
                files=file_list, ast=ast_info, ast_summary=format_ast_summary(ast_info)
            ))
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    base = re.sub(r"(\.git)?/?$", "", src) if src.startswith("http") else ""
    return commits, base


# ---------------------------------------------------------------- classification
ROLE_TOKENS = [
    ("infra", {"requirements", "dockerfile", "docker", "yml", "yaml", "ci", "github", "toml", "cfg"}),
    ("docs", {"md", "docs", "readme", "rst"}),
    ("ml", {"ml", "model", "models", "train", "training", "notebooks", "ipynb", "pkl", "csv", "dataset", "pipeline"}),
    ("frontend", {"js", "jsx", "ts", "tsx", "css", "html", "frontend", "static", "components", "web", "ui"}),
    ("backend", {"api", "server", "routes", "db", "backend", "py", "sql", "app"}),
]


def file_role(path):
    tokens = set(re.split(r"[^a-z0-9]+", path.lower()))
    for role, keys in ROLE_TOKENS:
        if tokens & keys:
            return role
    return "general"


def module_of(path):
    parts = path.split("/")
    return "(root)" if len(parts) == 1 else parts[0]


STRONG = re.compile(r"\b(switch\w*|replac\w*|migrat\w*|adopt\w*|refactor\w*|drop(?:ped)?|chose|decid\w*|introduc\w*|moved? to)\b", re.I)
WHY = re.compile(r"\b(?:because|due to|so that|in order to|since)\s+([^.;\n]+)", re.I)
ALT = re.compile(r"\b(?:instead of|rather than)\s+((?:(?!\b(?:because|since|so that|due to)\b)[^.,;\n])+)", re.I)
OPEN = re.compile(r"\b(todo|fixme|wip|not yet|for now|temporary|hack|incomplete|unfinished)\b", re.I)


# ---------------------------------------------------------------- confidence scoring
def score_decision(raw_score=None, is_explicit=False, why="", alternative="", ast_data=None):
    """
    Computes a graded confidence score (0.0 to 1.0) and derives the UI label:
    - >= 0.75: 'confirmed'
    - >= 0.45: 'likely'
    - < 0.45:  'inferred'
    """
    if raw_score is not None and isinstance(raw_score, (int, float)):
        score = float(raw_score)
    else:
        score = 0.35
        if is_explicit:
            score += 0.30
        if why and len(why.strip()) > 6:
            score += 0.15
        if alternative and len(alternative.strip()) > 3:
            score += 0.15
        if ast_data and (ast_data.get("added_classes") or ast_data.get("modified_signatures") or ast_data.get("added_imports")):
            score += 0.10

    score = max(0.0, min(1.0, round(score, 2)))
    if score >= 0.75:
        label = "confirmed"
    elif score >= 0.45:
        label = "likely"
    else:
        label = "inferred"
    return score, label


# Backward compatible wrapper
def llm_decisions(commits):
    """Extract decisions using the active LLM provider or rule-based fallback."""
    rows, _ = extract_decisions_hybrid(commits)
    return rows


# ---------------------------------------------------------------- analysis
def analyze(commits, base="", departing=""):
    link = (lambda s: f"{base}/commit/{s}") if base.startswith("http") else (lambda s: "")
    authors = collections.Counter(c["author"] for c in commits)
    mod_auth = collections.defaultdict(collections.Counter)
    mod_roles = collections.defaultdict(collections.Counter)
    co = collections.Counter()
    for c in commits:
        c["modules"] = sorted({module_of(f) for f in c["files"]})
        roles = collections.Counter(file_role(f) for f in c["files"])
        c["role"] = roles.most_common(1)[0][0] if roles else "general"
        for f in c["files"]:
            mod_roles[module_of(f)][file_role(f)] += 1
        for m in c["modules"]:
            mod_auth[m][c["author"]] += 1
        for a, b in itertools.combinations(c["modules"], 2):
            co[(a, b)] += 1
    mrole = {m: r.most_common(1)[0][0] for m, r in mod_roles.items()}

    # dependency graph (module co-change) and centrality
    G = nx.Graph()
    for (a, b), w in co.items():
        G.add_edge(a, b, weight=w)
    cen = nx.degree_centrality(G) if G.number_of_nodes() > 1 else {}
    nodes = [dict(id="p:" + p, label=p, group="person", value=n) for p, n in authors.items()]
    nodes += [dict(id="m:" + m, label=m, group=mrole.get(m, "general"), value=sum(cn.values()),
                   hub=round(cen.get(m, 0), 2)) for m, cn in mod_auth.items()]
    edges = [{"from": "p:" + p, "to": "m:" + m, "value": n, "kind": "owns"}
             for m, cn in mod_auth.items() for p, n in cn.items()]
    edges += [{"from": "m:" + a, "to": "m:" + b, "value": w, "kind": "co-change"} for (a, b), w in co.items()]

    items = []

    def add(**k):
        k.setdefault("status", "pending")
        k.setdefault("roles", [k.get("role", "general")])
        k.setdefault("author", "")
        k.setdefault("trade_offs", "")
        k.setdefault("ast_summary", "")
        k["id"] = f"i{len(items) + 1}"
        items.append(k)

    def base_fields(c):
        return dict(
            sha=c["sha"], url=link(c["sha"]), author=c["author"], date=c["date"],
            role=c["role"], modules=c["modules"], ast_summary=c.get("ast_summary", "")
        )

    by = {c["sha"][:8]: c for c in commits}
    
    # Hybrid extraction: pluggable LLM with AST diff context & fallback
    rows, engine = extract_decisions_hybrid(commits)

    # 1. Conflicts (reverts) and Tasks (TODO/FIXME)
    for c in commits:
        text = c["subject"] + "\n" + c["body"]
        if c["subject"].lower().startswith("revert"):
            score, label = score_decision(0.95, is_explicit=True)
            add(type="conflict", title=c["subject"], confidence=label, confidence_score=score,
                alternative="", why="This reverts earlier work. Check whether the original decision still stands.",
                **base_fields(c))
        if OPEN.search(text) and len([i for i in items if i["type"] == "task"]) < 25:
            high = bool(departing) and departing.lower() in c["author"].lower()
            score, label = score_decision(0.85, is_explicit=True)
            add(type="task", title=c["subject"], why="Marked as unfinished in the commit message.",
                alternative="", priority="high" if high else "normal", confidence=label,
                confidence_score=score, **base_fields(c))

    # 2. Extracted Decisions (from LLM or rule-based)
    for r in rows or []:
        sha_prefix = str(r.get("sha", ""))[:8]
        c = by.get(sha_prefix)
        if c:
            score, label = score_decision(
                raw_score=r.get("confidence_score"),
                is_explicit=bool(r.get("explicit")),
                why=r.get("why", ""),
                alternative=r.get("alternative", ""),
                ast_data=c.get("ast")
            )
            add(
                type="decision",
                title=r.get("decision") or c["subject"],
                why=r.get("why", ""),
                alternative=r.get("alternative", ""),
                trade_offs=r.get("trade_offs", ""),
                confidence=label,
                confidence_score=score,
                **base_fields(c)
            )

    # 3. Ownership risks and module co-change dependencies
    for m, cn in mod_auth.items():
        top, n = cn.most_common(1)[0]
        total = sum(cn.values())
        if total >= 3 and n / total >= 0.8:
            score, label = score_decision(0.90, is_explicit=True)
            add(type="risk", title=f"{m} depends on one person", author=top, role=mrole.get(m, "general"),
                why=f"{top} made {n} of {total} commits that touched {m}. Knowledge is concentrated in one person.",
                alternative="", modules=[m], confidence=label, confidence_score=score)
    for (a, b), w in co.most_common(10):
        if mrole.get(a) != mrole.get(b) and w >= 2:
            score, label = score_decision(0.55 if w >= 3 else 0.48)
            add(type="dependency", title=f"{a} and {b} change together", role=mrole.get(a, "general"),
                roles=[mrole.get(a, "general"), mrole.get(b, "general")], modules=[a, b], alternative="",
                why=f"{w} commits touched both ({mrole.get(a)} and {mrole.get(b)}). Treat the boundary as an interface.",
                confidence=label, confidence_score=score)

    if departing:
        d = departing.lower()
        mine = [i for i in items if not i["author"] or d in i["author"].lower()]
        if any(d in i["author"].lower() for i in items if i["author"]):
            items = mine
    items.sort(key=lambda i: (i.get("priority") != "high",))

    dates = sorted(c["date"] for c in commits)
    return dict(
        summary=dict(commits=len(commits), authors=len(authors), modules=len(mod_auth),
                     first=dates[0], last=dates[-1], engine=engine),
        contributors=[dict(name=p, commits=n, modules=[m for m, _ in sorted(
            ((m, cn[p]) for m, cn in mod_auth.items() if p in cn), key=lambda x: -x[1])[:3]])
            for p, n in authors.most_common()],
        graph=dict(nodes=nodes, edges=edges),
        items=items,
        timeline=[dict(sha=c["sha"][:8], date=c["date"], author=c["author"], title=c["subject"]) for c in commits[:15]],
    )


# ---------------------------------------------------------------- sample project
def demo_commits():
    rows = [
        ("Meera Kulkarni", "2026-11-02", "Update README with run instructions", "", ["README.md", "docs/setup.md"], {}),
        ("Rohan Iyer", "2026-11-01", "WIP: price chart tooltips", "TODO: mobile layout not yet handled.", ["web/chart.js", "web/style.css"], {}),
        ("Priya Nair", "2026-10-30", "Revert 'Switch API to GraphQL'", "GraphQL gateway doubled response time on the small server.", ["api/app.py", "api/schema.py"],
         {"removed_imports": ["strawberry"]}),
        ("Aarav Mehta", "2026-10-28", "Switch forecasting model from ARIMA to gradient boosting",
         "Using gradient boosting instead of ARIMA because it handles the festival-season spikes that ARIMA kept smoothing out.",
         ["ml/train.py", "ml/features.py", "api/forecast.py"],
         {"added_classes": ["GradientBoostingForecaster"], "added_imports": ["sklearn.ensemble.HistGradientBoostingRegressor"], "removed_imports": ["statsmodels.tsa.arima.model.ARIMA"]}),
        ("Priya Nair", "2026-10-26", "Switch API to GraphQL", "", ["api/app.py", "api/schema.py", "web/api.js"],
         {"added_classes": ["GraphQLSchema"], "added_imports": ["strawberry"]}),
        ("Rohan Iyer", "2026-10-24", "Add district filter to dashboard", "", ["web/index.html", "web/filters.js", "api/routes.py"], {}),
        ("Aarav Mehta", "2026-10-21", "Cache features in parquet",
         "Parquet cache so that retraining takes minutes rather than hours. TODO: invalidate cache when the mandi data source changes.",
         ["ml/features.py", "ml/cache.py", "api/forecast.py"],
         {"added_functions": ["cache_features(df)", "load_cached_features()"], "added_imports": ["pyarrow.parquet"]}),
        ("Priya Nair", "2026-10-18", "Adopt SQLite for price store",
         "SQLite instead of Postgres because the app runs on one college server and the ops overhead is not worth it.",
         ["api/db.py", "api/app.py", "requirements.txt"],
         {"added_classes": ["PriceStore"], "added_imports": ["sqlite3"], "removed_imports": ["psycopg2"]}),
        ("Aarav Mehta", "2026-10-15", "Add mandi price scraper", "", ["ml/scraper.py", "ml/features.py"],
         {"added_functions": ["scrape_mandi_prices(district)"]}),
        ("Rohan Iyer", "2026-10-12", "Migrate frontend to vanilla JS modules", "", ["web/api.js", "web/chart.js", "web/index.html"], {}),
        ("Priya Nair", "2026-10-10", "Add forecast endpoint", "", ["api/forecast.py", "api/app.py", "ml/predict.py"],
         {"added_functions": ["forecast_endpoint(crop, district)"]}),
        ("Aarav Mehta", "2026-10-08", "Add baseline ARIMA forecaster", "", ["ml/train.py", "ml/predict.py"],
         {"added_classes": ["ARIMAForecaster"]}),
        ("Meera Kulkarni", "2026-10-05", "Set up project structure and CI", "", [".github/workflows/ci.yml", "requirements.txt", "README.md"], {}),
        ("Priya Nair", "2026-10-03", "Add price store schema", "", ["api/db.py", "api/app.py"],
         {"added_functions": ["init_db()"]}),
        ("Aarav Mehta", "2026-10-02", "Add training notebook", "", ["ml/notebooks/explore.ipynb", "ml/train.py"], {}),
    ]
    commits = []
    for a, d, s, b, f, ast_meta in rows:
        commits.append(dict(
            sha=hashlib.sha1((s + d).encode()).hexdigest(),
            author=a, date=d, subject=s, body=b, files=f,
            ast=ast_meta, ast_summary=format_ast_summary(ast_meta)
        ))
    return commits
