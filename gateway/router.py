"""模型路由層：依敏感度、任務複雜度、預算、品質、可用性選擇模型。

含熔斷器（circuit breaker）與失敗降級候選清單。
"""
import time
from dataclasses import dataclass, field

from .config import cfg, model_by_name

# 熔斷器狀態：model_name -> {"failures": int, "open_until": ts}
_circuit: dict[str, dict] = {}


def record_failure(model_name: str):
    r = cfg()["routing"]
    st = _circuit.setdefault(model_name, {"failures": 0, "open_until": 0})
    st["failures"] += 1
    if st["failures"] >= r["circuit_failure_threshold"]:
        st["open_until"] = time.time() + r["circuit_cooldown_seconds"]


def record_success(model_name: str):
    _circuit[model_name] = {"failures": 0, "open_until": 0}


def is_available(model_name: str) -> bool:
    st = _circuit.get(model_name)
    if not st:
        return True
    if st["open_until"] > time.time():
        return False
    return True


def circuit_status() -> dict:
    now = time.time()
    return {name: {"failures": st["failures"],
                   "open": st["open_until"] > now,
                   "cooldown_remaining": max(0, round(st["open_until"] - now))}
            for name, st in _circuit.items()}


@dataclass
class RouteDecision:
    candidates: list[dict] = field(default_factory=list)  # 依優先序的模型設定
    reason: str = ""
    blocked: bool = False
    block_reason: str = ""


def _is_complex(messages: list[dict]) -> bool:
    r = cfg()["routing"]
    text = "\n".join(str(m.get("content", "")) for m in messages)
    if len(text) > r["complexity_char_threshold"]:
        return True
    low = text.lower()
    return any(kw.lower() in low for kw in r["complexity_keywords"])


def _ordered_models(names: list[str]) -> list[dict]:
    out = []
    for n in names:
        m = model_by_name(n)
        if m and is_available(n):
            out.append(m)
    return out


def route(requested_model: str, messages: list[dict], sensitive: bool,
          allowed_models: str = "") -> RouteDecision:
    """決定候選模型清單（第一個為主要，其餘為降級備援）。"""
    r = cfg()["routing"]
    d = RouteDecision()
    allowed = [x.strip() for x in allowed_models.split(",") if x.strip()] if allowed_models else None

    def filter_allowed(models: list[dict]) -> list[dict]:
        if not allowed:
            return models
        return [m for m in models if m["name"] in allowed]

    # 敏感資料政策：強制地端或直接拒絕（敏感資料不可出網）
    if sensitive and r["sensitive_route"] == "block":
        d.blocked = True
        d.block_reason = "請求含敏感資料，政策設定為拒絕"
        return d

    if requested_model and requested_model != r["auto_model_name"]:
        # 指定模型：仍需通過敏感度與白名單檢查
        m = model_by_name(requested_model)
        if not m:
            d.blocked = True
            d.block_reason = f"模型 {requested_model} 未註冊"
            return d
        if allowed and m["name"] not in allowed:
            d.blocked = True
            d.block_reason = f"此 API Key 不允許使用模型 {requested_model}"
            return d
        if sensitive and m["tier"] == "cloud" and r["sensitive_route"] == "local_only":
            # 改路由到地端（政策優先於使用者指定）
            local = filter_allowed(_ordered_models(r["local_preferred"]))
            if not local:
                d.blocked = True
                d.block_reason = "含敏感資料且無可用地端模型"
                return d
            d.candidates = local
            d.reason = f"sensitive→local（原指定 {requested_model} 為雲端，依政策改地端）"
            return d
        d.candidates = [m] + [x for x in filter_allowed(
            _ordered_models(r["local_preferred"] if m["tier"] == "local" else r["cloud_preferred"]))
            if x["name"] != m["name"]]
        d.reason = f"user-specified:{requested_model}"
        return d

    # auto 智慧路由
    local = filter_allowed(_ordered_models(r["local_preferred"]))
    cloud = filter_allowed(_ordered_models(r["cloud_preferred"]))
    if sensitive:
        if not local:
            d.blocked = True
            d.block_reason = "含敏感資料且無可用地端模型"
            return d
        d.candidates = local
        d.reason = "auto:sensitive→local_only"
        return d
    if _is_complex(messages):
        d.candidates = cloud + local  # 雲端失敗仍可降級地端
        d.reason = "auto:complex→cloud_preferred"
    else:
        d.candidates = local + cloud
        d.reason = "auto:simple→local_preferred"
    if not d.candidates:
        d.blocked = True
        d.block_reason = "無可用模型（可能全部熔斷或未設定）"
    return d
