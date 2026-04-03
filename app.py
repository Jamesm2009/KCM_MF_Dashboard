"""
Fund Performance Dashboard
RS Score: (1D×0.10) + (1W×0.20) + (1M×0.30) + (3M×0.40)

Fix: background thread for data loading to avoid web request timeouts.
     3y history for range bar; performance from within 1y window.
"""

from flask import Flask, render_template, jsonify
import yfinance as yf
import pandas as pd
import threading
from datetime import datetime, date, timedelta
import json, os

app = Flask(__name__)

cache = {
    "data": [],
    "last_updated": "Loading...",
    "vix_signal": "grey",
    "vix9d_value": "—",
    "vix_value": "—",
    "loading": True,
}
_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_funds():
    with open("funds.json", "r") as f:
        return json.load(f)


def period_return(closes, days):
    """% change over last `days` calendar days using a DatetimeIndex Series."""
    if len(closes) < 2:
        return None
    latest = closes.index[-1]
    target = latest - pd.Timedelta(days=days)
    past   = closes[closes.index <= target]
    if past.empty:
        return None
    return (closes.iloc[-1] - past.iloc[-1]) / past.iloc[-1] * 100


def ytd_return(closes):
    this_year = closes[closes.index.year == date.today().year]
    if this_year.empty:
        return None
    return (this_year.iloc[-1] - this_year.iloc[0]) / this_year.iloc[0] * 100


def zscore_1yr(closes_1yr):
    c = closes_1yr.dropna()
    if len(c) < 20:
        return None
    std = c.std()
    if std == 0:
        return None
    return round((c.iloc[-1] - c.mean()) / std, 2)


def sma_flag(closes, window):
    """Green if last > SMA, red if last < SMA, grey if equal."""
    c = closes.dropna()
    if len(c) < window:
        return "grey"
    sma  = c.tail(window).mean()
    last = c.iloc[-1]
    if last > sma:   return "green"
    if last < sma:   return "red"
    return "grey"


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
    lo   = round(c.min(), 2)
    hi   = round(c.max(), 2)
    last = round(c.iloc[-1], 2)
    rng  = hi - lo
    pct  = round((last - lo) / rng * 100, 1) if rng > 0 else 50.0
    return lo, hi, last, pct


def get_ttm_yield(tk):
    try:
        info = tk.fast_info          # faster than .info
        y    = getattr(info, "three_month_trailing_price_to_earnings", None)
        # fall back to full .info only if needed
        full = tk.info
        val  = full.get("trailingAnnualDividendYield") or full.get("dividendYield") or 0
        return round(val * 100, 2) if val and val > 0 else None
    except Exception:
        return None


def fetch_vix_signal():
    try:
        v9h  = yf.Ticker("^VIX9D").history(period="5d")
        vih  = yf.Ticker("^VIX").history(period="5d")
        if v9h.empty or vih.empty:
            return "grey", None, None
        v9  = round(v9h["Close"].iloc[-1], 2)
        vi  = round(vih["Close"].iloc[-1], 2)
        sig = "grey" if abs(v9 - vi) <= 0.1 else ("red" if v9 > vi else "green")
        return sig, v9, vi
    except Exception as e:
        print(f"  VIX error: {e}")
        return "grey", None, None


def fetch_all_funds():
    funds   = load_funds()
    results = []

    for fund in funds:
        ticker   = fund["symbol"]
        name     = fund.get("name", ticker)
        category = fund.get("category", "equity")
        ftype    = fund.get("type", "")
        ms_url   = fund.get("morningstar_url",
                   f"https://www.morningstar.com/search#q={ticker}")

        try:
            tk   = yf.Ticker(ticker)
            hist = tk.history(period="3y")

            if hist.empty:
                print(f"  WARN: no data for {ticker}")
                continue

            closes = hist["Close"]

            # ── Performance (use last ~14 months of the 3y history) ──────────
            cutoff_14m = closes.index[-1] - pd.Timedelta(days=425)
            c14m = closes[closes.index >= cutoff_14m]

            d1  = period_return(c14m, 1)
            w1  = period_return(c14m, 7)
            m1  = period_return(c14m, 30)
            m3  = period_return(c14m, 91)
            m6  = period_return(c14m, 182)
            ytd = ytd_return(c14m)
            y1  = period_return(c14m, 365)

            rs  = None
            if all(v is not None for v in [d1, w1, m1, m3]):
                rs = (d1 * 0.10) + (w1 * 0.20) + (m1 * 0.30) + (m3 * 0.40)

            # ── Z-Score (1-year window) ──────────────────────────────────────
            cutoff_1y = closes.index[-1] - pd.Timedelta(days=365)
            c1y  = closes[closes.index >= cutoff_1y]
            zsc  = zscore_1yr(c1y)
            ob_os = ("Overbought" if zsc and zsc > 2.10
                     else "Oversold" if zsc and zsc < -2.05 else "")

            # ── Flags ────────────────────────────────────────────────────────
            trade = sma_flag(closes, 21)
            trend = sma_flag(closes, 63)

            # ── Sparkline (8 months ≈ 170 trading days) ──────────────────────
            sparkline = make_sparkline(closes, days=170)

            # ── 3-Year price bar ─────────────────────────────────────────────
            lo3, hi3, last_px, bar_pct = price_bar_data(closes)

            # ── TTM Yield ────────────────────────────────────────────────────
            ttm = get_ttm_yield(tk)

            def fmt(v):
                return round(v, 2) if v is not None else None

            results.append({
                "symbol":          ticker,
                "name":            name,
                "type":            ftype,
                "category":        category,
                "morningstar_url": ms_url,
                "sparkline":       sparkline,
                "1D":  fmt(d1),  "1W":  fmt(w1),
                "1M":  fmt(m1),  "3M":  fmt(m3),
                "6M":  fmt(m6),  "YTD": fmt(ytd), "1Y": fmt(y1),
                "rs_score":   round(rs, 3) if rs is not None else None,
                "zscore":     zsc,
                "ob_os":      ob_os,
                "trade_flag": trade,
                "trend_flag": trend,
                "low3":       lo3,
                "high3":      hi3,
                "last_price": last_px,
                "bar_pct":    bar_pct,
                "ttm_yield":  ttm,
            })
            print(f"  OK  {ticker}  rs={'%.2f'%rs if rs else 'n/a'}  "
                  f"trade={trade}  trend={trend}")

        except Exception as e:
            print(f"  ERR {ticker}: {e}")

    # ── Rank ─────────────────────────────────────────────────────────────────
    scored   = sorted([r for r in results if r["rs_score"] is not None],
                      key=lambda x: x["rs_score"], reverse=True)
    unscored = [r for r in results if r["rs_score"] is None]
    for i, r in enumerate(scored):
        r["rank"] = i + 1
    for r in unscored:
        r["rank"] = None

    return scored + unscored


def run_update():
    """Run in a background thread so web requests never time out."""
    print(f"\n[{datetime.now():%Y-%m-%d %H:%M}] Background refresh starting...")
    try:
        data = fetch_all_funds()
        sig, v9, vi = fetch_vix_signal()
        with _lock:
            cache["data"]         = data
            cache["last_updated"] = datetime.now().strftime("%-m/%-d/%y %H:%M ET")
            cache["vix_signal"]   = sig
            cache["vix9d_value"]  = v9 if v9 else "—"
            cache["vix_value"]    = vi if vi else "—"
            cache["loading"]      = False
        print(f"  Done: {len(data)} funds loaded.\n")
    except Exception as e:
        print(f"  REFRESH ERROR: {e}")
        with _lock:
            cache["loading"] = False


def trigger_update():
    t = threading.Thread(target=run_update, daemon=True)
    t.start()


# ── Kick off background load at startup ───────────────────────────────────────
trigger_update()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    with _lock:
        data        = cache["data"]
        updated     = cache["last_updated"]
        vix_signal  = cache["vix_signal"]
        vix9d       = cache["vix9d_value"]
        vix         = cache["vix_value"]
        is_loading  = cache["loading"]

    return render_template("index.html",
                           funds=data,
                           last_updated=updated,
                           vix_signal=vix_signal,
                           vix9d=vix9d,
                           vix=vix,
                           is_loading=is_loading)


@app.route("/refresh")
def refresh():
    trigger_update()
    return jsonify({"status": "refresh started — check back in ~3 minutes"})


@app.route("/status")
def status():
    with _lock:
        return jsonify({
            "loading":      cache["loading"],
            "funds":        len(cache["data"]),
            "last_updated": cache["last_updated"],
            "vix_signal":   cache["vix_signal"],
        })


@app.route("/api/data")
def api_data():
    with _lock:
        return jsonify(cache["data"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
