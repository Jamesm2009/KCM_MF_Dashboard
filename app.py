"""
Fund Performance Dashboard
Uses yf.download() batch fetch — all tickers in ONE call, much faster.
RS Score: (1D×0.10) + (1W×0.20) + (1M×0.30) + (3M×0.40)
"""

from flask import Flask, render_template, jsonify
import yfinance as yf
import pandas as pd
import threading
from datetime import datetime, date
import json, os

app = Flask(__name__)

cache = {
    "data": [],
    "last_updated": "Loading...",
    "vix_signal": "grey",
    "vix9d_value": "—",
    "vix_value":  "—",
    "loading": True,
    "error": None,
}
_lock = threading.Lock()


def load_funds():
    with open("funds.json", "r") as f:
        return json.load(f)


# ── Calculation helpers ───────────────────────────────────────────────────────

def period_return(closes, days):
    if len(closes) < 2:
        return None
    latest = closes.index[-1]
    past   = closes[closes.index <= latest - pd.Timedelta(days=days)]
    if past.empty:
        return None
    return (closes.iloc[-1] - past.iloc[-1]) / past.iloc[-1] * 100


def ytd_return(closes):
    yr = closes[closes.index.year == date.today().year]
    if yr.empty:
        return None
    return (yr.iloc[-1] - yr.iloc[0]) / yr.iloc[0] * 100


def zscore_1yr(closes):
    cutoff = closes.index[-1] - pd.Timedelta(days=365)
    c = closes[closes.index >= cutoff].dropna()
    if len(c) < 20:
        return None
    std = c.std()
    if std == 0:
        return None
    return round((c.iloc[-1] - c.mean()) / std, 2)


def sma_flag(closes, window):
    c = closes.dropna()
    if len(c) < window:
        return "grey"
    sma = c.tail(window).mean()
    last = c.iloc[-1]
    return "green" if last > sma else ("red" if last < sma else "grey")


def make_sparkline(closes, days=170, w=90, h=28):
    c = closes.dropna().tail(days).values
    if len(c) < 2:
        return ""
    mn, mx = c.min(), c.max()
    if mn == mx:
        return ""
    n   = len(c) - 1
    pts = [f"{round(i/n*w,1)},{round((1-(v-mn)/(mx-mn))*(h-2)+1,1)}"
           for i, v in enumerate(c)]
    col = "#16a34a" if c[-1] >= c[0] else "#dc2626"
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="{col}" '
            f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
            f'</svg>')


def price_bar_data(closes):
    c = closes.dropna()
    if len(c) < 2:
        return None, None, None, None
    lo, hi = round(c.min(), 2), round(c.max(), 2)
    last   = round(c.iloc[-1], 2)
    rng    = hi - lo
    pct    = round((last - lo) / rng * 100, 1) if rng > 0 else 50.0
    return lo, hi, last, pct


# ── Main fetch ────────────────────────────────────────────────────────────────

def run_update():
    print(f"\n[{datetime.now():%Y-%m-%d %H:%M}] Starting batch download...")
    try:
        funds   = load_funds()
        symbols = [f["symbol"] for f in funds]
        vix_syms = ["^VIX9D", "^VIX"]

        # ── ONE batch download for all fund tickers ───────────────────────────
        print(f"  Downloading {len(symbols)} fund tickers (3y)...")
        fund_raw = yf.download(
            symbols,
            period="3y",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        print("  Fund download complete.")

        # With multiple tickers yf.download returns MultiIndex columns:
        # ("Close", "VFIAX"), ("Close", "VSMAX"), ...
        # Access closes_df["VFIAX"] for each fund.
        if isinstance(fund_raw.columns, pd.MultiIndex):
            closes_df = fund_raw["Close"]
        else:
            # Single ticker edge case
            closes_df = fund_raw[["Close"]].rename(columns={"Close": symbols[0]})

        # ── VIX data ──────────────────────────────────────────────────────────
        print("  Downloading VIX data...")
        vix_raw = yf.download(vix_syms, period="5d", auto_adjust=True,
                              progress=False, threads=True)
        vix_signal, vix9d_val, vix_val = "grey", "—", "—"
        try:
            if isinstance(vix_raw.columns, pd.MultiIndex):
                vc = vix_raw["Close"]
            else:
                vc = vix_raw[["Close"]]
            v9  = round(vc["^VIX9D"].dropna().iloc[-1], 2)
            vi  = round(vc["^VIX"].dropna().iloc[-1],   2)
            vix_signal = "grey" if abs(v9 - vi) <= 0.1 else ("red" if v9 > vi else "green")
            vix9d_val, vix_val = v9, vi
        except Exception as ve:
            print(f"  VIX parse error: {ve}")

        # ── Per-fund calculations ─────────────────────────────────────────────
        results = []
        for fund in funds:
            ticker   = fund["symbol"]
            name     = fund.get("name", ticker)
            category = fund.get("category", "equity")
            ftype    = fund.get("type", "")
            ms_url   = fund.get("morningstar_url",
                       f"https://www.morningstar.com/search#q={ticker}")

            try:
                if ticker not in closes_df.columns:
                    print(f"  SKIP {ticker}: not in download result")
                    continue

                closes = closes_df[ticker].dropna()
                if len(closes) < 30:
                    print(f"  SKIP {ticker}: insufficient data ({len(closes)} rows)")
                    continue

                d1  = period_return(closes, 1)
                w1  = period_return(closes, 7)
                m1  = period_return(closes, 30)
                m3  = period_return(closes, 91)
                m6  = period_return(closes, 182)
                ytd = ytd_return(closes)
                y1  = period_return(closes, 365)

                rs = None
                if all(v is not None for v in [d1, w1, m1, m3]):
                    rs = (d1*0.10) + (w1*0.20) + (m1*0.30) + (m3*0.40)

                zsc   = zscore_1yr(closes)
                ob_os = ("Overbought" if zsc and zsc > 2.10
                         else "Oversold" if zsc and zsc < -2.05 else "")

                trade = sma_flag(closes, 21)
                trend = sma_flag(closes, 63)
                spark = make_sparkline(closes, days=170)
                lo3, hi3, last_px, bar_pct = price_bar_data(closes)

                def fmt(v):
                    return round(v, 2) if v is not None else None

                results.append({
                    "symbol": ticker, "name": name,
                    "type": ftype, "category": category,
                    "morningstar_url": ms_url, "sparkline": spark,
                    "1D": fmt(d1), "1W": fmt(w1), "1M": fmt(m1),
                    "3M": fmt(m3), "6M": fmt(m6), "YTD": fmt(ytd), "1Y": fmt(y1),
                    "rs_score":   round(rs, 3) if rs is not None else None,
                    "zscore":     zsc, "ob_os": ob_os,
                    "trade_flag": trade, "trend_flag": trend,
                    "low3": lo3, "high3": hi3,
                    "last_price": last_px, "bar_pct": bar_pct,
                })
                print(f"  OK  {ticker}")

            except Exception as e:
                print(f"  ERR {ticker}: {e}")

        # ── Rank ─────────────────────────────────────────────────────────────
        scored   = sorted([r for r in results if r["rs_score"] is not None],
                          key=lambda x: x["rs_score"], reverse=True)
        unscored = [r for r in results if r["rs_score"] is None]
        for i, r in enumerate(scored):
            r["rank"] = i + 1
        for r in unscored:
            r["rank"] = None
        final = scored + unscored

        with _lock:
            cache["data"]         = final
            cache["last_updated"] = datetime.now().strftime("%-m/%-d/%y %H:%M ET")
            cache["vix_signal"]   = vix_signal
            cache["vix9d_value"]  = vix9d_val
            cache["vix_value"]    = vix_val
            cache["loading"]      = False
            cache["error"]        = None

        print(f"  SUCCESS: {len(final)} funds loaded.\n")

    except Exception as e:
        print(f"  FATAL ERROR in run_update: {e}")
        import traceback; traceback.print_exc()
        with _lock:
            cache["loading"] = False
            cache["error"]   = str(e)


def trigger_update():
    t = threading.Thread(target=run_update, daemon=True)
    t.start()
    return t


# Kick off at startup
trigger_update()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    with _lock:
        data       = cache["data"]
        updated    = cache["last_updated"]
        vix_signal = cache["vix_signal"]
        vix9d      = cache["vix9d_value"]
        vix        = cache["vix_value"]
        loading    = cache["loading"]
        error      = cache["error"]
    return render_template("index.html",
                           funds=data, last_updated=updated,
                           vix_signal=vix_signal, vix9d=vix9d, vix=vix,
                           is_loading=loading, error=error)


@app.route("/refresh")
def refresh():
    trigger_update()
    return jsonify({"status": "refresh started"})


@app.route("/status")
def status():
    with _lock:
        return jsonify({
            "loading":      cache["loading"],
            "funds":        len(cache["data"]),
            "last_updated": cache["last_updated"],
            "error":        cache["error"],
        })


@app.route("/api/data")
def api_data():
    with _lock:
        return jsonify(cache["data"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
