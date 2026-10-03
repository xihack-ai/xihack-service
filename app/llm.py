"""Strict live/record/replay model calls; failures never become mock verdicts.

Legacy fallback argument is accepted for call compatibility, never used as data.
LLM_CACHE_FILE retains the deployment path; writes are atomic within one process.
"""

import hashlib
import json
import os
import re
import time
import threading
from pathlib import Path
from urllib.parse import urlparse

import httpx
from hirespec_loop.errors import ModelTaskError

CACHE_FILE = Path(os.getenv("LLM_CACHE_FILE", str(Path(__file__).resolve().parent.parent / ".cache" / "llm_cache.json")))

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_CACHE_LOCK = threading.Lock()


def model_mode() -> str:
    requested = os.getenv("HIRELOOP_MODEL_MODE", "").upper()
    if requested not in {"", "LIVE", "RECORD", "REPLAY"}:
        raise ModelTaskError("HIRELOOP_MODEL_MODE 必须为 LIVE/RECORD/REPLAY；生产不提供模拟兜底。")
    if requested:
        return requested.lower()
    return "live" if is_configured() and not replay_only() else "offline_fallback"


def is_configured() -> bool:
    return bool(os.getenv("LLM_API_KEY"))


def replay_only() -> bool:
    return os.getenv("REPLAY_ONLY", "0") == "1"


def _cache_key(model: str, messages: list[dict], temperature: float = 0.3) -> str:
    payload = json.dumps({"evaluator_version": "grounded-v3-integrated", "base": os.getenv("LLM_BASE_URL"),
                          "model": model, "messages": messages, "temperature": temperature},
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _cache_read() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _cache_write(cache: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = CACHE_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(CACHE_FILE)
    except OSError:
        raise ModelTaskError("模型记录无法保存，请检查缓存目录权限；未宣称录制成功。") from None


def chat(messages: list[dict], temperature: float = 0.3, timeout: float = 30, retries: int = 2) -> str | None:
    """Return model or replay text; unavailable calls raise ModelTaskError."""
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    key = _cache_key(model, messages, temperature)

    mode = model_mode()
    cached = _cache_read().get(key)
    if mode == "replay":
        if not isinstance(cached, str):
            raise ModelTaskError("回放未找到对应记录，请用 RECORD 预跑相同输入。")
        return cached
    if mode == "offline_fallback" or not is_configured():
        raise ModelTaskError("未配置真实模型密钥，未使用模拟结果替代。")
    # Keep existing automatic cache behavior unless an explicit LIVE/RECORD is selected.
    if not os.getenv("HIRELOOP_MODEL_MODE") and isinstance(cached, str):
        return cached

    base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    payload = {"model": model, "messages": messages, "temperature": temperature}
    if urlparse(base).hostname == "api.deepseek.com":
        payload["thinking"] = {"type": "disabled"}
    for attempt in range(retries + 1):
        try:
            resp = httpx.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {os.environ['LLM_API_KEY']}"},
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("invalid model content")
            if mode == "record" or not os.getenv("HIRELOOP_MODEL_MODE"):
                with _CACHE_LOCK:
                    cache = _cache_read()
                    cache[key] = content
                    _cache_write(cache)
            return content
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise ModelTaskError("模型调用失败或超时，未使用模拟结果替代；请重试。") from None


def _parse_json(text: str) -> dict | None:
    """从模型输出里稳健地抽出 JSON 对象。"""
    text = text.strip()
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            obj = json.loads(text[start : end + 1])
        except ValueError:
            return None
    return obj if isinstance(obj, dict) else None


def chat_json(messages: list[dict], fallback: dict, **kwargs) -> dict:
    """Require a JSON object, fail closed instead of using the legacy fallback."""
    raw = chat(messages, **kwargs)
    if raw is None:
        raise ModelTaskError("模型未返回内容。")
    parsed = _parse_json(raw)
    if parsed is None:
        raise ModelTaskError("模型未返回合法 JSON，未使用模拟结果替代。")
    return parsed
