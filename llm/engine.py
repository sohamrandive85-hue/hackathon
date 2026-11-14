"""LLM Engine: Orchestrates provider selection, AST diff enrichment, retries and fallbacks."""
import logging
import os
import re
from .providers import (
    BaseLLMProvider,
    GeminiProvider,
    AnthropicProvider,
    OpenAIProvider,
    RuleBasedProvider,
)
from ast_analyzer import analyze_commit_ast, format_ast_summary

logger = logging.getLogger(__name__)


def get_provider(provider_name: str = None) -> BaseLLMProvider:
    """Select the configured LLM provider or auto-detect from environment keys."""
    choice = (provider_name or os.environ.get("LLM_PROVIDER", "")).strip().lower()

    if choice == "gemini":
        return GeminiProvider()
    elif choice == "anthropic":
        return AnthropicProvider()
    elif choice == "openai":
        return OpenAIProvider()
    elif choice == "rule-based":
        return RuleBasedProvider()

    # Auto-detection priority if LLM_PROVIDER is not specified
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return GeminiProvider()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicProvider()
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAIProvider()

    return RuleBasedProvider()


def enrich_commits_with_ast(commits: list[dict], repo_path: str = "") -> list[dict]:
    """Augment commit objects with AST structural changes and summary strings."""
    enriched = []
    for c in commits:
        c_copy = dict(c)
        ast_info = analyze_commit_ast(repo_path, c.get("sha", ""), c.get("files", []))
        c_copy["ast"] = ast_info
        c_copy["ast_summary"] = format_ast_summary(ast_info)
        enriched.append(c_copy)
    return enriched


def extract_decisions_hybrid(commits: list[dict], repo_path: str = "") -> tuple[list[dict], str]:
    """Execute decision extraction with AST context, falling back to rule-based on failure."""
    enriched = enrich_commits_with_ast(commits, repo_path)
    provider = get_provider()

    engine_name = provider.name.capitalize()
    if provider.is_configured() and provider.name != "rule-based":
        try:
            results = provider.extract_decisions(enriched)
            if results:
                return results, engine_name
        except Exception as e:
            clean_err = re.sub(r"https?://[^\s]+", "[REDACTED_URL]", str(e))
            clean_err = re.sub(r"key=[^\s&]+", "key=[REDACTED]", clean_err)
            logger.warning(f"Provider {engine_name} failed: {clean_err}. Falling back to Rule-based.")
            engine_name = f"Rule-based (fallback from {engine_name})"

    # Fallback to rule-based
    fallback = RuleBasedProvider()
    return fallback.extract_decisions(enriched), engine_name
