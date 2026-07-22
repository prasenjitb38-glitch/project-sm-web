import yfinance as yf
from concurrent.futures import ThreadPoolExecutor

def fetch_price(symbol):
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="1d")

        if hist.empty:
            return symbol, "-"

        return symbol, round(float(hist["Close"].iloc[-1]), 2)

    except:
        return symbol, "-"


def get_live_prices(symbols):

    prices = {}

    with ThreadPoolExecutor(max_workers=30) as executor:

        results = executor.map(fetch_price, symbols)

    for symbol, price in results:
        prices[symbol] = price

    return prices