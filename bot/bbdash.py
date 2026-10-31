"""Dashboard for the bollinger strategy: the normal dashboard plus the three
Bollinger Bands and the 50-period 4h average drawn on the price chart, and a
small panel with the current bands, trend, size and coin share.

It reuses bot/dashboard.py unchanged (that file is also mounted into the LIVE
bot's dashboard, so it must not be edited for this) and patches the page in
memory. `python -m bot dashboard` uses this automatically when the config
says strategy: bollinger.
"""

import json
import logging

from . import dashboard as base

log = logging.getLogger(__name__)

HEAD_CSS = """
#bbinfo { font-size: 13px; line-height: 1.55; margin-top: 6px; color: var(--text-2); }
#bbinfo b { color: var(--text); font-weight: 600; }
#bbinfo .sw { display: inline-block; width: 16px; height: 0; vertical-align: middle; margin: 0 4px 0 10px; border-top: 2px solid; }
#bbinfo .down { color: var(--sell); font-weight: 600; }
/* Oct 29: wider page + taller chart for the live bot */
main { max-width: 1700px !important; }
#pchart { height: 440px; }
"""

OVERLAY_JS = r"""
function bbPath(pts, t0, tEnd, xt, y, lo, hi, xmin, xmax) {
  let d = "", pen = false, prev = null;
  const put = (t, v) => {
    if (v == null || v < lo || v > hi) { pen = false; return; }
    const x = Math.max(xmin, Math.min(xmax, xt(t)));
    d += (pen ? "L" : "M") + x.toFixed(1) + " " + y(v).toFixed(1) + " "; pen = true;
  };
  for (const [t, v] of pts) {
    if (t < t0) { prev = v; continue; }
    if (t > tEnd) break;
    if (prev != null) { put(t0, prev); prev = null; }
    put(t, v);
  }
  if (prev != null) put(t0, prev);              // window shorter than one band step
  if (pts.length) put(tEnd, pts[pts.length - 1][1]);   // carry the latest value to the right edge
  return d;
}
function bbOverlay(vis, vn, xt, y, lo, hi, t0, step, xmin, xmax) {
  if (!data || !data.bb || !data.bb.bands) return "";
  const B = data.bb.bands, tEnd = vis[vn - 1][0] + step;
  const line = (pts, color, dash, w) => {
    const d = bbPath(pts, t0, tEnd, xt, y, lo, hi, xmin, xmax);
    return d ? `<path d="${d}" fill="none" stroke="${color}" stroke-width="${w}"${dash ? ` stroke-dasharray="${dash}"` : ""} opacity="0.9"/>` : "";
  };
  return line(B.map((r) => [r[0], r[1]]), "#e09a2b", "", 1.4) +
         line(B.map((r) => [r[0], r[2]]), "#8a63d2", "5 3", 1.4) +
         line(B.map((r) => [r[0], r[3]]), "#e09a2b", "", 1.4) +
         line(data.bb.sma || [], "#1f9e8f", "2 3", 1.8) + bbBuyLine(lo, hi, y, xmin, xmax) + bbMidLabel(lo, hi, y, xmax);
}
// Oct 29: pink "next buy" line = lower band, or the step-down limit if that is lower
function bbBuyAt() {
  if (!data || !data.bb || !data.bb.now) return null;
  let v = data.bb.now.lower;
  // Oct 30: pullback buys on -> while price is above the middle band, the next buy is a dip to the middle
  if (data.bb.settings && data.bb.settings.pullback_buys && data.price > data.bb.now.middle) v = data.bb.now.middle;
  if (data.bb.settings && data.bb.settings.top_buys && data.price > data.bb.now.upper) v = data.bb.now.upper;
  if (data.bb.next_limit != null) v = Math.min(v, data.bb.next_limit);
  return v;
}
// Oct 30 (Todd): all three buy zones drawn all the time, each in its own colour,
// with the candle-close countdown on the right. "(next)" marks the one price would hit first.
function bbZones() {
  if (!data || !data.bb || !data.bb.now) return [];
  const n = data.bb.now, st = data.bb.settings || {}, z = [];
  if (st.top_buys) z.push({ v: n.upper, c: "#e09a2b", name: "Top buy", how: `dip to it, close above, sells +${st.top_profit_pct != null ? st.top_profit_pct : 1}%` });
  if (st.pullback_buys) z.push({ v: n.middle, c: "#8a63d2", name: "Middle buy", how: "dip to it, close above" });
  z.push({ v: n.lower, c: "#ff3fa4", name: "Bounce buy", how: "dip under it, then close back above" });
  return z;
}
function bbBuyLine(lo, hi, y, xmin, xmax) {
  const nx = bbBuyAt();
  let s = "";
  for (const z of bbZones()) {
    if (z.v < lo || z.v > hi) continue;
    const yy = y(z.v).toFixed(1), ty = (y(z.v) - 5).toFixed(1);
    const next = nx != null && Math.abs(nx - z.v) < 1e-9 ? " (next)" : "";
    s += `<line x1="${xmin}" x2="${xmax}" y1="${yy}" y2="${yy}" stroke="${z.c}" stroke-width="2.2" stroke-dasharray="8 4"/>` +
         `<text x="${xmin + 6}" y="${ty}" fill="${z.c}" font-size="13" font-weight="700">${z.name} ~ ${usd(z.v)}${next}</text>` +
         `<text x="${xmax - 6}" y="${ty}" text-anchor="end" fill="${z.c}" font-size="13" font-weight="700"><tspan class="bbcdm">${bbLeft()}</tspan></text>`;
  }
  const lim = data && data.bb ? data.bb.next_limit : null;   // buys in a row: step-down cap
  if (lim != null && lim >= lo && lim <= hi) {
    const yy = y(lim).toFixed(1);
    s += `<line x1="${xmin}" x2="${xmax}" y1="${yy}" y2="${yy}" stroke="#d04545" stroke-width="1.6" stroke-dasharray="2 3"/>` +
         `<text x="${xmin + 6}" y="${(y(lim) + 14).toFixed(1)}" fill="#d04545" font-size="12" font-weight="700">Next buy in a row needs ${usd(lim)} or lower</text>`;
  }
  return s;
}
function bbMidLabel() { return ""; }
function bbLeft() {
  const P = 30 * 60e3, left = P - (Date.now() % P);
  const m = Math.floor(left / 60e3), s = Math.floor(left / 1e3) % 60;
  return m + ":" + String(s).padStart(2, "0");
}
// Oct 29: countdown to the next 30m candle close (Oct 30: also the purple middle-band timers)
setInterval(() => {
  const t = bbLeft();
  const el = document.getElementById("bbcd"); if (el) el.textContent = t;
  document.querySelectorAll(".bbcdm").forEach((e) => { e.textContent = t; });
}, 1000);
function bbInfo() {
  const el = document.getElementById("bbinfo");
  if (!el || !data || !data.bb || !data.bb.now) return;
  const n = data.bb.now, t = data.bb.trend || {}, s = data.bb.settings || {};
  const coin = data.equity ? data.balances.base * data.price / data.equity * 100 : 0;
  const ba = bbBuyAt();
  el.innerHTML =
    bbZones().map((z) => `<span class="sw" style="border-color:${z.c};border-top-style:dashed"></span>${z.name} <b style="color:${z.c}">${usd(z.v)}</b>${z.v === ba ? " <b>(next)</b>" : ""} <span style="font-size:12px">(${z.how})</span>`).join("<br>") +
    (data.bb.next_limit != null ? `<br><span style="color:#d04545">Next buy in a row needs ${usd(data.bb.next_limit)} or lower</span>` : "") +
    ` <br>Candle closes in <b id="bbcd" style="color:#ff3fa4">--:--</b><br>` +
    `<span class="sw" style="border-color:#e09a2b"></span>Upper <b>${usd(n.upper)}</b>` +
    `<span class="sw" style="border-color:#8a63d2;border-top-style:dashed"></span>Middle <b>${usd(n.middle)}</b>` +
    `<span class="sw" style="border-color:#e09a2b"></span>Lower <b>${usd(n.lower)}</b>` +
    `<span class="sw" style="border-color:#1f9e8f;border-top-style:dotted"></span>${s.trend_period || 50}-avg ${s.trend_timeframe || "4h"} <b>${t.sma ? usd(t.sma) : "-"}</b><br>` +
    `Band width ${n.width_pct.toFixed(2)}%${n.squeeze ? ' &middot; <b>SQUEEZE</b> (smaller buys)' : ""} &middot; ` +
    `Trend: <span class="${t.down ? "down" : ""}">${t.down ? "DOWN, half-size buys" : "ok, full size"}</span>` +
    (t.slope_pct != null ? ` (avg ${t.slope_pct >= 0 ? "+" : ""}${t.slope_pct.toFixed(2)}%/day)` : "") +
    ` &middot; SOL ${coin.toFixed(0)}% of money (cap ${s.max_coin_pct || 60}%)`;
}
"""


def patch_page(page):
    html = page.decode()
    edits = [
        ("</style>", HEAD_CSS + "</style>"),
        ('<svg id="pchart" height="320" role="img" aria-label="Price candles with the bot\'s orders and trades"></svg>',
         '<svg id="pchart" height="440" role="img" aria-label="Price candles with the bot\'s orders and trades"></svg>'
         '\n      <div id="bbinfo"></div>'),
        ("  const W = svg.clientWidth || 600, H = 320, L = 6, R = 62, T = 10, B = 26;",
         "  const W = svg.clientWidth || 600, H = 440, L = 6, R = 62, T = 10, B = 26;"),
        ("function renderPriceChart() {", OVERLAY_JS + "\nfunction renderPriceChart() {"),
        # keep the whole band visible when zoomed out
        ("  const pad = (hi - lo) * 0.06 || 1; lo -= pad; hi += pad;",
         "  if (!zoomed && data && data.bb && data.bb.bands) {\n"
         "    const tA = vis[0][0], tB = vis[vn - 1][0] + CANDLE_MS[pRange];\n"
         "    for (const r of data.bb.bands) if (r[0] >= tA && r[0] <= tB) { lo = Math.min(lo, r[3]); hi = Math.max(hi, r[1]); }\n"
         "    if (data.bb.now) { lo = Math.min(lo, data.bb.now.lower); hi = Math.max(hi, data.bb.now.upper); }\n"
         "  }\n"
         "  const pad = (hi - lo) * 0.06 || 1; lo -= pad; hi += pad;"),
        ("  // candles: hollow = up, filled = down",
         "  s += bbOverlay(vis, vn, xt, y, lo, hi, t0, step, L, W - R);\n  // candles: hollow = up, filled = down"),
        ('  s += `<line id="pxhair"', '  bbInfo();\n  s += `<line id="pxhair"'),
    ]
    for old, new in edits:
        if html.count(old) != 1:
            log.warning("bollinger dashboard: page layout changed, could not add %r", old[:50])
            continue
        html = html.replace(old, new)
    return html.encode()


def _snapshot(cfg, _orig=base.snapshot):
    out = _orig(cfg)
    try:
        out["bb"] = json.loads((cfg.data_dir / "bb-series.json").read_text())
    except (OSError, ValueError):
        out["bb"] = None
    try:  # step-down limit for buys in a row (same maths as bollinger._next_buy_limit)
        st = json.loads((cfg.data_dir / "state-bb-live.json").read_text()) if (cfg.data_dir / "state-bb-live.json").exists() else {}
        s = (st.get("bollinger") or st).get("streak") or {}
        if out["bb"] is not None and s.get("count") and s.get("last_buy"):
            out["bb"]["next_limit"] = s["last_buy"] * (1 - cfg.bollinger.step_down_pct * s["count"] / 100)
    except Exception as e:
        log.warning("next-buy limit: %s", e)
    return out


def serve(cfg, notifier=None):
    base.PAGE = patch_page(base.PAGE)
    base.snapshot = _snapshot
    base.serve(cfg, notifier=notifier)
