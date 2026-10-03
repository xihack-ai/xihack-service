"""模型调用封装:chat / chat_json + 磁盘缓存 + 重试 + REPLAY_ONLY。

- 未配置 LLM_API_KEY 或 REPLAY_ONLY=1(断网演练)时不发起网络请求;
- chat_json(messages, fallback=...) 永远返回 dict:
  命中缓存 -> 缓存内容;否则走网络;失败 -> fallback(合法 JSON,保证流程不中断)。
- 缓存文件 .cache/llm_cache.json,演示前预跑即可填满。
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

CACHE_FILE = Path(os.getenv("LLM_CACHE_FILE", str(Path(__file__).resolve().parent.parent / ".cache" / "llm_cache.json")))

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def is_configured() -> bool:
    return bool(os.getenv("LLM_API_KEY"))


def replay_only() -> bool:
    return os.getenv("REPLAY_ONLY", "0") == "1"


def _cache_key(model: str, messages: list[dict], temperature: float = 0.3) -> str:
    payload = json.dumps({"evaluator_version": "grounded-v2", "base": os.getenv("LLM_BASE_URL"),
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
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass  # 缓存失败不影响主流程


def chat(messages: list[dict], temperature: float = 0.3, timeout: float = 30, retries: int = 2) -> str | None:
    """返回模型文本;离线/失败返回 None(由调用方 fallback)。"""
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    key = _cache_key(model, messages, temperature)

    cached = _cache_read().get(key)
    if cached is not None:
        return cached

    if replay_only() or not is_configured():
        return None

    base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    payload = {"model": model, "messages": messages, "temperature": temperature}
    if urlparse(base).hostname == "api.deepseek.com":
        payload["thinking"] = {"type": "disabled"}
    last_err: Exception | None = None
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
            cache = _cache_read()
            cache[key] = content
            _cache_write(cache)
            return content
        except (httpx.HTTPError, KeyError, IndexError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    print(f"[llm] 调用失败: {type(last_err).__name__}")
    return None


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
    """要求模型输出 JSON 对象;任何失败路径返回 fallback(调用方仍需校验字段值)。"""
    raw = chat(messages, **kwargs)
    if raw is None:
        return dict(fallback)
    parsed = _parse_json(raw)
    if parsed is None:
        print("[llm] 输出不是合法 JSON,使用 fallback")
        return dict(fallback)
    return parsed
