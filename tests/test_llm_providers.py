"""Tests for pluggable LLM providers with mocked HTTP calls."""
import json
import logging
from unittest.mock import patch, MagicMock
from llm.providers import (
    clean_json_response,
    GeminiProvider,
    AnthropicProvider,
    OpenAIProvider,
    RuleBasedProvider,
)
from llm.engine import get_provider, extract_decisions_hybrid


def test_clean_json_response():
    # 1. Plain array
    raw = '[{"sha": "abc12345", "decision": "Adopt SQLite"}]'
    assert clean_json_response(raw)[0]["decision"] == "Adopt SQLite"

    # 2. Markdown code fenced array
    fenced = '```json\n[{"sha": "def67890", "decision": "Switch to Parquet"}]\n```'
    assert clean_json_response(fenced)[0]["decision"] == "Switch to Parquet"

    # 3. Object with decisions key
    obj = '{"decisions": [{"sha": "11223344", "decision": "Add GraphQL"}]}'
    assert clean_json_response(obj)[0]["decision"] == "Add GraphQL"

    # 4. Malformed/empty
    assert clean_json_response("") == []
    assert clean_json_response("Random text without json") == []


def test_get_provider_selection(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    # Defaults to Rule-based when no keys are set
    p = get_provider()
    assert isinstance(p, RuleBasedProvider)

    # Explicit provider via param or env
    assert isinstance(get_provider("gemini"), GeminiProvider)
    assert isinstance(get_provider("anthropic"), AnthropicProvider)
    assert isinstance(get_provider("openai"), OpenAIProvider)
    assert isinstance(get_provider("rule-based"), RuleBasedProvider)

    # Auto-detect via API key presence
    monkeypatch.setenv("GEMINI_API_KEY", "fake_gemini_key")
    assert isinstance(get_provider(), GeminiProvider)


@patch("requests.post")
def test_gemini_provider_mocked(mock_post, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake_key")
    p = GeminiProvider()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "candidates": [{
            "content": {
                "parts": [{
                    "text": json.dumps([{
                        "sha": "12345678",
                        "decision": "Adopt SQLite for price store",
                        "why": "Single server ops simplicity",
                        "alternative": "Postgres",
                        "trade_offs": "No multi-writer concurrency",
                        "explicit": True,
                        "confidence_score": 0.92,
                    }])
                }]
            }
        }]
    }
    mock_post.return_value = mock_resp

    commits = [{"sha": "12345678abcdef", "subject": "Adopt SQLite", "body": "", "files": ["api/db.py"]}]
    results = p.extract_decisions(commits)
    assert len(results) == 1
    assert results[0]["decision"] == "Adopt SQLite for price store"
    assert results[0]["confidence_score"] == 0.92

    # Verify key was sent via header, NOT query parameter in URL
    call_url = mock_post.call_args[0][0]
    assert "key=" not in call_url
    assert mock_post.call_args[1]["headers"]["x-goog-api-key"] == "fake_key"


@patch("requests.post")
def test_anthropic_provider_mocked(mock_post, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake_key")
    p = AnthropicProvider()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "content": [{
            "text": json.dumps([{
                "sha": "abcdef12",
                "decision": "Switch forecasting model",
                "why": "Handles festival spikes",
                "alternative": "ARIMA",
                "explicit": True,
                "confidence_score": 0.88,
            }])
        }]
    }
    mock_post.return_value = mock_resp

    commits = [{"sha": "abcdef12345678", "subject": "Switch model", "body": "", "files": ["ml/train.py"]}]
    results = p.extract_decisions(commits)
    assert len(results) == 1
    assert results[0]["decision"] == "Switch forecasting model"


@patch("requests.post")
def test_openai_provider_mocked(mock_post, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake_key")
    p = OpenAIProvider()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{
            "message": {
                "content": json.dumps([{
                    "sha": "99887766",
                    "decision": "Cache features in parquet",
                    "why": "Retraining in minutes",
                    "alternative": "",
                    "explicit": True,
                    "confidence_score": 0.85,
                }])
            }
        }]
    }
    mock_post.return_value = mock_resp

    commits = [{"sha": "998877665544", "subject": "Parquet cache", "body": "", "files": ["ml/cache.py"]}]
    results = p.extract_decisions(commits)
    assert len(results) == 1
    assert results[0]["decision"] == "Cache features in parquet"


@patch("requests.post")
def test_hybrid_fallback_on_api_error(mock_post, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake_key")
    mock_post.side_effect = Exception("API rate limit or connection timeout")

    # Should gracefully fall back to rule-based without raising an exception
    commits = [
        {
            "sha": "aabbccdd1122",
            "subject": "Adopt SQLite for price store",
            "body": "SQLite instead of Postgres because ops overhead is too high.",
            "files": ["api/db.py"],
            "ast": {"added_classes": ["PriceStore"]},
        }
    ]
    results, engine = extract_decisions_hybrid(commits)
    assert "fallback" in engine.lower() or "rule-based" in engine.lower()
    assert len(results) >= 1
    assert results[0]["sha"] == "aabbccdd"


def test_failing_provider_logs_contain_no_keys(caplog, monkeypatch):
    secret_key_val = "SECRET_GEMINI_KEY_ABC123XYZ"
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", secret_key_val)

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.reason = "Not Found"
        mock_post.return_value = mock_resp

        with caplog.at_level(logging.WARNING):
            commits = [{"sha": "12345678", "subject": "Test Commit", "body": "", "files": []}]
            results, engine = extract_decisions_hybrid(commits)

        log_text = caplog.text
        assert secret_key_val not in log_text
        assert "key=" not in log_text
        assert "x-goog-api-key" not in log_text
        # Ensure requests.post was called with header, NOT with query param
        call_args, call_kwargs = mock_post.call_args
        assert "key=" not in call_args[0]
        assert call_kwargs["headers"].get("x-goog-api-key") == secret_key_val
