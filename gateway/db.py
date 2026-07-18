"""SQLite 儲存層：API Key、請求紀錄（用量/成本/效能）、安全事件、稽核、模型回饋、節點。"""
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .config import BASE_DIR, cfg

_local = threading.local()


def _db_path() -> Path:
    p = BASE_DIR / cfg()["database"]["path"]
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        c = sqlite3.connect(_db_path(), check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        _local.conn = c
    return _local.conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    user_name TEXT DEFAULT '',
    department TEXT DEFAULT '',
    app_name TEXT DEFAULT '',
    role TEXT DEFAULT 'user',
    rpm_limit INTEGER,
    daily_budget_usd REAL,
    monthly_budget_usd REAL,
    allowed_models TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    ts REAL,
    api_key TEXT,
    user_name TEXT,
    department TEXT,
    app_name TEXT,
    model_requested TEXT,
    model_used TEXT,
    provider TEXT,
    tier TEXT,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    latency_ms REAL DEFAULT 0,
    first_token_ms REAL DEFAULT 0,
    status TEXT,
    error TEXT DEFAULT '',
    cached INTEGER DEFAULT 0,
    security_hits TEXT DEFAULT '',
    route_reason TEXT DEFAULT '',
    retries INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
CREATE TABLE IF NOT EXISTS security_events (
    id TEXT PRIMARY KEY,
    ts REAL,
    api_key TEXT,
    department TEXT,
    event_type TEXT,
    pattern_name TEXT,
    action TEXT,
    detail TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    ts REAL,
    actor TEXT,
    action TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    ts REAL,
    request_id TEXT,
    api_key TEXT,
    model TEXT,
    rating INTEGER,
    comment TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS nodes (
    name TEXT PRIMARY KEY,
    url TEXT DEFAULT '',
    version TEXT DEFAULT '',
    status TEXT DEFAULT 'online',
    load_pct REAL DEFAULT 0,
    capacity INTEGER DEFAULT 0,
    last_heartbeat REAL
);
"""


def init_db():
    c = conn()
    c.executescript(SCHEMA)
    c.commit()


def now() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex


# ---------------- API Keys ----------------

def create_api_key(name, user_name="", department="", app_name="", role="user",
                   rpm_limit=None, daily_budget=None, monthly_budget=None,
                   allowed_models="", key=None) -> dict:
    d = cfg()["default_limits"]
    key = key or ("sk-gw-" + uuid.uuid4().hex)
    conn().execute(
        "INSERT INTO api_keys(key,name,user_name,department,app_name,role,rpm_limit,"
        "daily_budget_usd,monthly_budget_usd,allowed_models,active,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,1,?)",
        (key, name, user_name, department, app_name, role,
         rpm_limit or d["rpm_limit"],
         daily_budget if daily_budget is not None else d["daily_budget_usd"],
         monthly_budget if monthly_budget is not None else d["monthly_budget_usd"],
         allowed_models, now()))
    conn().commit()
    audit("admin", "create_api_key", f"name={name} dept={department}")
    return get_api_key(key)


def get_api_key(key: str) -> dict | None:
    r = conn().execute("SELECT * FROM api_keys WHERE key=?", (key,)).fetchone()
    return dict(r) if r else None


def list_api_keys() -> list[dict]:
    rows = conn().execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def set_key_active(key: str, active: bool):
    conn().execute("UPDATE api_keys SET active=? WHERE key=?", (1 if active else 0, key))
    conn().commit()
    audit("admin", "set_key_active", f"key={key[:14]}... active={active}")


# ---------------- Requests / Usage ----------------

def log_request(**kw) -> str:
    rid = kw.get("id") or new_id()
    conn().execute(
        "INSERT INTO requests(id,ts,api_key,user_name,department,app_name,model_requested,"
        "model_used,provider,tier,prompt_tokens,completion_tokens,cost_usd,latency_ms,"
        "first_token_ms,status,error,cached,security_hits,route_reason,retries) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, kw.get("ts", now()), kw.get("api_key", ""), kw.get("user_name", ""),
         kw.get("department", ""), kw.get("app_name", ""), kw.get("model_requested", ""),
         kw.get("model_used", ""), kw.get("provider", ""), kw.get("tier", ""),
         kw.get("prompt_tokens", 0), kw.get("completion_tokens", 0), kw.get("cost_usd", 0),
         kw.get("latency_ms", 0), kw.get("first_token_ms", 0), kw.get("status", "ok"),
         kw.get("error", ""), 1 if kw.get("cached") else 0, kw.get("security_hits", ""),
         kw.get("route_reason", ""), kw.get("retries", 0)))
    conn().commit()
    return rid


def spend_since(api_key: str, since_ts: float) -> float:
    r = conn().execute(
        "SELECT COALESCE(SUM(cost_usd),0) s FROM requests WHERE api_key=? AND ts>=?",
        (api_key, since_ts)).fetchone()
    return r["s"] or 0.0


def dept_spend_since(department: str, since_ts: float) -> float:
    r = conn().execute(
        "SELECT COALESCE(SUM(cost_usd),0) s FROM requests WHERE department=? AND ts>=?",
        (department, since_ts)).fetchone()
    return r["s"] or 0.0


# ---------------- Security / Audit ----------------

def log_security_event(api_key, department, event_type, pattern_name, action, detail=""):
    conn().execute(
        "INSERT INTO security_events(id,ts,api_key,department,event_type,pattern_name,action,detail) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (new_id(), now(), api_key, department, event_type, pattern_name, action, detail))
    conn().commit()


def audit(actor, action, detail=""):
    conn().execute("INSERT INTO audit_log(id,ts,actor,action,detail) VALUES(?,?,?,?,?)",
                   (new_id(), now(), actor, action, detail))
    conn().commit()


# ---------------- Feedback / Nodes ----------------

def log_feedback(request_id, api_key, model, rating, comment=""):
    conn().execute("INSERT INTO feedback(id,ts,request_id,api_key,model,rating,comment) "
                   "VALUES(?,?,?,?,?,?,?)",
                   (new_id(), now(), request_id, api_key, model, rating, comment))
    conn().commit()


def upsert_node(name, url="", version="", status="online", load_pct=0.0, capacity=0):
    conn().execute(
        "INSERT INTO nodes(name,url,version,status,load_pct,capacity,last_heartbeat) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
        "url=excluded.url,version=excluded.version,status=excluded.status,"
        "load_pct=excluded.load_pct,capacity=excluded.capacity,last_heartbeat=excluded.last_heartbeat",
        (name, url, version, status, load_pct, capacity, now()))
    conn().commit()


def list_nodes() -> list[dict]:
    rows = conn().execute("SELECT * FROM nodes ORDER BY name").fetchall()
    return [dict(r) for r in rows]
