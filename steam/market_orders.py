import re
import threading
import time
import logging
import math
from typing import Any, List, Optional, Tuple

from steam.client import build_listing_url
from utils.delay import jittered_sleep
from utils.proxy_manager import get_proxy_manager
from app.database import db_get_item_nameid, db_set_item_nameid

logger = logging.getLogger(__name__)

_ITEM_NAMEID_TTL = 300
_SELL_ORDERS_TTL = 60
_item_nameid_cache: dict = {}
_sell_orders_cache: dict = {}
_item_nameid_cache_lock = threading.Lock()
_sell_orders_cache_lock = threading.Lock()
def clear_caches() -> None:
    with _item_nameid_cache_lock:
        _item_nameid_cache.clear()
    with _sell_orders_cache_lock:
        _sell_orders_cache.clear()
CURRENCY_CNY = 23
_ITEM_NAMEID_PATTERNS = [
    re.compile(r"Market_LoadOrderSpread\s*\(\s*(\d+)\s*\)", re.I),
    re.compile(r"item_nameid['\"]?\s*[:=]\s*['\"]?(\d+)", re.I),
]
def _format_request_error(prefix: str, exc: Exception) -> str:
    detail = str(exc).strip()
    if len(detail) > 120:
        detail = detail[:117] + "..."
    return f"{prefix}: {type(exc).__name__}" + (f" - {detail}" if detail else "")

def _http_error_reason(where: str, status_code: int) -> str:
    if status_code == 429:
        return f"{where} HTTP 429（Steam 限流）"
    if status_code == 403:
        return f"{where} HTTP 403（访问被拒绝，可能是 Cookie 失效、地区或 IP 风控）"
    if status_code in (500, 502, 503, 504):
        return f"{where} HTTP {status_code}（Steam 服务端或网络网关异常）"
    return f"{where} HTTP {status_code}"

def _extract_item_nameid(html: str) -> Optional[str]:
    for pat in _ITEM_NAMEID_PATTERNS:
        m = pat.search(html)
        if m:
            return m.group(1)
    return None
def get_item_nameid(
    session,
    market_hash_name: str,
    app_id: int = 730,
    *,
    timeout: int = 15,
    use_cache: bool = True,
    return_error: bool = False,
):
    key = (market_hash_name.strip(), app_id)
    db_nameid = db_get_item_nameid(key[0])
    if db_nameid:
        return (db_nameid, None) if return_error else db_nameid
    if use_cache:
        with _item_nameid_cache_lock:
            entry = _item_nameid_cache.get(key)
        if entry and time.time() < entry[1]:
            return (entry[0], None) if return_error else entry[0]
    url = build_listing_url(market_hash_name, app_id)
    headers = {
        "Accept": "*/*",
        "Referer": url,
    }
    pm = get_proxy_manager()
    last_error = ""
    for attempt in range(3):
        failed = (attempt > 0)
        proxies = pm.get_proxies_for_request(failed=failed)
        try:
            r = session.get(url, headers=headers, timeout=timeout, proxies=proxies)
            if r.status_code == 200:
                nameid = _extract_item_nameid(r.text)
                if nameid:
                    db_set_item_nameid(key[0], nameid)
                    if use_cache:
                        with _item_nameid_cache_lock:
                            _item_nameid_cache[key] = (nameid, time.time() + _ITEM_NAMEID_TTL)
                    return (nameid, None) if return_error else nameid
                last_error = "Steam 市场页面未解析到 item_nameid（可能物品名不正确、页面被风控或地区不可访问）"
                break
            last_error = _http_error_reason("Steam 市场页面", r.status_code)
        except Exception as e:
            last_error = _format_request_error("Steam 市场页面请求异常", e)
            logger.debug("获取item_nameid失败 (attempt=%d/3) proxies=%s, error=%s", attempt+1, proxies, type(e).__name__)
        
        if attempt < 2:
            jittered_sleep(1.0)

    if return_error:
        return None, last_error or "无法打开 Steam 市场页面"
    return None
_cb_lock = threading.Lock()  
_cb_fail_streak = 0          
_cb_open_until = 0.0         
_CB_FAIL_THRESHOLD = 5       
_CB_COOLDOWN_SEC = 300       

def fetch_item_orders_histogram(
    session,
    item_nameid: str,
    *,
    country: str = "CN",
    language: str = "english",
    currency: int = CURRENCY_CNY,
    timeout: int = 15,
    return_error: bool = False,
):
    global _cb_fail_streak, _cb_open_until
    with _cb_lock:
        if time.time() < _cb_open_until:
            remaining = max(1, int(math.ceil(_cb_open_until - time.time())))
            reason = f"Steam 市场请求熔断中，约 {remaining} 秒后重试（之前连续失败）"
            return (None, reason) if return_error else None
    url = "https://steamcommunity.com/market/itemordershistogram"
    params = {
        "country": country,
        "language": language,
        "currency": currency,
        "item_nameid": item_nameid,
        "no_render": "1",
        "two_factor_hash": "",
    }
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://steamcommunity.com/market/",
    }
    pm = get_proxy_manager()
    any_success = False
    last_error = ""
    
    for attempt in range(3):
        if attempt == 0:
            effective_proxies = None                                    
        else:
            effective_proxies = pm.get_proxies_for_request(failed=True) 
            
        try:
            r = session.get(
                url, params=params, headers=headers,
                timeout=timeout, proxies=effective_proxies, verify=False,
            )
            if r.status_code == 200:
                any_success = True
                data = r.json()
                if isinstance(data, dict) and data.get("success") == 1:
                    with _cb_lock:
                        _cb_fail_streak = 0  
                    return (data, None) if return_error else data

                if isinstance(data, dict):
                    msg = data.get("message") or data.get("error") or ""
                    suffix = f"，返回: {msg}" if msg else ""
                    last_error = f"Steam 直方图接口返回 success={data.get('success')}{suffix}"
                else:
                    last_error = "Steam 直方图接口返回非 JSON 对象"
                logger.debug("直方图 success非1 resp=%s", str(data)[:150])
            else:
                last_error = _http_error_reason("Steam 直方图接口", r.status_code)
                logger.debug("直方图 HTTP %s (attempt=%d)", r.status_code, attempt+1)
        except Exception as e:
            last_error = _format_request_error("Steam 直方图请求异常", e)
            logger.debug("直方图失败 (attempt=%d/3) proxy=%s err=%s: %s", attempt+1, effective_proxies is not None, type(e).__name__, str(e)[:60])
            
        if attempt < 2:
            jittered_sleep(1.0)
    with _cb_lock:
        _cb_fail_streak += 1
        if _cb_fail_streak >= _CB_FAIL_THRESHOLD:
            _cb_open_until = time.time() + _CB_COOLDOWN_SEC
            streak_snap = _cb_fail_streak
            _cb_fail_streak = 0
        else:
            streak_snap = None
    if streak_snap is not None:
        last_error = f"Steam 市场连续 {streak_snap} 轮全部失败，已熔断 {_CB_COOLDOWN_SEC // 60} 分钟，请确认加速器/代理是否正常"
        logger.warning(
            "Steam市场连续 %d 轮全部失败，熔断 %d 分钟。请确认加速器/代理是否正常。",
            streak_snap, _CB_COOLDOWN_SEC // 60
        )

    if return_error:
        if not last_error and not any_success:
            last_error = "Steam 直方图接口无响应"
        return None, last_error or "Steam 直方图接口返回空数据"
    return None
def cents_to_yuan(cents: int) -> float:
    return cents / 100.0
def _parse_sell_order_graph(raw: Any) -> List[Tuple[float, int]]:
    if not isinstance(raw, list):
        return []
    out: List[Tuple[float, int]] = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            price = float(row[0])
            volume = int(row[1])
            out.append((price, volume))
        except (ValueError, TypeError):
            continue
    return out
def get_sell_orders_cny(
    session,
    market_hash_name: str,
    app_id: int = 730,
    *,
    country: str = "CN",
    language: str = "english",
    request_delay: float = 1.0,
    use_cache: bool = True,
    return_error: bool = False,
):
    key = (market_hash_name.strip(), app_id)
    if use_cache:
        with _sell_orders_cache_lock:
            entry = _sell_orders_cache.get(key)
        if entry and time.time() < entry[1]:
            return (entry[0], None) if return_error else entry[0]
    item_nameid, nameid_error = get_item_nameid(
        session, market_hash_name, app_id, return_error=True
    )
    if not item_nameid:
        return (None, nameid_error or "无法获取 Steam item_nameid") if return_error else None
    if request_delay > 0:
        jittered_sleep(request_delay)
    data, histogram_error = fetch_item_orders_histogram(
        session,
        item_nameid,
        country=country,
        language=language,
        currency=CURRENCY_CNY,
        return_error=True,
    )
    if not data:
        return (None, histogram_error or "无法获取 Steam 直方图数据") if return_error else None
    raw_lowest = data.get("lowest_sell_order")
    lowest_price: Optional[float] = None
    if raw_lowest is not None:
        try:
            lowest_price = cents_to_yuan(int(raw_lowest))
        except (ValueError, TypeError):
            pass
    sell_orders = _parse_sell_order_graph(data.get("sell_order_graph", []))
    result = {"lowest_price": lowest_price, "sell_orders": sell_orders}
    if use_cache:
        with _sell_orders_cache_lock:
            _sell_orders_cache[key] = (result, time.time() + _SELL_ORDERS_TTL)
    if return_error:
        if not sell_orders:
            return result, "Steam 返回空卖单图（可能当前无寄售，或接口被限制返回了不完整数据）"
        if lowest_price is None:
            return result, "Steam 返回了卖单图，但 lowest_sell_order 无法解析"
        return result, None
    return result
STEAM_MIN_PRICE = 0.03
def _get_dynamic_thresholds(current_price: float) -> Tuple[float, float]:
    if current_price < 5.0:
        return 0.10, 0.08
    if current_price < 20.0:
        return 0.30, 0.05
    if current_price < 100.0:
        return 1.0, 0.03
    if current_price < 500.0:
        return 5.0, 0.02
    return 10.0, 0.015
def compute_smart_list_price(
    sell_orders: List[Tuple[float, int]],
    *,
    wall_volume_threshold: int = 20,
    max_ignore_volume: int = 4,
    min_lowest_tier_volume: int = 3,
    min_step: float = 0.01,
    min_floor_price: float = STEAM_MIN_PRICE,
    offset: float = 0.0,
) -> Tuple[Optional[float], str]:
    if not sell_orders:
        return None, "无卖单数据"
    sell_orders = sorted(sell_orders, key=lambda x: x[0])
    while len(sell_orders) >= 2 and sell_orders[0][1] <= min_lowest_tier_volume:
        sell_orders = sell_orders[1:]
    if not sell_orders:
        return None, "无卖单数据"
    wall_index = len(sell_orders) - 1
    cumulative = 0
    for i, (price, count) in enumerate(sell_orders):
        cumulative += count
        if cumulative >= wall_volume_threshold:
            wall_index = i
            break
    analysis_scope = sell_orders[: wall_index + 1]
    if len(analysis_scope) < 2:
        target = analysis_scope[0][0] - min_step
        final = max(min_floor_price, target + offset)
        return round(final, 2), "单档无断层"
    final_price = analysis_scope[0][0] - min_step
    reason = "常规压价"
    current_ignore_vol = 0
    for i in range(len(analysis_scope) - 1):
        p_curr, c_curr = analysis_scope[i]
        p_next, _ = analysis_scope[i + 1]
        current_ignore_vol += c_curr
        if current_ignore_vol > max_ignore_volume:
            reason = "阻挡量超阈值停"
            break
        gap_abs, gap_rel = _get_dynamic_thresholds(p_curr)
        diff = p_next - p_curr
        threshold = max(gap_abs, p_curr * gap_rel)
        if diff > threshold:
            final_price = p_next - min_step
            reason = f"断层跳跃({p_curr:.2f}→{p_next:.2f})"
    final = max(min_floor_price, final_price + offset)
    return round(final, 2), reason
def get_lowest_sell_price_cny(
    session,
    market_hash_name: str,
    app_id: int = 730,
    *,
    country: str = "CN",
    language: str = "english",
    request_delay: float = 1.0,
    use_cache: bool = True,
) -> Optional[float]:
    result = get_sell_orders_cny(
        session,
        market_hash_name,
        app_id,
        country=country,
        language=language,
        request_delay=request_delay,
        use_cache=use_cache,
    )
    return result.get("lowest_price") if result else None
