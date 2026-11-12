"""Tests for analyzer module, confidence scoring, and handover synthesis."""
from analyzer import (
    score_decision,
    demo_commits,
    analyze,
    file_role,
    module_of,
)


def test_file_role_and_module():
    assert file_role("api/routes.py") == "backend"
    assert file_role("web/app.js") == "frontend"
    assert file_role("ml/train.py") == "ml"
    assert file_role("docs/index.md") == "docs"
    assert file_role("docker-compose.yml") == "infra"
    assert module_of("api/routes.py") == "api"
    assert module_of("README.md") == "(root)"


def test_confidence_scoring_gradation():
    # 1. High confidence explicit decision with AST
    score_conf, label_conf = score_decision(
        is_explicit=True,
        why="Because performance was degraded",
        alternative="Postgres",
        ast_data={"added_classes": ["PriceStore"]}
    )
    assert score_conf >= 0.75
    assert label_conf == "confirmed"

    # 2. Medium confidence (Likely)
    score_likely, label_likely = score_decision(
        is_explicit=False,
        why="Better latency",
        ast_data={"added_imports": ["sqlite3"]}
    )
    assert 0.45 <= score_likely < 0.75
    assert label_likely == "likely"

    # 3. Low confidence (Inferred)
    score_inf, label_inf = score_decision(
        is_explicit=False,
        why="",
        alternative="",
        ast_data={}
    )
    assert score_inf < 0.45
    assert label_inf == "inferred"

    # 4. Explicit direct score override
    score_direct, label_direct = score_decision(raw_score=0.91)
    assert score_direct == 0.91
    assert label_direct == "confirmed"


def test_analyze_demo_dataset():
    commits = demo_commits()
    assert len(commits) == 15

    data = analyze(commits, base="https://github.com/example/demo", departing="Aarav Mehta")

    # Check summary
    assert data["summary"]["commits"] == 15
    assert data["summary"]["authors"] >= 3
    assert data["summary"]["modules"] >= 3
    assert "engine" in data["summary"]

    # Check graph
    assert len(data["graph"]["nodes"]) > 0
    assert len(data["graph"]["edges"]) > 0

    # Check items
    items = data["items"]
    assert len(items) > 0

    # Verify each item adheres to backward compatibility and includes graded confidence
    for item in items:
        assert "id" in item
        assert "type" in item
        assert "title" in item
        assert item["confidence"] in ("confirmed", "likely", "inferred")
        assert "confidence_score" in item
        assert 0.0 <= item["confidence_score"] <= 1.0
        assert "trade_offs" in item
        assert "ast_summary" in item
        assert "roles" in item

    # Check departing filter: departing tasks are prioritized
    high_tasks = [i for i in items if i.get("priority") == "high"]
    assert len(high_tasks) > 0
