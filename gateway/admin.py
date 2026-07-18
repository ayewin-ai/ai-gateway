"""管理 Admin API：Dashboard 六大面板資料來源。

面板：用量與歸屬 / 成本與配額 / 效能與可靠性 / 安全與稽核 / 模型品質 / 跨節點管理
驗證：Authorization: Bearer <master_key>
"""
import time

from fastapi import APIRouter, Header, HTTPException, Request

from . import cache, db, router as model_router
from .config import cfg

admin_router = APIRouter(prefix="/admin/api")


def _admin_auth(authorization: str | None):
    if not authorization or authorization[7:].strip() != cfg()["server"]["master_key"]:
        raise HTTPException(403, "需要 master key")


def _since(hours: float) -> float:
    return time.time() - hours * 3600


@admin_router.get("/overview")
async def overview(hours: float = 24, authorization: str | None = Header(None)):
    _admin_auth(authorization)
    c = db.conn()
    ts = _since(hours)
    row = c.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) cost, "
        "COALESCE(SUM(prompt_tokens),0) pt, COALESCE(SUM(completion_tokens),0) ct, "
        "COALESCE(AVG(latency_ms),0) avg_lat, "
        "SUM(CASE WHEN status!='ok' THEN 1 ELSE 0 END) errors, "
        "SUM(CASE WHEN cached=1 THEN 1 ELSE 0 END) cached "
        "FROM requests WHERE ts>=?", (ts,)).fetchone()
    sec = c.execute("SELECT COUNT(*) n FROM security_events WHERE ts>=?", (ts,)).fetchone()
    return {
        "window_hours": hours,
        "requests": row["n"], "cost_usd": round(row["cost"], 4),
        "prompt_tokens": row["pt"], "completion_tokens": row["ct"],
        "avg_latency_ms": round(row["avg_lat"], 1),
        "error_rate": round((row["errors"] or 0) / row["n"], 4) if row["n"] else 0,
        "cache_hits": row["cached"] or 0,
        "security_events": sec["n"],
        "cache": cache.stats(),
    }


@admin_router.get("/usage")
async def usage(hours: float = 24, group_by: str = "department",
                authorization: str | None = Header(None)):
    """用量與歸屬：使用者、部門、應用、模型、Token 與請求。"""
    _admin_auth(authorization)
    col = {"department": "department", "user": "user_name", "app": "app_name",
           "model": "model_used", "key": "api_key"}.get(group_by)
    if not col:
        raise HTTPException(400, "group_by 須為 department/user/app/model/key")
    rows = db.conn().execute(
        f"SELECT {col} g, COUNT(*) requests, "
        "COALESCE(SUM(prompt_tokens+completion_tokens),0) tokens, "
        "COALESCE(SUM(cost_usd),0) cost FROM requests WHERE ts>=? "
        f"GROUP BY {col} ORDER BY cost DESC", (_since(hours),)).fetchall()
    return [{"name": r["g"] or "(未標示)", "requests": r["requests"],
             "tokens": r["tokens"], "cost_usd": round(r["cost"], 4)} for r in rows]


@admin_router.get("/cost_trend")
async def cost_trend(hours: float = 24, buckets: int = 24,
                     authorization: str | None = Header(None)):
    """成本趨勢（Showback 基礎）。"""
    _admin_auth(authorization)
    start = _since(hours)
    width = hours * 3600 / buckets
    rows = db.conn().execute(
        "SELECT CAST((ts-?)/? AS INT) b, COALESCE(SUM(cost_usd),0) cost, COUNT(*) n, "
        "COALESCE(SUM(prompt_tokens+completion_tokens),0) tokens "
        "FROM requests WHERE ts>=? GROUP BY b ORDER BY b", (start, width, start)).fetchall()
    by_bucket = {r["b"]: r for r in rows}
    out = []
    for i in range(buckets):
        r = by_bucket.get(i)
        out.append({"ts": start + i * width,
                    "cost_usd": round(r["cost"], 6) if r else 0,
                    "requests": r["n"] if r else 0,
                    "tokens": r["tokens"] if r else 0})
    return out


@admin_router.get("/quotas")
async def quotas(authorization: str | None = Header(None)):
    """配額與超額狀況：每把 Key 的每日/每月預算使用率。"""
    _admin_auth(authorization)
    from .budget import _day_start_ts, _month_start_ts
    out = []
    for k in db.list_api_keys():
        daily = db.spend_since(k["key"], _day_start_ts())
        monthly = db.spend_since(k["key"], _month_start_ts())
        out.append({
            "name": k["name"], "department": k["department"], "user": k["user_name"],
            "key_prefix": k["key"][:14] + "...", "active": bool(k["active"]),
            "rpm_limit": k["rpm_limit"],
            "daily_spent": round(daily, 4), "daily_budget": k["daily_budget_usd"],
            "daily_pct": round(daily / k["daily_budget_usd"] * 100, 1) if k["daily_budget_usd"] else 0,
            "monthly_spent": round(monthly, 4), "monthly_budget": k["monthly_budget_usd"],
            "monthly_pct": round(monthly / k["monthly_budget_usd"] * 100, 1) if k["monthly_budget_usd"] else 0,
        })
    return out


@admin_router.get("/performance")
async def performance(hours: float = 24, authorization: str | None = Header(None)):
    """效能與可靠性：延遲、錯誤率、快取、模型健康與可用性。"""
    _admin_auth(authorization)
    ts = _since(hours)
    rows = db.conn().execute(
        "SELECT model_used m, COUNT(*) n, COALESCE(AVG(latency_ms),0) avg_lat, "
        "COALESCE(AVG(first_token_ms),0) avg_ftt, "
        "SUM(CASE WHEN status!='ok' THEN 1 ELSE 0 END) errors, "
        "SUM(retries) retries FROM requests WHERE ts>=? AND model_used!='' "
        "GROUP BY m ORDER BY n DESC", (ts,)).fetchall()
    lat_rows = db.conn().execute(
        "SELECT latency_ms FROM requests WHERE ts>=? AND status='ok' ORDER BY latency_ms",
        (ts,)).fetchall()
    lats = [r["latency_ms"] for r in lat_rows]
    p95 = lats[int(len(lats) * 0.95)] if lats else 0
    return {
        "p95_latency_ms": round(p95, 1),
        "cache": cache.stats(),
        "circuit_breakers": model_router.circuit_status(),
        "models": [{"model": r["m"], "requests": r["n"],
                    "avg_latency_ms": round(r["avg_lat"], 1),
                    "avg_first_token_ms": round(r["avg_ftt"], 1),
                    "error_rate": round((r["errors"] or 0) / r["n"], 4),
                    "retries": r["retries"] or 0,
                    "available": model_router.is_available(r["m"])} for r in rows],
    }


@admin_router.get("/security")
async def security_panel(hours: float = 24, limit: int = 50,
                         authorization: str | None = Header(None)):
    """安全與稽核：敏感資料攔截、政策命中、Key 與權限紀錄。"""
    _admin_auth(authorization)
    ts = _since(hours)
    by_type = db.conn().execute(
        "SELECT event_type, action, COUNT(*) n FROM security_events WHERE ts>=? "
        "GROUP BY event_type, action ORDER BY n DESC", (ts,)).fetchall()
    recent = db.conn().execute(
        "SELECT ts, department, event_type, pattern_name, action FROM security_events "
        "WHERE ts>=? ORDER BY ts DESC LIMIT ?", (ts, limit)).fetchall()
    audit = db.conn().execute(
        "SELECT ts, actor, action, detail FROM audit_log ORDER BY ts DESC LIMIT ?",
        (limit,)).fetchall()
    return {"by_type": [dict(r) for r in by_type],
            "recent_events": [dict(r) for r in recent],
            "audit_log": [dict(r) for r in audit]}


@admin_router.get("/quality")
async def quality(hours: float = 168, authorization: str | None = Header(None)):
    """模型品質：品質評分、使用者回饋。"""
    _admin_auth(authorization)
    ts = _since(hours)
    rows = db.conn().execute(
        "SELECT model, COUNT(*) n, SUM(CASE WHEN rating=1 THEN 1 ELSE 0 END) up, "
        "SUM(CASE WHEN rating=-1 THEN 1 ELSE 0 END) down "
        "FROM feedback WHERE ts>=? GROUP BY model ORDER BY n DESC", (ts,)).fetchall()
    return [{"model": r["model"], "feedback_count": r["n"], "up": r["up"], "down": r["down"],
             "score": round((r["up"] or 0) / r["n"] * 100, 1) if r["n"] else 0}
            for r in rows]


@admin_router.get("/nodes")
async def nodes(authorization: str | None = Header(None)):
    """跨節點管理：節點狀態、負載、容量、版本（水平擴充）。"""
    _admin_auth(authorization)
    out = []
    now = time.time()
    for n in db.list_nodes():
        stale = now - (n["last_heartbeat"] or 0) > 120
        out.append({**n, "status": "offline" if stale else n["status"]})
    return out


@admin_router.post("/nodes/heartbeat")
async def node_heartbeat(request: Request, authorization: str | None = Header(None)):
    """其他節點回報心跳（水平擴充：跨節點統一監控）。"""
    _admin_auth(authorization)
    b = await request.json()
    db.upsert_node(b["name"], url=b.get("url", ""), version=b.get("version", ""),
                   status=b.get("status", "online"), load_pct=b.get("load_pct", 0),
                   capacity=b.get("capacity", 0))
    return {"status": "ok"}


@admin_router.get("/keys")
async def keys_list(authorization: str | None = Header(None)):
    _admin_auth(authorization)
    return [{**k, "key": k["key"][:14] + "..."} for k in db.list_api_keys()]


@admin_router.post("/keys")
async def keys_create(request: Request, authorization: str | None = Header(None)):
    _admin_auth(authorization)
    b = await request.json()
    if not b.get("name"):
        raise HTTPException(400, "缺少 name")
    row = db.create_api_key(
        name=b["name"], user_name=b.get("user_name", ""),
        department=b.get("department", ""), app_name=b.get("app_name", ""),
        rpm_limit=b.get("rpm_limit"), daily_budget=b.get("daily_budget_usd"),
        monthly_budget=b.get("monthly_budget_usd"),
        allowed_models=b.get("allowed_models", ""))
    return row  # 完整 key 只在建立時回傳一次


@admin_router.post("/keys/{key_prefix}/toggle")
async def keys_toggle(key_prefix: str, authorization: str | None = Header(None)):
    _admin_auth(authorization)
    for k in db.list_api_keys():
        if k["key"].startswith(key_prefix.replace("...", "")):
            db.set_key_active(k["key"], not k["active"])
            return {"key": k["key"][:14] + "...", "active": not k["active"]}
    raise HTTPException(404, "找不到該 Key")


@admin_router.get("/requests")
async def recent_requests(limit: int = 50, authorization: str | None = Header(None)):
    """請求 Trace（可觀測性：請求與 Agent Trace）。"""
    _admin_auth(authorization)
    rows = db.conn().execute(
        "SELECT id, ts, user_name, department, app_name, model_requested, model_used, tier, "
        "prompt_tokens, completion_tokens, cost_usd, latency_ms, first_token_ms, status, "
        "cached, security_hits, route_reason, retries, error "
        "FROM requests ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
