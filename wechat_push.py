"""WeChat push module using ServerChan for LOF Fund Monitor."""

import aiohttp
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))
SERVERCHAN_URL = "https://sctapi.ftqq.com"

STATUS_ICON = {
    "开放": "✅",
    "限大额": "⚠️",
    "暂停": "🚫",
}


def _status_sort_key(fund: dict) -> tuple:
    """Sort helper: open/limited funds first, then suspended.
    Within same group, sort by premium_rate descending."""
    status = fund.get("purchase_status", "")
    # group 0 = 开放, 1 = 限大额, 2 = 暂停/未知
    if status == "开放":
        group = 0
    elif status == "限大额":
        group = 1
    else:
        group = 2
    # negate premium so higher premium comes first within group
    premium = abs(fund.get("premium_rate", 0) or 0)
    return (group, -premium)


def _status_label(status: str) -> str:
    """Return icon + status text."""
    icon = STATUS_ICON.get(status, "❓")
    return f"{icon}{status}"


def _fmt_vol(amount: float) -> str:
    """Format trade_amount (yuan) to 万元."""
    if amount and amount > 0:
        return f"{amount / 10000:.0f}万"
    return "--"


async def send_wechat_message(send_key: str, title: str, content: str) -> dict:
    """Send a message via ServerChan."""
    if not send_key:
        return {"success": False, "msg": "SendKey 未配置", "response": ""}
    url = f"{SERVERCHAN_URL}/{send_key}.send"
    data = {"title": title, "desp": content, "channel": "9"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                response_text = await resp.text()
                if resp.status == 200 and "success" in response_text.lower():
                    logger.info(f"微信推送成功: {title}")
                    return {"success": True, "msg": "推送成功", "response": response_text}
                else:
                    logger.warning(f"微信推送失败: {resp.status} {response_text}")
                    return {"success": False, "msg": f"推送失败({resp.status})", "response": response_text}
    except Exception as e:
        logger.error(f"微信推送异常: {e}")
        return {"success": False, "msg": str(e), "response": ""}



def build_threshold_alert_message(alerts: list, premium_upper: float = 3.0,
                                   discount_lower: float = -2.0,
                                   min_turnover: float = 60) -> str:
    """Build alert message body with exact filter criteria (Chinese)."""
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

    # Sort: open/limited first, then by premium desc
    alerts_sorted = sorted(alerts, key=_status_sort_key)

    lines = []
    lines.append(f"## ⚠️ 折溢价阈值告警\n")
    lines.append(f"**时间：** {now}  \n")

    # Conditions
    lines.append("**触发条件：**  ")
    conds = []
    has_prem = any(a.get("threshold_type") == "premium_upper" for a in alerts_sorted)
    has_disc = any(a.get("threshold_type") == "discount_lower" for a in alerts_sorted)
    if has_prem:
        conds.append(f"溢价率 ≥ {premium_upper}%")
    if has_disc:
        conds.append(f"折价率 ≤ {discount_lower}%")
    conds.append(f"成交金额 ≥ {int(min_turnover)} 万元")
    lines.append("  \n".join(conds))
    lines.append(f"  \n**告警数量：** {len(alerts_sorted)} 只  \n")
    lines.append("")

    for a in alerts_sorted:
        premium = a.get("premium_rate", 0) or 0
        direction = "🔴 溢价" if premium > 0 else "🟢 折价"
        status_label = _status_label(a.get("purchase_status", "未知"))
        vol = _fmt_vol(a.get("trade_amount", 0))
        lines.append(f"### {direction} **{a.get('fund_code', '')}** {a.get('fund_name', '')}\n")
        lines.append(f"- 折溢价率：**{premium:+.2f}%**  \n")
        lines.append(f"- 交易价格：{a.get('trade_price', '--')}  \n")
        lines.append(f"- 估算净值：{a.get('estimated_nav', '--')}  \n")
        lines.append(f"- 成交金额：{vol}  \n")
        lines.append(f"- 申购状态：{status_label}  \n")
        lines.append("")

    lines.append("---")
    lines.append("*自动告警推送 · 仅供参考*")

    return "\n".join(lines)
