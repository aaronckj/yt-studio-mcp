"""Optional LLM comment classifier (any OpenAI-compatible endpoint).

Configured via env:
  YT_MCP_LLM_URL    e.g. http://localhost:4000/v1  (LiteLLM, Ollama, vLLM…)
  YT_MCP_LLM_MODEL  model name at that endpoint
  YT_MCP_LLM_KEY    optional bearer token

Every request/response/error is logged (prompts included) — silent LLM
failures are debugging poison.
"""

from __future__ import annotations

import json
import logging
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger("yt_studio_mcp.llm")

PROMPT = """You are a spam filter for a family-friendly gaming YouTube channel.
Classify the comment below. Spam/scam signals: gift cards, "claim your prize",
telegram/whatsapp contact requests, crypto, adult content, impersonating the
channel, phishing links, unrelated advertising. Enthusiastic gamer talk,
criticism, or short reactions are NOT spam.

Comment author: {author}
Comment text:
{text}

Answer with ONLY a JSON object: {{"spam": true/false, "reason": "<max 10 words>"}}"""


def llm_configured() -> bool:
    return bool(os.environ.get("YT_MCP_LLM_URL") and os.environ.get("YT_MCP_LLM_MODEL"))


def classify_comment(author: str, text: str, timeout: int = 30) -> dict:
    """Return {"spam": bool, "reason": str} or {"error": str}."""
    url = os.environ["YT_MCP_LLM_URL"].rstrip("/") + "/chat/completions"
    model = os.environ["YT_MCP_LLM_MODEL"]
    prompt = PROMPT.format(author=author or "unknown", text=text[:1500])
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 100,
    }
    headers = {"content-type": "application/json"}
    if os.environ.get("YT_MCP_LLM_KEY"):
        headers["authorization"] = f"Bearer {os.environ['YT_MCP_LLM_KEY']}"
    logger.info("llm classify author=%r len=%d model=%s", author, len(text), model)
    try:
        req = Request(url, data=json.dumps(body).encode(), headers=headers)
        with urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except (HTTPError, URLError, TimeoutError) as exc:
        detail = getattr(exc, "reason", None) or getattr(exc, "code", None) or exc
        logger.error("llm request failed: %s (prompt was: %r)", detail, prompt[:300])
        return {"error": f"llm request failed: {detail}"}
    try:
        content = payload["choices"][0]["message"]["content"]
        logger.info("llm raw response: %r", content[:300])
        start = content.find("{")
        end = content.rfind("}")
        verdict = json.loads(content[start : end + 1])
        return {"spam": bool(verdict.get("spam")), "reason": str(verdict.get("reason", ""))}
    except (KeyError, IndexError, ValueError) as exc:
        logger.error("llm response unparseable: %s payload=%r", exc, payload)
        return {"error": f"llm response unparseable: {exc}"}
