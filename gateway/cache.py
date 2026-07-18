"""效能層：Prompt Cache（含部門租戶隔離，防跨使用者外洩）。"""
import hashlib
import json
import time

from .config import cfg

# cache_key -> (expire_ts, response_dict)
_store: dict[str, tuple[float, dict]] = {}
_stats = {"hits": 0, "misses": 0}


def _key(department: str, model: str, messages: list[dict], params: dict) -> str:
    c = cfg()["cache"]
    tenant = department if c.get("isolate_by_department") else "_global"
    raw = json.dumps({"t": tenant, "m": model, "msgs": messages,
                      "p": {k: params.get(k) for k in ("temperature", "max_tokens")}},
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def get(department: str, model: str, messages: list[dict], params: dict) -> dict | None:
    if not cfg()["cache"]["enabled"]:
        return None
    k = _key(department, model, messages, params)
    entry = _store.get(k)
    if entry and entry[0] > time.time():
        _stats["hits"] += 1
        return json.loads(json.dumps(entry[1]))  # 深拷貝，避免呼叫端修改快取
    if entry:
        del _store[k]
    _stats["misses"] += 1
    return None


def put(department: str, model: str, messages: list[dict], params: dict, response: dict):
    c = cfg()["cache"]
    if not c["enabled"]:
        return
    k = _key(department, model, messages, params)
    _store[k] = (time.time() + c["ttl_seconds"], response)
    # 簡易上限，避免無限成長
    if len(_store) > 5000:
        oldest = sorted(_store.items(), key=lambda kv: kv[1][0])[:1000]
        for kk, _ in oldest:
            _store.pop(kk, None)


def stats() -> dict:
    total = _stats["hits"] + _stats["misses"]
    return {**_stats, "entries": len(_store),
            "hit_rate": round(_stats["hits"] / total, 4) if total else 0.0}
