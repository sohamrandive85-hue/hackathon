"""AST structural analysis module for Python code changes.
Extracts touched functions, classes, changed signatures, and import diffs.
"""
import ast
import re
import subprocess


def parse_symbols(code: str) -> dict:
    """Extract functions, signatures, classes, and imports from Python source code."""
    result = {
        "functions": {},  # name: [arg_names]
        "classes": {},    # name: [method_names]
        "imports": set(), # imported module or symbol names
    }
    if not code or not code.strip():
        return result

    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return result

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            result["functions"][node.name] = args
        elif isinstance(node, ast.ClassDef):
            methods = [
                n.name for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            result["classes"][node.name] = methods
        elif isinstance(node, ast.Import):
            for alias in node.names:
                result["imports"].add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                result["imports"].add(f"{mod}.{alias.name}" if mod else alias.name)

    return result


def diff_python_code(old_code: str, new_code: str) -> dict:
    """Compare two versions of Python code and extract structural changes."""
    old_syms = parse_symbols(old_code)
    new_syms = parse_symbols(new_code)

    old_funcs = old_syms["functions"]
    new_funcs = new_syms["functions"]
    added_funcs = [f"{fn}({', '.join(new_funcs[fn])})" for fn in new_funcs if fn not in old_funcs]
    removed_funcs = [fn for fn in old_funcs if fn not in new_funcs]

    modified_signatures = []
    for fn in new_funcs:
        if fn in old_funcs and new_funcs[fn] != old_funcs[fn]:
            modified_signatures.append(
                f"{fn}({', '.join(old_funcs[fn])}) -> {fn}({', '.join(new_funcs[fn])})"
            )

    old_classes = old_syms["classes"]
    new_classes = new_syms["classes"]
    added_classes = [c for c in new_classes if c not in old_classes]
    removed_classes = [c for c in old_classes if c not in new_classes]

    added_imports = sorted(list(new_syms["imports"] - old_syms["imports"]))
    removed_imports = sorted(list(old_syms["imports"] - new_syms["imports"]))

    return {
        "added_functions": added_funcs,
        "removed_functions": removed_funcs,
        "modified_signatures": modified_signatures,
        "added_classes": added_classes,
        "removed_classes": removed_classes,
        "added_imports": added_imports,
        "removed_imports": removed_imports,
    }


def parse_diff_lines(diff_text: str) -> dict:
    """Heuristic AST extraction from patch diff text when full source is unavailable."""
    added_funcs = []
    modified_signatures = []
    added_classes = []
    added_imports = []
    removed_imports = []

    fn_def = re.compile(r"^\+\s*(?:async\s+)?def\s+([a-zA-Z_]\w*\s*\([^)]*\))")
    cls_def = re.compile(r"^\+\s*class\s+([a-zA-Z_]\w*)")
    imp_add = re.compile(r"^\+\s*(?:import\s+([\w, ]+)|from\s+([\w.]+)\s+import\s+([\w, *]+))")
    imp_rem = re.compile(r"^-\s*(?:import\s+([\w, ]+)|from\s+([\w.]+)\s+import\s+([\w, *]+))")

    for line in diff_text.splitlines():
        mf = fn_def.match(line)
        if mf:
            added_funcs.append(mf.group(1).strip())
        mc = cls_def.match(line)
        if mc:
            added_classes.append(mc.group(1).strip())
        ma = imp_add.match(line)
        if ma:
            added_imports.append((ma.group(1) or f"{ma.group(2)}.{ma.group(3)}").strip())
        mr = imp_rem.match(line)
        if mr:
            removed_imports.append((mr.group(1) or f"{mr.group(2)}.{mr.group(3)}").strip())

    return {
        "added_functions": added_funcs[:8],
        "removed_functions": [],
        "modified_signatures": modified_signatures[:5],
        "added_classes": added_classes[:5],
        "removed_classes": [],
        "added_imports": added_imports[:8],
        "removed_imports": removed_imports[:8],
    }


def analyze_commit_ast(repo_path: str, sha: str, touched_files: list[str]) -> dict:
    """Analyze AST changes for a commit across touched Python files."""
    py_files = [f for f in touched_files if f.endswith(".py")]
    if not py_files or not repo_path or repo_path == "demo":
        return {}

    summary = {
        "python_files": py_files,
        "added_functions": [],
        "modified_signatures": [],
        "added_classes": [],
        "added_imports": [],
        "removed_imports": [],
    }

    try:
        # Try git diff for the commit
        r = subprocess.run(
            ["git", "-C", repo_path, "diff", "-U0", f"{sha}~1..{sha}", "--"] + py_files,
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0 and r.stdout:
            parsed = parse_diff_lines(r.stdout)
            for k in ("added_functions", "modified_signatures", "added_classes", "added_imports", "removed_imports"):
                summary[k].extend(parsed.get(k, []))
    except Exception:
        pass

    # Clean duplicates while preserving order
    for k in ("added_functions", "modified_signatures", "added_classes", "added_imports", "removed_imports"):
        summary[k] = list(dict.fromkeys(summary[k]))

    return summary


def format_ast_summary(ast_info: dict) -> str:
    """Produce a concise string summary of AST changes for prompt injection."""
    if not ast_info:
        return ""
    parts = []
    if ast_info.get("added_classes"):
        parts.append("Classes added: " + ", ".join(ast_info["added_classes"]))
    if ast_info.get("added_functions"):
        parts.append("Functions added: " + ", ".join(ast_info["added_functions"]))
    if ast_info.get("modified_signatures"):
        parts.append("Signatures changed: " + ", ".join(ast_info["modified_signatures"]))
    if ast_info.get("added_imports"):
        parts.append("Imports added: " + ", ".join(ast_info["added_imports"]))
    if ast_info.get("removed_imports"):
        parts.append("Imports removed: " + ", ".join(ast_info["removed_imports"]))
    return "; ".join(parts)
