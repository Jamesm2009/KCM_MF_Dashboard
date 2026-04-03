"""
Fund Performance Dashboard
--------------------------
Fetches daily data from Yahoo Finance after market close.

Indicators calculated per fund:
  • RS Score   : (1D*0.10) + (1W*0.20) + (1M*0.30) + (3M*0.40)
  • Z-Score    : 1-year z-score of closing price
                 > 2.10  → Overbought
                 < -2.05 → Oversold
  • Trade Flag : vs 21-day simple moving average
                 last > 1.050 × SMA21 → green
                 last < 0.995 × SMA21 → red
                 else                 → grey
  • Trend Flag : vs 63-day simple moving average
                 last > 1.050 × SMA63 → green
                 last < 0.995 × SMA63 → red
                 else                 → grey
"""

from flask import Flask, render_template, jsonify
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, date
import json, os

app = Flask(__name__)

# ── In-memory cache ───────────────────────────────────────────────────────────
cache = {"data": None, "last_updated": "Never"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_funds():
    with open("funds.json", "r") as f:
        return json.load(f)


def period_return(hist, days):
    """% change over the last `days` calendar days."""
    if len(hist) < 2:
        return None
    latest_date = hist.index[-1]
    target_date = latest_date - pd.Timedelta(days=days)
    past = hist["Close"][hist.index <= target_date]
    if past.empty:
        return None
    return (hist["Close"].iloc[-1] - past.iloc[-1]) / past.iloc[-1] * 100


def ytd_return(hist):
    """% change from first trading day of this calendar year."""
    this_year = hist[hist.index.year == date.today().year]["Close"]
    if this_year.empty:
        return None
    return (this_year.iloc[-1] - this_year.iloc[0]) / this_year.iloc[0] * 100


def zscore_1yr(hist_1yr):
    """1-year z-score of the latest closing price."""
    closes = hist_1yr["Close"].dropna()
    if len(closes) < 20:
        return None
    mean = closes.mean()
    std  = closes.std()
    if std == 0:
        return None
    return round((closes.iloc[-1] - mean) / std, 2)


def trade_flag(hist):
    """Compares last price to 21-day SMA. Returns 'green', 'red', or 'grey'."""
    closes = hist["Close"].dropna()
    if len(closes) < 21:
        return "grey"
    sma21 = closes.tail(21).mean()
    last  = closes.iloc[-1]
    if last > 1.050 * sma21:
        return "green"
    elif last < 0.995 * sma21:
        return "red"
    return "grey"


def trend_flag(hist):
    """Compares last price to 63-day SMA. Returns 'green', 'red', or 'grey'."""
    closes = hist["Close"].dropna()
    if len(closes) < 63:
        return "grey"
    sma63 = closes.tail(63).mean()
    last  = closes.iloc[-1]
    if last > 1.050 * sma63:
        return "green"
    elif last < 0.995 * sma63:
        return "red"
    return "grey"


def get_morningstar_url(symbol, ms_url_override=None):
    if ms_url_override:
        return ms_url_override
    return f"https://www.morningstar.com/search#q={symbol}&filtersApplied=false"


def fetch_all_funds():
    funds   = load_funds()
    results = []

    for fund in funds:
        ticker      = fund["symbol"]
        name        = fund.get("name", ticker)
        ms_override = fund.get("morningstar_url", None)

        try:
            hist = yf.Ticker(ticker).history(period="14mo")
            if hist.empty:
                print(f"  WARNING: No data for {ticker}")
                continue

            d1  = period_return(hist, 1)
            w1  = period_return(hist, 7)
            m1  = period_return(hist, 30)
            m3  = period_return(hist, 91)
            m6  = period_return(hist, 182)
            ytd = ytd_return(hist)
            y1  = period_return(hist, 365)

            rs = None
            if all(v is not None for v in [d1, w1, m1, m3]):
                rs = (d1 * 0.10) + (w1 * 0.20) + (m1 * 0.30) + (m3 * 0.40)

            one_yr_ago = hist.index[-1] - pd.Timedelta(days=365)
            hist_1yr   = hist[hist.index >= one_yr_ago]

            zsc   = zscore_1yr(hist_1yr)
            trade = trade_flag(hist)
            trend = trend_flag(hist)

            if zsc is not None:
                if zsc > 2.10:
                    ob_os = "Overbought"
                elif zsc < -2.05:
                    ob_os = "Oversold"
                else:
                    ob_os = ""
            else:
                ob_os = ""

            def fmt(v):
                return round(v, 2) if v is not None else None

            results.append({
                "symbol":          ticker,
                "name":            name,
                "morningstar_url": get_morningstar_url(ticker, ms_override),
                "1D":              fmt(d1),
                "1W":              fmt(w1),
                "1M":              fmt(m1),
                "3M":              fmt(m3),
                "6M":              fmt(m6),
                "YTD":             fmt(ytd),
                "1Y":              fmt(y1),
                "rs_score":        round(rs, 3) if rs is not None else None,
                "zscore":          zsc,
                "ob_os":           ob_os,
                "trade_flag":      trade,
                "trend_flag":      trend,
            })
            print(f"  OK  {ticker}  z={zsc}  ob_os={ob_os}  trade={trade}  trend={trend}")

        except Exception as e:
            print(f"  ERR {ticker}: {e}")

    scored   = sorted([r for r in results if r["rs_score"] is not None],
                      key=lambda x: x["rs_score"], reverse=True)
    unscored = [r for r in results if r["rs_score"] is None]

    for i, r in enumerate(scored):
        r["rank"] = i + 1
    for r in unscored:
        r["rank"] = None

    return scored + unscored


def update_cache():
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Refreshing fund data ...")
    data = fetch_all_funds()
    cache["data"]         = data
    cache["last_updated"] = datetime.now().strftime("%-m/%-d/%y  %H:%M ET")
    print(f"Done: {len(data)} funds loaded.\n")


@app.route("/")
def index():
    if cache["data"] is None:
        update_cache()
    return render_template("index.html",
                           funds=cache["data"],
                           last_updated=cache["last_updated"])


@app.route("/refresh")
def refresh():
    update_cache()
    return jsonify({"status": "ok", "updated": cache["last_updated"],
                    "funds": len(cache["data"])})


@app.route("/api/data")
def api_data():
    if cache["data"] is None:
        update_cache()
    return jsonify(cache["data"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
