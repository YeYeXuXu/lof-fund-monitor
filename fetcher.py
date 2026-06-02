"""Data fetcher module for LOF Fund Monitor - fetches data from EastMoney APIs."""
import aiohttp
import re
import json
import logging
from bs4 import BeautifulSoup
from datetime import datetime

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "http://fund.eastmoney.com/",
}

HEADERS_QUOTE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "http://quote.eastmoney.com/",
}

HEADERS_F10 = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "http://fundf10.eastmoney.com/",
}


def parse_jsonpgz(text: str) -> dict:
    """Parse jsonpgz({...}) response, handling special characters in fund names."""
    # Find the outermost parentheses of jsonpgz(...)
    start = text.find('(')
    end = text.rfind(')')
    if start >= 0 and end > start:
        json_str = text[start+1:end].strip()
        if not json_str:
            return {}
        return json.loads(json_str)
    return {}


async def fetch_fund_estimate(session: aiohttp.ClientSession, fund_code: str) -> dict:
    """Fetch fund estimated NAV from fundgz.1234567.com.cn.
    During non-trading hours, gsz may be 0 or equal to dwjz, indicating no real-time estimate.
    """
    try:
        url = f"http://fundgz.1234567.com.cn/js/{fund_code}.js"
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            text = await resp.text()
            # Handle empty or non-JSONP responses
            if not text or 'jsonpgz' not in text:
                logger.warning(f"No estimate data for {fund_code} (non-trading hours or invalid response)")
                return {}
            data = parse_jsonpgz(text)
            if data:
                nav = float(data.get("dwjz", 0))
                est_nav = float(data.get("gsz", 0))
                est_change = float(data.get("gszzl", 0))
                
                return {
                    "fund_code": data.get("fundcode", fund_code),
                    "fund_name": data.get("name", ""),
                    "estimated_nav": est_nav if est_nav > 0 else nav,
                    "estimated_change_rate": est_change,
                    "nav": nav,
                    "nav_date": data.get("jzrq", ""),
                    "estimate_time": data.get("gztime", ""),
                }
    except json.JSONDecodeError as e:
        logger.debug(f"Invalid JSON in estimate response for {fund_code}: {e}")
    except Exception as e:
        logger.debug(f"Error fetching estimate for {fund_code}: {e}")
    return {}


async def fetch_stock_price(session: aiohttp.ClientSession, fund_code: str, market: str = "0") -> dict:
    """Fetch LOF secondary market trading price from eastmoney push2 API."""
    try:
        secid = f"{market}.{fund_code}"
        url = f"http://push2delay.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": secid,
            "fields": "f43,f44,f45,f46,f47,f48,f50,f57,f58,f169,f170,f171"
        }
        async with session.get(url, params=params, headers=HEADERS_QUOTE, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            # Handle non-JSON responses (some fund codes return text/plain)
            content_type = resp.headers.get('Content-Type', '')
            if 'json' not in content_type:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning(f"Non-JSON response for stock price {fund_code}")
                    return {}
            else:
                data = await resp.json()
            if data.get("rc") == 0 and data.get("data"):
                d = data["data"]
                # f43=current price, f44=high, f45=low, f46=open, f47=volume, f48=amount
                # f50=amplitude, f57=code, f58=name, f169=change, f170=change_rate
                price = d.get("f43", 0)
                change = d.get("f169", 0)
                change_rate = d.get("f170", 0)
                high = d.get("f44", 0)
                low = d.get("f45", 0)
                
                # Eastmoney push2 API for LOF/fund prices:
                # Price values are multiplied by 1000 (e.g., 750 = 0.750 yuan)
                # Change values are also multiplied by 1000 (e.g., -12 = -0.012)
                # Change rate values are multiplied by 100 (e.g., -157 = -1.57%)
                price_val = price / 1000
                change_val = change / 1000
                change_rate_val = change_rate / 100
                high_val = high / 1000
                low_val = low / 1000
                
                return {
                    "trade_price": round(price_val, 3),
                    "trade_price_change": round(change_val, 3),
                    "trade_price_change_rate": round(change_rate_val, 2),
                    "stock_name": d.get("f58", ""),
                    "high": round(high_val, 3),
                    "low": round(low_val, 3),
                    "volume": d.get("f47", 0),
                    "amount": d.get("f48", 0),
                }
    except Exception as e:
        logger.error(f"Error fetching stock price for {fund_code}: {e}")
    return {}


async def fetch_fund_holdings(session: aiohttp.ClientSession, fund_code: str) -> list:
    """Fetch fund top 10 domestic (A-share) holdings from eastmoney FundArchivesDatas API.
    
    Only returns A-share stocks (em_code starting with '0.' or '1.', e.g. 0.000001, 1.600519).
    Filters out all overseas stocks (US 105/106/107, HK 116, etc.) and non-numeric codes (AAPL).
    For QDII funds, this typically returns empty since their holdings are overseas.
    """
    try:
        url = "http://fundf10.eastmoney.com/FundArchivesDatas.aspx"
        params = {
            "type": "jjcc",
            "code": fund_code,
            "topline": "10",
            "year": "",
            "month": "",
            "rt": f"0.{int(datetime.now().timestamp()*1000)}"
        }
        async with session.get(url, params=params, headers=HEADERS_F10, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            text = await resp.text()
            # Parse HTML content from var apidata={ content:"...", ... }
            match = re.search(r'var apidata=\s*\{.*?content:\s*"(.*?)",\s*arryear', text, re.DOTALL)
            if not match:
                return []
            
            html_content = match.group(1)
            # Unescape the HTML content
            html_content = html_content.replace('\\n', '\n').replace('\\"', '"').replace('\\/', '/')
            
            soup = BeautifulSoup(html_content, "lxml")
            rows = soup.find_all("tr")
            
            holdings = []
            report_date = ""
            date_match = re.search(r'截止至：.*?>(.*?)<', text)
            if date_match:
                report_date = date_match.group(1)
            
            for row in rows[1:]:  # Skip header row
                tds = row.find_all("td")
                if len(tds) >= 7:
                    stock_code_link = tds[1].find("a")
                    stock_name_link = tds[2].find("a")
                    stock_code = stock_code_link.text.strip() if stock_code_link else tds[1].text.strip()
                    stock_name = stock_name_link.text.strip() if stock_name_link else tds[2].text.strip()
                    ratio_text = tds[6].text.strip().replace("%", "")
                    shares_text = tds[7].text.strip().replace(",", "") if len(tds) > 7 else "0"
                    value_text = tds[8].text.strip().replace(",", "") if len(tds) > 8 else "0"
                    
                    try:
                        ratio = float(ratio_text)
                    except ValueError:
                        ratio = 0.0
                    
                    try:
                        shares = float(shares_text)
                    except ValueError:
                        shares = 0.0
                    
                    try:
                        value = float(value_text)
                    except ValueError:
                        value = 0.0
                    
                    # Extract the eastmoney quote code from the link
                    em_code = ""
                    if stock_code_link and stock_code_link.get("href"):
                        href = stock_code_link["href"]
                        em_match = re.search(r'/r/(\d+\.\w+)', href)
                        if em_match:
                            em_code = em_match.group(1)
                    
                    # Only include A-share stocks (market codes 0.xxx for SZ, 1.xxx for SH)
                    # Filter out ALL overseas stocks: US (105/106/107), HK (116), non-numeric codes (AAPL)
                    if em_code:
                        market_prefix = em_code.split('.')[0]
                        if market_prefix not in ('0', '1'):
                            continue  # Skip non-A-share stocks
                    elif not stock_code.isdigit():
                        continue  # Skip non-numeric stock codes (US stocks without em_code link)

                    holdings.append({
                        "stock_code": stock_code,
                        "stock_name": stock_name,
                        "holding_ratio": ratio,
                        "shares": shares,
                        "market_value": value,
                        "report_date": report_date,
                        "em_code": em_code,
                    })
            
            return holdings
    except Exception as e:
        logger.error(f"Error fetching holdings for {fund_code}: {e}")
    return []


async def fetch_stock_change_rate(session: aiohttp.ClientSession, em_code: str) -> float:
    """Fetch a stock's real-time change rate using eastmoney push2 API.
    em_code format: market.code (e.g., 116.00700 for HK stocks, 1.600519 for SH, 0.000001 for SZ)
    For industry indices, use market code 100 (e.g., 100.HSCEI for Hang Seng CEI)
    """
    try:
        url = "http://push2delay.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": em_code,
            "fields": "f43,f170,f44,f45,f46,f47,f57,f58"
        }
        async with session.get(url, params=params, headers=HEADERS_QUOTE, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            # Handle non-JSON responses gracefully
            content_type = resp.headers.get('Content-Type', '')
            if 'json' not in content_type:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.error(f"Non-JSON response for {em_code}: {text[:100]}")
                    return 0.0
            else:
                data = await resp.json()
            
            if data.get("rc") == 0 and data.get("data"):
                change_rate = data["data"].get("f170", 0)
                # change rate is in basis points, e.g., -157 = -1.57%
                return change_rate / 100.0
            else:
                logger.warning(f"No data for {em_code}: rc={data.get('rc')}")
    except Exception as e:
        logger.error(f"Error fetching stock change for {em_code}: {e}")
    return 0.0


async def fetch_fund_purchase_status(session: aiohttp.ClientSession, fund_code: str) -> dict:
    """Fetch fund purchase/redeem status from the F10 fee page (more reliable)."""
    try:
        url = f"http://fundf10.eastmoney.com/jjfl_{fund_code}.html"
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            text = await resp.text()
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(text, "lxml")
            
            purchase_status = "未知"
            redeem_status = "未知"
            
            # Look for purchase/redeem status in the fee tables
            tables = soup.find_all("table")
            for table in tables:
                rows = table.find_all("tr")
                for row in rows:
                    cells = [td.text.strip() for td in row.find_all(["td", "th"])]
                    cell_text = " ".join(cells)
                    if "申购状态" in cell_text:
                        if "暂停申购" in cell_text:
                            purchase_status = "暂停"
                        elif "限大额" in cell_text:
                            purchase_status = "限大额"
                        elif "开放申购" in cell_text:
                            purchase_status = "开放"
                    if "赎回状态" in cell_text:
                        if "暂停赎回" in cell_text:
                            redeem_status = "暂停"
                        elif "开放赎回" in cell_text:
                            redeem_status = "开放"
            
            # Fallback: check the main fund page
            if purchase_status == "未知" or redeem_status == "未知":
                url2 = f"http://fund.eastmoney.com/{fund_code}.html"
                async with session.get(url2, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp2:
                    text2 = await resp2.text()
                    if purchase_status == "未知":
                        if "暂停申购" in text2 or "限制申购" in text2:
                            purchase_status = "暂停"
                        elif "限大额" in text2:
                            purchase_status = "限大额"
                        elif "开放申购" in text2:
                            purchase_status = "开放"
                    if redeem_status == "未知":
                        if "暂停赎回" in text2:
                            redeem_status = "暂停"
                        elif "开放赎回" in text2:
                            redeem_status = "开放"
            
            return {
                "purchase_status": purchase_status,
                "redeem_status": redeem_status,
                "yesterday_purchase_shares": 0,
            }
    except Exception as e:
        logger.error(f"Error fetching purchase status for {fund_code}: {e}")
    return {"purchase_status": "未知", "redeem_status": "未知", "yesterday_purchase_shares": 0}


async def fetch_fund_share_change(session: aiohttp.ClientSession, fund_code: str) -> dict:
    """Fetch latest quarterly share change data from EastMoney F10 (规模变动).
    
    Returns the most recent period's purchase/redeem shares (亿份) and total shares.
    Data is updated quarterly (not daily), but still provides useful context for
    the "昨日申购" column.
    
    Returns dict: {
        "date": "2026-03-31",
        "period_purchase": 77.65,  # 期间申购(亿份)
        "period_redeem": 74.93,    # 期间赎回(亿份)
        "total_shares": 404.14,    # 期末总份额(亿份)
        "yesterday_purchase_shares": 7765000000  # 期间申购(份), converted from 亿份
    }
    """
    try:
        url = "http://fundf10.eastmoney.com/FundArchivesDatas.aspx"
        params = {"type": "gmbd", "code": fund_code, "per": "1", "page": "1"}
        async with session.get(url, params=params, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            raw = await resp.read()
            text = raw.decode('utf-8', errors='replace')
            # Extract first data row from the HTML table
            rows = re.findall(
                r'<tr><td>(\d{4}-\d{2}-\d{2})</td>'
                r'<td[^>]*>([\d.]+)</td>'  # 期间申购(亿份)
                r'<td[^>]*>([\d.]+)</td>'  # 期间赎回(亿份)
                r'<td[^>]*>([\d.]+)</td>'  # 期末总份额(亿份)
                r'<td[^>]*>([\d.]+)</td>'  # 期末净资产(亿元)
                r'<td[^>]*>([^<]+)</td></tr>',
                text
            )
            if rows:
                row = rows[0]
                purchase_yi = float(row[1])  # 亿份
                # Convert 亿份 to 份 (shares) for storage
                purchase_shares = purchase_yi * 100000000
                return {
                    "date": row[0],
                    "period_purchase": purchase_yi,
                    "period_redeem": float(row[2]),
                    "total_shares": float(row[3]),
                    "yesterday_purchase_shares": purchase_shares,
                }
    except Exception as e:
        logger.debug(f"Error fetching share change for {fund_code}: {e}")
    return {}


async def fetch_index_info(session: aiohttp.ClientSession, index_code: str) -> dict:
    """Fetch index name and current value from eastmoney push2 API.
    index_code format: market.code (e.g., 0.399997 for CSI Liquor, 1.000001 for SSE Composite)
    Returns dict with index_name, index_value, change_rate.
    """
    try:
        url = "http://push2delay.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": index_code,
            "fields": "f43,f44,f45,f57,f58,f169,f170"
        }
        async with session.get(url, params=params, headers=HEADERS_QUOTE, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            content_type = resp.headers.get('Content-Type', '')
            if 'json' not in content_type:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    return {}
            else:
                data = await resp.json()

            if data.get("rc") == 0 and data.get("data"):
                d = data["data"]
                # For indices, f43 is the raw index value (not divided by 1000)
                # f170 is change rate in basis points (÷100 for percentage)
                raw_value = d.get("f43", 0)
                change_rate = d.get("f170", 0) / 100.0
                index_name = d.get("f58", "")
                index_code_raw = d.get("f57", "")

                # Detect if this is an index (large value) or a stock (small value)
                # Indices have values > 100 typically, stocks have prices < 1000 yuan
                # For LOF fund codes (6 digits), raw values are price*1000
                # For indices, raw values are the actual index points
                # We check: if the code is 6 digits AND value < 100000, treat as price (÷1000)
                # Otherwise treat as index value (no division)
                if len(index_code_raw) == 6 and raw_value < 100000:
                    index_value = raw_value / 1000
                else:
                    index_value = raw_value

                return {
                    "index_code": index_code,
                    "index_name": index_name,
                    "index_value": index_value,
                    "change_rate": round(change_rate, 2),
                }
    except Exception as e:
        logger.error(f"Error fetching index info for {index_code}: {e}")
    return {}


async def fetch_us_stock_change_rate(session: aiohttp.ClientSession, em_code: str) -> float:
    """Fetch a US stock's real-time change rate using eastmoney push2 API.
    em_code format: 105.AAPL for US stocks (market code 105).
    Returns change rate in percentage (e.g., -1.57 for -1.57%).
    """
    try:
        url = "http://push2delay.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": em_code,
            "fields": "f43,f44,f45,f46,f57,f58,f169,f170"
        }
        async with session.get(url, params=params, headers=HEADERS_QUOTE, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            content_type = resp.headers.get('Content-Type', '')
            if 'json' not in content_type:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning(f"Non-JSON response for US stock {em_code}")
                    return 0.0
            else:
                data = await resp.json()

            if data.get("rc") == 0 and data.get("data"):
                change_rate = data["data"].get("f170", 0)
                # US stock change rate is in basis points (÷100 for percentage)
                return change_rate / 100.0
            else:
                logger.warning(f"No data for US stock {em_code}")
    except Exception as e:
        logger.error(f"Error fetching US stock change for {em_code}: {e}")
    return 0.0


async def fetch_us_index_info(session: aiohttp.ClientSession, us_index_code: str) -> dict:
    """Fetch US index/commodity name and change rate from eastmoney push2 API.
    
    Supported secid formats:
    - Stock indices: 100.NDX (NASDAQ 100), 100.DJIA (Dow Jones), 100.SPX (S&P 500), 100.HSCEI (Hang Seng CEI)
    - Commodity futures: 101.GC00Y (COMEX Gold), 102.CL00Y (NYMEX Crude Oil), 101.SI00Y (COMEX Silver)
    - US ETFs: 105.NFTY (First Trust India NIFTY 50), 107.SLV (iShares Silver ETF), etc.
    
    Returns dict with index_name, index_value, change_rate.
    During non-trading hours, may return 0% change.
    """
    try:
        url = "http://push2delay.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": us_index_code,
            "fields": "f43,f44,f45,f57,f58,f169,f170"
        }
        async with session.get(url, params=params, headers=HEADERS_QUOTE, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            content_type = resp.headers.get('Content-Type', '')
            if 'json' not in content_type:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    return {}
            else:
                data = await resp.json()

            if data.get("rc") == 0 and data.get("data"):
                d = data["data"]
                raw_value = d.get("f43", 0)
                change_rate = d.get("f170", 0) / 100.0
                index_name = d.get("f58", "")
                index_code_raw = d.get("f57", "")

                # Determine value scaling based on market code
                market_code = us_index_code.split(".")[0] if "." in us_index_code else ""
                
                if market_code == "100":
                    # Stock indices (NASDAQ, S&P, Dow, HSCEI, etc.)
                    # Values are typically large (1000+), no division needed
                    if raw_value < 100000 and len(index_code_raw) <= 6:
                        index_value = raw_value / 1000
                    else:
                        index_value = raw_value
                elif market_code in ("101", "102", "112"):
                    # Commodity futures (COMEX Gold 101.GC00Y, NYMEX Oil 102.CL00Y, Brent 112.B00Y)
                    # Values are price * 1000 (e.g., 46062 = $46.062 for gold per oz * 1000... actually 3300620 = 3300.62)
                    # These use the same /1000 convention as stocks
                    index_value = raw_value / 1000
                elif market_code in ("105", "107"):
                    # US ETFs (105.NFTY, 107.SLV, etc.)
                    # Prices are in cents (value * 100)
                    index_value = raw_value / 100
                else:
                    # Unknown market code, try heuristic
                    if raw_value < 100000 and len(index_code_raw) <= 6:
                        index_value = raw_value / 1000
                    else:
                        index_value = raw_value

                return {
                    "index_code": us_index_code,
                    "index_name": index_name,
                    "index_value": round(index_value, 2),
                    "change_rate": round(change_rate, 2),
                }
    except Exception as e:
        logger.error(f"Error fetching US index info for {us_index_code}: {e}")
    return {}


async def fetch_overseas_holdings(session: aiohttp.ClientSession, fund_code: str) -> list:
    """Fetch QDII fund's overseas (non-A-share) holdings from eastmoney F10 page.
    
    Includes US stocks (market 105/106/107), HK stocks (market 116), and any non-numeric codes.
    Excludes A-share stocks (market 0/1) which belong in domestic holdings.
    For commodity/futures funds, the API may return no data at all.
    Only parses the FIRST table (current reporting period) to avoid mixing data from different periods.
    """
    try:
        url = "http://fundf10.eastmoney.com/FundArchivesDatas.aspx"
        params = {
            "type": "jjcc",
            "code": fund_code,
            "topline": "10",
            "year": "",
            "month": "",
            "rt": f"0.{int(datetime.now().timestamp()*1000)}"
        }
        async with session.get(url, params=params, headers=HEADERS_F10, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            text = await resp.text()
            match = re.search(r'var apidata=\s*\{.*?content:\s*"(.*?)",\s*arryear', text, re.DOTALL)
            if not match:
                return []

            html_content = match.group(1)
            html_content = html_content.replace('\\n', '\n').replace('\\"', '"').replace('\\/', '/')

            soup = BeautifulSoup(html_content, "lxml")
            # Only parse the FIRST table (current reporting period)
            # Subsequent tables are for previous periods with different column structures
            first_table = soup.find("table")
            if not first_table:
                return []
            rows = first_table.find_all("tr")

            holdings = []
            report_date = ""
            date_match = re.search(r'截止至：.*?>(.*?)<', text)
            if date_match:
                report_date = date_match.group(1)

            for row in rows[1:]:  # Skip header
                tds = row.find_all("td")
                if len(tds) >= 7:
                    stock_code_link = tds[1].find("a")
                    stock_name_link = tds[2].find("a")
                    stock_code = stock_code_link.text.strip() if stock_code_link else tds[1].text.strip()
                    stock_name = stock_name_link.text.strip() if stock_name_link else tds[2].text.strip()
                    ratio_text = tds[6].text.strip().replace("%", "")

                    try:
                        ratio = float(ratio_text)
                    except ValueError:
                        ratio = 0.0

                    # Extract the eastmoney quote code from the link
                    em_code = ""
                    if stock_code_link and stock_code_link.get("href"):
                        href = stock_code_link["href"]
                        # For US stocks: /r/105.AAPL or /us/105.AAPL
                        em_match = re.search(r'/r/(\d+\.\w+)', href)
                        if em_match:
                            em_code = em_match.group(1)
                        else:
                            # Try alternative format for US stocks
                            us_match = re.search(r'/(\d+\.\w+)', href)
                            if us_match:
                                em_code = us_match.group(1)

                    # Determine if this is an overseas (non-A-share) holding
                    # Overseas: US stocks (105/106/107), HK stocks (116), non-numeric codes (AAPL)
                    # Domestic: A-share (0/1)
                    is_overseas = False
                    if em_code:
                        market_prefix = em_code.split('.')[0]
                        is_overseas = market_prefix not in ('0', '1')
                    else:
                        # No em_code found - check if stock code is non-numeric (US stock)
                        is_overseas = not stock_code.isdigit()

                    if is_overseas:
                        # Auto-construct em_code for US stocks without one
                        if not em_code and not stock_code.isdigit():
                            em_code = f"105.{stock_code}"

                        holdings.append({
                            "stock_code": stock_code,
                            "stock_name": stock_name,
                            "holding_ratio": ratio,
                            "em_code": em_code,
                            "report_date": report_date,
                        })

            return holdings
    except Exception as e:
        logger.error(f"Error fetching overseas holdings for {fund_code}: {e}")
    return []


async def fetch_fund_nav_from_lsjz(session: aiohttp.ClientSession, fund_code: str) -> dict:
    """Fetch latest NAV from eastmoney F10 historical NAV page.
    
    This is a fallback for QDII/overseas funds that don't have real-time
    estimate data on fundgz.1234567.com.cn (which only covers domestic funds).
    
    Returns dict with: nav, nav_date, daily_change_rate
    """
    try:
        url = "http://fund.eastmoney.com/f10/F10DataApi.aspx"
        params = {
            "type": "lsjz",
            "code": fund_code,
            "per": "1",
            "page": "1",
        }
        async with session.get(url, params=params, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            text = await resp.text()
            # The API returns JavaScript: var apidata={ content:"<table>...</table>", records:N, ...}
            # We need to extract the HTML content from the JS string
            content_match = re.search(r'content:"(.*?)",records', text, re.DOTALL)
            if not content_match:
                return {}
            
            html_content = content_match.group(1)
            # Unescape the HTML content
            html_content = html_content.replace('\\n', '\n').replace('\\"', '"').replace('\\/', '/')
            
            soup = BeautifulSoup(html_content, "lxml")
            table = soup.find("table")
            if table:
                rows = table.find_all("tr")
                if len(rows) >= 2:
                    cells = [td.text.strip() for td in rows[1].find_all("td")]
                    if len(cells) >= 4:
                        nav_date = cells[0]
                        nav = float(cells[1])
                        # Parse daily growth rate (e.g., "-0.91%" -> -0.91)
                        growth_str = cells[3].replace("%", "").strip()
                        try:
                            daily_change_rate = float(growth_str)
                        except ValueError:
                            daily_change_rate = 0.0

                        return {
                            "nav": nav,
                            "nav_date": nav_date,
                            "daily_change_rate": daily_change_rate,
                        }
    except Exception as e:
        logger.error(f"Error fetching NAV from lsjz for {fund_code}: {e}")
    return {}


async def fetch_fund_info(session: aiohttp.ClientSession, fund_code: str) -> dict:
    """Fetch basic fund info to verify fund exists and get name.
    Falls back to the lsjz API + fund detail page for QDII funds not on fundgz.
    """
    try:
        url = f"http://fundgz.1234567.com.cn/js/{fund_code}.js"
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            text = await resp.text()
            if not text or 'jsonpgz' not in text or text.strip() == 'jsonpgz();':
                # Fallback: try lsjz API (works for QDII/overseas funds)
                nav_data = await fetch_fund_nav_from_lsjz(session, fund_code)
                if nav_data:
                    # Also fetch fund name from the fund detail page
                    fund_name = await _fetch_fund_name_from_page(session, fund_code)
                    return {
                        "fund_code": fund_code,
                        "fund_name": fund_name,
                    }
                logger.warning(f"Fund {fund_code} not found on any API")
                return {}
            data = parse_jsonpgz(text)
            if data:
                return {
                    "fund_code": data.get("fundcode", fund_code),
                    "fund_name": data.get("name", ""),
                }
    except Exception as e:
        logger.error(f"Error fetching fund info for {fund_code}: {e}")
        # Fallback: try lsjz API
        try:
            nav_data = await fetch_fund_nav_from_lsjz(session, fund_code)
            if nav_data:
                fund_name = await _fetch_fund_name_from_page(session, fund_code)
                return {
                    "fund_code": fund_code,
                    "fund_name": fund_name,
                }
        except Exception:
            pass
    return {}


async def _fetch_fund_name_from_page(session: aiohttp.ClientSession, fund_code: str) -> str:
    """Fetch fund name from the eastmoney fund detail page as a fallback."""
    try:
        url = f"http://fund.eastmoney.com/{fund_code}.html"
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            text = await resp.text()
            soup = BeautifulSoup(text, "lxml")
            # Try the dedicated fund name span first
            name_span = soup.find("span", class_="funCur-FundName")
            if name_span:
                return name_span.text.strip()
            # Fallback: extract from title
            title = soup.find("title")
            if title:
                name = title.text.split("(")[0].split("（")[0].strip()
                if name:
                    return name
    except Exception as e:
        logger.debug(f"Could not fetch fund name from page for {fund_code}: {e}")
    return ""


async def fetch_all_fund_data(fund_code: str, market: str = "0") -> dict:
    """Fetch all data for a single fund in one pass."""
    result = {
        "fund_code": fund_code,
        "nav": 0,
        "nav_date": "",
        "estimated_nav": 0,
        "estimated_change_rate": 0,
        "trade_price": 0,
        "trade_price_change": 0,
        "premium_rate": 0,
        "purchase_status": "未知",
        "redeem_status": "未知",
        "yesterday_purchase_shares": 0,
        "holdings": [],
    }
    
    async with aiohttp.ClientSession() as session:
        # Fetch estimate and NAV
        est_data = await fetch_fund_estimate(session, fund_code)
        if est_data:
            result["nav"] = est_data.get("nav", 0)
            result["nav_date"] = est_data.get("nav_date", "")
            result["estimated_nav"] = est_data.get("estimated_nav", 0)
            result["estimated_change_rate"] = est_data.get("estimated_change_rate", 0)
        else:
            # Fallback for QDII/overseas funds
            nav_data = await fetch_fund_nav_from_lsjz(session, fund_code)
            if nav_data:
                result["nav"] = nav_data.get("nav", 0)
                result["nav_date"] = nav_data.get("nav_date", "")
                result["estimated_nav"] = nav_data.get("nav", 0)
                result["estimated_change_rate"] = nav_data.get("daily_change_rate", 0)
        
        # Fetch trading price
        price_data = await fetch_stock_price(session, fund_code, market)
        if price_data:
            result["trade_price"] = price_data.get("trade_price", 0)
            result["trade_price_change"] = price_data.get("trade_price_change", 0)
        
        # Fetch holdings
        holdings = await fetch_fund_holdings(session, fund_code)
        result["holdings"] = holdings
        
        # Fetch purchase status
        status = await fetch_fund_purchase_status(session, fund_code)
        result["purchase_status"] = status.get("purchase_status", "未知")
        result["redeem_status"] = status.get("redeem_status", "未知")
        result["yesterday_purchase_shares"] = status.get("yesterday_purchase_shares", 0)
        
        # Calculate premium rate: (trade_price - nav) / nav * 100
        if result["nav"] > 0 and result["trade_price"] > 0:
            result["premium_rate"] = round((result["trade_price"] - result["nav"]) / result["nav"] * 100, 2)
    
    return result
