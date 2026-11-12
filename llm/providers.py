"""Pluggable LLM Providers: Gemini, Anthropic, OpenAI, and Rule-Based Fallback.
Uses direct HTTP via `requests` to eliminate heavy SDK dependencies.
"""
import json
import logging
import os
import re
import time
import requests

logger = logging.getLogger(__name__)

DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",
    "anthropic": "claude-3-5-sonnet-20241022",
    "openai": "gpt-4o-mini",
}


def clean_json_response(text: str) -> list[dict]:
    """Extract and parse JSON array or list payload from LLM responses."""
    if not text:
        return []
    text = text.strip()
    # Strip markdown code fences if present
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fence:
        text = fence.group(1).strip()
    # Direct json array match
    arr_match = re.search(r"\[\s*\{[\s\S]*\}\s*\]", text)
    if arr_match:
        try:
            return json.loads(arr_match.group(0))
        except Exception:
            pass
    # Object with "items" or "decisions"
    obj_match = re.search(r"\{[\s\S]*\}", text)
    if obj_match:
        try:
            parsed = json.loads(obj_match.group(0))
            if isinstance(parsed, dict):
                for k in ("decisions", "items", "data"):
                    if isinstance(parsed.get(k), list):
                        return parsed[k]
        except Exception:
            pass
    return []


class BaseLLMProvider:
    """Base interface for all LLM providers."""

    name: str = "base"

    def is_configured(self) -> bool:
        raise NotImplementedError

    def extract_decisions(self, commits_with_ast: list[dict]) -> list[dict]:
        raise NotImplementedError

    def _build_prompt(self, commits_with_ast: list[dict]) -> str:
        lines = []
        for c in commits_with_ast[:60]:
            ast_ctx = c.get("ast_summary", "")
            ast_part = f" | AST: {ast_ctx}" if ast_ctx else ""
            files_part = f" | Files: {', '.join(c.get('files', [])[:5])}"
            line = (
                f"{c['sha'][:8]} | Author: {c.get('author', '')} | Subject: {c.get('subject', '')} "
                f"| Body: {c.get('body', '')[:250]}{files_part}{ast_part}"
            )
            lines.append(line)

        prompt = (
            "You are an expert software architect analyzing Git commit history and code diffs.\n"
            "Extract key architectural and engineering decisions, technical trade-offs, and design rationales.\n"
            "Use the commit message, files changed, and AST structural changes (functions/classes added, signature changes, imports).\n"
            "Return ONLY a valid JSON array of objects with the following schema:\n"
            "[\n"
            "  {\n"
            '    "sha": "8-char sha from commit",\n'
            '    "decision": "Brief summary of the architectural or engineering choice",\n'
            '    "why": "The stated or inferred technical reason/rationale",\n'
            '    "alternative": "Rejected or replaced alternative (e.g. Postgres instead of SQLite), or empty string",\n'
            '    "trade_offs": "Any noted technical tradeoffs (performance, simplicity, ops overhead), or empty string",\n'
            '    "explicit": true or false (true ONLY if the commit text itself states the reason or alternative),\n'
            '    "confidence_score": 0.0 to 1.0 (float reflecting certainty based on textual and AST evidence)\n'
            "  }\n"
            "]\n"
            "Rules:\n"
            "- Only include commits that represent significant architectural, library, data, or structural decisions.\n"
            "- Never invent false facts. Set explicit=false if inferred from AST or context.\n"
            "- If confidence is low or uncertain, assign confidence_score < 0.5.\n\n"
            "Commits to analyze:\n" + "\n".join(lines)
        )
        return prompt


class GeminiProvider(BaseLLMProvider):
    name = "gemini"

    def __init__(self):
        self.api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
        self.model = os.environ.get("GEMINI_MODEL", DEFAULT_MODELS["gemini"])

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def extract_decisions(self, commits_with_ast: list[dict]) -> list[dict]:
        if not self.is_configured():
            return []
        prompt = self._build_prompt(commits_with_ast)
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            },
        }

        # Timeout & retry loop
        for attempt in range(3):
            try:
                r = requests.post(url, json=payload, timeout=45)
                if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                r.raise_for_status()
                data = r.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return clean_json_response(text)
            except Exception as e:
                logger.warning(f"Gemini API attempt {attempt + 1} failed: {e}")
                if attempt == 2:
                    raise
        return []


class AnthropicProvider(BaseLLMProvider):
    name = "anthropic"

    def __init__(self):
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODELS["anthropic"])

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def extract_decisions(self, commits_with_ast: list[dict]) -> list[dict]:
        if not self.is_configured():
            return []
        prompt = self._build_prompt(commits_with_ast)
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": self.model,
            "max_tokens": 3500,
            "messages": [{"role": "user", "content": prompt}],
        }

        for attempt in range(3):
            try:
                r = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    json=payload,
                    timeout=45,
                )
                if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                r.raise_for_status()
                text = r.json()["content"][0]["text"]
                return clean_json_response(text)
            except Exception as e:
                logger.warning(f"Anthropic API attempt {attempt + 1} failed: {e}")
                if attempt == 2:
                    raise
        return []


class OpenAIProvider(BaseLLMProvider):
    name = "openai"

    def __init__(self):
        self.api_key = os.environ.get("OPENAI_API_KEY", "")
        self.model = os.environ.get("OPENAI_MODEL", DEFAULT_MODELS["openai"])

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def extract_decisions(self, commits_with_ast: list[dict]) -> list[dict]:
        if not self.is_configured():
            return []
        prompt = self._build_prompt(commits_with_ast)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": "You are a software architect that extracts decisions into JSON: {'decisions': [...]}.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        }

        for attempt in range(3):
            try:
                r = requests.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=45,
                )
                if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
                return clean_json_response(content)
            except Exception as e:
                logger.warning(f"OpenAI API attempt {attempt + 1} failed: {e}")
                if attempt == 2:
                    raise
        return []


class RuleBasedProvider(BaseLLMProvider):
    """Offline heuristic extractor blending regex pattern matching with AST signals."""

    name = "rule-based"

    STRONG = re.compile(
        r"\b(switch\w*|replac\w*|migrat\w*|adopt\w*|refactor\w*|drop(?:ped)?|chose|decid\w*|introduc\w*|moved? to)\b",
        re.I,
    )
    WHY = re.compile(r"\b(?:because|due to|so that|in order to|since)\s+([^.;\n]+)", re.I)
    ALT = re.compile(
        r"\b(?:instead of|rather than)\s+((?:(?!\b(?:because|since|so that|due to)\b)[^.,;\n])+)",
        re.I,
    )

    def is_configured(self) -> bool:
        return True

    def extract_decisions(self, commits_with_ast: list[dict]) -> list[dict]:
        results = []
        for c in commits_with_ast:
            text = f"{c.get('subject', '')}\n{c.get('body', '')}"
            s = self.STRONG.search(text)
            w = self.WHY.search(text)
            a = self.ALT.search(text)

            ast_data = c.get("ast", {})
            has_ast_sig = bool(ast_data.get("modified_signatures") or ast_data.get("added_classes"))
            has_ast_imports = bool(ast_data.get("added_imports") or ast_data.get("removed_imports"))

            if s or (w and a) or (s and (has_ast_sig or has_ast_imports)):
                why_str = w.group(1).strip() if w else ""
                alt_str = a.group(1).strip() if a else ""

                # Base confidence score calculation
                score = 0.40  # baseline heuristic
                if w:
                    score += 0.25
                if a:
                    score += 0.20
                if has_ast_sig or has_ast_imports:
                    score += 0.15

                # Cap score at 0.95 for heuristic
                score = min(0.95, round(score, 2))
                is_explicit = bool(w or a)

                results.append({
                    "sha": c["sha"][:8],
                    "decision": c.get("subject", ""),
                    "why": why_str,
                    "alternative": alt_str,
                    "trade_offs": "",
                    "explicit": is_explicit,
                    "confidence_score": score,
                })

        return results
