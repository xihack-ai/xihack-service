"""Provider-neutral async model boundary. No credentials or network by default."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from .errors import ModelTaskError


JSONResult = str | dict[str, Any]
M = TypeVar("M", bound=BaseModel)


class JSONClient(Protocol):
    # Must include provider, exact model revision and non-secret generation settings.
    cache_identity: str

    async def generate_json(
        self, system_prompt: str, payload: dict[str, Any], output_schema: dict[str, Any]
    ) -> JSONResult: ...


class CallableJSONClient:
    def __init__(
        self,
        generate_json: Callable[[str, dict[str, Any], dict[str, Any]], Awaitable[JSONResult]],
        *,
        provider: str,
        model: str,
        settings: dict[str, Any] | None = None,
    ) -> None:
        if not provider.strip() or not model.strip():
            raise ValueError("必须指定 provider 和实际 model 版本用于缓存隔离")
        self._generate = generate_json
        self.cache_identity = json.dumps(
            {"provider": provider, "model": model, "settings": settings or {}},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )

    async def generate_json(
        self, system_prompt: str, payload: dict[str, Any], output_schema: dict[str, Any]
    ) -> JSONResult:
        return await self._generate(system_prompt, payload, output_schema)


async def validated_call(
    client: JSONClient,
    prompt: str,
    payload: dict[str, Any],
    model: type[M],
    *,
    timeout_seconds: float = 30.0,
    validate_context: Callable[[M], None] | None = None,
) -> M:
    """One schema/context repair retry; transport failures are explicit task errors."""
    repair_errors: list[dict[str, Any]] | None = None
    for attempt in range(2):
        request = {"data": payload}
        if repair_errors is not None:
            request["validation_errors"] = repair_errors
            request["repair_instruction"] = "上次输出未通过校验，请重新返回完整合法 JSON。"
        try:
            raw = await asyncio.wait_for(
                client.generate_json(prompt, request, model.model_json_schema()), timeout=timeout_seconds
            )
        except Exception:
            # Do not expose SDK exceptions (which may contain a resume, URL or token).
            raise ModelTaskError("模型调用失败或超时，请重试该任务。") from None
        try:
            if isinstance(raw, str):
                parsed = model.model_validate_json(raw)
            elif isinstance(raw, dict):
                parsed = model.model_validate(raw)
            else:
                raise ValueError("模型必须返回 JSON 字符串或对象")
            if validate_context is not None:
                validate_context(parsed)
            return parsed
        except ValidationError as exc:
            # Error type and location are sufficient for repair; never echo input values.
            repair_errors = [{"loc": list(e["loc"]), "type": e["type"]} for e in exc.errors()]
        except (ValueError, TypeError):
            repair_errors = [{"loc": [], "type": "context_mismatch_or_invalid_output"}]
        if attempt == 1:
            raise ModelTaskError("模型输出在一次修复后仍未通过结构或上下文校验。") from None
    raise AssertionError("unreachable")
