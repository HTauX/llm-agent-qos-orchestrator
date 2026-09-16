from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

try:
    from langchain_google_genai import ChatGoogleGenerativeAI  # type: ignore
except Exception:  # pragma: no cover
    ChatGoogleGenerativeAI = None  # type: ignore

try:
    from langchain_openai import ChatOpenAI  # type: ignore
except Exception:  # pragma: no cover
    ChatOpenAI = None  # type: ignore


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    """
    OpenAI-compatible LLM configuration (can also be used for DeepSeek, etc.).
    """

    api_key: str
    base_url: Optional[str]
    model: str


def load_openai_compatible_config() -> Optional[OpenAICompatibleConfig]:
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return None

    base_url = (os.getenv("OPENAI_BASE_URL") or "").strip() or None
    model = (os.getenv("OPENAI_MODEL") or "").strip() or "gpt-4o-mini"
    return OpenAICompatibleConfig(api_key=api_key, base_url=base_url, model=model)


@dataclass(frozen=True)
class GeminiConfig:
    """
    Gemini LLM configuration.
    """

    api_key: str
    model: str


def load_gemini_config() -> Optional[GeminiConfig]:
    # Prefer explicit GEMINI_* vars; allow GOOGLE_* as an alias for convenience.
    api_key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    if not api_key:
        return None

    model = (os.getenv("GEMINI_MODEL") or os.getenv("GOOGLE_MODEL") or "").strip() or "gemini-1.5-flash"
    return GeminiConfig(api_key=api_key, model=model)


def _normalize_provider(raw: str) -> str:
    p = (raw or "").strip().lower().replace("-", "_")
    if not p or p == "auto":
        return ""
    if p in {"gemini", "google", "google_genai", "genai"}:
        return "gemini"
    if p in {"openai", "openai_compatible", "openai_compat", "deepseek"}:
        return "openai_compatible"
    return p


def _pick_provider() -> str:
    """
    Provider selection rules:
    1) If LLM_PROVIDER / QOS_LLM_PROVIDER is set, honor it.
    2) Else auto-detect: prefer Gemini if GEMINI/GOOGLE key exists; otherwise fall back to OpenAI-compatible.
    """

    explicit = _normalize_provider(os.getenv("LLM_PROVIDER") or os.getenv("QOS_LLM_PROVIDER") or "")
    if explicit:
        return explicit

    if load_gemini_config():
        return "gemini"
    if load_openai_compatible_config():
        return "openai_compatible"
    return ""


def build_chat_llm(
    *,
    temperature: float = 0.2,
    streaming: bool = False,
) -> Optional[Any]:
    provider = _pick_provider()
    if provider == "gemini":
        cfg = load_gemini_config()
        if not cfg:
            return None
        if ChatGoogleGenerativeAI is None:  # pragma: no cover
            raise RuntimeError("Gemini provider selected, but `langchain-google-genai` is not installed")

        return ChatGoogleGenerativeAI(
            model=cfg.model,
            api_key=cfg.api_key,
            temperature=temperature,
            streaming=streaming,
        )

    if provider == "openai_compatible":
        cfg = load_openai_compatible_config()
        if not cfg:
            return None
        if ChatOpenAI is None:  # pragma: no cover
            raise RuntimeError("OpenAI-compatible provider selected, but `langchain-openai` is not installed")

        return ChatOpenAI(
            model=cfg.model,
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            temperature=temperature,
            streaming=streaming,
        )

    if provider:
        raise ValueError(f"Unsupported LLM_PROVIDER: {provider}")
    return None


def llm_label() -> str:
    provider = _pick_provider()
    if not provider:
        return "LLM(disabled)"

    if provider == "gemini":
        cfg = load_gemini_config()
        if not cfg:
            return "LLM(disabled)"
        return f"LLM(provider=gemini, model={cfg.model})"

    if provider == "openai_compatible":
        cfg = load_openai_compatible_config()
        if not cfg:
            return "LLM(disabled)"
        return f"LLM(provider=openai_compatible, model={cfg.model}, base_url={cfg.base_url or '<default>'})"

    return f"LLM(provider={provider})"


def strip_markdown_code_fence(text: str) -> str:
    """
    Handle common LLM outputs: ```json\n{...}\n``` / ```\n{...}\n```
    """

    s = (text or "").strip()
    if not s.startswith("```"):
        return s

    first_nl = s.find("\n")
    if first_nl == -1:
        return s

    body = s[first_nl + 1 :].strip()
    if body.endswith("```"):
        body = body[:-3].strip()
    return body


def try_parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    """
    Try to parse LLM output into a JSON object (dict).
    - First, try json.loads directly
    - Then, try removing Markdown code fences
    - Then, try extracting the substring from the first '{' to the last '}'

    """

    raw = (text or "").strip()
    if not raw:
        return None

    for candidate in (raw, strip_markdown_code_fence(raw)):
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else None
        except Exception:
            pass

    # Compatible with cases where the LLM outputs explanations plus multiple JSON blocks:
    # - Extract top-level `{...}` substrings one by one
    # - Search from back to front for the last parsable JSON object

    candidates = _extract_json_object_candidates(raw)
    for candidate in reversed(candidates):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def _extract_json_object_candidates(text: str) -> list[str]:
    """
    Extract all “top-level JSON object” candidate substrings from a text
    (based on brace matching, ignoring braces inside strings).
    """

    s = text or ""
    out: list[str] = []
    depth = 0
    start: Optional[int] = None
    in_str = False
    escape = False

    for i, ch in enumerate(s):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
            continue

        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
            continue

        if ch == "}":
            if depth <= 0:
                continue
            depth -= 1
            if depth == 0 and start is not None:
                out.append(s[start : i + 1])
                start = None

    return out
