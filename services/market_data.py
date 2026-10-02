"""Market-data providers, session clock, and a persistence seam.

Yahoo Finance is the connected source. It is delayed historical data, not a
live broker stream. Zerodha, Upstox, Angel One, Dhan, and Fyers stay
unconnected until credentials exist. This module never invents prices.
"""
from datetime import datetime, time
from threading import Lock
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
LIVE = "LIVE"
DELAYED = "DELAYED"
HISTORICAL = "HISTORICAL"
UNAVAILABLE = "UNAVAILABLE"
STALE = "STALE"
NOT_CONNECTED = "NOT_CONNECTED"

CANDLE_TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d")
INTERVALS = {
    "1m": pd.Timedelta(minutes=1),
    "3m": pd.Timedelta(minutes=3),
    "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15),
    "30m": pd.Timedelta(minutes=30),
    "1h": pd.Timedelta(hours=1),
    "4h": pd.Timedelta(hours=4),
}
BROKER_NAMES = ("zerodha", "upstox", "angelone", "dhan", "fyers")
STREAM_NOTE = "No live broker stream is connected. Quotes from Yahoo Finance are delayed."


def now_ist(moment=None):
    if moment is None:
        return datetime.now(IST)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def market_status(moment=None, exchange="NSE"):
    """NSE and BSE cash session in India time. Holidays are not a separate calendar."""
    exchange = str(exchange or "NSE").upper()
    if exchange not in {"NSE", "BSE", "NIFTY", "BANKNIFTY"}:
        return "UNKNOWN"
    current = now_ist(moment)
    if current.weekday() >= 5:
        return "CLOSED"
    clock = current.time()
    if clock < time(9, 0):
        return "CLOSED"
    if clock < time(9, 15):
        return "PRE-MARKET"
    if clock < time(15, 30):
        return "OPEN"
    if clock < time(16, 0):
        return "POST-MARKET"
    return "CLOSED"


def as_ist_naive(stamp):
    stamp = pd.Timestamp(stamp)
    if stamp.tzinfo is not None:
        return stamp.tz_convert("Asia/Kolkata").tz_localize(None)
    return stamp


def bar_epoch(stamp):
    stamp = pd.Timestamp(stamp)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("Asia/Kolkata")
    else:
        stamp = stamp.tz_convert("Asia/Kolkata")
    return int(stamp.timestamp())


def normalise_timeframe(value):
    text = str(value or "1d").strip().lower()
    aliases = {"1hr": "1h", "60m": "1h", "4hr": "4h", "d": "1d", "1day": "1d"}
    return aliases.get(text, text)


def classify_history(frame, timeframe, moment=None):
    """Delayed when the last bar is recent. Never returns LIVE."""
    if frame is None or getattr(frame, "empty", True):
        return UNAVAILABLE
    last = as_ist_naive(frame.index[-1])
    age = now_ist(moment).replace(tzinfo=None) - last
    timeframe = normalise_timeframe(timeframe)
    limit = pd.Timedelta(days=8) if timeframe in {"1d", "1wk", "1mo"} else pd.Timedelta(days=5)
    if age > limit:
        return STALE
    return DELAYED


def drop_open_candle(frame, timeframe, moment=None):
    """Remove the bar that has not closed yet. Signals must not use it."""
    if frame is None or getattr(frame, "empty", True):
        return frame
    timeframe = normalise_timeframe(timeframe)
    current = now_ist(moment).replace(tzinfo=None)
    last = as_ist_naive(frame.index[-1])
    session = market_status(moment)
    if timeframe == "1d" and last.date() == current.date() and session in {"PRE-MARKET", "OPEN"}:
        return frame.iloc[:-1]
    delta = INTERVALS.get(timeframe)
    if delta is not None and last + delta > current:
        return frame.iloc[:-1]
    return frame


def unavailable(symbol, message="DATA UNAVAILABLE", status=UNAVAILABLE, provider="yahoo"):
    return {
        "symbol": symbol,
        "data_status": status,
        "provider": provider,
        "stream": NOT_CONNECTED,
        "message": message,
        "ltp": None,
    }


class MarketStore:
    """Process memory by default. A later SQLite or PostgreSQL store can replace these methods."""

    def __init__(self):
        self._lock = Lock()
        self.quotes = {}
        self.candles = {}
        self.signals = {}
        self.alerts = {}
        self.watchlist = {}
        self.backtests = {}
        self.trades = []

    def save_quote(self, quote):
        symbol = str(quote.get("symbol") or "").upper()
        if not symbol or quote.get("ltp") is None:
            return
        with self._lock:
            self.quotes[symbol] = dict(quote)

    def latest_quote(self, symbol):
        with self._lock:
            found = self.quotes.get(str(symbol).upper())
            return None if found is None else dict(found)

    def save_signal(self, row):
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            return
        with self._lock:
            self.signals[symbol] = dict(row)

    def remember_alert(self, key):
        """Return True the first time this event is seen."""
        with self._lock:
            if key in self.alerts:
                return False
            self.alerts[key] = True
            if len(self.alerts) > 1000:
                for old in list(self.alerts)[:200]:
                    self.alerts.pop(old, None)
            return True


class MarketDataProvider:
    name = "base"
    streaming = False

    def status(self):
        return {"provider": self.name, "data_status": NOT_CONNECTED, "stream": NOT_CONNECTED}

    def get_quote(self, symbol, exchange="NSE"):
        return unavailable(symbol, "Provider is not implemented", NOT_CONNECTED, self.name)

    def get_quotes(self, symbols, exchange="NSE"):
        return [self.get_quote(symbol, exchange) for symbol in symbols]

    def get_candles(self, symbol, timeframe, exchange="NSE"):
        return {"symbol": symbol, "timeframe": timeframe, "data_status": NOT_CONNECTED, "candles": [], "provider": self.name}

    def get_intraday_candles(self, symbol, timeframe, exchange="NSE"):
        return self.get_candles(symbol, timeframe, exchange)

    def get_historical_candles(self, symbol, timeframe, exchange="NSE"):
        return self.get_candles(symbol, timeframe, exchange)

    def subscribe_quotes(self, symbols):
        return {"ok": False, "data_status": NOT_CONNECTED, "stream": NOT_CONNECTED, "message": STREAM_NOTE, "symbols": list(symbols or [])}

    def subscribe_candles(self, symbols, timeframe):
        return {"ok": False, "data_status": NOT_CONNECTED, "stream": NOT_CONNECTED, "message": STREAM_NOTE, "symbols": list(symbols or []), "timeframe": timeframe}


class UnconnectedProvider(MarketDataProvider):
    def __init__(self, name):
        self.name = name

    def status(self):
        return {
            "provider": self.name,
            "data_status": NOT_CONNECTED,
            "stream": NOT_CONNECTED,
            "message": f"{self.name} credentials are not configured.",
        }

    def get_quote(self, symbol, exchange="NSE"):
        return unavailable(symbol, f"{self.name} is not connected", NOT_CONNECTED, self.name)

    def get_candles(self, symbol, timeframe, exchange="NSE"):
        return {
            "symbol": symbol,
            "timeframe": normalise_timeframe(timeframe),
            "data_status": NOT_CONNECTED,
            "provider": self.name,
            "stream": NOT_CONNECTED,
            "candles": [],
            "message": f"{self.name} is not connected",
        }


class YahooDelayedProvider(MarketDataProvider):
    name = "yahoo"

    def __init__(self, daily_loader, candle_loader, store):
        self.daily_loader = daily_loader
        self.candle_loader = candle_loader
        self.store = store

    def status(self):
        return {
            "provider": self.name,
            "data_status": DELAYED,
            "stream": NOT_CONNECTED,
            "message": STREAM_NOTE,
            "market_status": market_status(),
        }

    def _frame(self, symbol, timeframe):
        timeframe = normalise_timeframe(timeframe)
        if timeframe == "1d":
            return self.daily_loader(symbol)
        return self.candle_loader(symbol, timeframe)

    def get_quote(self, symbol, exchange="NSE", timeframe="1d"):
        symbol = str(symbol or "").upper().strip()
        exchange = str(exchange or "NSE").upper()
        timeframe = normalise_timeframe(timeframe)
        if exchange == "BSE":
            return unavailable(symbol, "BSE quotes are not connected on the Yahoo NSE feed", NOT_CONNECTED, self.name)
        if timeframe not in CANDLE_TIMEFRAMES:
            return unavailable(symbol, "Unsupported timeframe", UNAVAILABLE, self.name)
        try:
            frame = self._frame(symbol, timeframe)
        except Exception:
            frame = None
        status = classify_history(frame, timeframe)
        if status == UNAVAILABLE:
            cached = self.store.latest_quote(symbol)
            if cached and cached.get("timeframe") == timeframe:
                cached["data_status"] = STALE
                cached["message"] = "Last valid quote. The latest request returned no candles."
                cached["stream"] = NOT_CONNECTED
                return cached
            return unavailable(symbol, "DATA UNAVAILABLE", UNAVAILABLE, self.name)
        row = frame.iloc[-1]
        previous = frame["Close"].iloc[-2] if len(frame) > 1 else None
        price = float(row["Close"])
        prev = None if previous is None or pd.isna(previous) else float(previous)
        change = None if prev is None else price - prev
        daily = frame if timeframe == "1d" else None
        if daily is None:
            try:
                daily = self.daily_loader(symbol)
            except Exception:
                daily = None
        week_high = week_low = None
        if daily is not None and not getattr(daily, "empty", True):
            window = daily.tail(252)
            week_high = float(window["High"].max())
            week_low = float(window["Low"].min())
        quote = {
            "symbol": symbol,
            "exchange": "NSE",
            "ltp": round(price, 2),
            "change": None if change is None else round(change, 2),
            "change_pct": None if change is None or not prev else round(change / prev * 100, 2),
            "open": None if pd.isna(row["Open"]) else round(float(row["Open"]), 2),
            "high": None if pd.isna(row["High"]) else round(float(row["High"]), 2),
            "low": None if pd.isna(row["Low"]) else round(float(row["Low"]), 2),
            "previous_close": None if prev is None else round(prev, 2),
            "volume": None if "Volume" not in row or pd.isna(row["Volume"]) else float(row["Volume"]),
            "week52_high": None if week_high is None else round(week_high, 2),
            "week52_low": None if week_low is None else round(week_low, 2),
            "timeframe": timeframe,
            "candle_time": bar_epoch(frame.index[-1]),
            "data_status": status,
            "provider": self.name,
            "stream": NOT_CONNECTED,
            "market_status": market_status(exchange="NSE"),
            "price_basis": f"last {timeframe} close from Yahoo Finance",
            "message": STREAM_NOTE,
        }
        self.store.save_quote(quote)
        return quote

    def get_quotes(self, symbols, exchange="NSE", timeframe="1d"):
        rows = []
        for symbol in list(symbols or [])[:20]:
            rows.append(self.get_quote(symbol, exchange, timeframe))
        return rows

    def _candles(self, symbol, timeframe, exchange="NSE"):
        symbol = str(symbol or "").upper().strip()
        timeframe = normalise_timeframe(timeframe)
        if str(exchange or "NSE").upper() == "BSE":
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "data_status": NOT_CONNECTED,
                "provider": self.name,
                "stream": NOT_CONNECTED,
                "candles": [],
                "message": "BSE candles are not connected",
            }
        if timeframe not in CANDLE_TIMEFRAMES:
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "data_status": UNAVAILABLE,
                "provider": self.name,
                "stream": NOT_CONNECTED,
                "candles": [],
                "message": "Unsupported timeframe",
            }
        try:
            frame = self._frame(symbol, timeframe)
        except Exception:
            frame = None
        if frame is None or getattr(frame, "empty", True):
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "data_status": UNAVAILABLE,
                "provider": self.name,
                "stream": NOT_CONNECTED,
                "candles": [],
                "message": "DATA UNAVAILABLE",
            }
        closed = drop_open_candle(frame, timeframe)
        candles = []
        source = closed if closed is not None and not closed.empty else frame.iloc[0:0]
        for stamp, row in source.iterrows():
            candles.append({
                "time": bar_epoch(stamp),
                "open": round(float(row["Open"]), 2),
                "high": round(float(row["High"]), 2),
                "low": round(float(row["Low"]), 2),
                "close": round(float(row["Close"]), 2),
                "volume": 0 if "Volume" not in row or pd.isna(row["Volume"]) else float(row["Volume"]),
            })
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "data_status": HISTORICAL,
            "quote_status": classify_history(frame, timeframe),
            "provider": self.name,
            "stream": NOT_CONNECTED,
            "market_status": market_status(),
            "forming_candle_excluded": len(candles) != len(frame),
            "candles": candles,
            "message": "Historical Yahoo candles. This is not a live candle stream.",
        }

    def get_candles(self, symbol, timeframe, exchange="NSE"):
        return self._candles(symbol, timeframe, exchange)

    def get_intraday_candles(self, symbol, timeframe, exchange="NSE"):
        timeframe = normalise_timeframe(timeframe)
        if timeframe == "1d":
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "data_status": UNAVAILABLE,
                "candles": [],
                "provider": self.name,
                "message": "1d is historical, not an intraday interval",
            }
        return self._candles(symbol, timeframe, exchange)

    def get_historical_candles(self, symbol, timeframe, exchange="NSE"):
        return self._candles(symbol, timeframe, exchange)


def provider_registry(yahoo):
    providers = {yahoo.name: yahoo}
    for name in BROKER_NAMES:
        providers[name] = UnconnectedProvider(name)
    return providers


def zone_feed(payload, daily):
    rows = list(payload.get("demand") or []) + list(payload.get("supply") or [])
    calculated = any(isinstance(row, dict) and (row.get("text") or row.get("bottom") is not None or row.get("top") is not None) for row in rows)
    history_status = classify_history(daily, "1d")
    if history_status == UNAVAILABLE:
        state = "Unavailable"
    elif history_status == STALE:
        state = "Stale"
    elif calculated:
        state = "Calculated"
    else:
        state = "Unavailable"
    return {
        "status": state,
        "data_status": HISTORICAL if history_status != UNAVAILABLE else UNAVAILABLE,
        "note": "Daily, weekly, and monthly zones use historical candles. They are not recalculated from a live stream.",
    }


def alert_key(symbol, timeframe, rule_type, candle_time):
    return f"{str(symbol).upper()}|{timeframe}|{rule_type}|{candle_time}"
