import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_新版订单簿支持INR转CNY(monkeypatch):
    from steam import market_orders

    html = (
        'window.SSR.renderContext=JSON.parse("'
        '{\\"queryData\\":\\"{\\\\\\"queries\\\\\\":[{\\\\\\"state\\\\\\":'
        '{\\\\\\"data\\\\\\":{\\\\\\"eCurrency\\\\\\":24,'
        '\\\\\\"amtMinSellOrder\\\\\\":6091,'
        '\\\\\\"rgCompactSellOrders\\\\\\":[6091,2,6516,4]}}}]}\\",'
        '\\"localizationSettings\\":{}}'
        '");'
    )
    monkeypatch.setattr(market_orders, "_load_exchange_rates", lambda: {"INR": 0.0709})

    result, error = market_orders._extract_ssr_orderbook_cny(html)

    assert error is None
    assert result["lowest_price"] == 4.32
    assert result["sell_orders"] == [(4.32, 2), (4.62, 4)]


def test_新版订单簿缺少非CNY汇率会报清晰原因(monkeypatch):
    from steam import market_orders

    html = (
        'window.SSR.renderContext=JSON.parse("'
        '{\\"queryData\\":\\"{\\\\\\"queries\\\\\\":[{\\\\\\"state\\\\\\":'
        '{\\\\\\"data\\\\\\":{\\\\\\"eCurrency\\\\\\":24,'
        '\\\\\\"rgCompactSellOrders\\\\\\":[6091,2]}}}]}\\",'
        '\\"localizationSettings\\":{}}'
        '");'
    )
    monkeypatch.setattr(market_orders, "_load_exchange_rates", lambda: {})

    result, error = market_orders._extract_ssr_orderbook_cny(html)

    assert result is None
    assert "INR" in error
    assert "exchange_rate.json" in error


def test_汇率文件里的Steam市场币种都有ECurrency映射():
    from steam import market_orders

    rate_codes = {
        "USD", "INR", "RUB", "HKD", "EUR", "KZT", "UAH", "TRY", "ARS",
        "VND", "IDR", "BRL", "CLP", "JPY", "PHP",
    }
    steam_codes = set(market_orders._STEAM_CURRENCY_CODES.values())

    assert rate_codes <= steam_codes


def test_汇率文件包含的非Steam本地市场币种不会误映射():
    from steam import market_orders

    steam_codes = set(market_orders._STEAM_CURRENCY_CODES.values())

    assert "PKR" not in steam_codes
    assert "AZN" not in steam_codes


def test_新版订单簿按querykey精确匹配请求的market_hash_name():
    import json

    from steam import market_orders

    ctx = {
        "queryData": json.dumps(
            {
                "queries": [
                    {
                        "queryKey": ["market", "orderbook", 730, "AK-47 | Redline (Factory New)"],
                        "state": {
                            "data": {
                                "eCurrency": 23,
                                "amtMinSellOrder": 999999,
                                "rgCompactSellOrders": [999999, 1],
                            }
                        },
                    },
                    {
                        "queryKey": ["market", "orderbook", 730, "AK-47 | Redline (Minimal Wear)"],
                        "state": {
                            "data": {
                                "eCurrency": 23,
                                "amtMinSellOrder": 158684,
                                "rgCompactSellOrders": [158684, 1, 158888, 1],
                            }
                        },
                    },
                ]
            }
        ),
        "localizationSettings": {},
    }
    html = f"window.SSR.renderContext=JSON.parse({json.dumps(json.dumps(ctx, ensure_ascii=False))});"

    result, error = market_orders._extract_ssr_orderbook_cny(
        html,
        market_hash_name="AK-47 | Redline (Minimal Wear)",
    )

    assert error is None
    assert result["lowest_price"] == 1586.84
    assert result["sell_orders"] == [(1586.84, 1), (1588.88, 1)]


def test_get_sell_orders_cny_优先使用新版ssr而不是旧item_nameid缓存(monkeypatch):
    from steam import market_orders

    market_orders.clear_caches()
    monkeypatch.setattr(market_orders, "db_get_item_nameid", lambda name: "stale-id")
    monkeypatch.setattr(
        market_orders,
        "_fetch_ssr_sell_orders_cny",
        lambda *args, **kwargs: ({"lowest_price": 12.34, "sell_orders": [(12.34, 2)]}, None),
    )
    monkeypatch.setattr(
        market_orders,
        "fetch_item_orders_histogram",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("histogram should not be used")),
    )

    result = market_orders.get_sell_orders_cny(object(), "AK-47 | Redline (Minimal Wear)", use_cache=False)

    assert result == {"lowest_price": 12.34, "sell_orders": [(12.34, 2)]}
