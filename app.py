{% macro pct_cell(v) %}
  {% if v is none %}
    <td class="num na">—</td>
  {% elif v == 0 %}
    <td class="num zero">0.00%</td>
  {% elif v >= 5 %}
    <td class="num pos-3">+{{ '%.2f'|format(v) }}%</td>
  {% elif v >= 2 %}
    <td class="num pos-2">+{{ '%.2f'|format(v) }}%</td>
  {% elif v >= 0.5 %}
    <td class="num pos-1">+{{ '%.2f'|format(v) }}%</td>
  {% elif v > 0 %}
    <td class="num pos-0">+{{ '%.2f'|format(v) }}%</td>
  {% elif v > -0.5 %}
    <td class="num neg-0">{{ '%.2f'|format(v) }}%</td>
  {% elif v > -2 %}
    <td class="num neg-1">{{ '%.2f'|format(v) }}%</td>
  {% elif v > -5 %}
    <td class="num neg-2">{{ '%.2f'|format(v) }}%</td>
  {% else %}
    <td class="num neg-3">{{ '%.2f'|format(v) }}%</td>
  {% endif %}
{% endmacro %}

{% macro rs_class(v) %}{% if v is none %}{% elif v >= 3 %}rs-pos-hi{% elif v >= 1 %}rs-pos-mid{% elif v > 0 %}rs-pos-lo{% elif v > -1 %}rs-neg-lo{% elif v > -3 %}rs-neg-mid{% else %}rs-neg-hi{% endif %}{% endmacro %}

<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Fund Performance Dashboard</title>
<style>
  :root {
    --bg:      #1a1d23;
    --surface: #22262e;
    --border:  #2e3340;
    --text:    #e2e6f0;
    --muted:   #8892a4;
    --accent:  #4a9eff;
    --gold:    #f5c842;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 13px; }

  /* ── Header ── */
  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 10px 18px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
    position: sticky;
    top: 0;
    z-index: 10;
  }
  header h1 { font-size: 17px; font-weight: 700; }
  .rs-formula { font-size: 11px; color: var(--muted); font-style: italic; margin-top: 2px; }
  .meta { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .badge { background: #2e3340; border-radius: 6px; padding: 4px 10px; font-size: 11px; color: var(--muted); }
  .badge span { color: var(--text); font-weight: 600; }
  .btn-refresh { background: var(--accent); color: #fff; border: none; border-radius: 6px; padding: 6px 14px; font-size: 12px; font-weight: 600; cursor: pointer; text-decoration: none; display: inline-block; }
  .btn-refresh:hover { opacity: .85; }

  /* ── Legend ── */
  .legend {
    display: flex;
    gap: 18px;
    padding: 8px 18px;
    background: #1e2330;
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
    align-items: center;
  }
  .legend-title { font-size: 11px; color: var(--muted); font-weight: 700; text-transform: uppercase; letter-spacing: .5px; }
  .legend-item { display: flex; align-items: center; gap: 5px; font-size: 11px; color: var(--muted); }
  .flag-dot { width: 12px; height: 12px; border-radius: 50%; display: inline-block; }
  .dot-green { background: #27ae60; }
  .dot-red   { background: #c0392b; }
  .dot-grey  { background: #555e6e; }
  .pill-ob   { background: #7d2d00; color: #ffb347; border-radius: 4px; padding: 1px 6px; font-size: 10px; font-weight: 700; }
  .pill-os   { background: #003366; color: #66b2ff; border-radius: 4px; padding: 1px 6px; font-size: 10px; font-weight: 700; }

  /* ── Table ── */
  .table-wrap { overflow-x: auto; padding: 12px 10px; }
  table { border-collapse: collapse; min-width: 1050px; width: 100%; }

  thead th {
    background: #2a2f3a;
    color: var(--muted);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: .5px;
    text-transform: uppercase;
    padding: 7px 9px;
    border-bottom: 2px solid var(--border);
    cursor: pointer;
    user-select: none;
    white-space: nowrap;
  }
  thead th:hover { color: var(--text); }
  thead th.sorted-asc::after  { content: " ▲"; color: var(--accent); }
  thead th.sorted-desc::after { content: " ▼"; color: var(--accent); }
  .group-header { background: #1e2330 !important; text-align: center; font-size: 10px; border-bottom: 1px solid var(--accent) !important; }
  .gh-perf  { color: #4aff8a !important; }
  .gh-ind   { color: #ffcc44 !important; }
  .gh-flag  { color: #ff8c44 !important; }

  tbody tr { border-bottom: 1px solid var(--border); transition: background .12s; }
  tbody tr:hover { background: #2a2f3a; }
  td { padding: 6px 9px; white-space: nowrap; }

  td.rank   { font-weight: 800; font-size: 15px; color: var(--gold); text-align: center; width: 36px; }
  td.symbol { font-weight: 700; font-size: 13px; font-family: 'Courier New', monospace; }
  td.symbol a { color: var(--accent); text-decoration: none; }
  td.symbol a:hover { text-decoration: underline; }
  td.fname  { color: var(--text); max-width: 210px; overflow: hidden; text-overflow: ellipsis; }
  td.num    { text-align: right; font-variant-numeric: tabular-nums; font-size: 12px; min-width: 60px; }
  td.rs     { text-align: right; font-weight: 700; font-size: 13px; min-width: 68px; font-variant-numeric: tabular-nums; }
  td.zsc    { text-align: right; font-variant-numeric: tabular-nums; font-size: 12px; min-width: 52px; color: var(--muted); }
  td.flag-cell { text-align: center; min-width: 52px; }

  /* Performance colors */
  .pos-3 { background: #0d5c1e; color: #7fffa0; }
  .pos-2 { background: #1a7a2a; color: #b2ffc0; }
  .pos-1 { background: #236b30; color: #d4ffd8; }
  .pos-0 { background: #1e3d26; color: #c8f0ce; }
  .neg-0 { background: #3d1e1e; color: #f0c8c8; }
  .neg-1 { background: #6b2323; color: #ffd4d4; }
  .neg-2 { background: #7a1a1a; color: #ffb2b2; }
  .neg-3 { background: #5c0d0d; color: #ff9999; }
  .zero  { background: #252a35; color: var(--muted); }
  .na    { color: var(--muted); text-align: right; }

  /* RS score colors */
  .rs-pos-hi  { color: #7fffa0; }
  .rs-pos-mid { color: #b2ffd4; }
  .rs-pos-lo  { color: #d4ffe4; }
  .rs-neg-lo  { color: #ffd4d4; }
  .rs-neg-mid { color: #ffb2b2; }
  .rs-neg-hi  { color: #ff8080; }

  /* Flag circles */
  .flag-circle {
    display: inline-block;
    width: 14px; height: 14px;
    border-radius: 50%;
    vertical-align: middle;
  }
  .flag-green { background: #27ae60; box-shadow: 0 0 5px #27ae6088; }
  .flag-red   { background: #c0392b; box-shadow: 0 0 5px #c0392b88; }
  .flag-grey  { background: #555e6e; }

  /* Overbought / Oversold pills */
  .pill-overbought { background: #7d2d00; color: #ffb347; border-radius: 4px; padding: 2px 7px; font-size: 10px; font-weight: 700; letter-spacing: .3px; }
  .pill-oversold   { background: #003366; color: #66b2ff; border-radius: 4px; padding: 2px 7px; font-size: 10px; font-weight: 700; letter-spacing: .3px; }
  .pill-none       { color: var(--muted); font-size: 11px; }

  footer { text-align: center; padding: 18px; color: var(--muted); font-size: 11px; }
</style>
</head>
<body>

<header>
  <div>
    <h1>📊 Fund Performance Dashboard</h1>
    <div class="rs-formula">RS Score = (1D×0.10) + (1W×0.20) + (1M×0.30) + (3M×0.40) &nbsp;|&nbsp; Rank 1 = strongest</div>
  </div>
  <div class="meta">
    <div class="badge">Last Update: <span>{{ last_updated }}</span></div>
    <div class="badge">Funds: <span>{{ funds | length }}</span></div>
    <a class="btn-refresh" href="/refresh" onclick="return doRefresh()">⟳ Refresh</a>
  </div>
</header>

<!-- Legend bar -->
<div class="legend">
  <span class="legend-title">Legend:</span>

  <span class="legend-item"><b style="color:#ffcc44">Trade/Trend Flags</b></span>
  <span class="legend-item"><span class="flag-dot dot-green"></span> Price &gt; avg (strong)</span>
  <span class="legend-item"><span class="flag-dot dot-grey"></span>  In between</span>
  <span class="legend-item"><span class="flag-dot dot-red"></span>  Price &lt; avg (weak)</span>

  <span style="color:var(--border)">|</span>

  <span class="legend-item"><b style="color:#ffcc44">Z-Score Flags (1yr)</b></span>
  <span class="legend-item"><span class="pill-ob">Overbought</span> &nbsp;z &gt; 2.10</span>
  <span class="legend-item"><span class="pill-os">Oversold</span> &nbsp;z &lt; −2.05</span>

  <span style="color:var(--border)">|</span>

  <span class="legend-item" style="font-size:10px; color:var(--muted)">Trade = 21-day SMA &nbsp;|&nbsp; Trend = 63-day SMA &nbsp;|&nbsp; Symbol links to Morningstar</span>
</div>

<div class="table-wrap">
  <table id="fundTable">
    <thead>
      <tr>
        <th class="group-header" colspan="3"></th>
        <th class="group-header gh-perf" colspan="7">◀ Performance % ▶</th>
        <th class="group-header gh-ind"  colspan="3">◀ Indicators ▶</th>
        <th class="group-header gh-flag" colspan="2">◀ Flags ▶</th>
      </tr>
      <tr>
        <th onclick="sortTable(0)"  data-col="0">Rank</th>
        <th onclick="sortTable(1)"  data-col="1">Symbol</th>
        <th onclick="sortTable(2)"  data-col="2">Fund Name</th>
        <th onclick="sortTable(3)"  data-col="3">1D</th>
        <th onclick="sortTable(4)"  data-col="4">1W</th>
        <th onclick="sortTable(5)"  data-col="5">1M</th>
        <th onclick="sortTable(6)"  data-col="6">3M</th>
        <th onclick="sortTable(7)"  data-col="7">6M</th>
        <th onclick="sortTable(8)"  data-col="8">YTD</th>
        <th onclick="sortTable(9)"  data-col="9">1Y</th>
        <th onclick="sortTable(10)" data-col="10">RS Score</th>
        <th onclick="sortTable(11)" data-col="11">Z-Score</th>
        <th onclick="sortTable(12)" data-col="12">OB / OS</th>
        <th onclick="sortTable(13)" data-col="13">Trade</th>
        <th onclick="sortTable(14)" data-col="14">Trend</th>
      </tr>
    </thead>
    <tbody>
      {% for f in funds %}
      <tr>
        <td class="rank">{{ f.rank if f.rank else '—' }}</td>

        <td class="symbol">
          <a href="{{ f.morningstar_url }}" target="_blank" title="Morningstar: {{ f.symbol }}">{{ f.symbol }}</a>
        </td>

        <td class="fname">{{ f.name }}</td>

        {{ pct_cell(f['1D'])  }}
        {{ pct_cell(f['1W'])  }}
        {{ pct_cell(f['1M'])  }}
        {{ pct_cell(f['3M'])  }}
        {{ pct_cell(f['6M'])  }}
        {{ pct_cell(f['YTD']) }}
        {{ pct_cell(f['1Y'])  }}

        <td class="rs {{ rs_class(f.rs_score) }}">
          {{ '%.3f'|format(f.rs_score) if f.rs_score is not none else '—' }}
        </td>

        <!-- Z-Score -->
        <td class="zsc">
          {% if f.zscore is not none %}
            {{ '%.2f'|format(f.zscore) }}
          {% else %}—{% endif %}
        </td>

        <!-- Overbought / Oversold pill -->
        <td class="flag-cell">
          {% if f.ob_os == 'Overbought' %}
            <span class="pill-overbought">Overbought</span>
          {% elif f.ob_os == 'Oversold' %}
            <span class="pill-oversold">Oversold</span>
          {% else %}
            <span class="pill-none">—</span>
          {% endif %}
        </td>

        <!-- Trade flag -->
        <td class="flag-cell" title="Trade: 21-day SMA">
          <span class="flag-circle flag-{{ f.trade_flag }}"></span>
        </td>

        <!-- Trend flag -->
        <td class="flag-cell" title="Trend: 63-day SMA">
          <span class="flag-circle flag-{{ f.trend_flag }}"></span>
        </td>

      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>

<footer>
  Data: Yahoo Finance via yfinance &nbsp;|&nbsp;
  Refreshes after US market close (4 PM ET) &nbsp;|&nbsp;
  Symbol links open Morningstar portfolio page &nbsp;|&nbsp;
  Click any column to sort
</footer>

<script>
let sortCol = 0, sortAsc = true;

function sortTable(col) {
  const table = document.getElementById("fundTable");
  const tbody = table.tBodies[0];
  const rows  = Array.from(tbody.rows);
  if (sortCol === col) { sortAsc = !sortAsc; }
  else { sortCol = col; sortAsc = col <= 2; }
  rows.sort((a, b) => {
    const av = cellVal(a.cells[col]);
    const bv = cellVal(b.cells[col]);
    if (av === null && bv === null) return 0;
    if (av === null) return 1;
    if (bv === null) return -1;
    return sortAsc ? (av > bv ? 1 : -1) : (av < bv ? 1 : -1);
  });
  rows.forEach(r => tbody.appendChild(r));
  document.querySelectorAll("thead tr:nth-child(2) th").forEach((th, i) => {
    th.classList.remove("sorted-asc","sorted-desc");
    if (i === col) th.classList.add(sortAsc ? "sorted-asc" : "sorted-desc");
  });
}

function cellVal(cell) {
  const t = cell.innerText.trim();
  if (t === "—" || t === "") return null;
  // Flag circles: sort by color text from class
  const circle = cell.querySelector(".flag-circle");
  if (circle) {
    if (circle.classList.contains("flag-green")) return 1;
    if (circle.classList.contains("flag-grey"))  return 0;
    if (circle.classList.contains("flag-red"))   return -1;
    return null;
  }
  const n = parseFloat(t.replace(/%/g,""));
  return isNaN(n) ? t.toLowerCase() : n;
}

function doRefresh() {
  const btn = document.querySelector(".btn-refresh");
  btn.textContent = "⏳ Refreshing…";
  btn.style.opacity = ".6";
  fetch("/refresh")
    .then(r => r.json())
    .then(d => {
      btn.textContent = "✓ Done — reloading";
      setTimeout(() => location.reload(), 800);
    })
    .catch(() => { btn.textContent = "✗ Error"; btn.style.opacity = "1"; });
  return false;
}
</script>
</body>
</html>
