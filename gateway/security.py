"""安全治理層：DLP 敏感資料偵測/遮罩、提示注入偵測。"""
import re
from dataclasses import dataclass, field

from .config import cfg


@dataclass
class ScanResult:
    hits: list[dict] = field(default_factory=list)   # [{type, name, action}]
    sensitive: bool = False
    blocked: bool = False
    block_reason: str = ""
    masked_messages: list | None = None


def _compiled_patterns():
    sec = cfg()["security"]
    dlp = [(p["name"], re.compile(p["regex"], re.IGNORECASE), p.get("description", ""))
           for p in sec.get("dlp_patterns", [])]
    inj = [re.compile(p, re.IGNORECASE) for p in sec.get("injection_patterns", [])]
    return dlp, inj


def scan_messages(messages: list[dict]) -> ScanResult:
    """掃描輸入訊息：DLP + 敏感關鍵字 + 提示注入。回傳遮罩後訊息與判定結果。"""
    sec = cfg()["security"]
    dlp_action = sec.get("dlp_action", "mask")
    inj_action = sec.get("prompt_injection_action", "flag")
    keywords = sec.get("sensitive_keywords", [])
    dlp, inj = _compiled_patterns()

    result = ScanResult()
    masked = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, str):
            masked.append(msg)
            continue
        new_content = content
        for name, pattern, _desc in dlp:
            if pattern.search(new_content):
                result.sensitive = True
                result.hits.append({"type": "dlp", "name": name, "action": dlp_action})
                if dlp_action == "block":
                    result.blocked = True
                    result.block_reason = f"偵測到敏感資料（{name}），依政策拒絕"
                else:
                    new_content = pattern.sub(f"[MASKED:{name}]", new_content)
        for kw in keywords:
            if kw.lower() in new_content.lower():
                result.sensitive = True
                result.hits.append({"type": "keyword", "name": kw, "action": "flag"})
        for pattern in inj:
            if pattern.search(new_content):
                result.hits.append({"type": "prompt_injection",
                                    "name": pattern.pattern[:40], "action": inj_action})
                if inj_action == "block":
                    result.blocked = True
                    result.block_reason = "偵測到疑似提示注入攻擊，依政策拒絕"
        masked.append({**msg, "content": new_content})
    result.masked_messages = masked
    return result


def scan_output(text: str) -> tuple[str, list[dict]]:
    """輸出安全檢查：回覆中的敏感資料一律遮罩。"""
    dlp, _ = _compiled_patterns()
    hits = []
    for name, pattern, _desc in dlp:
        if pattern.search(text):
            hits.append({"type": "dlp_output", "name": name, "action": "mask"})
            text = pattern.sub(f"[MASKED:{name}]", text)
    return text, hits
