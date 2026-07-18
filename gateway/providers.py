"""模型供應商轉接層：Ollama（地端）、OpenAI 相容雲端、Anthropic、Mock（離線 Demo）。

統一輸入輸出為 OpenAI Chat Completions 格式。
"""
import asyncio
import hashlib
import json
import os
import time

import httpx

from .db import new_id


class ProviderError(Exception):
    pass


def _estimate_tokens(text: str) -> int:
    # 無 usage 回傳時的粗略估算（CJK 約 1 字 1 token，英文約 4 字元 1 token）
    cjk = sum(1 for ch in text if ord(ch) > 0x2E80)
    other = len(text) - cjk
    return max(1, cjk + other // 4)


def _messages_text(messages: list[dict]) -> str:
    return "\n".join(str(m.get("content", "")) for m in messages)


async def call_model(model_cfg: dict, messages: list[dict], params: dict) -> dict:
    """非串流呼叫。回傳 OpenAI 格式 response dict，附加 _first_token_ms。"""
    provider = model_cfg["provider"]
    if provider == "mock":
        return await _call_mock(model_cfg, messages, params)
    if provider in ("ollama", "openai"):
        return await _call_openai_compat(model_cfg, messages, params)
    if provider == "anthropic":
        return await _call_anthropic(model_cfg, messages, params)
    raise ProviderError(f"未知供應商: {provider}")


async def stream_model(model_cfg: dict, messages: list[dict], params: dict):
    """串流呼叫：yield OpenAI 格式 SSE data 字串；最後 yield ('__usage__', dict)。"""
    provider = model_cfg["provider"]
    if provider == "mock":
        async for item in _stream_mock(model_cfg, messages, params):
            yield item
        return
    if provider in ("ollama", "openai"):
        async for item in _stream_openai_compat(model_cfg, messages, params):
            yield item
        return
    # Anthropic 等其他供應商：以非串流結果模擬單一 chunk 輸出
    resp = await call_model(model_cfg, messages, params)
    text = resp["choices"][0]["message"]["content"]
    chunk = {
        "id": resp["id"], "object": "chat.completion.chunk", "model": resp["model"],
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    done = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"
    yield ("__usage__", resp.get("usage", {}))


def _api_key_for(model_cfg: dict) -> str:
    env = model_cfg.get("api_key_env")
    if env:
        key = os.environ.get(env, "")
        if not key:
            raise ProviderError(f"環境變數 {env} 未設定，模型 {model_cfg['name']} 不可用")
        return key
    return "no-key"


# ---------------- OpenAI 相容（含 Ollama） ----------------

async def _call_openai_compat(model_cfg: dict, messages: list[dict], params: dict) -> dict:
    url = model_cfg["base_url"].rstrip("/") + "/chat/completions"
    body = {"model": model_cfg["upstream_model"], "messages": messages, "stream": False}
    for k in ("temperature", "max_tokens", "top_p"):
        if k in params:
            body[k] = params[k]
    headers = {"Authorization": f"Bearer {_api_key_for(model_cfg)}"}
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise ProviderError(f"{model_cfg['name']} 連線失敗: {e}")
    if r.status_code != 200:
        raise ProviderError(f"{model_cfg['name']} HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    data["_first_token_ms"] = (time.monotonic() - t0) * 1000
    if "usage" not in data or not data["usage"]:
        text = data["choices"][0]["message"]["content"] or ""
        data["usage"] = {"prompt_tokens": _estimate_tokens(_messages_text(messages)),
                         "completion_tokens": _estimate_tokens(text)}
    return data


async def _stream_openai_compat(model_cfg: dict, messages: list[dict], params: dict):
    url = model_cfg["base_url"].rstrip("/") + "/chat/completions"
    body = {"model": model_cfg["upstream_model"], "messages": messages, "stream": True,
            "stream_options": {"include_usage": True}}
    for k in ("temperature", "max_tokens", "top_p"):
        if k in params:
            body[k] = params[k]
    headers = {"Authorization": f"Bearer {_api_key_for(model_cfg)}"}
    usage = {}
    full_text = []
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("POST", url, json=body, headers=headers) as r:
                if r.status_code != 200:
                    text = (await r.aread()).decode(errors="replace")[:200]
                    raise ProviderError(f"{model_cfg['name']} HTTP {r.status_code}: {text}")
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("usage"):
                        usage = obj["usage"]
                    for ch in obj.get("choices", []):
                        delta = ch.get("delta", {}).get("content")
                        if delta:
                            full_text.append(delta)
                    yield f"data: {payload}\n\n"
    except httpx.HTTPError as e:
        raise ProviderError(f"{model_cfg['name']} 連線失敗: {e}")
    yield "data: [DONE]\n\n"
    if not usage:
        usage = {"prompt_tokens": _estimate_tokens(_messages_text(messages)),
                 "completion_tokens": _estimate_tokens("".join(full_text))}
    yield ("__usage__", usage)


# ---------------- Anthropic ----------------

async def _call_anthropic(model_cfg: dict, messages: list[dict], params: dict) -> dict:
    url = model_cfg["base_url"].rstrip("/") + "/v1/messages"
    system = "\n".join(m["content"] for m in messages if m.get("role") == "system")
    conv = [m for m in messages if m.get("role") != "system"]
    body = {"model": model_cfg["upstream_model"], "messages": conv,
            "max_tokens": params.get("max_tokens", 4096)}
    if system:
        body["system"] = system
    if "temperature" in params:
        body["temperature"] = params["temperature"]
    headers = {"x-api-key": _api_key_for(model_cfg), "anthropic-version": "2023-06-01"}
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise ProviderError(f"{model_cfg['name']} 連線失敗: {e}")
    if r.status_code != 200:
        raise ProviderError(f"{model_cfg['name']} HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []))
    usage = data.get("usage", {})
    return {
        "id": "chatcmpl-" + new_id()[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_cfg["name"],
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": usage.get("input_tokens", 0),
                  "completion_tokens": usage.get("output_tokens", 0)},
        "_first_token_ms": (time.monotonic() - t0) * 1000,
    }


# ---------------- Mock（離線 Demo / PoC） ----------------

def _mock_reply(model_cfg: dict, messages: list[dict]) -> str:
    last = next((m.get("content", "") for m in reversed(messages)
                 if m.get("role") == "user"), "")
    tier = "地端小模型" if model_cfg["tier"] == "local" else "雲端大模型"
    digest = hashlib.md5(str(last).encode()).hexdigest()[:8]
    return (f"[{model_cfg['upstream_model']}｜{tier}模擬回覆 #{digest}] "
            f"已收到您的請求（{len(str(last))} 字元）。"
            f"此為 Gateway 離線 Demo 模式產生的回覆，用於驗證路由、配額、成本與安全治理流程。")


async def _call_mock(model_cfg: dict, messages: list[dict], params: dict) -> dict:
    latency = 0.05 if model_cfg["tier"] == "local" else 0.15
    await asyncio.sleep(latency)
    text = _mock_reply(model_cfg, messages)
    return {
        "id": "chatcmpl-" + new_id()[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_cfg["name"],
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": _estimate_tokens(_messages_text(messages)),
                  "completion_tokens": _estimate_tokens(text)},
        "_first_token_ms": latency * 1000,
    }


async def _stream_mock(model_cfg: dict, messages: list[dict], params: dict):
    resp = await _call_mock(model_cfg, messages, params)
    text = resp["choices"][0]["message"]["content"]
    cid = resp["id"]
    for i in range(0, len(text), 12):
        chunk = {"id": cid, "object": "chat.completion.chunk", "model": model_cfg["name"],
                 "choices": [{"index": 0, "delta": {"content": text[i:i + 12]},
                              "finish_reason": None}]}
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        await asyncio.sleep(0.01)
    done = {"id": cid, "object": "chat.completion.chunk", "model": model_cfg["name"],
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"
    yield ("__usage__", resp["usage"])
