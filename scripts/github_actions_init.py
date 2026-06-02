#!/usr/bin/env python3
"""Initialize LOF Fund Monitor runtime configuration for GitHub Actions.

This script creates the SQLite schema, imports default_funds.json, and writes
WeChat alert settings from GitHub Secrets/Variables. It never prints the
SendKey and masks it in GitHub Actions logs.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from db import (  # noqa: E402
    get_all_funds,
    get_wechat_config,
    init_db,
    save_wechat_config,
    seed_default_funds,
)

DEFAULT_PUSH_TIMES = "09:35,10:00,10:30,11:00,11:25,13:05,13:30,14:00,14:30,14:55"


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip()


def _bool_env(name: str, default: bool) -> int:
    raw = _env(name, "")
    if raw == "":
        return 1 if default else 0
    return 1 if raw.lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"} else 0


def _float_env(name: str, default: float) -> float:
    raw = _env(name, "")
    if raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[WARN] {name}={raw!r} 不是数字，使用默认值 {default}")
        return default


def _normalize_push_times(raw: str) -> str:
    """Return comma-separated HH:MM values, dropping invalid fragments."""
    raw = raw or DEFAULT_PUSH_TIMES
    valid: list[str] = []
    for part in re.split(r"[,，;；\s]+", raw):
        part = part.strip()
        if not part:
            continue
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", part)
        if not match:
            print(f"[WARN] 忽略无效推送时间: {part}")
            continue
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            valid.append(f"{hour:02d}:{minute:02d}")
        else:
            print(f"[WARN] 忽略越界推送时间: {part}")
    return ",".join(dict.fromkeys(valid)) or DEFAULT_PUSH_TIMES


async def main() -> None:
    send_key = _env("WECHAT_SEND_KEY", "")
    if send_key:
        # Prevent accidental display in Actions logs.
        print(f"::add-mask::{send_key}")

    await init_db()
    await seed_default_funds()

    config = await get_wechat_config()

    # Important: never keep a SendKey that was accidentally committed inside a DB.
    # In Actions, the SendKey must come from the WECHAT_SEND_KEY repository secret.
    config["send_key"] = send_key
    config["push_enabled"] = _bool_env("WECHAT_PUSH_ENABLED", bool(send_key))
    config["push_interval"] = 60
    config["push_time"] = _normalize_push_times(_env("WECHAT_PUSH_TIME", DEFAULT_PUSH_TIMES))
    config["premium_alert_enabled"] = _bool_env("PREMIUM_ALERT_ENABLED", True)
    config["discount_alert_enabled"] = _bool_env("DISCOUNT_ALERT_ENABLED", True)
    config["premium_upper"] = _float_env("PREMIUM_UPPER", 3.0)
    config["discount_lower"] = _float_env("DISCOUNT_LOWER", -5.0)
    config["min_turnover"] = _float_env("MIN_TURNOVER", 60.0)

    await save_wechat_config(config)
    funds = await get_all_funds()

    print("[OK] GitHub Actions 运行配置已写入 SQLite。")
    print(f"[OK] 已导入/保留基金数量: {len(funds)}")
    print(f"[OK] 微信告警: {'已启用' if config['push_enabled'] and send_key else '未启用或缺少 WECHAT_SEND_KEY'}")
    print(f"[OK] 告警检查时间: {config['push_time']}")
    print(
        "[OK] 阈值: "
        f"溢价 >= {config['premium_upper']}%, "
        f"折价 <= {config['discount_lower']}%, "
        f"成交额 >= {config['min_turnover']} 万元"
    )


if __name__ == "__main__":
    asyncio.run(main())
