"""A small client for Alpaca's REST API: trading, market data, the market
screener and news. Alpaca is a US stock broker with a free paper-trading
account and free market data (the IEX feed), so the whole bot can be tried
without spending anything.

Docs: https://docs.alpaca.markets/reference
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

DATA_URL = "https://data.alpaca.markets"


class AlpacaError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Alpaca:
    def __init__(self, key, secret, trading_url, feed="iex", opener=None):
        self.trading_url = trading_url.rstrip("/")
        self.feed = feed
        self.headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                        "accept": "application/json"}
        self._open = opener or urllib.request.urlopen

    # --- plumbing ---------------------------------------------------------

    def _request(self, method, base, path, params=None, body=None):
        url = f"{base}{path}"
        if params:
            params = {k: v for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = dict(self.headers)
        if data is not None:
            headers["content-type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._open(request, timeout=20) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise AlpacaError(f"Alpaca {method} {path} failed (HTTP {e.code}): {detail}",
                              status=e.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise AlpacaError(f"cannot reach Alpaca ({path}): {e}") from None
        return json.loads(text) if text.strip() else None

    def _trade(self, method, path, params=None, body=None):
        return self._request(method, self.trading_url, path, params, body)

    def _data(self, path, params=None):
        return self._request("GET", DATA_URL, path, params)

    # --- account and orders ----------------------------------------------

    def account(self):
        return self._trade("GET", "/v2/account")

    def clock(self):
        return self._trade("GET", "/v2/clock")

    def calendar_day(self, date):
        """Today's trading hours, e.g. {"date": "2026-11-05", "open": "09:30", "close": "16:00"}.
        None on weekends and holidays."""
        days = self._trade("GET", "/v2/calendar", {"start": date, "end": date}) or []
        return days[0] if days and days[0].get("date") == date else None

    def asset(self, symbol):
        return self._trade("GET", f"/v2/assets/{urllib.parse.quote(symbol)}")

    def positions(self):
        return self._trade("GET", "/v2/positions") or []

    def open_orders(self):
        return self._trade("GET", "/v2/orders", {"status": "open", "nested": "true", "limit": 500}) or []

    def closed_orders(self, symbol, after=None):
        """Recent finished orders for one symbol, newest first (legs included)."""
        return self._trade("GET", "/v2/orders", {"status": "closed", "symbols": symbol,
                                                 "after": after, "nested": "true",
                                                 "direction": "desc", "limit": 50}) or []

    def bracket_buy(self, symbol, qty, stop_price, take_profit_price, time_in_force="day"):
        """Buy at market with a stop-loss and a take-profit attached. When one of
        the two fills, Alpaca cancels the other."""
        return self._trade("POST", "/v2/orders", body={
            "symbol": symbol, "qty": str(int(qty)), "side": "buy", "type": "market",
            "time_in_force": time_in_force, "order_class": "bracket",
            "take_profit": {"limit_price": f"{take_profit_price:.2f}"},
            "stop_loss": {"stop_price": f"{stop_price:.2f}"},
        })

    def close_position(self, symbol):
        """Sell a whole position at market (cancels its stop/take-profit first)."""
        return self._trade("DELETE", f"/v2/positions/{urllib.parse.quote(symbol)}",
                           {"cancel_orders": "true"})

    def close_all(self):
        return self._trade("DELETE", "/v2/positions", {"cancel_orders": "true"})

    # --- market data ------------------------------------------------------

    def movers(self, top=50):
        """Today's biggest percentage gainers and losers."""
        return self._data("/v1beta1/screener/stocks/movers", {"top": top}) or {}

    def most_active(self, top=50):
        data = self._data("/v1beta1/screener/stocks/most-actives", {"by": "volume", "top": top}) or {}
        return data.get("most_actives", [])

    def snapshots(self, symbols):
        """Latest trade, quote, today's bar and yesterday's bar for each symbol."""
        result = {}
        symbols = list(symbols)
        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i + 100]
            data = self._data("/v2/stocks/snapshots", {"symbols": ",".join(chunk), "feed": self.feed})
            result.update({k: v for k, v in (data or {}).items() if v})
        return result

    def bars(self, symbols, timeframe, start, limit=10000):
        """Price bars per symbol, oldest first: {"AAPL": [{"t","o","h","l","c","v","vw"}, ...]}."""
        out, token = {}, None
        while True:
            data = self._data("/v2/stocks/bars", {
                "symbols": ",".join(symbols), "timeframe": timeframe, "start": start,
                "feed": self.feed, "limit": limit, "page_token": token, "adjustment": "raw",
            }) or {}
            for sym, rows in (data.get("bars") or {}).items():
                out.setdefault(sym, []).extend(rows)
            token = data.get("next_page_token")
            if not token:
                return out

    def news(self, start, symbols=None, limit=50, pages=4):
        """Recent news articles, newest first."""
        articles, token = [], None
        for _ in range(pages):
            data = self._data("/v1beta1/news", {
                "start": start, "symbols": ",".join(symbols) if symbols else None,
                "limit": limit, "sort": "desc", "include_content": "false", "page_token": token,
            }) or {}
            articles.extend(data.get("news") or [])
            token = data.get("next_page_token")
            if not token:
                break
        return articles
