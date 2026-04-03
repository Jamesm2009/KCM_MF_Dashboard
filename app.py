"""
Fund Performance Dashboard — Full Build
RS Score: (1D×0.10) + (1W×0.20) + (1M×0.30) + (3M×0.40)
"""

from flask import Flask, render_template, jsonify
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, date
import json, os

app = Flask(__name__)
cache = {"data": None, "last_updated": "Never", "vix_signal": "grey",
         "vix_value": None, "vix9d_value": None}


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_funds():
    with open("funds.json", "r") as f:
        return json.load(f)


def period_return(hist, days):
    if len(hist) < 2:
        return None
    latest_date = hist.index[-1]
    target_date = latest_date - pd.Timedelta(days=days)
    past = hist["Close"][hist.index <= target_date]
    if past.empty:
        return None
    return (hist["Close"].iloc[-1] - past.iloc[-1]) / past.iloc[-1] * 100


def ytd_return(hist):
    this_year = hist[hist.index.year == date.today().year]["Close"]
    if this_year.empty:
        return None
    return (this_year.iloc[-1] - this_year.iloc[0]) / this_year.iloc[0] * 100


def zscore_1yr(hist_1yr):
    closes = hist_1yr["Close"].dropna()
    if len(closes) < 20:
        return None
    mean, std = closes.mean(), closes.std()
    if std == 0:
        return None
    return round((closes.iloc[-1] - mean) / std, 2)


def trade_flag(hist):
    """Green if last > 21d SMA, red if last < 21d SMA, grey if equal."""
    closes = hist["Close"].dropna()
    if len(closes) < 21:
        return "grey"
    sma = closes.tail(21).mean()
    last = closes.iloc[-1]
    if last > sma:
        return "green"
    elif last < sma:
        return "red"
    return "grey"


def trend_flag(hist):
    """Green if last > 63d SMA, red if last < 63d SMA, grey if equal."""
    closes = hist["Close"].dropna()
    if len(closes) < 63:
        return "grey"
    sma = closes.tail(63).mean()
    last = closes.iloc[-1]
    if last > sma:
        return "green"
    elif last < sma:
        return "red"
    return "grey"


def make_sparkline(hist, days=170, w=90, h=28):
    """Return inline SVG sparkline for the last `days` trading days."""
    closes = hist["Close"].dropna().tail(days).values
    if len(closes) < 2:
        return ""
    mn, mx = closes.min(), closes.max()
    if mn == mx:
        return ""
    pts = []
    n = len(closes) - 1
    for i, v in enumerate(closes):
        x = round(i / n * w, 1)
        y = round((1 - (v - mn) / (mx - mn)) * (h - 2) + 1, 1)
        pts.append(f"{x},{y}")
    color = "#16a34a" if closes[-1] >= closes[0] else "#dc2626"
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" '
            f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
            f'</svg>')


def price_bar_data(hist):
    """Return (low_3y, high_3y, last, pct_position) for 3-year range bar."""
    closes = hist["Close"].dropna()
    if len(closes) < 2:
        return None, None, None, None
    low3  = round(closes.min(), 2)
    high3 = round(closes.max(), 2)
    last  = round(closes.iloc[-1], 2)
    rng   = high3 - low3
    pct   = round((last - low3) / rng * 100, 1) if rng > 0 else 50.0
    return low3, high3, last, pct


def get_ttm_yield(ticker_obj):
    """Fetch TTM yield from yfinance info. Returns float (e.g. 0.045) or None."""
    try:
        info = ticker_obj.info
        y = info.get("trailingAnnualDividendYield") or info.get("dividendYield")
        if y and y > 0:
            return round(y * 100, 2)   # convert to %
    except Exception:
        pass
    return None


def get_morningstar_url(symbol, override=None):
    if override:
        return override
    return f"https://www.morningstar.com/search#q={symbol}"


def fetch_vix_signal():
    """Compare VIX9D vs VIX. Green=Risk On, Red=Risk Off, Grey=neutral."""
    try:
        vix9d_hist = yf.Ticker("^VIX9D").history(period="5d")
        vix_hist   = yf.Ticker("^VIX").history(period="5d")
        if vix9d_hist.empty or vix_hist.empty:
            return "grey", None, None
        v9  = round(vix9d_hist["Close"].iloc[-1], 2)
        vix = round(vix_hist["Close"].iloc[-1], 2)
        diff = v9 - vix
        if abs(diff) <= 0.1:
            signal = "grey"
        elif v9 > vix:
            signal = "red"    # VIX9D > VIX → short-term fear spike → Risk OFF
        else:
            signal = "green"  # VIX9D < VIX → calm short-term → Risk ON
        return signal, round(v9, 2), round(vix, 2)
    except Exception as e:
        print(f"  VIX fetch error: {e}")
        return "grey", None, None


def fetch_all_funds():
    funds   = load_funds()
    results = []

    for fund in funds:
        ticker      = fund["symbol"]
        name        = fund.get("name", ticker)
        ftype       = fund.get("type", "")
        category    = fund.get("category", "equity")
        ms_override = fund.get("morningstar_url", None)

        try:
            tk   = yf.Ticker(ticker)
            hist = tk.history(period="3y")
            if hist.empty:
                print(f"  WARN: No data for {ticker}")
                continue

            # Performance (using last 14 months window for accuracy)
            hist_14m = hist.last("14ME")
            d1  = period_return(hist_14m, 1)
            w1  = period_return(hist_14m, 7)
            m1  = period_return(hist_14m, 30)
            m3  = period_return(hist_14m, 91)
            m6  = period_return(hist_14m, 182)
            ytd = ytd_return(hist_14m)
            y1  = period_return(hist_14m, 365)

            # RS Score
            rs = None
            if all(v is not None for v in [d1, w1, m1, m3]):
                rs = (d1 * 0.10) + (w1 * 0.20) + (m1 * 0.30) + (m3 * 0.40)

            # Z-Score (1 year window)
            one_yr_ago = hist.index[-1] - pd.Timedelta(days=365)
            hist_1yr   = hist[hist.index >= one_yr_ago]
            zsc = zscore_1yr(hist_1yr)
            if zsc is not None:
                ob_os = "Overbought" if zsc > 2.10 else ("Oversold" if zsc < -2.05 else "")
            else:
                ob_os = ""

            # Flags
            trade = trade_flag(hist)
            trend = trend_flag(hist)

            # Sparkline (8 months ≈ 170 trading days)
            sparkline = make_sparkline(hist, days=170)

            # 3-Year price bar
            low3, high3, last_price, bar_pct = price_bar_data(hist)

            # TTM Yield
            ttm = get_ttm_yield(tk)

            def fmt(v):
                return round(v, 2) if v is not None else None

            results.append({
                "symbol":          ticker,
                "name":            name,
                "type":            ftype,
                "category":        category,
                "morningstar_url": get_morningstar_url(ticker, ms_override),
                "sparkline":       sparkline,
                "1D":  fmt(d1),  "1W":  fmt(w1),  "1M":  fmt(m1),
                "3M":  fmt(m3),  "6M":  fmt(m6),  "YTD": fmt(ytd), "1Y": fmt(y1),
                "rs_score":   round(rs, 3) if rs is not None else None,
                "zscore":     zsc,
                "ob_os":      ob_os,
                "trade_flag": trade,
                "trend_flag": trend,
                "low3":       low3,
                "high3":      high3,
                "last_price": last_price,
                "bar_pct":    bar_pct,
                "ttm_yield":  ttm,
            })
            print(f"  OK  {ticker}  rs={rs:.2f if rs else 'n/a'}  trade={trade}  trend={trend}  ttm={ttm}")

        except Exception as e:
            print(f"  ERR {ticker}: {e}")

    # Rank by RS Score
    scored   = sorted([r for r in results if r["rs_score"] is not None],
                      key=lambda x: x["rs_score"], reverse=True)
    unscored = [r for r in results if r["rs_score"] is None]
    for i, r in enumerate(scored):
        r["rank"] = i + 1
    for r in unscored:
        r["rank"] = None

    return scored + unscored


def update_cache():
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Refreshing ...")
    data = fetch_all_funds()
    vix_signal, vix9d_val, vix_val = fetch_vix_signal()
    cache["data"]        = data
    cache["last_updated"] = datetime.now().strftime("%-m/%-d/%y  %H:%M ET")
    cache["vix_signal"]  = vix_signal
    cache["vix9d_value"] = vix9d_val
    cache["vix_value"]   = vix_val
    print(f"Done: {len(data)} funds | VIX signal={vix_signal} VIX9D={vix9d_val} VIX={vix_val}\n")


@app.route("/")
def index():
    if cache["data"] is None:
        update_cache()
    return render_template("index.html",
                           funds=cache["data"],
                           last_updated=cache["last_updated"],
                           vix_signal=cache["vix_signal"],
                           vix9d=cache["vix9d_value"],
                           vix=cache["vix_value"])


@app.route("/refresh")
def refresh():
    update_cache()
    return jsonify({"status": "ok", "updated": cache["last_updated"],
                    "funds": len(cache["data"]), "vix_signal": cache["vix_signal"]})


@app.route("/api/data")
def api_data():
    if cache["data"] is None:
        update_cache()
    return jsonify(cache["data"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
