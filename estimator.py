"""NAV estimation algorithm module for LOF Fund Monitor."""
import aiohttp
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from fetcher import fetch_stock_change_rate, fetch_index_info, fetch_us_stock_change_rate, fetch_us_index_info

logger = logging.getLogger(__name__)

# China Standard Time (UTC+8)
CST = timezone(timedelta(hours=8))

# Time period boundaries for overseas estimation (Beijing time)
# Period 1: 09:00 - 16:00 (A-share trading hours)
# Period 2: 16:00 - 21:00 (After A-share close, before US open)
# Period 3: 21:00 - 09:00 (US market open / overnight)
PERIOD1_START = 9 * 100    # 0900
PERIOD1_END = 16 * 100     # 1600
PERIOD2_END = 21 * 100     # 2100


def get_overseas_period() -> int:
    """Determine current time period for overseas estimation.
    Returns:
        1: 09:00-16:00 Beijing (A-share trading, US closed)
        2: 16:00-21:00 Beijing (A-share closed, US not yet open)
        3: 21:00-09:00 Beijing (US market open)
    """
    now = datetime.now(CST)
    current_time = now.hour * 100 + now.minute
    if PERIOD1_START <= current_time < PERIOD1_END:
        return 1
    elif PERIOD1_END <= current_time < PERIOD2_END:
        return 2
    else:
        return 3


async def estimate_nav_by_holdings(
    session: aiohttp.ClientSession,
    nav: float,
    holdings: list,
) -> dict:
    """Estimate NAV based on top 10 holdings' change rates.
    
    Formula: estimated_nav = nav * (1 + sum(holding_ratio * stock_change_rate / 100) / sum(holding_ratio) * adjustment_factor)
    
    Where adjustment_factor accounts for the fact that top 10 holdings don't cover 100% of the fund.
    We use the ratio of total holding ratio to scale the impact.
    """
    if not holdings or nav <= 0:
        return {"estimated_nav": nav, "estimated_change_rate": 0, "details": []}
    
    total_ratio = sum(h.get("holding_ratio", 0) for h in holdings)
    if total_ratio <= 0:
        return {"estimated_nav": nav, "estimated_change_rate": 0, "details": []}
    
    # Fetch change rates for all holdings concurrently
    details = []
    tasks = []
    for h in holdings:
        em_code = h.get("em_code", "")
        if em_code:
            tasks.append(fetch_stock_change_rate(session, em_code))
        else:
            tasks.append(asyncio_coro_return(0.0))
    
    import asyncio
    change_rates = await asyncio.gather(*tasks, return_exceptions=True)
    
    weighted_change = 0
    for i, h in enumerate(holdings):
        ratio = h.get("holding_ratio", 0)
        try:
            change_rate = change_rates[i] if not isinstance(change_rates[i], Exception) else 0.0
        except (IndexError, TypeError):
            change_rate = 0.0
        
        # ratio is in percent (e.g., 7.68 means 7.68%)
        # change_rate is in percent (e.g., -1.13 means -1.13%)
        # contribution = (ratio/100) * (change_rate/100) as decimal change
        contribution = (ratio / 100.0) * (change_rate / 100.0)
        weighted_change += contribution
        
        details.append({
            "stock_code": h.get("stock_code", ""),
            "stock_name": h.get("stock_name", ""),
            "holding_ratio": ratio,
            "change_rate": round(change_rate, 2),
            "contribution": round(contribution, 4),
        })
    
    # Scale by the coverage ratio (holdings_ratio / 100)
    # This accounts for the non-held portion of the fund
    coverage_ratio = total_ratio / 100.0
    # Scale the weighted change to account for full fund coverage
    # If top 10 holdings cover 46.84% of the fund, we assume the rest 53.16% has similar behavior
    scaled_change = weighted_change / coverage_ratio if coverage_ratio > 0 else weighted_change
    
    estimated_nav = round(nav * (1 + scaled_change), 4)
    estimated_change_rate = round(scaled_change * 100, 2)
    
    return {
        "estimated_nav": estimated_nav,
        "estimated_change_rate": estimated_change_rate,
        "details": details,
    }


async def estimate_nav_by_industry_index(
    session: aiohttp.ClientSession,
    nav: float,
    index_code: str,
) -> dict:
    """Estimate NAV based on industry index change rate.
    
    This algorithm uses an industry index (like CSI Liquor Index 0.399997) to estimate
    the fund's NAV change, assuming the fund closely tracks the index.
    
    Formula: estimated_nav = nav * (1 + index_change_rate / 100)
    
    Returns enriched details including index name, current value, and change rate.
    """
    if not index_code or nav <= 0:
        return {"estimated_nav": nav, "estimated_change_rate": 0, "details": [], "index_name": "", "index_value": 0}
    
    # Fetch index info (name, value, change rate) in one call
    index_info = await fetch_index_info(session, index_code)
    
    if index_info:
        change_rate = index_info.get("change_rate", 0)
        index_name = index_info.get("index_name", "")
        index_value = index_info.get("index_value", 0)
    else:
        # Fallback: just get change rate
        change_rate = await fetch_stock_change_rate(session, index_code)
        index_name = ""
        index_value = 0
    
    estimated_nav = round(nav * (1 + change_rate / 100), 4)
    
    return {
        "estimated_nav": estimated_nav,
        "estimated_change_rate": round(change_rate, 2),
        "index_name": index_name,
        "index_value": index_value,
        "details": [{
            "index_code": index_code,
            "index_name": index_name,
            "index_value": index_value,
            "change_rate": round(change_rate, 2),
        }],
    }


async def asyncio_coro_return(val):
    """Helper to return a value as a coroutine."""
    return val


async def estimate_nav_by_overseas_holdings(
    session: aiohttp.ClientSession,
    nav: float,
    cn_change_rate: float,
    overseas_holdings: list,
    us_index_code: str,
) -> dict:
    """Estimate NAV for QDII funds with US stock holdings.
    
    This algorithm handles funds that hold both A-share assets and US stocks,
    using different data sources based on the current time period:
    
    Period 1 (09:00-16:00 Beijing):
      - A-share part: use real-time change rate from fund estimate API
      - US stock part: use US index (NASDAQ/Dow) change rate as proxy
    
    Period 2 (16:00-21:00 Beijing):
      - A-share part: use closing change rate (same as period 1 end value)
      - US stock part: use US index (NASDAQ/Dow) change rate as proxy
    
    Period 3 (21:00+ Beijing):
      - A-share part: use closing change rate (fixed)
      - US stock part: use real-time US stock individual change rates
    
    Formula:
      estimated_change = cn_ratio × cn_change + us_ratio × us_change
      estimated_nav = nav × (1 + estimated_change / 100)
    
    Args:
        session: aiohttp session for API calls
        nav: latest published NAV
        cn_change_rate: A-share component change rate (from Tiantian Fund API, in %)
        overseas_holdings: list of US stock holdings with stock_code, stock_name, holding_ratio, em_code
        us_index_code: US index code for futures/real-time estimation (e.g., "100.NDX")
    
    Returns:
        dict with estimated_nav, estimated_change_rate, period, details
    """
    if nav <= 0:
        return {
            "estimated_nav": nav, "estimated_change_rate": 0, "period": 0,
            "cn_ratio": 0, "us_ratio": 0, "details": [],
            "us_index_name": "", "us_index_change_rate": 0,
        }

    period = get_overseas_period()
    
    # Calculate ratios from overseas holdings
    us_total_ratio = sum(h.get("holding_ratio", 0) for h in overseas_holdings)
    # The remaining portion is A-share / domestic
    cn_ratio = max(0, 100 - us_total_ratio) / 100.0  # as decimal
    us_ratio = us_total_ratio / 100.0  # as decimal
    
    # If no overseas holdings configured, treat as 50/50 split and use index only
    if not overseas_holdings and us_index_code:
        cn_ratio = 0.0
        us_ratio = 1.0
        # If we have a US index but no holdings, estimate purely by index
    
    # Get US component change rate based on period
    us_change_rate = 0.0
    us_index_name = ""
    details = []
    
    if period in (1, 2):
        # Period 1 & 2: Use US index change rate (futures / previous close proxy)
        if us_index_code:
            us_index_info = await fetch_us_index_info(session, us_index_code)
            if us_index_info:
                us_change_rate = us_index_info.get("change_rate", 0)
                us_index_name = us_index_info.get("index_name", "")
                details.append({
                    "source": "us_index",
                    "index_code": us_index_code,
                    "index_name": us_index_name,
                    "index_value": us_index_info.get("index_value", 0),
                    "change_rate": round(us_change_rate, 2),
                    "period": f"时段{period}",
                    "note": "美股休市，使用指数涨跌估算" if us_change_rate == 0 else "美股休市，使用指数涨跌估算",
                })
            else:
                details.append({
                    "source": "us_index",
                    "index_code": us_index_code,
                    "change_rate": 0,
                    "period": f"时段{period}",
                    "note": "无法获取美股指数数据",
                })
        
        # Also show individual US holdings' previous close info (if available)
        if overseas_holdings:
            for h in overseas_holdings:
                details.append({
                    "source": "us_holding",
                    "stock_code": h.get("stock_code", ""),
                    "stock_name": h.get("stock_name", ""),
                    "holding_ratio": h.get("holding_ratio", 0),
                    "note": "盘后数据，未使用实时价格",
                })
    
    elif period == 3:
        # Period 3: US market is open, use real-time US stock change rates
        if overseas_holdings:
            # Fetch real-time change rates for all US holdings concurrently
            tasks = []
            for h in overseas_holdings:
                em_code = h.get("em_code", "")
                if em_code:
                    tasks.append(fetch_us_stock_change_rate(session, em_code))
                else:
                    tasks.append(asyncio_coro_return(0.0))
            
            change_rates = await asyncio.gather(*tasks, return_exceptions=True)
            
            us_weighted_change = 0
            for i, h in enumerate(overseas_holdings):
                ratio = h.get("holding_ratio", 0)
                try:
                    change_rate = change_rates[i] if not isinstance(change_rates[i], Exception) else 0.0
                except (IndexError, TypeError):
                    change_rate = 0.0
                
                # contribution = (ratio/100) * (change_rate/100)
                contribution = (ratio / 100.0) * (change_rate / 100.0)
                us_weighted_change += contribution
                
                details.append({
                    "source": "us_holding",
                    "stock_code": h.get("stock_code", ""),
                    "stock_name": h.get("stock_name", ""),
                    "holding_ratio": ratio,
                    "em_code": h.get("em_code", ""),
                    "change_rate": round(change_rate, 2),
                    "contribution": round(contribution, 4),
                    "period": "时段3",
                    "note": "美股实时",
                })
            
            # Scale by coverage ratio (us_total_ratio / 100)
            coverage_ratio = us_total_ratio / 100.0 if us_total_ratio > 0 else 1.0
            if coverage_ratio > 0 and overseas_holdings:
                us_change_rate = (us_weighted_change / coverage_ratio) * 100
            else:
                us_change_rate = 0
        
        elif us_index_code:
            # No individual holdings, use index as proxy
            us_index_info = await fetch_us_index_info(session, us_index_code)
            if us_index_info:
                us_change_rate = us_index_info.get("change_rate", 0)
                us_index_name = us_index_info.get("index_name", "")
                details.append({
                    "source": "us_index",
                    "index_code": us_index_code,
                    "index_name": us_index_name,
                    "change_rate": round(us_change_rate, 2),
                    "period": "时段3",
                    "note": "美股交易中，使用指数实时涨跌",
                })
    
    # Calculate combined estimated change rate
    # cn_change_rate is the A-share component change (from Tiantian Fund API)
    # us_change_rate is the US component change (from index or real-time stocks)
    # weighted: cn_ratio × cn_change + us_ratio × us_change
    estimated_change = cn_ratio * cn_change_rate + us_ratio * us_change_rate
    
    estimated_nav = round(nav * (1 + estimated_change / 100), 4)
    
    return {
        "estimated_nav": estimated_nav,
        "estimated_change_rate": round(estimated_change, 2),
        "period": period,
        "cn_ratio": round(cn_ratio * 100, 2),
        "us_ratio": round(us_ratio * 100, 2),
        "cn_change_rate": round(cn_change_rate, 2),
        "us_change_rate": round(us_change_rate, 2),
        "us_index_name": us_index_name,
        "details": details,
    }
