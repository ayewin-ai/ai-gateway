"""設定載入：config.yaml + 環境變數覆寫。"""
import os
from pathlib import Path

import yaml

_CONFIG: dict = {}
BASE_DIR = Path(__file__).resolve().parent.parent


def load_config(path: str | None = None) -> dict:
    global _CONFIG
    cfg_path = Path(path) if path else BASE_DIR / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        _CONFIG = yaml.safe_load(f)
    # 環境變數覆寫 master key
    env_master = os.environ.get("GATEWAY_MASTER_KEY")
    if env_master:
        _CONFIG["server"]["master_key"] = env_master
    return _CONFIG


def cfg() -> dict:
    if not _CONFIG:
        load_config()
    return _CONFIG


def model_by_name(name: str) -> dict | None:
    for m in cfg().get("models", []):
        if m["name"] == name:
            return m
    return None
