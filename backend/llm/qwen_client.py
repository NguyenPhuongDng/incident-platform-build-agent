"""Thin Qwen (DashScope OpenAI-compatible) client.

Exposes four primitives used across the platform:
  - chat            : plain text completion
  - chat_with_tools : one round of the tool-calling loop (caller drives the loop)
  - chat_json       : JSON-mode completion with repair retry
  - embed           : batched embeddings

Every call is logged with model, role, latency and token usage.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Iterable

from openai import OpenAI

from backend.app import trace
from backend.app.config import settings

logger = logging.getLogger("llm")

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

# Qwen3-family models reject non-stream calls while "thinking" is on.
_EXTRA_BODY = {"enable_thinking": False}


class LLMError(RuntimeError):
    pass


def _client() -> OpenAI:
    if not settings.qwen_api_key:
        raise LLMError("QWEN_API_KEY chưa được cấu hình trong .env")
    return OpenAI(
        api_key=settings.qwen_api_key,
        base_url=settings.qwen_base_url,
        timeout=settings.llm_timeout,
        max_retries=0,  # retries handled here so we can log each attempt
    )


def _log(role: str, model: str, started: float, resp: Any, extra: str = "") -> None:
    usage = getattr(resp, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", None) if usage else None
    completion_tokens = getattr(usage, "completion_tokens", None) if usage else None
    logger.info(
        "llm_call role=%s model=%s ms=%d prompt_tokens=%s completion_tokens=%s %s",
        role,
        model,
        int((time.time() - started) * 1000),
        prompt_tokens,
        completion_tokens,
        extra,
    )


def _trace_call(role: str, model: str, started: float, attempt: int,
                payload: dict[str, Any], *, resp: Any = None, err: str = "") -> None:
    usage = getattr(resp, "usage", None)
    message = resp.choices[0].message if resp and getattr(resp, "choices", None) else None
    trace.write(
        "llm",
        role=role,
        model=model,
        ms=int((time.time() - started) * 1000),
        attempt=attempt + 1,
        messages=payload.get("messages"),
        tools=[t.get("function", {}).get("name") for t in payload.get("tools") or []],
        response=getattr(message, "content", None),
        tool_calls=[
            {"tool": c.function.name, "args": c.function.arguments}
            for c in (getattr(message, "tool_calls", None) or [])
        ],
        prompt_tokens=getattr(usage, "prompt_tokens", None),
        completion_tokens=getattr(usage, "completion_tokens", None),
        error=err or None,
    )


def _call(role: str, model: str, *, allow_thinking_flag: bool = True, **kwargs: Any) -> Any:
    """Call chat.completions with retry/backoff, degrading gracefully on extra_body."""
    last_err: Exception | None = None
    use_extra = allow_thinking_flag
    for attempt in range(settings.llm_max_retries):
        started = time.time()
        try:
            payload = dict(kwargs)
            if use_extra:
                payload["extra_body"] = {**_EXTRA_BODY, **payload.get("extra_body", {})}
            resp = _client().chat.completions.create(model=model, **payload)
            _log(role, model, started, resp)
            _trace_call(role, model, started, attempt, payload, resp=resp)
            return resp
        except Exception as exc:  # noqa: BLE001 - surfaced to caller after retries
            last_err = exc
            msg = str(exc)
            _trace_call(role, model, started, attempt, payload, err=msg)
            if use_extra and ("enable_thinking" in msg or "extra_body" in msg):
                # Model does not understand the flag; drop it and retry immediately.
                use_extra = False
                continue
            logger.warning("llm_retry role=%s model=%s attempt=%d err=%s", role, model, attempt + 1, msg[:300])
            time.sleep(min(2**attempt, 8))
    raise LLMError(f"Gọi LLM thất bại sau {settings.llm_max_retries} lần: {last_err}")


def chat(
    messages: list[dict[str, Any]],
    *,
    role: str = "unknown",
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int | None = None,
) -> str:
    resp = _call(
        role,
        model or settings.chat_model,
        messages=messages,
        temperature=temperature,
        **({"max_tokens": max_tokens} if max_tokens else {}),
    )
    return resp.choices[0].message.content or ""


def chat_with_tools(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    role: str = "unknown",
    model: str | None = None,
    temperature: float = 0.2,
) -> Any:
    """Return the raw assistant message (may carry tool_calls)."""
    kwargs: dict[str, Any] = {"messages": messages, "temperature": temperature}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    resp = _call(role, model or settings.chat_model, **kwargs)
    return resp.choices[0].message


def extract_json(text: str) -> Any:
    """Best-effort JSON extraction from an LLM answer."""
    if text is None:
        raise ValueError("empty text")
    raw = text.strip()
    fenced = _JSON_FENCE.search(raw)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Fall back to the widest {...} / [...] span.
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = raw.find(opener), raw.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"Không parse được JSON từ: {text[:400]}")


def chat_json(
    messages: list[dict[str, Any]],
    *,
    role: str = "unknown",
    model: str | None = None,
    temperature: float = 0.2,
    repair_attempts: int = 1,
    as_dict: bool = True,
) -> Any:
    """JSON-mode completion; on parse failure, ask the model to fix its own output.

    With `as_dict` (the default) a bare scalar or list counts as a failure too: every
    caller here wants an object, and small models sometimes answer with just a number.
    """
    convo = list(messages)
    mdl = model or settings.chat_model
    last_text = ""
    for attempt in range(repair_attempts + 1):
        resp = _call(
            role,
            mdl,
            messages=convo,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        last_text = resp.choices[0].message.content or ""
        try:
            parsed = extract_json(last_text)
            if as_dict and not isinstance(parsed, dict):
                raise ValueError(f"cần một object JSON, nhận được {type(parsed).__name__}")
            return parsed
        except ValueError as exc:
            if attempt >= repair_attempts:
                break
            logger.warning("json_repair role=%s err=%s", role, exc)
            convo = convo + [
                {"role": "assistant", "content": last_text},
                {
                    "role": "user",
                    "content": f"Output vừa rồi không dùng được ({exc}). Hãy trả lời lại bằng "
                    "DUY NHẤT một object JSON đúng schema đã nêu, bắt đầu bằng '{' và "
                    "kết thúc bằng '}', không kèm giải thích.",
                },
            ]
    raise LLMError(f"Model không trả về JSON hợp lệ: {last_text[:400]}")


def embed(texts: Iterable[str], *, role: str = "embed", batch_size: int = 10) -> list[list[float]]:
    """Embed texts in small batches, with retry and backoff."""
    items = [t if t.strip() else " " for t in texts]
    out: list[list[float]] = []
    for i in range(0, len(items), batch_size):
        batch = items[i : i + batch_size]
        last_err: Exception | None = None
        for attempt in range(settings.llm_max_retries):
            started = time.time()
            try:
                resp = _client().embeddings.create(
                    model=settings.embed_model,
                    input=batch,
                    dimensions=settings.embed_dim,
                )
                _log(role, settings.embed_model, started, resp, extra=f"batch={len(batch)}")
                out.extend([d.embedding for d in resp.data])
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                logger.warning("embed_retry attempt=%d err=%s", attempt + 1, str(exc)[:300])
                time.sleep(min(2**attempt, 8))
        else:
            raise LLMError(f"Embedding thất bại: {last_err}")
    return out
