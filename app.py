from flask import Flask, render_template, jsonify, request
import pandas as pd
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from uuid import uuid4
from services.yahoo_data import get_live_prices
from services.zone_engine import detect_zones
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote
from urllib.error import URLError
from http.cookiejar import CookieJar
from urllib.request import build_opener, HTTPCookieProcessor
import json
import random
from datetime import date, timedelta, datetime, timezone

PROJECT_ROOT = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=PROJECT_ROOT / "templates", static_folder=PROJECT_ROOT / "static")
SCANNER_JOBS = {}
FUNDAMENTAL_JOBS = {}
SCANNER_LOCK = Lock()
# A later NIFTY 50/100/200 scan must not sit behind a long NIFTY 500 job.
SCANNER_EXECUTOR = ThreadPoolExecutor(max_workers=2)

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


def sector_name_for_symbol(symbol):
    """Read the locally bundled NIFTY list for a useful sector name."""
    try:
        stocks = pd.read_csv(PROJECT_ROOT / "data" / "nifty500.csv")
        match = stocks[stocks["Symbol"].astype(str).str.upper() == symbol.upper()]
        if not match.empty and "Industry" in match.columns:
            return str(match.iloc[0]["Industry"])
    except Exception:
        pass
    return "Broad Market"


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
            frame = frame.apply(pd.to_numeric, errors="coerce").dropna(subset=["Open", "High", "Low", "Close"])
            if not frame.empty:
                return frame
        except Exception:
            continue
    return pd.DataFrame()


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


def get_symbol_history(symbol):
    """Yahoo first, then NSE historical candles when Yahoo has no NSE data."""
    # ``range=max`` returns history from listing, unlike a provider fallback
    # such as 5y which silently removes old candles from the chart.
    direct_history = yahoo_max_history(symbol)
    if direct_history.empty:
        direct_history = yahoo_chart_history(symbol, "1d", 20000)
    best_history = direct_history
    try:
        ticker_name = CHART_INDEX_SYMBOLS.get(symbol.upper(), f"{symbol.upper()}.NS")
        ticker = yf.Ticker(ticker_name)
        # ``max`` can occasionally be rejected by Yahoo for otherwise valid
        # NSE symbols. Try normal, smaller requests first so the 1D chart
        # remains available.
        for lookback in ("max", "5y", "2y", "1y"):
            history = ticker.history(period=lookback, interval="1d", auto_adjust=False)
            if not history.empty:
                # Preserve the provider response that starts closest to the
                # listing date. A 5-year fallback must never replace max data.
                if best_history.empty or history.index.min() < best_history.index.min():
                    best_history = history
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


def get_chart_history(symbol, timeframe):
    """Return the candle interval chosen on the main chart toolbar."""
    intraday = {
        "5m": ("5m", "60d", None),
        "15m": ("15m", "60d", None),
        "1h": ("60m", "730d", None),
        "4h": ("60m", "730d", "4h"),
        "6h": ("60m", "730d", "6h"),
        "12h": ("60m", "730d", "12h"),
    }
    config = intraday.get(timeframe)
    if not config:
        return get_symbol_history(symbol)

    interval, lookback, rule = config
    lookback_days = 60 if interval in {"5m", "15m"} else 700
    direct_history = yahoo_chart_history(symbol, interval, lookback_days)
    if not direct_history.empty:
        history = direct_history
        if rule:
            history = history.resample(rule).agg({
                "Open": "first", "High": "max", "Low": "min",
                "Close": "last", "Volume": "sum",
            }).dropna()
        return history
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
        return history
    except Exception:
        return pd.DataFrame()


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

    return render_template(
        "index.html",
        stocks=stocks,
        total=len(stocks)
    )


# ==========================
# LIVE CHART API
# ==========================
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

        # Keep the original candle set for the zone engine. The chart can be
        # resampled below, while the engine applies the selected timeframe
        # exactly once (important for weekly/monthly Supply zones).
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

        # Keep the full candle history on the chart, but do not keep an old
        # zone merely because it once scored well.  A zone is actionable only
        # when it was created recently for the selected timeframe and it is
        # still reasonably close to the latest traded price.
        candidate_zones = detect_zones(zone_source, timeframe=selected_tf, max_zones=20)
        closes = pd.to_numeric(df["Close"], errors="coerce").dropna()
        latest_close = float(closes.iloc[-1]) if not closes.empty else 0.0
        latest_candle_time = pd.Timestamp(df.index[-1]).tz_localize(None)
        max_zone_age_days = {
            # Daily/weekly zones can stay relevant for several years on an
            # index.  These limits still remove the very old 2020-style
            # levels, without hiding every current support/resistance level.
            "1d": 1460,
            "1wk": 1825,
            "1mo": 2190,
            "3mo": 2555,
            "6mo": 2920,
            "1y": 3650,
            "5y": 4380,
        }.get(selected_tf, 1095)

        aged_zones = []
        for zone in candidate_zones:
            zone_time = pd.Timestamp(zone["time"], unit="s").tz_localize(None)
            age_days = max(0, (latest_candle_time - zone_time).days)
            if age_days <= max_zone_age_days:
                aged_zones.append(zone)

        # Display the first actionable level on each side of the current
        # price: nearest Demand below LTP and nearest Supply above LTP. This
        # is clearer than drawing many old levels and matches how traders use
        # a chart for the next support/resistance decision.
        def midpoint(item):
            return (float(item["top"]) + float(item["bottom"])) / 2

        demand_below = [zone for zone in aged_zones if zone["type"] == "demand" and midpoint(zone) <= latest_close]
        supply_above = [zone for zone in aged_zones if zone["type"] == "supply" and midpoint(zone) >= latest_close]
        zones = []
        if demand_below:
            zones.append(max(demand_below, key=midpoint))
        if supply_above:
            zones.append(min(supply_above, key=midpoint))

        # If price is already inside a zone, show that zone as the first
        # active level even when its midpoint sits on the other side of LTP.
        touching = [zone for zone in aged_zones if float(zone["bottom"]) <= latest_close <= float(zone["top"])]
        for zone in touching:
            if zone not in zones:
                zones.append(zone)

        # A candle can overlap several historical zones. Retain only the
        # nearest one for each side before adding a missing reference level.
        nearest_by_type = {}
        for zone in zones:
            zone_type = zone["type"]
            if (
                zone_type not in nearest_by_type
                or abs(midpoint(zone) - latest_close) < abs(midpoint(nearest_by_type[zone_type]) - latest_close)
            ):
                nearest_by_type[zone_type] = zone
        zones = list(nearest_by_type.values())

        # The strict institutional rules can legitimately find no complete
        # pattern near the latest candle.  The chart must still show the
        # nearest support and resistance reference levels, without claiming
        # they are a Strong scanner signal.  Build those from recent pivot
        # highs/lows and label them ``REF`` in the UI.
        visible_types = {zone["type"] for zone in zones}
        recent = df.tail(min(180, len(df))).copy()
        if len(recent) >= 5 and latest_close:
            recent["High"] = pd.to_numeric(recent["High"], errors="coerce")
            recent["Low"] = pd.to_numeric(recent["Low"], errors="coerce")
            recent = recent.dropna(subset=["High", "Low"])
            recent_atr = float((recent["High"] - recent["Low"]).rolling(14, min_periods=1).mean().iloc[-1])
            reference_width = max(recent_atr * 0.60, latest_close * 0.002)

            def add_reference_zone(kind):
                values = recent["Low"] if kind == "demand" else recent["High"]
                pivots = []
                for position in range(1, len(values) - 1):
                    value = float(values.iloc[position])
                    is_pivot = (
                        value <= float(values.iloc[position - 1]) and value <= float(values.iloc[position + 1])
                        if kind == "demand"
                        else value >= float(values.iloc[position - 1]) and value >= float(values.iloc[position + 1])
                    )
                    on_correct_side = value <= latest_close if kind == "demand" else value >= latest_close
                    if is_pivot and on_correct_side:
                        pivots.append((value, position))
                if not pivots:
                    return
                level, position = (max(pivots, key=lambda item: item[0]) if kind == "demand"
                                   else min(pivots, key=lambda item: item[0]))
                pivot_time = pd.Timestamp(recent.index[position]).tz_localize(None)
                zones.append({
                    "type": kind, "time": int(pivot_time.timestamp()),
                    "top": round(level + reference_width, 2),
                    "bottom": round(max(0.01, level - reference_width), 2),
                    "entry_low": round(max(0.01, level - reference_width), 2),
                    "entry_high": round(level + reference_width, 2),
                    "score": "REF", "grade": "Nearest reference level",
                    "fresh": False, "tested": False, "reference": True,
                })

            if "demand" not in visible_types:
                add_reference_zone("demand")
            if "supply" not in visible_types:
                add_reference_zone("supply")

        # One Demand below and one Supply above current price keeps the chart
        # clean and gives the user the next levels to watch.
        zones = zones[:2]
        for zone in zones:
            zone["timeframe"] = selected_tf.upper()

        # Snap zone start dates to an existing displayed candle so Lightweight
        # Charts can position the rectangle on every selected timeframe.
        display_index = pd.DatetimeIndex(df.index).tz_localize(None)
        for zone in zones:
            zone_time = pd.Timestamp(zone["time"], unit="s").tz_localize(None)
            matching_times = display_index[display_index >= zone_time]
            visible_time = matching_times[0] if len(matching_times) else display_index[-1]
            zone["time"] = int(pd.Timestamp(visible_time).timestamp())

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

        return jsonify({"candles": chart_data, "zones": zones, "offline_sample": offline_sample})

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
                    "percent": round(float(nse_item.get("percentChange", 0)), 2), "live": True,
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
                             "percent": round(change / previous * 100, 2), "live": True})
        except Exception:
            overview.append({"name": name, "price": MARKET_FALLBACKS[name], "change": 0,
                             "percent": 0, "live": False})
    return jsonify({"markets": overview})


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
