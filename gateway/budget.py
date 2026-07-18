"""成本與配額層：Token 成本換算、每日/每月預算、RPM 限速、預警與熔斷。"""
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import cfg
from . import db

# RPM 滑動視窗：api_key -> deque[timestamps]
_rpm_windows: dict[str, deque] = {}


def compute_cost(model_cfg: dict, prompt_tokens: int, completion_tokens: int) -> float:
    return (prompt_tokens * model_cfg.get("price_input", 0.0)
            + completion_tokens * model_cfg.get("price_output", 0.0)) / 1_000_000


def _day_start_ts() -> float:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _month_start_ts() -> float:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def check_rate_limit(api_key: str, rpm_limit: int) -> bool:
    """True = 通過；False = 超過 RPM 限制。"""
    now = time.time()
    win = _rpm_windows.setdefault(api_key, deque())
    while win and win[0] < now - 60:
        win.popleft()
    if len(win) >= rpm_limit:
        return False
    win.append(now)
    return True


@dataclass
class BudgetStatus:
    allowed: bool = True
    reason: str = ""
    warn: bool = False
    daily_spent: float = 0.0
    daily_budget: float = 0.0
    monthly_spent: float = 0.0
    monthly_budget: float = 0.0


def check_budget(key_row: dict) -> BudgetStatus:
    """檢查每日/每月預算：超過 → 硬性熔斷（拒絕）；達預警比例 → 軟性警告。"""
    warn_ratio = cfg()["default_limits"]["budget_warn_ratio"]
    st = BudgetStatus(
        daily_spent=db.spend_since(key_row["key"], _day_start_ts()),
        daily_budget=key_row["daily_budget_usd"] or 0,
        monthly_spent=db.spend_since(key_row["key"], _month_start_ts()),
        monthly_budget=key_row["monthly_budget_usd"] or 0,
    )
    if st.daily_budget and st.daily_spent >= st.daily_budget:
        st.allowed = False
        st.reason = (f"已超過每日預算 ${st.daily_budget:.4f}"
                     f"（已用 ${st.daily_spent:.4f}），依政策熔斷")
        return st
    if st.monthly_budget and st.monthly_spent >= st.monthly_budget:
        st.allowed = False
        st.reason = (f"已超過每月預算 ${st.monthly_budget:.4f}"
                     f"（已用 ${st.monthly_spent:.4f}），依政策熔斷")
        return st
    if ((st.daily_budget and st.daily_spent >= st.daily_budget * warn_ratio)
            or (st.monthly_budget and st.monthly_spent >= st.monthly_budget * warn_ratio)):
        st.warn = True
    return st
