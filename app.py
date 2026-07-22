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
from datetime import date, timedelta

PROJECT_ROOT = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=PROJECT_ROOT / "templates", static_folder=PROJECT_ROOT / "static")
SCANNER_JOBS = {}
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


def get_symbol_history(symbol):
    """Yahoo first, then NSE historical candles when Yahoo has no NSE data."""
    try:
        history = yf.Ticker(f"{symbol.upper()}.NS").history(period="max", interval="1d", auto_adjust=False)
        if not history.empty:
            return history
    except Exception:
        pass
    try:
        end = date.today()
        start = end - timedelta(days=365)
        query = f"/api/historical/cm/equity?symbol={quote(symbol.upper())}&series=[%22EQ%22]&from={start:%d-%m-%Y}&to={end:%d-%m-%Y}"
        rows = nse_json(query).get("data", [])
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
        df = get_symbol_history(symbol)

        if df.empty:
            return jsonify([])

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

        zones = detect_zones(df, timeframe="1d", max_zones=4)
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

        return jsonify({"candles": chart_data, "zones": zones})

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
            } for zone in zones if zone["fresh"] and zone["score"] >= 70]
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
        "SENSEX": "^BSESN", "NIFTY IT": "^CNXIT",
    }
    nse_names = {"NIFTY 50": "NIFTY 50", "BANK NIFTY": "NIFTY BANK", "NIFTY IT": "NIFTY IT"}
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
    sector, ticker_symbol = SECTOR_INDICES.get(symbol.upper(), ("Broad Market", "^NSEI"))
    try:
        data = yf.Ticker(ticker_symbol).history(period="5d", interval="1d", auto_adjust=False)
        if len(data) < 2:
            raise ValueError("Insufficient sector data")
        change = float(data["Close"].iloc[-1] - data["Close"].iloc[-2])
        return jsonify({"sector": sector, "trend": "Bullish" if change >= 0 else "Bearish"})
    except Exception:
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
        debug=False,
    )
