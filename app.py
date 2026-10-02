from flask import Flask, make_response, render_template, jsonify, request
import pandas as pd
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from uuid import uuid4
from services.yahoo_data import get_live_prices
from services import sd_zones
from services.zone_cache import Superseded, detect_zones, set_cancel_check
from services.chart_backtest import ConfigError as ChartBacktestConfigError
from services.chart_backtest import clean_config as clean_chart_backtest_config
from services.chart_backtest import run_chart_backtest
from services.technical_analysis import (
    ALERT_TYPES,
    CONDITION_LABELS,
    INDICATOR_DEFAULTS,
    INTRADAY_SCAN,
    SCAN_CATALOG,
    SCAN_TIMEFRAMES,
    SCAN_PARAM_DEFAULTS,
    analysis_payload,
    build_context,
    clean_scan_params,
    evaluate_alert,
    mtf_board,
    mtf_status_text,
    resample_chart,
    scan_symbol,
)
from services.signal_engine import build_signal, run_backtest
from services.trading_intelligence import build_replay, compare_presets, confirmation_engine, position_size, trade_plan
from services.paper_trading import PaperBook, extend_backtest_report, run_condition_backtest, size_position
from services.market_data import (
    CANDLE_TIMEFRAMES,
    DELAYED,
    HISTORICAL,
    NOT_CONNECTED,
    STALE,
    MarketStore,
    YahooDelayedProvider,
    alert_key,
    drop_open_candle,
    market_status,
    normalise_timeframe,
    provider_registry,
    zone_feed,
)
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote, unquote
from urllib.error import URLError
from http.cookiejar import CookieJar
from urllib.request import build_opener, HTTPCookieProcessor
import json
import random
import math
import time
from datetime import date, timedelta, datetime, timezone
import os

PROJECT_ROOT = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=PROJECT_ROOT / "templates", static_folder=PROJECT_ROOT / "static")
SCANNER_JOBS = {}
FUNDAMENTAL_JOBS = {}
TECHNICAL_JOBS = {}
BACKTEST_JOBS = {}
INTELLIGENCE_JOBS = {}
SIGNAL_HISTORY = []
ALERT_EVENTS = []
SCANNER_LOCK = Lock()
TECHNICAL_LOCK = Lock()
BACKTEST_LOCK = Lock()
INTELLIGENCE_LOCK = Lock()
ALERT_LOCK = Lock()
TECHNICAL_EXECUTOR = ThreadPoolExecutor(max_workers=2)
BACKTEST_EXECUTOR = ThreadPoolExecutor(max_workers=1)
# A later NIFTY 50/100/200 scan must not sit behind a long NIFTY 500 job.
SCANNER_EXECUTOR = ThreadPoolExecutor(max_workers=2)

# Broker secrets must stay in environment variables on the server (or Render),
# never in JavaScript, localStorage, a repository, or an APK.
BROKER_CONNECTIONS = {
    "sharekhan": {
        "name": "Mirae Asset Sharekhan (SKAPI)",
        "required_variables": ["SHAREKHAN_API_KEY", "SHAREKHAN_SECURE_KEY"],
        "note": "Complete Sharekhan's secure OTP/TOTP login to start a live session.",
    },
}
# Values entered through the Windows desktop app live only in the running
# process. They are never written to disk, source control, or a cloud host.
LOCAL_BROKER_SESSIONS = {}

INDEX_UNIVERSES = {
    "nifty50": ("NIFTY 50", "nifty50.csv", "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv"),
    "nifty100": ("NIFTY 100", "nifty100.csv", "https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv"),
    "nifty200": ("NIFTY 200", "nifty200.csv", "https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv"),
    "nifty500": ("NIFTY 500", "nifty500.csv", "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"),
}

# Shown only when the public market feed is temporarily unavailable.  The UI
# labels these figures as "Last available" so they are never presented as live.
MARKET_FALLBACKS = {
    "NIFTY 50": 23996.25, "BANK NIFTY": 49245.15,
    "SENSEX": 75122.84, "NIFTY IT": 35858.45,
}

# Search-friendly index symbols used by the chart search box.
CHART_INDEX_SYMBOLS = {
    "NIFTY50": "^NSEI", "NIFTY": "^NSEI", "NIFTY100": "^CNX100",
    "NIFTY200": "^CNX200", "BANKNIFTY": "^NSEBANK", "SENSEX": "^BSESN",
    "NIFTYIT": "^CNXIT",
}

SECTOR_INDICES = {
    "TCS": ("IT", "^CNXIT"), "INFY": ("IT", "^CNXIT"), "HCLTECH": ("IT", "^CNXIT"),
    "WIPRO": ("IT", "^CNXIT"), "TECHM": ("IT", "^CNXIT"),
    "HDFCBANK": ("Banking", "^NSEBANK"), "ICICIBANK": ("Banking", "^NSEBANK"),
    "SBIN": ("Banking", "^NSEBANK"), "KOTAKBANK": ("Banking", "^NSEBANK"),
    "AXISBANK": ("Banking", "^NSEBANK"),
    "RELIANCE": ("Energy", "^CNXENERGY"), "ONGC": ("Energy", "^CNXENERGY"),
    "NTPC": ("Energy", "^CNXENERGY"), "MARUTI": ("Auto", "^CNXAUTO"),
    "TATAMOTORS": ("Auto", "^CNXAUTO"), "M&M": ("Auto", "^CNXAUTO"),
    "SUNPHARMA": ("Pharma", "^CNXPHARMA"), "DRREDDY": ("Pharma", "^CNXPHARMA"),
}

# The bundled constituent CSV intentionally contains only Symbol/Company, so
# keep a small symbol map for stocks that are commonly selected in the UI.
# Unknown symbols are classified below from their company name instead of
# being shown as the unhelpful "Broad Market" label.
SECTOR_NAME_OVERRIDES = {
    "GAIL": "Oil, Gas & Consumable Fuels", "COALINDIA": "Oil, Gas & Consumable Fuels",
    "IOC": "Oil, Gas & Consumable Fuels", "BPCL": "Oil, Gas & Consumable Fuels",
    "HINDALCO": "Metals & Mining", "TATASTEEL": "Metals & Mining",
    "JSWSTEEL": "Metals & Mining", "ASIANPAINT": "Consumer Durables",
    "CIPLA": "Pharmaceuticals", "ITC": "Fast Moving Consumer Goods",
    "HINDUNILVR": "Fast Moving Consumer Goods", "BHARTIARTL": "Telecommunication",
    "POWERGRID": "Power", "ADANIGREEN": "Power", "TATAPOWER": "Power",
    "LT": "Construction", "ULTRACEMCO": "Construction Materials",
    "TITAN": "Consumer Durables", "BAJAJ-AUTO": "Automobile",
}


def sector_name_for_symbol(symbol):
    """Read the locally bundled NIFTY list for a useful sector name."""
    symbol = str(symbol).upper().strip()
    if symbol in SECTOR_INDICES:
        return SECTOR_INDICES[symbol][0]
    if symbol in SECTOR_NAME_OVERRIDES:
        return SECTOR_NAME_OVERRIDES[symbol]
    try:
        stocks = pd.read_csv(PROJECT_ROOT / "data" / "nifty500.csv")
        match = stocks[stocks["Symbol"].astype(str).str.upper() == symbol]
        if not match.empty and "Industry" in match.columns:
            industry = str(match.iloc[0]["Industry"]).strip()
            if industry and industry.lower() != "nan":
                return industry
        if not match.empty:
            company = str(match.iloc[0].get("Company", "")).lower()
            keyword_sectors = (
                (("bank", "financial", "finance", "insurance"), "Financial Services"),
                (("pharma", "health", "hospital", "laborator"), "Pharmaceuticals"),
                (("steel", "metal", "aluminium", "aluminum", "mining"), "Metals & Mining"),
                (("power", "energy", "gas", "oil", "petroleum", "coal"), "Oil, Gas & Power"),
                (("cement", "construction"), "Construction Materials"),
                (("auto", "motor", "tyre", "tire"), "Automobile"),
                (("telecom", "communication"), "Telecommunication"),
                (("paint", "consumer", "retail", "foods", "beverage"), "Consumer Goods"),
                (("software", "technology", "tech"), "Information Technology"),
            )
            for keywords, sector in keyword_sectors:
                if any(keyword in company for keyword in keywords):
                    return sector
    except Exception:
        pass
    return "Equity Market"


def nse_json(endpoint):
    """Read NSE's public JSON API with the browser headers it requires."""
    cookies = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookies))
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nseindia.com/",
    }
    opener.open(Request("https://www.nseindia.com/", headers=headers), timeout=10).read(1)
    with opener.open(Request(f"https://www.nseindia.com{endpoint}", headers=headers), timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def yahoo_chart_history(symbol, interval="1d", lookback_days=20000):
    """Read Yahoo's chart feed directly when yfinance's session is blocked."""
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    # If the PC clock is ahead, the newest request can have no candles. Try
    # older valid windows too; the returned chart is still genuine market data.
    for days_back in (0, 365, 730):
        end = datetime.now(timezone.utc) - timedelta(days=days_back)
        start = end - timedelta(days=lookback_days)
        ticker = CHART_INDEX_SYMBOLS.get(symbol.upper(), symbol.upper() + ".NS")
        endpoint = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker)}"
            f"?period1={int(start.timestamp())}&period2={int(end.timestamp())}&interval={interval}"
        )
        try:
            with urlopen(Request(endpoint, headers=headers), timeout=15) as response:
                result = json.loads(response.read().decode("utf-8")).get("chart", {}).get("result", [])
            if not result or not result[0].get("timestamp"):
                continue
            payload = result[0]
            quote_data = payload["indicators"]["quote"][0]
            frame = pd.DataFrame({
                "Open": quote_data.get("open", []), "High": quote_data.get("high", []),
                "Low": quote_data.get("low", []), "Close": quote_data.get("close", []),
                "Volume": quote_data.get("volume", []),
            }, index=pd.to_datetime(payload["timestamp"], unit="s", utc=True).tz_convert("Asia/Kolkata").tz_localize(None))
            frame = frame.apply(pd.to_numeric, errors="coerce")
            if interval == "1d" and days_back == 0:
                frame = _complete_session_bar(symbol, frame, payload.get("meta") or {})
            frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
            if not frame.empty:
                return frame
        except Exception:
            continue
    return pd.DataFrame()


def _complete_session_bar(symbol, frame, meta):
    """Fill the latest daily bar that Yahoo returns with empty prices.

    Yahoo's daily chart often leaves the newest session's OHLC empty while its
    meta block already carries that session's price, high, low and volume.
    The open comes from the first 5-minute candle of the same session. If any
    of those values is missing, the frame is returned unchanged.
    """
    price, stamp = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    high, low = meta.get("regularMarketDayHigh"), meta.get("regularMarketDayLow")
    if frame.empty or None in (price, stamp, high, low):
        return frame
    day = pd.Timestamp(int(stamp), unit="s", tz="UTC").tz_convert("Asia/Kolkata").tz_localize(None).normalize()
    dates = frame.index.normalize()
    same_day = (dates == day).nonzero()[0]
    if len(same_day):
        label = frame.index[same_day[-1]]
        if frame.loc[label, ["Open", "High", "Low", "Close"]].notna().all():
            return frame
    else:
        complete = frame.dropna(subset=["Open", "High", "Low", "Close"])
        if complete.empty or day <= complete.index[-1].normalize():
            return frame
        label = day + (complete.index[-1] - complete.index[-1].normalize())
    session_open = frame.loc[label, "Open"] if label in frame.index else None
    if session_open is None or pd.isna(session_open):
        intraday = yahoo_chart_history(symbol, "5m", 3)
        session = intraday[intraday.index.normalize() == day] if not intraday.empty else intraday
        if session.empty:
            return frame
        session_open = float(session["Open"].iloc[0])
    volume = meta.get("regularMarketVolume")
    frame.loc[label, ["Open", "High", "Low", "Close", "Volume"]] = [
        float(session_open), float(high), float(low), float(price), float(volume) if volume is not None else math.nan,
    ]
    return frame.sort_index()


def yahoo_max_history(symbol):
    """Fetch every daily candle Yahoo has, starting at the listing date."""
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    ticker = CHART_INDEX_SYMBOLS.get(symbol.upper(), symbol.upper() + ".NS")
    endpoint = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker)}"
        "?range=max&interval=1d&events=history"
    )
    try:
        with urlopen(Request(endpoint, headers=headers), timeout=20) as response:
            result = json.loads(response.read().decode("utf-8")).get("chart", {}).get("result", [])
        if not result or not result[0].get("timestamp"):
            return pd.DataFrame()
        payload = result[0]
        quote_data = payload["indicators"]["quote"][0]
        frame = pd.DataFrame({
            "Open": quote_data.get("open", []), "High": quote_data.get("high", []),
            "Low": quote_data.get("low", []), "Close": quote_data.get("close", []),
            "Volume": quote_data.get("volume", []),
        }, index=pd.to_datetime(payload["timestamp"], unit="s", utc=True).tz_convert("Asia/Kolkata").tz_localize(None))
        return frame.apply(pd.to_numeric, errors="coerce").dropna(subset=["Open", "High", "Low", "Close"])
    except Exception:
        return pd.DataFrame()


def _median_day_gap(frame):
    """Typical spacing between candles, in days."""
    if frame is None or len(frame) < 5 or not isinstance(frame.index, pd.DatetimeIndex):
        return None
    gaps = pd.Series(frame.index).diff().dt.total_seconds().dropna() / 86400
    if gaps.empty:
        return None
    return float(gaps.median())


def _bar_start(frame):
    stamp = pd.Timestamp(frame.index.min())
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("Asia/Kolkata").tz_localize(None)
    return stamp


def _prefer_history(current, candidate):
    """Keep real daily history when a longer-looking feed is only monthly."""
    if candidate is None or getattr(candidate, "empty", True):
        return current
    if current is None or getattr(current, "empty", True):
        return candidate
    current_gap = _median_day_gap(current)
    candidate_gap = _median_day_gap(candidate)
    current_daily = current_gap is not None and current_gap <= 5
    candidate_daily = candidate_gap is not None and candidate_gap <= 5
    if candidate_daily and not current_daily:
        return candidate
    if current_daily and not candidate_daily:
        return current
    current_start = _bar_start(current)
    candidate_start = _bar_start(candidate)
    if candidate_start < current_start - pd.Timedelta(days=10):
        return candidate
    if len(candidate) > len(current) and candidate_start <= current_start:
        return candidate
    return current


_HISTORY_CACHE = {}
_CHART_CACHE = {}
_HISTORY_LOCK = Lock()
_HISTORY_TTL_SECONDS = 900


def _usable_daily(frame):
    gap = _median_day_gap(frame)
    return frame is not None and not getattr(frame, "empty", True) and gap is not None and gap <= 5 and len(frame) >= 250


def _fetch_symbol_history(symbol):
    """Yahoo first, then NSE historical candles when Yahoo has no NSE data."""
    # ``range=max`` is supposed to return history from listing. Yahoo sometimes
    # answers that request with a few hundred widely spaced candles. Those must
    # not replace a real daily series, or every daily study is calculated on
    # the wrong bars. A normal daily chart request is tried first and kept
    # when it is already a real daily series.
    best_history = pd.DataFrame()
    for candidate in (yahoo_chart_history(symbol, "1d", 20000), yahoo_max_history(symbol)):
        best_history = _prefer_history(best_history, candidate)
        if _usable_daily(best_history):
            return best_history
    try:
        ticker_name = CHART_INDEX_SYMBOLS.get(symbol.upper(), f"{symbol.upper()}.NS")
        ticker = yf.Ticker(ticker_name)
        # ``max`` can occasionally be rejected by Yahoo for otherwise valid
        # NSE symbols. Try normal, smaller requests first so the 1D chart
        # remains available.
        for lookback in ("1y", "2y", "5y", "max"):
            try:
                history = ticker.history(period=lookback, interval="1d", auto_adjust=False)
            except Exception:
                continue
            best_history = _prefer_history(best_history, history)
            if _usable_daily(best_history):
                return best_history
    except Exception:
        pass
    if not best_history.empty:
        return best_history
    try:
        # Some computers have a clock ahead of the market-data server. Try
        # recent one-year windows until NSE returns the latest available data.
        rows = []
        for days_back in (0, 365, 730):
            end = date.today() - timedelta(days=days_back)
            start = end - timedelta(days=365)
            query = f"/api/historical/cm/equity?symbol={quote(symbol.upper())}&series=[%22EQ%22]&from={start:%d-%m-%Y}&to={end:%d-%m-%Y}"
            rows = nse_json(query).get("data", [])
            if rows:
                break
        frame = pd.DataFrame(rows)
        if frame.empty:
            return frame
        frame["Date"] = pd.to_datetime(frame["CH_TIMESTAMP"], errors="coerce")
        frame = frame.dropna(subset=["Date"]).set_index("Date").sort_index()
        frame = frame.rename(columns={
            "CH_OPENING_PRICE": "Open", "CH_TRADE_HIGH_PRICE": "High",
            "CH_TRADE_LOW_PRICE": "Low", "CH_CLOSING_PRICE": "Close", "CH_TOT_TRADED_QTY": "Volume",
        })
        for column in ["Open", "High", "Low", "Close", "Volume"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame.dropna(subset=["Open", "High", "Low", "Close"])
    except Exception:
        return pd.DataFrame()


def get_symbol_history(symbol, fresh=False):
    """Return daily OHLCV, reusing a short in-memory copy for this process."""
    key = str(symbol).upper().strip()
    now = time.monotonic()
    if not fresh:
        with _HISTORY_LOCK:
            cached = _HISTORY_CACHE.get(key)
            if cached and now - cached[0] < _HISTORY_TTL_SECONDS:
                return cached[1].copy()
    frame = _fetch_symbol_history(key)
    if frame is not None and not getattr(frame, "empty", True):
        with _HISTORY_LOCK:
            _HISTORY_CACHE[key] = (time.monotonic(), frame.copy())
    return frame


def get_chart_history(symbol, timeframe, fresh=False):
    """Return the candle interval chosen on the main chart toolbar."""
    key = (str(symbol).upper().strip(), str(timeframe))
    now = time.monotonic()
    if not fresh:
        with _HISTORY_LOCK:
            cached = _CHART_CACHE.get(key)
            if cached and now - cached[0] < _HISTORY_TTL_SECONDS:
                return cached[1].copy()

    def store(frame):
        if frame is not None and not getattr(frame, "empty", True):
            with _HISTORY_LOCK:
                _CHART_CACHE[key] = (time.monotonic(), frame.copy())
        return frame

    intraday = {
        "1m": ("1m", "7d", None),
        "5m": ("5m", "60d", None),
        "15m": ("15m", "60d", None),
        "30m": ("30m", "60d", None),
        "1h": ("60m", "730d", None),
        "4h": ("60m", "730d", "4h"),
        "6h": ("60m", "730d", "6h"),
        "12h": ("60m", "730d", "12h"),
    }
    if timeframe == "3m":
        base = yahoo_chart_history(symbol, "1m", 7)
        if base is None or getattr(base, "empty", True):
            return pd.DataFrame()
        history = base.resample("3min").agg({
            "Open": "first", "High": "max", "Low": "min",
            "Close": "last", "Volume": "sum",
        }).dropna()
        return store(history)
    config = intraday.get(timeframe)
    if not config:
        return get_symbol_history(symbol, fresh=fresh)

    interval, lookback, rule = config
    lookback_days = 7 if interval == "1m" else 60 if interval in {"5m", "15m", "30m"} else 700
    direct_history = yahoo_chart_history(symbol, interval, lookback_days)
    if not direct_history.empty:
        history = direct_history
        if rule:
            history = history.resample(rule).agg({
                "Open": "first", "High": "max", "Low": "min",
                "Close": "last", "Volume": "sum",
            }).dropna()
        return store(history)
    try:
        history = yf.Ticker(f"{symbol.upper()}.NS").history(
            period=lookback, interval=interval, auto_adjust=False
        )
        if history.empty:
            return history
        if rule:
            history = history.resample(rule).agg({
                "Open": "first", "High": "max", "Low": "min",
                "Close": "last", "Volume": "sum",
            }).dropna()
        return store(history)
    except Exception:
        return pd.DataFrame()


_RECENT_CACHE = {}
_RECENT_TTL_SECONDS = 20
_RECENT_FEEDS = {"1d": ("1d", 10), "5m": ("5m", 2), "15m": ("15m", 3), "30m": ("30m", 4), "1h": ("60m", 6)}


def _with_recent_bars(symbol, timeframe, base):
    """Refresh the newest bars of a cached frame from Yahoo's chart feed.

    The long history stays cached for _HISTORY_TTL_SECONDS. Quotes read the
    last few days again at most every _RECENT_TTL_SECONDS. Recent bars are
    only merged when both frames use the same timestamp convention;
    otherwise the cached frame is returned unchanged.
    """
    feed = _RECENT_FEEDS.get(timeframe)
    if feed is None or base is None or getattr(base, "empty", True):
        return base
    key = (str(symbol).upper().strip(), timeframe)
    now = time.monotonic()
    with _HISTORY_LOCK:
        cached = _RECENT_CACHE.get(key)
    if cached and now - cached[0] < _RECENT_TTL_SECONDS:
        recent = cached[1]
    else:
        try:
            recent = yahoo_chart_history(symbol, feed[0], feed[1])
        except Exception:
            recent = pd.DataFrame()
        with _HISTORY_LOCK:
            _RECENT_CACHE[key] = (time.monotonic(), recent)
    if recent is None or recent.empty:
        return base
    if getattr(base.index, "tz", None) is not None or getattr(recent.index, "tz", None) is not None:
        return base
    columns = [column for column in ("Open", "High", "Low", "Close", "Volume") if column in base.columns and column in recent.columns]
    first = recent.index[0]
    if timeframe == "1d":
        # Daily bars are matched by trading date; the cached frame keeps its
        # own timestamp labels so the chart's last candle time does not move.
        work = base.copy()
        dates = work.index.normalize()
        if len(work) < 2 or first.normalize() < dates[0]:
            return base
        clock = work.index[-2] - dates[-2]
        for stamp, row in recent[columns].iterrows():
            day = stamp.normalize()
            matches = (dates == day).nonzero()[0]
            if len(matches):
                work.loc[work.index[matches[-1]], columns] = row.values
            elif day > dates[-1]:
                work.loc[day + clock, columns] = row.values
                dates = work.index.normalize()
        return work
    if first not in base.index:
        return base
    merged = pd.concat([base[base.index < first][columns], recent[columns]])
    return merged[~merged.index.duplicated(keep="last")].sort_index()


def get_quote_daily(symbol):
    return _with_recent_bars(symbol, "1d", get_symbol_history(symbol))


def get_quote_candles(symbol, timeframe):
    return _with_recent_bars(symbol, timeframe, get_chart_history(symbol, timeframe))


MARKET_STORE = MarketStore()
PAPER_BOOK = PaperBook(PROJECT_ROOT / "data" / "paper_trading.sqlite")
PAPER_JOBS = {}
YAHOO_PROVIDER = YahooDelayedProvider(get_quote_daily, get_quote_candles, MARKET_STORE)
MARKET_PROVIDERS = provider_registry(YAHOO_PROVIDER)
LIVE_SCAN = {
    "running": False,
    "stop": False,
    "job_id": None,
    "universe": None,
    "timeframe": None,
    "last_update": None,
    "scanned": 0,
    "matches": 0,
    "unavailable": 0,
    "errors": 0,
    "results": [],
    "data_status": DELAYED,
    "stream": NOT_CONNECTED,
    "message": "Live scan is stopped. Yahoo data is delayed, not a live stream.",
}
LIVE_SCAN_LOCK = Lock()
LIVE_SCAN_CAP = 12


def offline_sample_history(symbol, timeframe):
    """Create a clearly labelled visual fallback when every feed is offline.

    This is intentionally only for checking the chart UI: it is never used for
    zone detection or scanner signals.
    """
    base_prices = {
        "RELIANCE": 1400, "TCS": 3200, "INFY": 1500, "HDFCBANK": 1800,
        "ICICIBANK": 1300, "SBIN": 850, "AXISBANK": 1100,
    }
    seed = sum((index + 1) * ord(letter) for index, letter in enumerate(symbol.upper()))
    rng = random.Random(seed)
    base = float(base_prices.get(symbol.upper(), 400 + (seed % 1200)))
    intraday_frequency = {"5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h", "6h": "6h", "12h": "12h"}
    frequency = intraday_frequency.get(timeframe, "B")
    periods = 280 if timeframe in intraday_frequency else 5000
    times = pd.date_range(end=pd.Timestamp.now().floor("min"), periods=periods, freq=frequency)
    price = base
    rows = []
    for _ in times:
        move = rng.uniform(-0.024, 0.025)
        opening = price
        closing = max(1.0, opening * (1 + move))
        high = max(opening, closing) * (1 + rng.uniform(0.001, 0.012))
        low = min(opening, closing) * (1 - rng.uniform(0.001, 0.012))
        rows.append((opening, high, low, closing, rng.randint(50_000, 900_000)))
        price = closing
    frame = pd.DataFrame(rows, index=times, columns=["Open", "High", "Low", "Close", "Volume"])
    frame.index.name = "Date"
    return frame


def load_index_universe(index_key):
    """Load a cached index list, or download the latest official constituent list."""
    label, filename, url = INDEX_UNIVERSES[index_key]
    path = PROJECT_ROOT / "data" / filename
    if path.exists():
        stocks = pd.read_csv(path)
    else:
        try:
            request = Request(url, headers={"User-Agent": "Project-SM-Scanner/1.0"})
            with urlopen(request, timeout=30) as response:
                stocks = pd.read_csv(response)
            path.parent.mkdir(parents=True, exist_ok=True)
            stocks.to_csv(path, index=False)
        except Exception:
            # Keep the scanner usable offline.  The smaller lists are created
            # from the locally available NIFTY 500 file until their official
            # CSV can be downloaded on a later scan.
            cached_500 = PROJECT_ROOT / "data" / "nifty500.csv"
            if not cached_500.exists():
                raise
            limit = {"nifty50": 50, "nifty100": 100, "nifty200": 200}.get(index_key, 500)
            stocks = pd.read_csv(cached_500).head(limit)

    stocks.columns = [str(column).strip() for column in stocks.columns]
    symbol_column = next((column for column in stocks.columns if column.lower() == "symbol"), None)
    company_column = next((column for column in stocks.columns if column.lower() in {"company", "company name"}), None)
    if not symbol_column:
        raise ValueError(f"{label} constituent file has no Symbol column")
    result = pd.DataFrame({"Symbol": stocks[symbol_column]})
    result["Company"] = stocks[company_column] if company_column else result["Symbol"]
    industry_column = next((column for column in stocks.columns if column.lower() == "industry"), None)
    if industry_column:
        result["Industry"] = stocks[industry_column]
    return label, result.dropna(subset=["Symbol"]).drop_duplicates(subset=["Symbol"])


def _ratio(value, percentage=False):
    """Normalise Yahoo values that can arrive as decimals or percentages."""
    if value is None or pd.isna(value):
        return None
    try:
        value = float(value)
        return value / 100 if percentage and value > 1 else value
    except (TypeError, ValueError):
        return None


def get_fundamental_scan(symbol, company, criteria=None):
    """Evaluate the user's quality-ratio checklist from available public data."""
    criteria = criteria or {}
    def threshold(name, fallback):
        try:
            return float(criteria.get(name, fallback))
        except (TypeError, ValueError):
            return fallback
    opm_min = threshold("opm", .20)
    debt_equity_max = threshold("debt_equity", 1)
    roe_min = threshold("roe", .15)
    roce_min = threshold("roce", .15)
    interest_multiple = threshold("interest_multiple", 2)
    info = yf.Ticker(f"{symbol}.NS").get_info()
    opm = _ratio(info.get("operatingMargins"))
    roe = _ratio(info.get("returnOnEquity"))
    debt_equity = _ratio(info.get("debtToEquity"), percentage=True)
    trailing_eps, forward_eps = _ratio(info.get("trailingEps")), _ratio(info.get("forwardEps"))
    operating_cashflow = _ratio(info.get("operatingCashflow"))

    # Yahoo does not reliably supply promoter holding or 10-year company
    # history for every NSE symbol. Those criteria are reported as unavailable,
    # never invented or counted as a pass.
    checks = {
        "OPM": None if opm is None else opm >= opm_min,
        "EPS Stable": None if trailing_eps is None or forward_eps is None or trailing_eps <= 0 else forward_eps >= trailing_eps * .75,
        "D/E": None if debt_equity is None else debt_equity < debt_equity_max,
        "ROE": None if roe is None else roe >= roe_min,
        "ROCE": None,
        "Net Profit / Interest": None,
        "Promoter Holding": None,
        "Cash Flow": None if operating_cashflow is None else operating_cashflow > 0,
        "Balance Sheet": None,
        "10Y Sales & Profit Growth": None,
    }
    try:
        income = yf.Ticker(f"{symbol}.NS").financials
        balance = yf.Ticker(f"{symbol}.NS").balance_sheet
        if not income.empty and not balance.empty:
            def value(frame, names, column=0):
                row = next((name for name in names if name in frame.index), None)
                if row is None or frame.shape[1] <= column:
                    return None
                return _ratio(frame.loc[row].iloc[column])
            ebit = value(income, ["EBIT", "Operating Income"])
            interest = value(income, ["Interest Expense", "Interest Expense Non Operating"])
            net_income = value(income, ["Net Income", "Net Income Common Stockholders"])
            assets = value(balance, ["Total Assets"])
            current_liabilities = value(balance, ["Current Liabilities", "Total Current Liabilities"])
            equity_now = value(balance, ["Stockholders Equity", "Total Equity Gross Minority Interest"])
            equity_previous = value(balance, ["Stockholders Equity", "Total Equity Gross Minority Interest"], 1)
            capital_employed = (assets - current_liabilities) if assets is not None and current_liabilities is not None else None
            checks["ROCE"] = None if ebit is None or not capital_employed else ebit / capital_employed >= roce_min
            checks["Net Profit / Interest"] = None if net_income is None or not interest or interest >= 0 else net_income / abs(interest) >= interest_multiple
            checks["Balance Sheet"] = None if equity_now is None or equity_previous is None else equity_now >= equity_previous
    except Exception:
        pass

    available = [passed for passed in checks.values() if passed is not None]
    passed = sum(available)
    score = round(passed / len(available) * 100) if available else 0
    return {
        "symbol": symbol, "company": company, "score": score,
        "passed": passed, "available": len(available), "checks": checks,
        "opm": opm, "roe": roe, "debt_equity": debt_equity,
    }


def run_fundamental_scanner(job_id, symbols, criteria):
    def scan(row):
        try:
            return get_fundamental_scan(row.Symbol.strip().upper(), row.Company, criteria)
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(scan, row) for row in symbols.itertuples(index=False)]
        for future in as_completed(futures):
            result = future.result()
            with SCANNER_LOCK:
                job = FUNDAMENTAL_JOBS[job_id]
                job["completed"] += 1
                if result is None:
                    job["unavailable"] += 1
                # Require at least six known metrics and an 80% pass score so
                # an incomplete provider response cannot become a false result.
                elif result["available"] >= 6 and result["score"] >= 80:
                    job["results"].append(result)
    with SCANNER_LOCK:
        job = FUNDAMENTAL_JOBS[job_id]
        job["results"].sort(key=lambda row: (row["score"], row["passed"]), reverse=True)
        job["status"] = "complete"


@app.post("/api/fundamental-scanner")
def start_fundamental_scanner():
    settings = request.get_json(silent=True) or {}
    index_key = settings.get("universe", "nifty50")
    if index_key not in INDEX_UNIVERSES:
        return jsonify({"error": "Invalid index universe"}), 400
    try:
        label, symbols = load_index_universe(index_key)
    except Exception as error:
        return jsonify({"error": f"Could not load index list: {error}"}), 503
    job_id = str(uuid4())
    with SCANNER_LOCK:
        FUNDAMENTAL_JOBS[job_id] = {"status": "running", "completed": 0, "total": len(symbols), "unavailable": 0, "results": [], "universe": label}
    SCANNER_EXECUTOR.submit(run_fundamental_scanner, job_id, symbols, settings.get("criteria", {}))
    return jsonify({"job_id": job_id})


@app.get("/api/fundamental-scanner/<job_id>")
def fundamental_scanner_status(job_id):
    with SCANNER_LOCK:
        job = FUNDAMENTAL_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Fundamental scan job not found"}), 404
        return jsonify(job)


def detect_supply_demand_zones(df, max_zones=4):
    """Return only strong, fresh reversal zones.

    A demand zone is the last bearish candle before an upward impulse. A supply
    zone is the last bullish candle before a downward impulse. A fresh zone has
    not been revisited after its confirmation candles have closed.
    """
    if len(df) < 20:
        return []

    work = df.copy()
    work["range"] = work["High"] - work["Low"]
    work["atr"] = work["range"].rolling(14, min_periods=5).mean()
    zones = []

    for index in range(5, len(work) - 3):
        candle = work.iloc[index]
        following = work.iloc[index + 1:index + 4]
        # Do not count the three confirmation candles as a zone retest.
        later = work.iloc[index + 4:]
        candle_range = float(candle["range"])
        atr = float(candle["atr"] or 0)
        # A strong departure must move at least 1.25 ATR or 1.4 candle ranges.
        minimum_impulse = max(candle_range * 1.4, atr * 1.25)

        # Demand: a down candle followed by a decisive up move.
        if candle["Close"] <= candle["Open"]:
            impulse = float(following["Close"].max() - candle["High"])
            # Fresh demand has not been touched after the impulse confirmation.
            retested = not later.empty and float(later["Low"].min()) <= float(max(candle["Open"], candle["Close"]))
            if impulse >= minimum_impulse and not retested:
                zones.append({
                    "type": "demand",
                    "time": int(pd.Timestamp(candle.name).tz_localize(None).timestamp()),
                    "top": round(float(max(candle["Open"], candle["Close"])), 2),
                    "bottom": round(float(candle["Low"]), 2),
                    "strength": "strong",
                    "fresh": True,
                })

        # Supply: an up candle followed by a decisive down move.
        if candle["Close"] >= candle["Open"]:
            impulse = float(candle["Low"] - following["Close"].min())
            # Fresh supply has not been touched after the impulse confirmation.
            retested = not later.empty and float(later["High"].max()) >= float(min(candle["Open"], candle["Close"]))
            if impulse >= minimum_impulse and not retested:
                zones.append({
                    "type": "supply",
                    "time": int(pd.Timestamp(candle.name).tz_localize(None).timestamp()),
                    "top": round(float(candle["High"]), 2),
                    "bottom": round(float(min(candle["Open"], candle["Close"])), 2),
                    "strength": "strong",
                    "fresh": True,
                })

    # Keep the newest non-overlapping zones so the chart stays readable.
    active = []
    for zone in reversed(zones):
        overlaps = any(
            zone["type"] == existing["type"]
            and zone["bottom"] <= existing["top"]
            and zone["top"] >= existing["bottom"]
            for existing in active
        )
        if not overlaps:
            active.append(zone)
        if len(active) >= max_zones:
            break
    return list(reversed(active))


# ==========================
# HOME
# ==========================
@app.route("/")
def home():

    df = pd.read_csv(PROJECT_ROOT / "data" / "nifty500.csv")

    yahoo_symbols = [
        f"{s.strip().upper()}.NS"
        for s in df["Symbol"]
    ]

    prices = {}

    stocks = []

    for _, row in df.iterrows():

        symbol = row["Symbol"].strip().upper()

        stocks.append({

            "symbol": symbol,

            "company": row["Company"],

            "price": prices.get(f"{symbol}.NS", "-")

        })

    response = make_response(render_template(
        "index.html",
        stocks=stocks,
        total=len(stocks)
    ))
    response.headers["Cache-Control"] = "no-store"
    return response


# ==========================
# LIVE CHART API
# ==========================
@app.get("/api/broker/status")
def broker_status():
    """Expose configuration state only; credentials are never returned."""
    provider = request.args.get("provider", "").strip().lower()
    details = BROKER_CONNECTIONS.get(provider)
    if not details:
        return jsonify({"provider": provider, "configured": False, "message": "Select a supported broker."})

    local_ready = bool(LOCAL_BROKER_SESSIONS.get(provider))
    missing = [name for name in details["required_variables"] if not os.getenv(name)]
    if missing and not local_ready:
        return jsonify({
            "provider": provider,
            "configured": False,
            "message": "Server setup required: " + ", ".join(missing),
        })
    connected = bool(LOCAL_BROKER_SESSIONS.get(provider, {}).get("connected"))
    return jsonify({
        "provider": provider,
        "configured": True,
        "connected": connected,
        "message": (
            "Sharekhan live session is connected."
            if connected
            else "Credentials are ready for this local session. " + details["note"]
        ),
    })


@app.post("/api/broker/local-session")
def configure_local_broker_session():
    """Allow credentials only from the same Windows computer and keep them in RAM."""
    if request.remote_addr not in {"127.0.0.1", "::1"}:
        return jsonify({"ok": False, "message": "For security, enter broker keys only in the local Windows software."}), 403

    payload = request.get_json(silent=True) or {}
    provider = str(payload.get("provider", "")).strip().lower()
    api_key = str(payload.get("api_key", "")).strip()
    secure_key = str(payload.get("secure_key", "")).strip()
    if provider != "sharekhan" or not api_key or not secure_key:
        return jsonify({"ok": False, "message": "Enter both Sharekhan API Key and Secure Key."}), 400
    if len(secure_key.encode("utf-8")) != 32:
        return jsonify({
            "ok": False,
            "message": "Sharekhan Secure Key must be exactly 32 characters. Copy the Secure Key from the same Sharekhan App.",
        }), 400

    try:
        from SharekhanApi.sharekhanConnect import SharekhanConnect
        login_client = SharekhanConnect(api_key)
        login_url = login_client.login_url(vendor_key="", version_id=None)
    except ImportError:
        return jsonify({
            "ok": False,
            "message": "Sharekhan SDK is not installed. Run: py -m pip install shareconnect websocket-client",
        }), 503
    except Exception:
        return jsonify({
            "ok": False,
            "message": "Sharekhan SDK could not create the login URL (LOGIN_URL_FAILED).",
        }), 502
    LOCAL_BROKER_SESSIONS[provider] = {
        "api_key": api_key,
        "secure_key": secure_key,
        "version_id": None,
    }
    return jsonify({
        "ok": True,
        "login_url": login_url,
        "message": "Credentials accepted. Opening Sharekhan OTP/TOTP login...",
    })


@app.post("/api/broker/sharekhan/callback")
def complete_sharekhan_session():
    """Exchange Sharekhan's one-time request token without exposing secrets."""
    if request.remote_addr not in {"127.0.0.1", "::1"}:
        return jsonify({"ok": False, "message": "Sharekhan callback is allowed only in the local software."}), 403
    broker_session = LOCAL_BROKER_SESSIONS.get("sharekhan")
    request_token = str((request.get_json(silent=True) or {}).get("request_token", "")).strip()
    # Defensive compatibility for callbacks parsed by older frontend builds:
    # encrypted base64-style tokens may have had "+" converted to spaces or
    # may arrive percent-encoded more than once.
    request_token = request_token.replace(" ", "+")
    for _ in range(2):
        decoded_token = unquote(request_token)
        if decoded_token == request_token:
            break
        request_token = decoded_token
    # The published SDK calls urlsafe_b64decode directly and does not restore
    # omitted Base64 padding, although valid callback tokens may omit it.
    request_token += "=" * (-len(request_token) % 4)
    if not broker_session or not request_token:
        return jsonify({"ok": False, "message": "Broker credentials or request token is missing."}), 400
    try:
        from SharekhanApi.sharekhanConnect import SharekhanConnect
    except ImportError:
        return jsonify({
            "ok": False,
            "message": "Sharekhan SDK is not installed. Run: pip install shareconnect websocket-client",
        }), 503
    client = SharekhanConnect(broker_session["api_key"])
    try:
        generated_session = client.generate_session_without_versionId(
            request_token, broker_session["secure_key"]
        )
    except Exception as exc:
        app.logger.warning("Sharekhan session decryption failed: %s", type(exc).__name__)
        # A failed/expired token must never be reused. Also force the next
        # attempt to re-enter the matching API Key and Secure Key pair.
        LOCAL_BROKER_SESSIONS.pop("sharekhan", None)
        error_name = type(exc).__name__
        if error_name == "InvalidTag":
            guidance = "The Secure Key does not match this API Key/token."
        elif error_name in {"Error", "ValueError"}:
            guidance = "The callback token or Secure Key format is invalid."
        else:
            guidance = "The Sharekhan SDK could not process this callback token."
        return jsonify({
            "ok": False,
            "message": (
                f"{guidance} (SESSION_DECRYPT_FAILED/{error_name}). "
                "Re-copy the API Key and 32-character Secure Key from the same "
                "Sharekhan App, then start a new login."
            ),
        }), 502
    try:
        token_result = client.get_access_token(
            broker_session["api_key"], generated_session, 12345
        )
        access_token = token_result
        if isinstance(token_result, dict):
            access_token = (
                token_result.get("access_token")
                or token_result.get("accessToken")
                or token_result.get("token")
                or token_result
            )
        broker_session["access_token"] = access_token
        broker_session["connected"] = True
        return jsonify({"ok": True, "message": "Sharekhan live session connected successfully."})
    except Exception as exc:
        app.logger.warning("Sharekhan access-token exchange failed: %s", type(exc).__name__)
        return jsonify({
            "ok": False,
            "message": "Sharekhan rejected the access-token request (ACCESS_TOKEN_FAILED). Confirm Static IP and try again.",
        }), 502


@app.route("/api/chart/<symbol>/<period>")
def chart(symbol, period):

    try:
        selected_tf = period
        df = get_chart_history(symbol, selected_tf)
        offline_sample = False

        # Providers can return numbers as text. Normalise every candle before
        # resampling or calculating zone indicators.
        if not df.empty:
            df = df.copy()
            for column in ["Open", "High", "Low", "Close"]:
                df[column] = pd.to_numeric(df[column], errors="coerce")
            if "Volume" not in df.columns:
                df["Volume"] = 0
            df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce").fillna(0)
            df = df.dropna(subset=["Open", "High", "Low", "Close"])

        if df.empty:
            df = offline_sample_history(symbol, selected_tf)
            offline_sample = True

        # Zones are always built from daily candles (daily, weekly and
        # monthly structure), whatever timeframe the chart displays.
        zone_source = None
        if not offline_sample:
            if selected_tf in {"5m", "15m", "1h", "4h", "6h", "12h"}:
                try:
                    zone_source = get_symbol_history(symbol)
                except Exception:
                    zone_source = None
            else:
                zone_source = df.copy()

        # Keep the full available daily history, including the earliest
        # candles returned after a stock's listing date.

        # -------- Higher Timeframe --------
        if selected_tf != "1d":

            rule = None

            if selected_tf == "1wk":
                rule = "W"
            elif selected_tf == "1mo":
                rule = "ME"
            elif selected_tf == "3mo":
                rule = "3ME"
            elif selected_tf == "6mo":
                rule = "6ME"
            elif selected_tf == "1y":
                rule = "YE"
            elif selected_tf == "5y":
                rule = "5YE"

            if rule:
                df = df.resample(rule).agg({
                    "Open": "first",
                    "High": "max",
                    "Low": "min",
                    "Close": "last",
                    "Volume": "sum"
                }).dropna()

        # Resampling may remove the original index label. Give the serialised
        # chart data one stable timestamp column for every timeframe.
        df.index.name = "Date"

        # Nearby Demand/Supply from the shared zone engine. When no valid
        # zone is close to price the side stays empty (see zone_messages).
        zones = []
        zone_messages = {"demand": sd_zones.NO_DEMAND, "supply": sd_zones.NO_SUPPLY}
        if zone_source is not None and len(zone_source):
            found = sd_zones.analyze(zone_source)
            zones = sd_zones.chart_rectangles(found["display"], df.index)
            zone_messages = found["messages"]

        df = df.reset_index()

        chart_data = []

        for _, row in df.iterrows():

            if "Date" in row.index:
                t = row["Date"]
            else:
                t = row["Datetime"]

            chart_data.append({
                "time": int(pd.Timestamp(t).tz_localize(None).timestamp()),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"])
            })

        return jsonify({"candles": chart_data, "zones": zones, "zone_messages": zone_messages, "offline_sample": offline_sample})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def run_scanner(job_id, timeframe, symbols):
    """Scan the NIFTY 500 in the background so the UI stays responsive."""

    def scan_stock(row):
        symbol = row.Symbol.strip().upper()
        try:
            history = get_symbol_history(symbol)
            if history.empty:
                return None
            # Evaluate several recent zones first; only then apply the strict
            # quality filter below, so a valid older premium zone is not missed.
            zones = detect_zones(history, timeframe=timeframe, max_zones=12)
            latest_close = float(pd.to_numeric(history["Close"], errors="coerce").dropna().iloc[-1])
            # A very old zone can technically remain fresh forever (for
            # example an entry near Rs.37 while the stock trades near Rs.2637).
            # Keep scanner signals close enough to the current market price.
            actionable = []
            for zone in zones:
                midpoint = (zone["entry_low"] + zone["entry_high"]) / 2
                price_distance = abs(midpoint - latest_close) / latest_close if latest_close else 1
                # Never show a completed or broken trade as a new scanner
                # signal. Demand is invalid below its entry-low; Supply is
                # invalid above its entry-high. Their target must also still
                # be ahead of the current price.
                if zone["type"] == "demand":
                    zone_is_intact = latest_close >= float(zone["entry_low"])
                    target_is_open = latest_close <= float(zone["exit"])
                else:
                    zone_is_intact = latest_close <= float(zone["entry_high"])
                    target_is_open = latest_close >= float(zone["exit"])
                if (
                    zone["score"] >= 60
                    and zone["risk_reward"] >= 2
                    and price_distance <= 0.20
                    and zone_is_intact
                    and target_is_open
                ):
                    actionable.append(zone)
            return [{
                "symbol": symbol,
                "company": row.Company,
                "pattern": zone["pattern"],
                "pattern_name": zone["pattern_name"],
                "zone_type": zone["type"].title(),
                "timeframe": zone["timeframe"],
                "score": zone["score"],
                "grade": zone["grade"],
                "stars": zone["stars"],
                "status": "Fresh" if zone["fresh"] else "Tested",
                "entry": f"₹{zone['entry_low']:,.2f} – ₹{zone['entry_high']:,.2f}",
                "exit": f"₹{zone['exit']:,.2f}",
                "strength": zone["grade"],
                "base_candles": zone["base_candles"],
                "departure_atr": zone["departure_atr"],
                "volume_ratio": zone["volume_ratio"],
                "bos": zone["bos"],
                "fvg": zone["fvg"],
                "liquidity_sweep": zone["liquidity_sweep"],
                "order_block": zone["order_block"],
                "choch": zone["choch"],
                "risk_reward": zone["risk_reward"],
                "htf": zone["higher_timeframe"],
                "ltp": round(latest_close, 2),
            } for zone in actionable]
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(scan_stock, row) for row in symbols.itertuples(index=False)]
        for future in as_completed(futures):
            found = future.result()
            with SCANNER_LOCK:
                job = SCANNER_JOBS[job_id]
                job["completed"] += 1
                if found is None:
                    job["unavailable"] += 1
                else:
                    job["results"].extend(found)

    with SCANNER_LOCK:
        job = SCANNER_JOBS[job_id]
        job["results"].sort(key=lambda item: item["score"], reverse=True)
        job["status"] = "complete"


@app.post("/api/scanner")
def start_scanner():
    settings = request.get_json(silent=True) or {}
    timeframe = settings.get("timeframe", "1d")
    index_key = settings.get("universe", "nifty500")
    if timeframe not in {"1d", "1wk", "1mo", "3mo", "6mo", "1y", "5y"}:
        return jsonify({"error": "Invalid timeframe"}), 400
    if index_key not in INDEX_UNIVERSES:
        return jsonify({"error": "Invalid index universe"}), 400
    try:
        label, symbols = load_index_universe(index_key)
    except Exception as error:
        return jsonify({"error": f"Could not load {INDEX_UNIVERSES[index_key][0]} list: {error}"}), 503
    job_id = str(uuid4())
    with SCANNER_LOCK:
        SCANNER_JOBS[job_id] = {"status": "running", "completed": 0, "total": len(symbols), "results": [], "unavailable": 0, "universe": label}
    SCANNER_EXECUTOR.submit(run_scanner, job_id, timeframe, symbols)
    return jsonify({"job_id": job_id})


@app.get("/api/scanner/<job_id>")
def scanner_status(job_id):
    with SCANNER_LOCK:
        job = SCANNER_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Scanner job not found"}), 404
        return jsonify(job)
@app.get("/api/market-overview")
def market_overview():
    """Small live market snapshot for the dashboard header."""
    indices = {
        "NIFTY 50": "^NSEI", "BANK NIFTY": "^NSEBANK",
        "SENSEX": "^BSESN",
    }
    nse_names = {"NIFTY 50": "NIFTY 50", "BANK NIFTY": "NIFTY BANK"}
    nse_values = {}
    try:
        for item in nse_json("/api/allIndices").get("data", []):
            nse_values[item.get("index")] = item
    except Exception:
        pass
    overview = []
    for name, ticker_symbol in indices.items():
        nse_item = nse_values.get(nse_names.get(name))
        if nse_item:
            try:
                overview.append({
                    "name": name, "price": round(float(nse_item["last"]), 2),
                    "change": round(float(nse_item.get("variation", 0)), 2),
                    "percent": round(float(nse_item.get("percentChange", 0)), 2),
                    "live": False, "data_status": DELAYED,
                })
                continue
            except (KeyError, TypeError, ValueError):
                pass
        try:
            data = yf.Ticker(ticker_symbol).history(period="5d", interval="1d", auto_adjust=False)
            if len(data) < 2:
                raise ValueError("Insufficient market data")
            last, previous = float(data["Close"].iloc[-1]), float(data["Close"].iloc[-2])
            change = last - previous
            overview.append({"name": name, "price": round(last, 2), "change": round(change, 2),
                             "percent": round(change / previous * 100, 2), "live": False, "data_status": DELAYED})
        except Exception:
            overview.append({"name": name, "price": MARKET_FALLBACKS[name], "change": 0,
                             "percent": 0, "live": False, "data_status": STALE,
                             "message": "Last stored fallback. This is not a live quote."})
    return jsonify({"markets": overview})


def _collect_market_movers(value, output):
    """Find mover rows despite small shape changes in NSE's public response."""
    if isinstance(value, list):
        for item in value:
            _collect_market_movers(item, output)
    elif isinstance(value, dict):
        symbol = value.get("symbol") or value.get("symbolName")
        percent = value.get("pChange", value.get("perChange", value.get("percentChange")))
        last = value.get("lastPrice", value.get("last"))
        if symbol and percent is not None:
            try:
                output.append({
                    "name": str(symbol).replace(".NS", ""),
                    "price": round(float(str(last).replace(",", "")), 2) if last is not None else None,
                    "percent": round(float(str(percent).replace(",", "")), 2),
                })
            except (TypeError, ValueError):
                pass
        for child in value.values():
            if isinstance(child, (dict, list)):
                _collect_market_movers(child, output)


@app.get("/api/market-ticker")
def market_ticker():
    """Latest index, mover and US-market values for the scrolling header."""
    items = []
    for name, ticker_symbol in {
        "NIFTY 50": "^NSEI",
        "BANK NIFTY": "^NSEBANK",
        "S&P 500": "^GSPC",
        "NASDAQ": "^IXIC",
        "DOW JONES": "^DJI",
    }.items():
        try:
            data = yf.Ticker(ticker_symbol).history(period="5d", interval="1d", auto_adjust=False)
            if len(data) < 2:
                raise ValueError("Insufficient data")
            last = float(data["Close"].iloc[-1])
            previous = float(data["Close"].iloc[-2])
            if not math.isfinite(last) or not math.isfinite(previous) or previous == 0:
                raise ValueError("Invalid market data")
            items.append({
                "name": name,
                "price": round(last, 2),
                "percent": round((last - previous) / previous * 100, 2),
                "kind": "index",
            })
        except Exception:
            items.append({"name": name, "price": None, "percent": None, "kind": "index"})

    for direction, endpoint in (
        ("Top Gainer", "/api/live-analysis-variations?index=gainers"),
        ("Top Loser", "/api/live-analysis-variations?index=losers"),
    ):
        movers = []
        try:
            _collect_market_movers(nse_json(endpoint), movers)
        except Exception:
            movers = []
        if movers:
            selected = max(movers, key=lambda row: row["percent"]) if direction == "Top Gainer" else min(movers, key=lambda row: row["percent"])
            selected.update({"label": direction, "kind": "mover"})
            items.append(selected)
        else:
            items.append({"name": direction, "label": direction, "price": None, "percent": None, "kind": "mover"})
    return jsonify({"items": items, "updated_at": datetime.now(timezone.utc).isoformat()})


@app.get("/api/sector-trend/<symbol>")
def sector_trend(symbol):
    """Return the matching NSE sector's latest direction for the dashboard."""
    sector, ticker_symbol = SECTOR_INDICES.get(symbol.upper(), (sector_name_for_symbol(symbol), "^NSEI"))
    try:
        data = yf.Ticker(ticker_symbol).history(period="5d", interval="1d", auto_adjust=False)
        if len(data) < 2:
            raise ValueError("Insufficient sector data")
        change = float(data["Close"].iloc[-1] - data["Close"].iloc[-2])
        return jsonify({"sector": sector, "trend": "Bullish" if change >= 0 else "Bearish"})
    except Exception:
        # If the sector index feed is down, retain a useful live direction
        # based on the selected stock instead of leaving the Sector card blank.
        try:
            stock_data = get_symbol_history(symbol)
            if len(stock_data) >= 2:
                change = float(stock_data["Close"].iloc[-1] - stock_data["Close"].iloc[-2])
                return jsonify({"sector": sector, "trend": "Bullish" if change >= 0 else "Bearish", "proxy": True})
        except Exception:
            pass
        return jsonify({"sector": sector, "trend": "Unavailable"})


ANALYSIS_INTRADAY = {"5m", "15m", "1h", "4h", "6h", "12h"}
ANALYSIS_TIMEFRAMES = ANALYSIS_INTRADAY | {"1d", "1wk", "1mo", "3mo", "6mo", "1y", "5y"}


def _display_history(symbol, timeframe, daily):
    if timeframe in ANALYSIS_INTRADAY:
        return get_chart_history(symbol, timeframe)
    if timeframe == "1d":
        return daily
    return resample_chart(daily, timeframe)


ANALYSIS_CACHE = {}
ANALYSIS_CACHE_LOCK = Lock()
ANALYSIS_CACHE_SECONDS = 90
ANALYSIS_LATEST = {}


@app.get("/api/analysis/<symbol>")
def stock_analysis(symbol):
    """Technicals, multi-timeframe zones, and chart overlays for one symbol."""
    timeframe = (request.args.get("timeframe") or "1d").strip()
    if timeframe not in ANALYSIS_TIMEFRAMES:
        return jsonify({"error": "Invalid timeframe"}), 400
    symbol = symbol.upper().strip()
    cache_key = (symbol, timeframe)
    client = (request.args.get("client") or "").strip()[:64]
    token = None
    with ANALYSIS_CACHE_LOCK:
        if client:
            token = ANALYSIS_LATEST.get(client, 0) + 1
            ANALYSIS_LATEST[client] = token
        cached = ANALYSIS_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < ANALYSIS_CACHE_SECONDS:
        return jsonify(cached[1])
    if client:
        set_cancel_check(lambda: ANALYSIS_LATEST.get(client) != token)
    try:
        response = app.make_response(_stock_analysis_uncached(symbol, timeframe))
    except Superseded:
        return jsonify({"superseded": True, "message": "A newer analysis request replaced this one."}), 409
    finally:
        set_cancel_check(None)
    if response.status_code == 200:
        with ANALYSIS_CACHE_LOCK:
            ANALYSIS_CACHE[cache_key] = (time.time(), response.get_json())
            for key in [k for k, v in ANALYSIS_CACHE.items() if time.time() - v[0] >= ANALYSIS_CACHE_SECONDS]:
                ANALYSIS_CACHE.pop(key, None)
    return response


def _stock_analysis_uncached(symbol, timeframe):
    try:
        daily = get_symbol_history(symbol)
        if daily is None or getattr(daily, "empty", True):
            return jsonify({"error": "No market data for this symbol"}), 404
        display = _display_history(symbol, timeframe, daily)
        intraday = {}
        if timeframe in {"5m", "15m", "1h", "4h"} and display is not None and not display.empty:
            intraday[timeframe] = display

        def load_intraday(item):
            if item in intraday:
                return intraday[item]
            return get_chart_history(symbol, item)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(load_intraday, item): item for item in ("5m", "15m", "1h", "4h")}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    frame = future.result()
                except Exception:
                    frame = None
                if frame is not None and not getattr(frame, "empty", True):
                    intraday[item] = frame
        payload = analysis_payload(daily, display, timeframe, intraday)
        if not payload:
            return jsonify({"error": "No market data for this symbol", "data_unavailable": True}), 404
        if payload.get("data_unavailable"):
            payload["symbol"] = symbol
            return jsonify(payload), 404
        payload["symbol"] = symbol
        payload["market"] = YAHOO_PROVIDER.status()
        payload["zone_feed"] = zone_feed(payload, daily)
        return jsonify(payload)
    except Exception as error:
        return jsonify({"error": str(error)}), 500


@app.get("/api/technical-scanner/catalog")
def technical_scanner_catalog():
    return jsonify({
        "categories": SCAN_CATALOG,
        "logic": ["AND", "OR"],
        "timeframes": [{"id": item, "label": item} for item in SCAN_TIMEFRAMES],
        "candle_basis": "selected timeframe",
        "notes": [
            "RSI, MACD, ADX, EMA, patterns, volume, and VWAP use the selected scanner timeframe.",
            "Daily, weekly, and monthly trend and zone conditions always use those timeframes.",
            "Session VWAP is unavailable on daily, weekly, and monthly scans.",
            "Missing intraday history is data-unavailable. Daily candles are not substituted.",
            "Golden cross and death cross are true only on the bar where SMA 50 crosses SMA 200.",
            "Double bottom and double top are rule-based: two pivots within 3%, a neckline at least 3% away, and a close through the neckline.",
            "Head and shoulders, triangles, cup and handle, flags, and wedges are not calculated.",
            "52-week high needs at least 240 daily bars. With fewer bars that condition is unavailable.",
            "Price range is unavailable until a minimum or maximum price is entered.",
            "NOT and nested condition groups are not calculated. Logic is flat AND or flat OR.",
        ],
        "indicator_defaults": INDICATOR_DEFAULTS,
        "param_defaults": SCAN_PARAM_DEFAULTS,
    })


def _scanner_frame(symbol, timeframe, daily, fresh=False):
    if timeframe == "1d":
        return daily
    if timeframe in {"1wk", "1mo"}:
        return resample_chart(daily, timeframe)
    return get_chart_history(symbol, timeframe, fresh=fresh)


def _attach_scan_mtf(found, symbol, daily, timeframe, scan_df, fresh=False):
    frames = {}
    for item in ("5m", "15m", "1h", "4h"):
        if item == timeframe and scan_df is not None and not getattr(scan_df, "empty", True):
            frames[item] = scan_df
            continue
        try:
            loaded = get_chart_history(symbol, item, fresh=fresh)
        except Exception:
            loaded = None
        if loaded is not None and not getattr(loaded, "empty", True):
            frames[item] = loaded
    board = mtf_board(daily, frames, found.get("zone_map") or {})
    found["mtf"] = board
    found["mtf_status"] = mtf_status_text(board)
    found.pop("zone_map", None)
    return found


def run_technical_scanner(job_id, symbols, condition_ids, logic, timeframe, fresh=False, params=None):
    def scan(row):
        symbol = str(row.Symbol).strip().upper()
        company = "" if pd.isna(row.Company) else str(row.Company)

        def progress(stage):
            with TECHNICAL_LOCK:
                job = TECHNICAL_JOBS.get(job_id)
                if job:
                    job["current_symbol"] = symbol
                    job["stage"] = stage

        try:
            progress("Loading history")
            history = get_symbol_history(symbol, fresh=fresh)
            if history is None or getattr(history, "empty", True):
                return "unavailable"
            scan_df = _scanner_frame(symbol, timeframe, history, fresh=fresh)
            if timeframe in INTRADAY_SCAN and (scan_df is None or getattr(scan_df, "empty", True)):
                return "unavailable"
            found = scan_symbol(
                scan_df,
                symbol,
                company,
                condition_ids,
                logic,
                daily_df=history,
                timeframe=timeframe,
                progress=progress,
                fresh=fresh,
                params=params,
            )
            if isinstance(found, dict):
                industry = getattr(row, "Industry", "")
                found["industry"] = "" if industry is None or (isinstance(industry, float) and pd.isna(industry)) else str(industry)
            if not isinstance(found, dict):
                return found
            progress("Multi-timeframe")
            return _attach_scan_mtf(found, symbol, history, timeframe, scan_df, fresh=fresh)
        except Exception:
            app.logger.exception("Technical scan failed for %s", symbol)
            return "unavailable"

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(scan, row) for row in symbols.itertuples(index=False)]
        for future in as_completed(futures):
            found = future.result()
            with TECHNICAL_LOCK:
                job = TECHNICAL_JOBS[job_id]
                job["completed"] += 1
                if found == "unavailable":
                    job["unavailable"] += 1
                elif isinstance(found, dict):
                    job["results"].append(found)
    with TECHNICAL_LOCK:
        job = TECHNICAL_JOBS[job_id]
        job["results"].sort(
            key=lambda row: (row.get("change_pct") is not None, row.get("change_pct") or 0),
            reverse=True,
        )
        job["status"] = "complete"


@app.post("/api/technical-scanner")
def start_technical_scanner():
    settings = request.get_json(silent=True) or {}
    logic = settings.get("logic", "AND")
    if logic not in {"AND", "OR"}:
        return jsonify({"error": "Scanner logic must be AND or OR"}), 400
    timeframe = str(settings.get("timeframe") or "1d").strip()
    if timeframe not in SCAN_TIMEFRAMES:
        return jsonify({"error": "Invalid scanner timeframe"}), 400
    condition_ids = [item for item in settings.get("conditions", []) if item in CONDITION_LABELS]
    if not condition_ids:
        return jsonify({"error": "Select at least one calculated condition"}), 400
    index_key = settings.get("universe", "nifty50")
    if index_key not in INDEX_UNIVERSES:
        return jsonify({"error": "Invalid index universe"}), 400
    limit = settings.get("limit")
    try:
        label, symbols = load_index_universe(index_key)
    except Exception as error:
        return jsonify({"error": f"Could not load index list: {error}"}), 503
    if limit not in (None, ""):
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return jsonify({"error": "Symbol limit must be a number"}), 400
        if limit < 1 or limit > 500:
            return jsonify({"error": "Symbol limit must be between 1 and 500"}), 400
        symbols = symbols.head(limit)
    job_id = str(uuid4())
    fresh = bool(settings.get("fresh"))
    params = clean_scan_params(settings.get("params"))
    if params.get("rsi_min") is not None and params.get("rsi_max") is not None and params["rsi_min"] > params["rsi_max"]:
        return jsonify({"error": "RSI minimum must not be above the maximum"}), 400
    if params.get("price_min") is not None and params.get("price_max") is not None and params["price_min"] > params["price_max"]:
        return jsonify({"error": "Minimum price must not be above the maximum price"}), 400
    with TECHNICAL_LOCK:
        TECHNICAL_JOBS[job_id] = {
            "status": "running",
            "completed": 0,
            "total": len(symbols),
            "unavailable": 0,
            "results": [],
            "universe": label,
            "logic": logic,
            "timeframe": timeframe,
            "current_symbol": None,
            "stage": "Loading history",
            "fresh": fresh,
            "params": params,
        }
    TECHNICAL_EXECUTOR.submit(run_technical_scanner, job_id, symbols, condition_ids, logic, timeframe, fresh, params)
    return jsonify({"job_id": job_id})


@app.get("/api/technical-scanner/<job_id>")
def technical_scanner_status(job_id):
    with TECHNICAL_LOCK:
        job = TECHNICAL_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Technical scan job not found"}), 404
        return jsonify(job)


def _signal_frame(symbol, timeframe, fresh=False):
    """Candles for the requested timeframe only. Missing history is not replaced."""
    if timeframe in ANALYSIS_INTRADAY:
        return get_chart_history(symbol, timeframe, fresh=fresh)
    daily = get_symbol_history(symbol, fresh=fresh)
    if daily is None or getattr(daily, "empty", True):
        return daily
    if timeframe == "1d":
        return daily
    return resample_chart(daily, timeframe)


def _run_backtest_job(job_id, frame, timeframe, strategy, capital, risk_pct, start, end, symbol):
    def progress(text):
        with BACKTEST_LOCK:
            job = BACKTEST_JOBS.get(job_id)
            if job:
                job["stage"] = text

    try:
        result = run_backtest(
            frame,
            timeframe,
            strategy=strategy,
            capital=capital,
            risk_pct=risk_pct,
            start=start,
            end=end,
            progress=progress,
        )
        for trade in result.get("trades") or []:
            trade["symbol"] = symbol
        with INTELLIGENCE_LOCK:
            for trade in result.get("trades") or []:
                SIGNAL_HISTORY.insert(0, dict(trade))
            del SIGNAL_HISTORY[300:]
        result["symbol"] = symbol
        result = extend_backtest_report(result)
        try:
            PAPER_BOOK.save_backtest(result)
        except Exception:
            app.logger.exception("Could not store the backtest run")
        with BACKTEST_LOCK:
            job = BACKTEST_JOBS[job_id]
            job["result"] = result
            job["status"] = "unavailable" if result.get("data_unavailable") else "complete"
            job["stage"] = "Completed"
    except Exception as error:
        app.logger.exception("Backtest failed for %s", symbol)
        with BACKTEST_LOCK:
            BACKTEST_JOBS[job_id]["status"] = "error"
            BACKTEST_JOBS[job_id]["error"] = str(error)
            BACKTEST_JOBS[job_id]["stage"] = "Error"


@app.get("/api/signal/<symbol>")
def stock_signal(symbol):
    timeframe = (request.args.get("timeframe") or "1d").strip()
    if timeframe not in ANALYSIS_TIMEFRAMES:
        return jsonify({"error": "Invalid timeframe"}), 400
    fresh = request.args.get("fresh") in {"1", "true", "True"}
    symbol = symbol.upper().strip()
    try:
        frame = _signal_frame(symbol, timeframe, fresh)
    except Exception as error:
        return jsonify({"error": str(error), "data_unavailable": True, "message": "DATA UNAVAILABLE"}), 503
    if frame is None or getattr(frame, "empty", True):
        return jsonify({
            "symbol": symbol,
            "timeframe": timeframe,
            "data_unavailable": True,
            "message": "DATA UNAVAILABLE",
        }), 404
    include_vwap = timeframe in ANALYSIS_INTRADAY
    payload = build_signal(frame, include_vwap=include_vwap, zones_calculated=True)
    payload["symbol"] = symbol
    payload["timeframe"] = timeframe
    if payload.get("data_unavailable"):
        payload["message"] = payload.get("message") or "DATA UNAVAILABLE"
        return jsonify(payload), 404
    return jsonify(payload)


@app.post("/api/backtest")
def start_backtest():
    body = request.get_json(silent=True) or {}
    symbol = str(body.get("symbol") or "").upper().strip()
    timeframe = str(body.get("timeframe") or "1d").strip()
    strategy = str(body.get("strategy") or "indicator").strip()
    if not symbol:
        return jsonify({"error": "Symbol is required"}), 400
    if timeframe not in ANALYSIS_TIMEFRAMES:
        return jsonify({"error": "Invalid timeframe"}), 400
    if strategy not in {"indicator", "structure"}:
        return jsonify({"error": "Strategy must be indicator or structure"}), 400
    try:
        capital = float(100000 if body.get("capital") in (None, "") else body.get("capital"))
        risk_pct = float(1 if body.get("risk_pct") in (None, "") else body.get("risk_pct"))
    except (TypeError, ValueError):
        return jsonify({"error": "Capital and risk must be numbers"}), 400
    if capital <= 0 or risk_pct <= 0 or risk_pct > 100:
        return jsonify({"error": "Capital must be positive and risk must be between 0 and 100"}), 400
    fresh = bool(body.get("fresh"))
    try:
        frame = _signal_frame(symbol, timeframe, fresh)
    except Exception as error:
        return jsonify({"error": str(error), "data_unavailable": True, "message": "DATA UNAVAILABLE"}), 503
    if frame is None or getattr(frame, "empty", True):
        return jsonify({
            "symbol": symbol,
            "timeframe": timeframe,
            "data_unavailable": True,
            "message": "DATA UNAVAILABLE",
        }), 404
    job_id = str(uuid4())
    with BACKTEST_LOCK:
        BACKTEST_JOBS[job_id] = {
            "job_id": job_id,
            "status": "running",
            "stage": "Preparing candles",
            "symbol": symbol,
            "timeframe": timeframe,
            "strategy": strategy,
            "result": None,
            "error": None,
        }
    BACKTEST_EXECUTOR.submit(
        _run_backtest_job,
        job_id,
        frame,
        timeframe,
        strategy,
        capital,
        risk_pct,
        body.get("start") or None,
        body.get("end") or None,
        symbol,
    )
    return jsonify({"job_id": job_id})


def _backtest_job(job_id):
    with BACKTEST_LOCK:
        job = BACKTEST_JOBS.get(job_id)
        if not job:
            return None
        return {
            "job_id": job_id,
            "status": job.get("status"),
            "stage": job.get("stage"),
            "error": job.get("error"),
            "symbol": job.get("symbol"),
            "timeframe": job.get("timeframe"),
            "strategy": job.get("strategy"),
            "result": job.get("result"),
        }


@app.get("/api/backtest/<job_id>")
def backtest_status(job_id):
    job = _backtest_job(job_id)
    if not job:
        return jsonify({"error": "Backtest job not found"}), 404
    return jsonify(job)


@app.get("/api/backtest/<job_id>/trades")
def backtest_trades(job_id):
    job = _backtest_job(job_id)
    if not job:
        return jsonify({"error": "Backtest job not found"}), 404
    result = job.get("result") or {}
    return jsonify({"job_id": job_id, "status": job.get("status"), "trades": result.get("trades") or []})


@app.get("/api/backtest/<job_id>/equity")
def backtest_equity(job_id):
    job = _backtest_job(job_id)
    if not job:
        return jsonify({"error": "Backtest job not found"}), 404
    result = job.get("result") or {}
    return jsonify({"job_id": job_id, "status": job.get("status"), "equity": result.get("equity") or []})


def _run_chart_backtest_job(job_id, frame, timeframe, config, capital, risk_pct, start, end, symbol):
    def progress(text, percent=None):
        with BACKTEST_LOCK:
            job = BACKTEST_JOBS.get(job_id)
            if job:
                job["stage"] = text
                if percent is not None:
                    job["progress"] = percent

    try:
        result = run_chart_backtest(
            frame, timeframe, config, symbol=symbol, start=start, end=end,
            capital=capital, risk_pct=risk_pct, progress=progress,
        )
        if not result.get("data_unavailable"):
            result = extend_backtest_report(result)
            try:
                PAPER_BOOK.save_backtest(result)
            except Exception:
                app.logger.exception("Could not store the chart backtest run")
        with BACKTEST_LOCK:
            job = BACKTEST_JOBS[job_id]
            job["result"] = result
            job["status"] = "unavailable" if result.get("data_unavailable") else "complete"
            job["stage"] = "Completed"
            job["progress"] = 100
    except ChartBacktestConfigError as error:
        with BACKTEST_LOCK:
            BACKTEST_JOBS[job_id].update({"status": "error", "error": str(error), "stage": "Stopped"})
    except Exception as error:
        app.logger.exception("Chart backtest failed for %s", symbol)
        with BACKTEST_LOCK:
            BACKTEST_JOBS[job_id].update({"status": "error", "error": str(error), "stage": "Error"})


@app.post("/api/chart-backtest")
def start_chart_backtest():
    body = request.get_json(silent=True) or {}
    symbol = str(body.get("symbol") or "").upper().strip()
    timeframe = str(body.get("timeframe") or "1d").strip()
    if not symbol:
        return jsonify({"error": "Symbol is required"}), 400
    if timeframe not in ANALYSIS_TIMEFRAMES:
        return jsonify({"error": "Invalid timeframe"}), 400
    try:
        config = clean_chart_backtest_config(body, timeframe)
    except ChartBacktestConfigError as error:
        return jsonify({"error": str(error)}), 400
    try:
        capital = float(100000 if body.get("capital") in (None, "") else body.get("capital"))
        risk_pct = float(1 if body.get("risk_pct") in (None, "") else body.get("risk_pct"))
    except (TypeError, ValueError):
        return jsonify({"error": "Capital and risk must be numbers"}), 400
    if capital <= 0 or risk_pct <= 0 or risk_pct > 100:
        return jsonify({"error": "Capital must be positive and risk must be between 0 and 100"}), 400
    try:
        frame = _signal_frame(symbol, timeframe, bool(body.get("fresh")))
    except Exception as error:
        return jsonify({"error": str(error), "data_unavailable": True, "message": "DATA UNAVAILABLE"}), 503
    if frame is None or getattr(frame, "empty", True):
        return jsonify({"symbol": symbol, "timeframe": timeframe, "data_unavailable": True, "message": "DATA UNAVAILABLE"}), 404
    job_id = str(uuid4())
    with BACKTEST_LOCK:
        BACKTEST_JOBS[job_id] = {
            "job_id": job_id, "status": "running", "stage": "Waiting for the backtest worker", "progress": 0,
            "symbol": symbol, "timeframe": timeframe, "strategy": "chart", "result": None, "error": None,
        }
    BACKTEST_EXECUTOR.submit(
        _run_chart_backtest_job, job_id, frame, timeframe, config, capital, risk_pct,
        body.get("start") or None, body.get("end") or None, symbol,
    )
    return jsonify({"job_id": job_id})


@app.get("/api/chart-backtest/<job_id>")
def chart_backtest_status(job_id):
    with BACKTEST_LOCK:
        job = BACKTEST_JOBS.get(job_id)
        if not job or job.get("strategy") != "chart":
            return jsonify({"error": "Chart backtest job not found"}), 404
        return jsonify(dict(job))


@app.get("/api/chart-backtest/run/<run_id>")
def chart_backtest_run(run_id):
    result = PAPER_BOOK.load_backtest(run_id)
    if not result or result.get("strategy") != "chart":
        return jsonify({"error": "Stored chart backtest not found"}), 404
    return jsonify(result)


@app.post("/api/alerts/evaluate")
def evaluate_alerts():
    """Check alert rules against real candles. Results stay in this response.

    Nothing is emailed, pushed, or sent to a broker.
    """
    body = request.get_json(silent=True) or {}
    symbol = str(body.get("symbol") or "").upper().strip()
    rules = body.get("rules") or []
    if not symbol or not isinstance(rules, list) or not rules:
        return jsonify({"error": "Symbol and at least one rule are required"}), 400
    timeframe = str(body.get("timeframe") or "1d").strip()
    if timeframe not in set(SCAN_TIMEFRAMES) | ANALYSIS_INTRADAY:
        return jsonify({"error": "Invalid timeframe"}), 400
    try:
        daily = get_symbol_history(symbol)
    except Exception as error:
        return jsonify({"error": str(error), "data_unavailable": True}), 503
    if daily is None or getattr(daily, "empty", True):
        return jsonify({"error": "No market data for this symbol", "data_unavailable": True}), 404
    include_vwap = timeframe in INTRADAY_SCAN
    if include_vwap:
        scan_df = get_chart_history(symbol, timeframe)
        if scan_df is None or getattr(scan_df, "empty", True):
            return jsonify({
                "symbol": symbol,
                "timeframe": timeframe,
                "data_unavailable": True,
                "message": f"No {timeframe} candles. The alert was not evaluated on daily candles.",
                "results": [],
                "delivery": "In-app only. Nothing is emailed, pushed, or sent to a broker.",
            }), 404
    elif timeframe in {"1wk", "1mo"}:
        scan_df = resample_chart(daily, timeframe)
    else:
        scan_df = daily
    context = build_context(scan_df, daily_df=daily, include_zones=True, include_vwap=include_vwap)
    if context is None:
        return jsonify({
            "symbol": symbol,
            "data_unavailable": True,
            "message": "Not enough candles to evaluate alerts.",
            "results": [],
        }), 404
    results = []
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("type") not in ALERT_TYPES:
            results.append({
                "id": None if not isinstance(rule, dict) else rule.get("id"),
                "type": None if not isinstance(rule, dict) else rule.get("type"),
                "triggered": False,
                "state": "unavailable",
                "detail": "Unknown alert type",
                "delivery": "in-app only",
            })
            continue
        outcome = evaluate_alert(rule, context)
        item = {
            "id": rule.get("id"),
            "type": rule.get("type"),
            "triggered": bool(outcome.get("triggered")),
            "state": outcome.get("state"),
            "detail": outcome.get("detail"),
            "delivery": "in-app only",
        }
        candle_time = None
        if scan_df is not None and not getattr(scan_df, "empty", True):
            candle_time = int(pd.Timestamp(scan_df.index[-1]).timestamp())
        item["candle_time"] = candle_time
        if item["triggered"] and candle_time is not None:
            first = MARKET_STORE.remember_alert(alert_key(symbol, timeframe, item["type"], candle_time))
            item["duplicate"] = not first
        else:
            item["duplicate"] = False
        results.append(item)
        if item["triggered"] and not item["duplicate"]:
            with ALERT_LOCK:
                ALERT_EVENTS.insert(0, {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "time": datetime.now(timezone.utc).isoformat(),
                    **item,
                })
                del ALERT_EVENTS[50:]
    return jsonify({
        "symbol": symbol,
        "timeframe": timeframe,
        "results": results,
        "delivery": "In-app only. Nothing is emailed, pushed, or sent to a broker.",
    })


def _intelligence_job(kind, job_id, frame, timeframe, body, symbol):
    try:
        if kind == "replay":
            result = build_replay(frame, timeframe, body.get("start"), body.get("end"))
        else:
            result = compare_presets(
                frame,
                timeframe,
                capital=float(body.get("capital") or 100000),
                risk_pct=float(body.get("risk_pct") or 1),
                start=body.get("start"),
                end=body.get("end"),
            )
        status = "unavailable" if result.get("data_unavailable") else "complete"
        error = None
    except Exception as error_value:
        app.logger.exception("%s failed for %s", kind, symbol)
        result = None
        status = "error"
        error = str(error_value)
    with INTELLIGENCE_LOCK:
        job = INTELLIGENCE_JOBS[job_id]
        job["result"] = result
        job["status"] = status
        job["error"] = error


def _start_intelligence(kind):
    body = request.get_json(silent=True) or {}
    symbol = str(body.get("symbol") or "").upper().strip()
    timeframe = str(body.get("timeframe") or "1d").strip()
    if not symbol:
        return jsonify({"error": "Symbol is required"}), 400
    if timeframe not in ANALYSIS_TIMEFRAMES:
        return jsonify({"error": "Invalid timeframe"}), 400
    try:
        frame = _signal_frame(symbol, timeframe, bool(body.get("fresh")))
    except Exception as error:
        return jsonify({"error": str(error), "data_unavailable": True, "message": "DATA UNAVAILABLE"}), 503
    if frame is None or getattr(frame, "empty", True):
        return jsonify({"symbol": symbol, "timeframe": timeframe, "data_unavailable": True, "message": "DATA UNAVAILABLE"}), 404
    job_id = str(uuid4())
    with INTELLIGENCE_LOCK:
        INTELLIGENCE_JOBS[job_id] = {
            "job_id": job_id,
            "kind": kind,
            "status": "running",
            "symbol": symbol,
            "timeframe": timeframe,
            "result": None,
            "error": None,
        }
    BACKTEST_EXECUTOR.submit(_intelligence_job, kind, job_id, frame, timeframe, body, symbol)
    return jsonify({"job_id": job_id})


@app.post("/api/replay")
def start_replay():
    return _start_intelligence("replay")


@app.get("/api/replay/<job_id>")
def replay_status(job_id):
    with INTELLIGENCE_LOCK:
        job = INTELLIGENCE_JOBS.get(job_id)
        if not job or job.get("kind") != "replay":
            return jsonify({"error": "Replay job not found"}), 404
        return jsonify(dict(job))


@app.post("/api/strategies/compare")
def start_strategy_compare():
    return _start_intelligence("compare")


@app.get("/api/strategies/compare/<job_id>")
def strategy_compare_status(job_id):
    with INTELLIGENCE_LOCK:
        job = INTELLIGENCE_JOBS.get(job_id)
        if not job or job.get("kind") != "compare":
            return jsonify({"error": "Comparison job not found"}), 404
        return jsonify(dict(job))


@app.post("/api/position-size")
def position_size_route():
    body = request.get_json(silent=True) or {}
    result = position_size(body.get("capital"), body.get("risk_pct"), body.get("entry"), body.get("stop_loss"))
    code = 400 if result.get("data_unavailable") else 200
    return jsonify(result), code


@app.get("/api/signal-history")
def signal_history():
    side = str(request.args.get("side") or "").upper()
    result_filter = str(request.args.get("result") or "").lower()
    symbol = str(request.args.get("symbol") or "").upper()
    timeframe = str(request.args.get("timeframe") or "")
    with INTELLIGENCE_LOCK:
        rows = list(SIGNAL_HISTORY)
    if side in {"BUY", "SELL"}:
        rows = [row for row in rows if row.get("side") == side]
    if symbol:
        rows = [row for row in rows if str(row.get("symbol") or "").upper() == symbol]
    if timeframe:
        rows = [row for row in rows if row.get("timeframe") == timeframe]
    if result_filter == "win":
        rows = [row for row in rows if (row.get("pnl") or 0) > 0]
    elif result_filter == "loss":
        rows = [row for row in rows if (row.get("pnl") or 0) < 0]
    return jsonify({
        "trades": rows[:200],
        "note": "This list contains backtest trades from the current server session. It is not a live fill record.",
    })


@app.post("/api/watchlist/intelligence")
def watchlist_intelligence():
    """Cheap indicator snapshot per symbol. Zones stay unavailable unless requested."""
    body = request.get_json(silent=True) or {}
    symbols = []
    for item in body.get("symbols") or []:
        text = str(item or "").upper().strip()
        if text and text not in symbols:
            symbols.append(text)
    if not symbols:
        return jsonify({"error": "At least one symbol is required"}), 400
    include_zones = bool(body.get("include_zones"))
    symbols = symbols[:8 if include_zones else 12]
    timeframe = str(body.get("timeframe") or "1d").strip()
    if timeframe not in ANALYSIS_TIMEFRAMES:
        return jsonify({"error": "Invalid timeframe"}), 400
    from services.technical_analysis import (
        _snapshot_values,
        add_indicators,
        cached_nearest_zones,
        prepare_ohlcv,
        resample_chart,
        trend_from_frame,
    )
    from services.signal_engine import build_signal_from_enriched
    rows = []
    for symbol in symbols:
        try:
            frame = _signal_frame(symbol, timeframe, False)
        except Exception:
            frame = None
        if frame is None or getattr(frame, "empty", True):
            rows.append({"symbol": symbol, "data_unavailable": True, "message": "Data Unavailable"})
            continue
        enriched = add_indicators(prepare_ohlcv(frame), include_vwap=timeframe in ANALYSIS_INTRADAY)
        if enriched.empty:
            rows.append({"symbol": symbol, "data_unavailable": True, "message": "Data Unavailable"})
            continue
        zones = None
        zones_ready = False
        if include_zones and timeframe == "1d":
            zones = cached_nearest_zones(enriched, ("1d",), (symbol, str(enriched.index[-1]))).get("1d")
            zones_ready = True
        signal = build_signal_from_enriched(
            enriched,
            include_vwap=timeframe in ANALYSIS_INTRADAY,
            zones=zones,
            zones_calculated=zones_ready,
        )
        values = _snapshot_values(enriched)
        daily = enriched if timeframe == "1d" else None
        if daily is None:
            try:
                daily = prepare_ohlcv(get_symbol_history(symbol))
            except Exception:
                daily = None
        mtf = []
        if daily is None or daily.empty:
            mtf = [
                {"label": "Monthly", "status": "Unavailable"},
                {"label": "Weekly", "status": "Unavailable"},
                {"label": "Daily", "status": "Unavailable"},
            ]
        else:
            for label, rule in (("Monthly", "1mo"), ("Weekly", "1wk"), ("Daily", "1d")):
                source = daily if rule == "1d" else resample_chart(daily, rule)
                mtf.append({"label": label, "status": trend_from_frame(source).get("status") or "Unavailable"})
        rows.append({
            "symbol": symbol,
            "timeframe": timeframe,
            "price": values.get("price"),
            "trend": trend_from_frame(enriched).get("status"),
            "signal": signal.get("signal"),
            "label": signal.get("label"),
            "score": signal.get("strength"),
            "entry": signal.get("entry"),
            "stop_loss": signal.get("stop_loss"),
            "target_1": signal.get("target_1"),
            "target_2": signal.get("target_2"),
            "demand": signal.get("demand") if zones_ready else None,
            "supply": signal.get("supply") if zones_ready else None,
            "demand_state": "calculated" if zones_ready else "unavailable",
            "supply_state": "calculated" if zones_ready else "unavailable",
            "rsi": values.get("rsi"),
            "macd": values.get("macd"),
            "volume": values.get("volume"),
            "rvol": values.get("rvol"),
            "mtf": mtf,
            "setups": [],
            "signal_age": None,
        })
    return jsonify({
        "rows": rows,
        "delivery": "Calculated from stored candles. This is not a broker order book.",
        "zones": "included" if include_zones and timeframe == "1d" else "Data Unavailable unless Include zones is selected on a daily timeframe",
    })


@app.get("/api/fundamentals/<symbol>")
def fundamentals_one(symbol):
    """Public fundamental fields for one symbol. Missing fields stay null."""
    symbol = symbol.upper().strip()
    try:
        info = yf.Ticker(f"{symbol}.NS").get_info() or {}
    except Exception:
        return jsonify({"symbol": symbol, "data_unavailable": True, "message": "Data Unavailable"}), 404
    if not info or (info.get("regularMarketPrice") in (None, 0) and not info.get("marketCap")):
        return jsonify({"symbol": symbol, "data_unavailable": True, "message": "Data Unavailable"}), 404
    try:
        scan = get_fundamental_scan(symbol, info.get("shortName") or symbol)
    except Exception:
        return jsonify({"symbol": symbol, "data_unavailable": True, "message": "Data Unavailable"}), 404
    return jsonify({
        "symbol": symbol,
        "company": info.get("shortName") or info.get("longName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "market_cap": info.get("marketCap"),
        "market_cap_status": "Data Unavailable" if info.get("marketCap") in (None, "") else "reported",
        "checks": scan.get("checks"),
        "score": scan.get("score"),
        "opm": scan.get("opm"),
        "roe": scan.get("roe"),
        "debt_equity": scan.get("debt_equity"),
        "news": "Data Unavailable",
    })


def _market_provider(name=None):
    return MARKET_PROVIDERS.get(str(name or "yahoo").strip().lower())


def _closed_signal_frame(symbol, timeframe):
    timeframe = normalise_timeframe(timeframe)
    if timeframe in ANALYSIS_TIMEFRAMES or timeframe in {"1d", "1wk", "1mo"}:
        frame = _signal_frame(symbol, timeframe)
    else:
        frame = get_chart_history(symbol, timeframe)
    if frame is None or getattr(frame, "empty", True):
        return None
    closed = drop_open_candle(frame, timeframe)
    if closed is None or getattr(closed, "empty", True):
        return None
    return closed


def _signal_row(symbol, timeframe, include_zones=False):
    closed = _closed_signal_frame(symbol, timeframe)
    if closed is None or len(closed) < 40:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "signal": "DATA UNAVAILABLE",
            "data_status": "UNAVAILABLE",
            "stream": NOT_CONNECTED,
            "message": "DATA UNAVAILABLE",
        }
    include_vwap = timeframe in INTRADAY_SCAN
    zones = None
    if include_zones and timeframe == "1d":
        from services.technical_analysis import cached_nearest_zones
        zones = cached_nearest_zones(closed, ("1d",), (symbol, str(closed.index[-1]))).get("1d")
    signal = build_signal(closed, include_vwap=include_vwap, zones=zones, zones_calculated=bool(include_zones and zones))
    if signal.get("data_unavailable"):
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "signal": "DATA UNAVAILABLE",
            "data_status": "UNAVAILABLE",
            "stream": NOT_CONNECTED,
            "message": "DATA UNAVAILABLE",
        }
    plan = trade_plan(signal, {"demand": (zones or {}).get("demand"), "supply": (zones or {}).get("supply")} if zones else None)
    confirm = confirmation_engine(signal, zones, None)
    row = {
        "symbol": symbol,
        "timeframe": timeframe,
        "ltp": signal.get("price"),
        "signal": signal.get("signal"),
        "label": signal.get("label"),
        "score": signal.get("strength"),
        "entry": signal.get("entry"),
        "stop_loss": signal.get("stop_loss"),
        "target_1": signal.get("target_1"),
        "target_2": signal.get("target_2"),
        "target_3": plan.get("target_3"),
        "risk_reward": signal.get("risk_reward"),
        "reasons": signal.get("reasons") or [],
        "invalidation": confirm.get("invalidation"),
        "trend": signal.get("trend"),
        "demand": signal.get("demand") or "Unavailable",
        "supply": signal.get("supply") or "Unavailable",
        "signal_time": signal.get("time"),
        "data_status": DELAYED,
        "stream": NOT_CONNECTED,
        "market_status": market_status(),
        "zones": "Calculated" if include_zones and zones else "Unavailable",
        "message": "Signal uses the last closed candle from delayed Yahoo data. It is not a live signal.",
    }
    MARKET_STORE.save_signal(row)
    return row


def _live_scan_once(job_id, symbols, condition_ids, logic, timeframe):
    scanned = matches = unavailable = errors = 0
    rows = []
    for row in symbols:
        with LIVE_SCAN_LOCK:
            if LIVE_SCAN["stop"] or LIVE_SCAN["job_id"] != job_id:
                return
        symbol = str(row.Symbol).strip().upper()
        scanned += 1
        try:
            history = get_symbol_history(symbol)
            if history is None or getattr(history, "empty", True):
                unavailable += 1
                continue
            scan_df = _scanner_frame(symbol, timeframe, history)
            if timeframe in INTRADAY_SCAN and (scan_df is None or getattr(scan_df, "empty", True)):
                unavailable += 1
                continue
            found = scan_symbol(scan_df, symbol, "", condition_ids, logic, daily_df=history, timeframe=timeframe)
            if found == "unavailable" or found is None:
                if found == "unavailable":
                    unavailable += 1
            elif found:
                matches += 1
                rows.append({
                    "symbol": found.get("symbol"),
                    "ltp": found.get("ltp"),
                    "change_pct": found.get("change_pct"),
                    "score": found.get("score"),
                    "rsi": found.get("rsi"),
                    "macd": found.get("macd"),
                    "trend": found.get("trend"),
                    "demand": found.get("demand_zone") or "Unavailable",
                    "supply": found.get("supply_zone") or "Unavailable",
                    "data_status": DELAYED,
                })
        except Exception:
            errors += 1
        with LIVE_SCAN_LOCK:
            if LIVE_SCAN["job_id"] == job_id:
                LIVE_SCAN["scanned"] = scanned
                LIVE_SCAN["matches"] = matches
                LIVE_SCAN["unavailable"] = unavailable
                LIVE_SCAN["errors"] = errors
    with LIVE_SCAN_LOCK:
        if LIVE_SCAN["job_id"] != job_id:
            return
        LIVE_SCAN["results"] = rows
        LIVE_SCAN["last_update"] = datetime.now(timezone.utc).isoformat()
        LIVE_SCAN["scanned"] = scanned
        LIVE_SCAN["matches"] = matches
        LIVE_SCAN["unavailable"] = unavailable
        LIVE_SCAN["errors"] = errors
        LIVE_SCAN["data_status"] = DELAYED
        LIVE_SCAN["message"] = "Cycle used delayed Yahoo candles. No live stream is connected."


def _live_scan_loop(job_id, symbols, condition_ids, logic, timeframe):
    try:
        while True:
            with LIVE_SCAN_LOCK:
                if LIVE_SCAN["stop"] or LIVE_SCAN["job_id"] != job_id:
                    break
            _live_scan_once(job_id, symbols, condition_ids, logic, timeframe)
            for _ in range(120):
                with LIVE_SCAN_LOCK:
                    if LIVE_SCAN["stop"] or LIVE_SCAN["job_id"] != job_id:
                        break
                time.sleep(1)
            else:
                continue
            break
    finally:
        with LIVE_SCAN_LOCK:
            if LIVE_SCAN["job_id"] == job_id:
                LIVE_SCAN["running"] = False
                LIVE_SCAN["stop"] = True


@app.get("/api/market/status")
def market_feed_status():
    return jsonify({
        "market_status": market_status(),
        "timezone": "Asia/Kolkata",
        "provider": YAHOO_PROVIDER.status(),
        "brokers": [MARKET_PROVIDERS[name].status() for name in ("zerodha", "upstox", "angelone", "dhan", "fyers")],
        "stream": NOT_CONNECTED,
        "transport": "poll",
        "candle_timeframes": list(CANDLE_TIMEFRAMES),
        "message": "LIVE is not connected. Yahoo Finance quotes are delayed.",
    })


@app.get("/api/market/providers")
def market_providers():
    return jsonify({"providers": [item.status() for item in MARKET_PROVIDERS.values()], "active": "yahoo", "stream": NOT_CONNECTED})


@app.get("/api/market/quote/<symbol>")
def market_quote(symbol):
    provider = _market_provider(request.args.get("provider"))
    if provider is None:
        return jsonify({"data_status": NOT_CONNECTED, "message": "Unknown provider"}), 404
    quote = provider.get_quote(symbol, request.args.get("exchange") or "NSE", normalise_timeframe(request.args.get("timeframe") or "1d"))
    code = 200 if quote.get("ltp") is not None else 404
    return jsonify(quote), code


@app.post("/api/market/quotes")
def market_quotes():
    body = request.get_json(silent=True) or {}
    provider = _market_provider(body.get("provider"))
    if provider is None:
        return jsonify({"data_status": NOT_CONNECTED, "message": "Unknown provider"}), 404
    symbols = body.get("symbols") or []
    if not isinstance(symbols, list) or not symbols:
        return jsonify({"error": "symbols is required"}), 400
    rows = provider.get_quotes(symbols, body.get("exchange") or "NSE", normalise_timeframe(body.get("timeframe") or "1d"))
    return jsonify({"quotes": rows, "stream": NOT_CONNECTED, "data_status": DELAYED, "capped": len(symbols) > 20})


@app.get("/api/market/candles/<symbol>/<timeframe>")
def market_candles(symbol, timeframe):
    provider = _market_provider(request.args.get("provider"))
    if provider is None:
        return jsonify({"data_status": NOT_CONNECTED, "message": "Unknown provider"}), 404
    kind = (request.args.get("kind") or "historical").lower()
    if kind == "intraday":
        payload = provider.get_intraday_candles(symbol, timeframe, request.args.get("exchange") or "NSE")
    else:
        payload = provider.get_historical_candles(symbol, timeframe, request.args.get("exchange") or "NSE")
    code = 200 if payload.get("candles") else 404
    return jsonify(payload), code


@app.post("/api/market/subscribe")
def market_subscribe():
    body = request.get_json(silent=True) or {}
    provider = _market_provider(body.get("provider") or "yahoo")
    if provider is None:
        return jsonify({"data_status": NOT_CONNECTED, "message": "Unknown provider"}), 404
    if body.get("channel") == "candles":
        result = provider.subscribe_candles(body.get("symbols") or [], body.get("timeframe") or "1m")
    else:
        result = provider.subscribe_quotes(body.get("symbols") or [])
    return jsonify(result), 409


@app.get("/api/market/signal/<symbol>")
def market_signal(symbol):
    timeframe = normalise_timeframe(request.args.get("timeframe") or "1d")
    allowed = set(CANDLE_TIMEFRAMES) | set(ANALYSIS_TIMEFRAMES)
    if timeframe not in allowed:
        return jsonify({"error": "Invalid timeframe", "data_status": "UNAVAILABLE"}), 400
    symbol = symbol.upper().strip()
    try:
        row = _signal_row(symbol, timeframe, include_zones=request.args.get("zones") in {"1", "true", "True"})
    except Exception as error:
        return jsonify({"symbol": symbol, "signal": "DATA UNAVAILABLE", "data_status": "UNAVAILABLE", "message": str(error)}), 503
    code = 404 if row.get("signal") == "DATA UNAVAILABLE" else 200
    return jsonify(row), code


@app.get("/api/market/monitor")
def market_monitor():
    timeframe = normalise_timeframe(request.args.get("timeframe") or "1d")
    symbols = [item.strip().upper() for item in str(request.args.get("symbols") or "").split(",") if item.strip()][:8]
    if not symbols:
        return jsonify({"error": "symbols is required", "rows": []}), 400
    include_zones = request.args.get("zones") in {"1", "true", "True"}
    rows = []
    for symbol in symbols:
        try:
            rows.append(_signal_row(symbol, timeframe, include_zones=include_zones))
        except Exception:
            rows.append({"symbol": symbol, "timeframe": timeframe, "signal": "DATA UNAVAILABLE", "data_status": "UNAVAILABLE", "stream": NOT_CONNECTED})
    return jsonify({
        "rows": rows,
        "timeframe": timeframe,
        "data_status": DELAYED,
        "stream": NOT_CONNECTED,
        "market_status": market_status(),
        "message": "Monitor rows use the last closed delayed candle. Demand and supply stay Unavailable unless zones=1.",
    })


@app.post("/api/market/live-scan/start")
def start_live_scan():
    body = request.get_json(silent=True) or {}
    index_key = str(body.get("universe") or body.get("index") or "nifty50").lower()
    if index_key in {"all", "all_stocks", "nse_all", "nse"}:
        return jsonify({
            "error": "All Stocks is not available. No full NSE file is configured, and polling it would exceed the request limit.",
            "data_status": "UNAVAILABLE",
        }), 400
    if index_key not in INDEX_UNIVERSES:
        return jsonify({"error": "Universe must be nifty50, nifty100, nifty200, or nifty500"}), 400
    timeframe = normalise_timeframe(body.get("timeframe") or "1d")
    if timeframe not in set(SCAN_TIMEFRAMES):
        return jsonify({"error": "Live scan uses the existing scanner timeframes"}), 400
    condition_ids = body.get("conditions") or []
    if not isinstance(condition_ids, list) or not condition_ids:
        return jsonify({"error": "At least one scanner condition is required"}), 400
    unknown = [item for item in condition_ids if item not in CONDITION_LABELS]
    if unknown:
        return jsonify({"error": "Unknown scanner condition", "conditions": unknown}), 400
    logic = str(body.get("logic") or "AND").upper()
    if logic not in {"AND", "OR"}:
        return jsonify({"error": "Logic must be AND or OR"}), 400
    try:
        limit = int(body.get("limit") or 8)
    except (TypeError, ValueError):
        return jsonify({"error": "Limit must be a number"}), 400
    limit = max(1, min(limit, LIVE_SCAN_CAP))
    with LIVE_SCAN_LOCK:
        if LIVE_SCAN["running"]:
            return jsonify({
                "error": "A live scan is already running",
                "job_id": LIVE_SCAN["job_id"],
                "running": True,
            }), 409
        try:
            _label, stocks = load_index_universe(index_key)
        except Exception as error:
            return jsonify({"error": str(error), "data_status": "UNAVAILABLE"}), 503
        selected = list(stocks.head(limit).itertuples(index=False))
        job_id = str(uuid4())
        LIVE_SCAN.update({
            "running": True,
            "stop": False,
            "job_id": job_id,
            "universe": index_key,
            "timeframe": timeframe,
            "last_update": None,
            "scanned": 0,
            "matches": 0,
            "unavailable": 0,
            "errors": 0,
            "results": [],
            "data_status": DELAYED,
            "stream": NOT_CONNECTED,
            "message": "Scan started on delayed Yahoo data. A second scan is blocked until this one stops.",
        })
    TECHNICAL_EXECUTOR.submit(_live_scan_loop, job_id, selected, condition_ids, logic, timeframe)
    return jsonify({"job_id": job_id, "running": True, "limit": limit, "data_status": DELAYED, "stream": NOT_CONNECTED})


@app.post("/api/market/live-scan/stop")
def stop_live_scan():
    with LIVE_SCAN_LOCK:
        LIVE_SCAN["stop"] = True
        LIVE_SCAN["running"] = False
        job_id = LIVE_SCAN["job_id"]
    return jsonify({"ok": True, "job_id": job_id, "running": False, "stream": NOT_CONNECTED})


@app.get("/api/market/live-scan")
def live_scan_status():
    with LIVE_SCAN_LOCK:
        payload = {key: value for key, value in LIVE_SCAN.items() if key != "results"}
        payload["results"] = list(LIVE_SCAN["results"])
    return jsonify(payload)


@app.get("/api/alerts/events")
def alert_events():
    with ALERT_LOCK:
        events = list(ALERT_EVENTS)
    return jsonify({
        "events": events,
        "delivery": "In-app only. Nothing is emailed, pushed, or sent to a broker.",
    })


def _paper_candle(symbol, timeframe):
    frame = _closed_signal_frame(symbol, timeframe)
    if frame is None or getattr(frame, "empty", True):
        return None
    row = frame.iloc[-1]
    stamp = pd.Timestamp(frame.index[-1])
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("Asia/Kolkata")
    return {
        "time": stamp.isoformat(),
        "high": float(row["High"]),
        "low": float(row["Low"]),
        "close": float(row["Close"]),
        "data_status": HISTORICAL,
    }


def _manual_paper_preview(symbol, direction, timeframe, capital, risk_pct, maximum_loss, row):
    """Paper trade against or without a signal: entry at the last close, stop 1.5 ATR away, editable."""
    closed = _closed_signal_frame(symbol, timeframe)
    if closed is None or len(closed) < 20:
        return {"message": "DATA UNAVAILABLE", "data_status": "UNAVAILABLE", "broker_order": False}, 404
    high = pd.to_numeric(closed["High"], errors="coerce")
    low = pd.to_numeric(closed["Low"], errors="coerce")
    close = pd.to_numeric(closed["Close"], errors="coerce")
    true_range = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = float(true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().iloc[-1])
    entry = float(close.iloc[-1])
    if not math.isfinite(atr) or atr <= 0 or not math.isfinite(entry):
        return {"message": "DATA UNAVAILABLE", "data_status": "UNAVAILABLE", "broker_order": False}, 404
    sign = 1 if direction == "BUY" else -1
    entry = round(entry, 2)
    stop = round(entry - sign * 1.5 * atr, 2)
    risk = abs(entry - stop)
    sized = size_position(capital, risk_pct, entry, stop, maximum_loss)
    if sized.get("data_unavailable"):
        return sized, 400
    return {
        "broker_order": False,
        "confirmation": True,
        "manual": True,
        "symbol": symbol,
        "direction": direction,
        "timeframe": timeframe,
        "entry_price": round(entry, 2),
        "stop_loss": round(stop, 2),
        "target_1": round(entry + sign * 2 * risk, 2),
        "target_2": round(entry + sign * 3 * risk, 2),
        "target_3": None,
        "quantity": sized["quantity"],
        "risk": sized["risk_amount"],
        "risk_reward": "1:2.0",
        "signal_score": row.get("score"),
        "signal_reasons": [f"Manual {direction}: the last closed candle signal is {row.get('signal')}",
                           "Stop 1.5 x ATR(14) from the last close; Target 1 = 2R, Target 2 = 3R"],
        "demand_zone": row.get("demand") or "Unavailable",
        "supply_zone": row.get("supply") or "Unavailable",
        "market_trend": row.get("trend") or "Unavailable",
        "data_status": DELAYED,
        "market_status": row.get("market_status"),
        "stop_distance": sized.get("stop_distance"),
        "maximum_loss": sized.get("maximum_loss"),
        "message": (f"Manual paper trade. The signal is {row.get('signal')}, not {direction}. "
                    "Edit entry, stop and target, then confirm. No broker order is sent."),
    }, 200


def _paper_preview(symbol, direction, timeframe, capital, risk_pct, maximum_loss):
    row = _signal_row(symbol, timeframe, include_zones=False)
    if row.get("signal") == "DATA UNAVAILABLE":
        return row, 404
    if direction not in {"BUY", "SELL"}:
        return {"message": "Direction must be BUY or SELL"}, 400
    if row.get("signal") != direction:
        return _manual_paper_preview(symbol, direction, timeframe, capital, risk_pct, maximum_loss, row)
    if row.get("entry") is None or row.get("stop_loss") is None:
        return {"message": "DATA UNAVAILABLE", "data_status": "UNAVAILABLE", "broker_order": False}, 404
    sized = size_position(capital, risk_pct, row["entry"], row["stop_loss"], maximum_loss)
    if sized.get("data_unavailable"):
        return sized, 400
    return {
        "broker_order": False,
        "confirmation": True,
        "symbol": symbol,
        "direction": direction,
        "timeframe": timeframe,
        "entry_price": row["entry"],
        "stop_loss": row["stop_loss"],
        "target_1": row.get("target_1"),
        "target_2": row.get("target_2"),
        "target_3": row.get("target_3"),
        "quantity": sized["quantity"],
        "risk": sized["risk_amount"],
        "risk_reward": row.get("risk_reward"),
        "signal_score": row.get("score"),
        "signal_reasons": row.get("reasons") or [],
        "demand_zone": row.get("demand") or "Unavailable",
        "supply_zone": row.get("supply") or "Unavailable",
        "market_trend": row.get("trend") or "Unavailable",
        "data_status": DELAYED,
        "market_status": row.get("market_status"),
        "stop_distance": sized.get("stop_distance"),
        "maximum_loss": sized.get("maximum_loss"),
        "message": "Confirm or edit quantity, stop, and target. No broker order is sent.",
    }, 200


@app.post("/api/paper/preview")
def paper_preview():
    body = request.get_json(silent=True) or {}
    symbol = str(body.get("symbol") or "").upper().strip()
    if not symbol:
        return jsonify({"message": "Symbol is required"}), 400
    try:
        payload, code = _paper_preview(
            symbol,
            str(body.get("direction") or "").upper(),
            normalise_timeframe(body.get("timeframe") or "1d"),
            body.get("capital") or 100000,
            body.get("risk_pct") or 1,
            body.get("maximum_loss"),
        )
    except Exception as error:
        return jsonify({"message": "DATA UNAVAILABLE", "error": str(error), "broker_order": False}), 503
    if code == 200:
        _cap_by_cash(payload)
    return jsonify(payload), code


def _cap_by_cash(payload):
    try:
        cash = float(PAPER_BOOK.portfolio()["available_capital"])
        entry = float(payload["entry_price"])
        stop = float(payload["stop_loss"])
        quantity = float(payload["quantity"])
    except (KeyError, TypeError, ValueError):
        return
    if entry <= 0 or quantity * entry <= cash:
        return
    capped = math.floor(max(cash, 0) / entry * 10000) / 10000
    payload["quantity"] = capped
    payload["risk"] = round(capped * abs(entry - stop), 2)
    payload["capped_by"] = "available virtual cash"
    note = f"Quantity capped at {capped} by available virtual cash ₹{cash:,.2f}."
    payload["message"] = f"{payload.get('message') or ''} {note}".strip()


@app.post("/api/paper/trades")
def paper_open_trade():
    body = request.get_json(silent=True) or {}
    result = PAPER_BOOK.open_trade(body)
    code = 400 if result.get("data_unavailable") else 200
    return jsonify(result), code


@app.get("/api/paper/trades")
def paper_list_trades():
    return jsonify({
        "trades": PAPER_BOOK.list_trades(request.args.get("status"), request.args.get("symbol")),
        "broker_order": False,
        "data_status": DELAYED,
    })


@app.post("/api/paper/trades/<trade_id>/exit")
def paper_exit_trade(trade_id):
    body = request.get_json(silent=True) or {}
    mode = str(body.get("mode") or "full").lower()
    trade = PAPER_BOOK.get_trade(trade_id)
    if trade is None:
        return jsonify({"message": "Trade not found"}), 404
    quantity = None if mode == "full" else body.get("quantity")
    result = PAPER_BOOK.exit_trade(trade_id, body.get("exit_price"), quantity, body.get("reason") or ("partial exit" if mode == "partial" else "manual full exit"))
    code = 400 if result.get("data_unavailable") else 200
    return jsonify(result), code


@app.post("/api/paper/sync")
def paper_sync():
    updated = []
    for trade in PAPER_BOOK.list_trades(status="open"):
        candle = _paper_candle(trade["symbol"], trade["timeframe"])
        if candle is None:
            updated.append({"trade_id": trade["id"], "symbol": trade["symbol"], "data_status": "UNAVAILABLE", "message": "DATA UNAVAILABLE"})
            continue
        updated.append(PAPER_BOOK.apply_candle(trade["id"], candle, candle["data_status"]))
    return jsonify({"results": updated, "broker_order": False, "data_status": DELAYED, "rule": "A stop and a target on the same bar are booked as a stop."})


@app.get("/api/paper/portfolio")
def paper_portfolio():
    quotes = {}
    symbols = {trade["symbol"] for trade in PAPER_BOOK.list_trades(status="open")}
    for symbol in list(symbols)[:12]:
        quote = YAHOO_PROVIDER.get_quote(symbol, "NSE", "1d")
        quotes[symbol] = quote
    report = PAPER_BOOK.portfolio(quotes)
    return jsonify(report)


@app.post("/api/paper/journal")
def paper_add_note():
    body = request.get_json(silent=True) or {}
    result = PAPER_BOOK.add_note(body.get("trade_id"), body.get("notes"), body.get("chart_ref"))
    code = 400 if result.get("data_unavailable") else 200
    return jsonify(result), code


@app.get("/api/paper/journal")
def paper_journal():
    trades = PAPER_BOOK.list_trades(status="closed")
    notes = PAPER_BOOK.journal(request.args.get("trade_id"))
    return jsonify({"trades": trades, "notes": notes, "broker_order": False})


@app.get("/api/paper/strategies")
def paper_strategies():
    return jsonify({"strategies": PAPER_BOOK.list_strategies()})


@app.post("/api/paper/strategies")
def paper_save_strategy():
    body = request.get_json(silent=True) or {}
    result = PAPER_BOOK.save_strategy(body.get("name"), body.get("conditions") or [], body.get("logic") or "AND", normalise_timeframe(body.get("timeframe") or "1d"), body.get("id"))
    code = 400 if result.get("data_unavailable") else 200
    return jsonify(result), code


@app.post("/api/paper/strategies/<strategy_id>/duplicate")
def paper_duplicate_strategy(strategy_id):
    found = PAPER_BOOK.get_strategy(strategy_id)
    if found is None:
        return jsonify({"message": "Strategy not found"}), 404
    result = PAPER_BOOK.save_strategy(found["name"] + " copy", found["conditions"], found["logic"], found["timeframe"])
    return jsonify(result)


@app.delete("/api/paper/strategies/<strategy_id>")
def paper_delete_strategy(strategy_id):
    return jsonify(PAPER_BOOK.delete_strategy(strategy_id))


def _run_strategy_backtest(job_id, strategy, symbol, start, end, capital, risk_pct, maximum_loss):
    try:
        frame = _signal_frame(symbol, strategy["timeframe"])
        result = run_condition_backtest(
            frame,
            strategy["timeframe"],
            strategy["conditions"],
            logic=strategy["logic"],
            capital=capital,
            risk_pct=risk_pct,
            maximum_loss=maximum_loss,
            start=start,
            end=end,
            symbol=symbol,
        )
        if not result.get("data_unavailable"):
            PAPER_BOOK.save_backtest(result)
            with INTELLIGENCE_LOCK:
                for trade in result.get("trades") or []:
                    SIGNAL_HISTORY.insert(0, dict(trade))
                del SIGNAL_HISTORY[300:]
        status = "unavailable" if result.get("data_unavailable") else "complete"
        error = None
    except Exception as error_value:
        app.logger.exception("Strategy backtest failed")
        result = None
        status = "error"
        error = str(error_value)
    with INTELLIGENCE_LOCK:
        job = PAPER_JOBS.get(job_id)
        if job is not None:
            job["status"] = status
            job["result"] = result
            job["error"] = error


@app.post("/api/paper/strategies/<strategy_id>/backtest")
def paper_strategy_backtest(strategy_id):
    found = PAPER_BOOK.get_strategy(strategy_id)
    if found is None:
        return jsonify({"message": "Strategy not found"}), 404
    body = request.get_json(silent=True) or {}
    symbol = str(body.get("symbol") or "").upper().strip()
    if not symbol:
        return jsonify({"message": "Symbol is required"}), 400
    job_id = str(uuid4())
    with INTELLIGENCE_LOCK:
        PAPER_JOBS[job_id] = {"job_id": job_id, "status": "running", "strategy_id": strategy_id, "symbol": symbol, "result": None}
    BACKTEST_EXECUTOR.submit(
        _run_strategy_backtest,
        job_id,
        found,
        symbol,
        body.get("start"),
        body.get("end"),
        float(body.get("capital") or 100000),
        float(body.get("risk_pct") or 1),
        body.get("maximum_loss"),
    )
    return jsonify({"job_id": job_id, "status": "running", "broker_order": False})


@app.get("/api/paper/strategies/backtest/<job_id>")
def paper_strategy_backtest_status(job_id):
    with INTELLIGENCE_LOCK:
        job = PAPER_JOBS.get(job_id)
        if not job:
            return jsonify({"message": "Backtest job not found"}), 404
        return jsonify(dict(job))


@app.route("/health")
def health():
    """Used by the desktop shell to wait for the local server."""
    return jsonify({"status": "ok"})


@app.route("/splash")
def splash():
    return render_template("splash.html")


# ==========================
# RUN
# ==========================
if __name__ == "__main__":
    import os
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        # Local `python app.py` development server automatically reloads
        # after a saved change. Render and the desktop sidecar do not use it.
        debug=True,
    )
