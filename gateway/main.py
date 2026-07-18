"""Enterprise AI Gateway 主程式。

核心功能：
  1. 統一 OpenAI 相容 API（POST /v1/chat/completions、GET /v1/models）
  2. 地端/雲端多模型智慧路由（model=auto，依敏感度/複雜度/可用性）
  3. API Key 身分權限管理、部門/應用歸屬
  4. Token 成本治理：配額、預算、預警、熔斷、Showback
  5. 提示與回覆安全檢查：DLP 遮罩/阻擋、提示注入偵測、完整稽核
  6. 快取、限速、重試、故障切換（circuit breaker）
  7. 可觀測性：延遲、首 Token、Token I/O、錯誤、安全命中、Agent Trace
  8. 管理 Dashboard（/dashboard）

啟動：python -m gateway.main  （或 uvicorn gateway.main:app --port 8080）
"""
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import budget, cache, db, providers, router as model_router, security
from .admin import admin_router
from .config import cfg, load_config

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


async def _heartbeat_loop():
    """本節點定期回報心跳（跨節點管理面板資料來源）。"""
    import asyncio
    s = cfg()["server"]
    while True:
        recent = db.conn().execute(
            "SELECT COUNT(*) n FROM requests WHERE ts>=?", (time.time() - 60,)).fetchone()
        load_pct = min(100.0, (recent["n"] or 0) / 100 * 100)
        db.upsert_node(s["node_name"], url=f"http://localhost:{s['port']}",
                       version="0.1.0", status="online", load_pct=load_pct, capacity=100)
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    load_config()
    db.init_db()
    s = cfg()["server"]
    db.upsert_node(s["node_name"], url=f"http://localhost:{s['port']}",
                   version="0.1.0", status="online", load_pct=0.0, capacity=100)
    # 首次啟動建立示範 Key
    if not db.list_api_keys():
        db.create_api_key(name="demo-key", user_name="demo", department="IT",
                          app_name="poc", key="sk-gw-demo")
    hb = asyncio.create_task(_heartbeat_loop())
    yield
    hb.cancel()


app = FastAPI(title="Enterprise AI Gateway", version="0.1.0", lifespan=lifespan)
app.include_router(admin_router)


def _auth(authorization: str | None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少 Authorization: Bearer <api-key>")
    key = authorization[7:].strip()
    row = db.get_api_key(key)
    if not row or not row["active"]:
        raise HTTPException(401, "API Key 無效或已停用")
    return row


@app.get("/health")
async def health():
    return {"status": "ok", "node": cfg()["server"]["node_name"], "ts": time.time()}


@app.get("/v1/models")
async def list_models(authorization: str | None = Header(None)):
    _auth(authorization)
    data = [{"id": m["name"], "object": "model", "owned_by": m["provider"],
             "tier": m["tier"], "quality": m["quality"],
             "available": model_router.is_available(m["name"])}
            for m in cfg()["models"]]
    data.insert(0, {"id": cfg()["routing"]["auto_model_name"], "object": "model",
                    "owned_by": "gateway-router", "tier": "auto", "quality": None,
                    "available": True})
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: str | None = Header(None),
                           x_trace_id: str | None = Header(None)):
    key_row = _auth(authorization)
    t_start = time.monotonic()

    # ---- 1. 限速（RPM） ----
    if not budget.check_rate_limit(key_row["key"], key_row["rpm_limit"]):
        db.log_request(api_key=key_row["key"], user_name=key_row["user_name"],
                       department=key_row["department"], app_name=key_row["app_name"],
                       status="rate_limited", error="RPM limit exceeded")
        raise HTTPException(429, f"超過限速：{key_row['rpm_limit']} 次/分鐘")

    # ---- 2. 預算檢查（硬性熔斷 / 軟性預警） ----
    bstat = budget.check_budget(key_row)
    if not bstat.allowed:
        db.log_request(api_key=key_row["key"], user_name=key_row["user_name"],
                       department=key_row["department"], app_name=key_row["app_name"],
                       status="budget_blocked", error=bstat.reason)
        db.log_security_event(key_row["key"], key_row["department"], "budget_circuit_break",
                              "budget", "block", bstat.reason)
        raise HTTPException(429, bstat.reason)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "請求 body 須為 JSON")
    messages = body.get("messages")
    if not messages or not isinstance(messages, list):
        raise HTTPException(400, "缺少 messages")
    requested_model = body.get("model", cfg()["routing"]["auto_model_name"])
    stream = bool(body.get("stream", False))
    params = {k: v for k, v in body.items()
              if k in ("temperature", "max_tokens", "top_p")}

    # ---- 3. 輸入安全檢查（DLP / 提示注入） ----
    scan = security.scan_messages(messages)
    hits_str = ",".join(f"{h['type']}:{h['name']}" for h in scan.hits)
    for h in scan.hits:
        detail = "" if not cfg()["security"].get("store_prompt_in_audit") else str(messages)[:500]
        db.log_security_event(key_row["key"], key_row["department"], h["type"],
                              h["name"], h["action"], detail)
    if scan.blocked:
        db.log_request(api_key=key_row["key"], user_name=key_row["user_name"],
                       department=key_row["department"], app_name=key_row["app_name"],
                       model_requested=requested_model, status="security_blocked",
                       error=scan.block_reason, security_hits=hits_str)
        raise HTTPException(400, scan.block_reason)
    safe_messages = scan.masked_messages

    # ---- 4. 路由決策 ----
    decision = model_router.route(requested_model, safe_messages, scan.sensitive,
                                  key_row.get("allowed_models", ""))
    if decision.blocked:
        db.log_request(api_key=key_row["key"], user_name=key_row["user_name"],
                       department=key_row["department"], app_name=key_row["app_name"],
                       model_requested=requested_model, status="route_blocked",
                       error=decision.block_reason, security_hits=hits_str)
        raise HTTPException(400, decision.block_reason)

    # ---- 5. 快取（非串流） ----
    if not stream:
        cached = cache.get(key_row["department"], requested_model, safe_messages, params)
        if cached:
            rid = db.log_request(
                api_key=key_row["key"], user_name=key_row["user_name"],
                department=key_row["department"], app_name=key_row["app_name"],
                model_requested=requested_model, model_used=cached.get("model", ""),
                provider="cache", tier="cache", cached=True, status="ok",
                latency_ms=(time.monotonic() - t_start) * 1000,
                security_hits=hits_str, route_reason="cache_hit")
            cached["gateway"] = {"request_id": rid, "cached": True,
                                 "model_used": cached.get("model", ""),
                                 "route_reason": "cache_hit", "cost_usd": 0.0}
            return JSONResponse(cached, headers=_gw_headers(rid, "cache", 0.0, bstat))

    # ---- 6. 呼叫模型（重試 + 降級 + 熔斷） ----
    max_retries = cfg()["routing"]["max_retries"]
    last_err = None
    attempts = 0
    for m in decision.candidates:
        for _ in range(max_retries):
            attempts += 1
            try:
                if stream:
                    return await _stream_response(
                        m, safe_messages, params, key_row, requested_model,
                        decision.reason, hits_str, bstat, t_start, attempts - 1)
                resp = await providers.call_model(m, safe_messages, params)
                model_router.record_success(m["name"])
                return await _final_response(
                    resp, m, key_row, requested_model, decision.reason,
                    hits_str, bstat, t_start, attempts - 1, safe_messages, params)
            except providers.ProviderError as e:
                last_err = str(e)
                model_router.record_failure(m["name"])
                if not model_router.is_available(m["name"]):
                    break  # 該模型已熔斷，換下一個候選

    db.log_request(api_key=key_row["key"], user_name=key_row["user_name"],
                   department=key_row["department"], app_name=key_row["app_name"],
                   model_requested=requested_model, status="error",
                   error=last_err or "所有候選模型皆失敗", retries=attempts,
                   security_hits=hits_str, route_reason=decision.reason,
                   latency_ms=(time.monotonic() - t_start) * 1000)
    raise HTTPException(502, f"所有候選模型皆失敗：{last_err}")


def _gw_headers(rid: str, model_used: str, cost: float, bstat) -> dict:
    h = {"x-gateway-request-id": rid, "x-gateway-model": model_used,
         "x-gateway-cost-usd": f"{cost:.6f}"}
    if bstat.warn:
        h["x-gateway-budget-warning"] = (
            f"daily {bstat.daily_spent:.4f}/{bstat.daily_budget:.2f}; "
            f"monthly {bstat.monthly_spent:.4f}/{bstat.monthly_budget:.2f}")
    return h


async def _final_response(resp, m, key_row, requested_model, route_reason,
                          hits_str, bstat, t_start, retries, safe_messages, params):
    # 輸出安全檢查
    text = resp["choices"][0]["message"]["content"] or ""
    masked_text, out_hits = security.scan_output(text)
    if out_hits:
        resp["choices"][0]["message"]["content"] = masked_text
        for h in out_hits:
            db.log_security_event(key_row["key"], key_row["department"],
                                  h["type"], h["name"], h["action"])
        hits_str = ",".join(filter(None, [hits_str] + [
            f"{h['type']}:{h['name']}" for h in out_hits]))

    usage = resp.get("usage", {})
    pt, ct = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    cost = budget.compute_cost(m, pt, ct)
    latency = (time.monotonic() - t_start) * 1000
    rid = db.log_request(
        api_key=key_row["key"], user_name=key_row["user_name"],
        department=key_row["department"], app_name=key_row["app_name"],
        model_requested=requested_model, model_used=m["name"], provider=m["provider"],
        tier=m["tier"], prompt_tokens=pt, completion_tokens=ct, cost_usd=cost,
        latency_ms=latency, first_token_ms=resp.pop("_first_token_ms", 0),
        status="ok", security_hits=hits_str, route_reason=route_reason, retries=retries)
    resp["model"] = m["name"]
    resp["gateway"] = {"request_id": rid, "model_used": m["name"], "tier": m["tier"],
                       "route_reason": route_reason, "cost_usd": round(cost, 6),
                       "cached": False}
    cache.put(key_row["department"], requested_model, safe_messages, params, resp)
    return JSONResponse(resp, headers=_gw_headers(rid, m["name"], cost, bstat))


async def _stream_response(m, safe_messages, params, key_row, requested_model,
                           route_reason, hits_str, bstat, t_start, retries):
    rid = db.new_id()

    async def gen():
        usage = {}
        first_token_ms = 0.0
        status, err = "ok", ""
        try:
            async for item in providers.stream_model(m, safe_messages, params):
                if isinstance(item, tuple) and item[0] == "__usage__":
                    usage = item[1]
                    continue
                if not first_token_ms:
                    first_token_ms = (time.monotonic() - t_start) * 1000
                yield item
            model_router.record_success(m["name"])
        except providers.ProviderError as e:
            model_router.record_failure(m["name"])
            status, err = "error", str(e)
            yield f'data: {json.dumps({"error": str(e)}, ensure_ascii=False)}\n\n'
            yield "data: [DONE]\n\n"
        pt, ct = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        cost = budget.compute_cost(m, pt, ct)
        db.log_request(
            id=rid, api_key=key_row["key"], user_name=key_row["user_name"],
            department=key_row["department"], app_name=key_row["app_name"],
            model_requested=requested_model, model_used=m["name"],
            provider=m["provider"], tier=m["tier"], prompt_tokens=pt,
            completion_tokens=ct, cost_usd=cost,
            latency_ms=(time.monotonic() - t_start) * 1000,
            first_token_ms=first_token_ms, status=status, error=err,
            security_hits=hits_str, route_reason=route_reason, retries=retries)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers=_gw_headers(rid, m["name"], 0.0, bstat))


@app.post("/v1/feedback")
async def feedback(request: Request, authorization: str | None = Header(None)):
    """模型品質使用者回饋（品質評分、使用者回饋）。"""
    key_row = _auth(authorization)
    body = await request.json()
    rating = body.get("rating")
    if rating not in (-1, 1):
        raise HTTPException(400, "rating 須為 1（讚）或 -1（倒讚）")
    db.log_feedback(body.get("request_id", ""), key_row["key"],
                    body.get("model", ""), rating, body.get("comment", ""))
    return {"status": "ok"}


@app.get("/dashboard")
async def dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/")
async def index():
    return {"service": "Enterprise AI Gateway", "docs": "/docs",
            "dashboard": "/dashboard", "openai_compatible_endpoint": "/v1/chat/completions"}


if __name__ == "__main__":
    import uvicorn
    load_config()
    s = cfg()["server"]
    uvicorn.run(app, host=s["host"], port=s["port"])
