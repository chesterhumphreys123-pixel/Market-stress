#!/usr/bin/env python3
"""
Daily Crash Monitor
Pulls 10 market stress indicators, grades each green/amber/red, checks for
dangerous clusters, logs to CSV and builds a personal dashboard page
(site/index.html).

Env vars:
  FRED_API_KEY       free key from https://fred.stlouisfed.org/docs/api/api_key.html
  CRASH_LOG          optional, CSV path (default crash_monitor_log.csv)
  SITE_DIR           optional, output folder for the page (default site)
  SLACK_WEBHOOK_URL  optional, also posts a summary to Slack if set
"""
import csv
import html
import datetime as dt
import os
from dataclasses import dataclass, field

import pandas as pd
import requests
import yfinance as yf

FRED_KEY = os.environ.get("FRED_API_KEY")
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL")
LOG_FILE = os.environ.get("CRASH_LOG", "crash_monitor_log.csv")
SITE_DIR = os.environ.get("SITE_DIR", "site")

GREEN, AMBER, RED, NA = 0, 1, 2, None
LABEL = {GREEN: "GREEN", AMBER: "AMBER", RED: "RED", NA: "N/A"}


@dataclass
class Result:
    name: str
    level: int | None
    detail: str
    metrics: dict = field(default_factory=dict)


# ---------- helpers ----------
def grade(value, amber, red, higher_is_worse=True):
    if value is None or pd.isna(value):
        return NA
    if higher_is_worse:
        return RED if value >= red else AMBER if value >= amber else GREEN
    return RED if value <= red else AMBER if value <= amber else GREEN


def worst(*levels):
    valid = [lv for lv in levels if lv is not None]
    return max(valid) if valid else NA


def fred(series_id, days=400):
    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    r = requests.get(
        "https://api.stlouisfed.org/fred/series/observations",
        params={"series_id": series_id, "api_key": FRED_KEY,
                "file_type": "json", "observation_start": start},
        timeout=30)
    r.raise_for_status()
    obs = r.json()["observations"]
    s = pd.Series({pd.Timestamp(o["date"]): float(o["value"])
                   for o in obs if o["value"] != "."})
    return s.sort_index()


def yahoo(ticker, period="1y"):
    s = yf.Ticker(ticker).history(period=period, auto_adjust=False)["Close"].dropna()
    s.index = s.index.tz_localize(None)
    return s


def chg(s, n=1):
    return float(s.iloc[-1] - s.iloc[-1 - n]) if len(s) > n else None


def pct(s, n=1):
    return float((s.iloc[-1] / s.iloc[-1 - n] - 1) * 100) if len(s) > n else None


# ---------- the 10 checks ----------
def check_yields():
    y10, y30 = fred("DGS10"), fred("DGS30")
    d10, d30 = chg(y10) * 100, chg(y30) * 100
    lvl = worst(grade(d10, 10, 20), grade(d30, 10, 20))
    return Result("Treasury yields", lvl,
                  f"10y {y10.iloc[-1]:.2f}% ({d10:+.0f}bps), 30y {y30.iloc[-1]:.2f}% ({d30:+.0f}bps)",
                  {"y10": y10.iloc[-1], "d10_bps": d10, "y30": y30.iloc[-1], "d30_bps": d30})


def check_auctions():
    start = (dt.date.today() - dt.timedelta(days=400)).isoformat()
    r = requests.get(
        "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query",
        params={"filter": f"auction_date:gte:{start},security_type:in:(Note,Bond)",
                "fields": "auction_date,security_type,security_term,floating_rate,inflation_index_security,bid_to_cover_ratio",
                "sort": "-auction_date", "page[size]": 500},
        timeout=30)
    r.raise_for_status()
    df = pd.DataFrame(r.json()["data"])
    # nominal fixed-rate notes and bonds only (exclude FRNs and TIPS)
    df = df[(df["floating_rate"] == "No") & (df["inflation_index_security"] == "No")]
    df = df[~df["bid_to_cover_ratio"].isin([None, "", "null"])].copy()
    df["btc"] = df["bid_to_cover_ratio"].astype(float)
    df["auction_date"] = pd.to_datetime(df["auction_date"])
    recent = df[df["auction_date"] >= pd.Timestamp.today().normalize() - pd.Timedelta(days=7)]
    if recent.empty:
        return Result("Treasury auctions", GREEN, "No note/bond auctions in the last 7 days")
    levels, notes = [], []
    for _, row in recent.iterrows():
        hist = df[(df["security_term"] == row["security_term"])
                  & (df["auction_date"] < row["auction_date"])].head(6)
        if hist.empty:
            continue
        gap = row["btc"] - hist["btc"].mean()
        levels.append(grade(gap, -0.15, -0.30, higher_is_worse=False))
        notes.append(f"{row['security_term']} {row['btc']:.2f}x ({gap:+.2f} vs 6-auction avg)")
    return Result("Treasury auctions", worst(*levels) if levels else GREEN,
                  "; ".join(notes) or "No comparable history", {"auctions": " | ".join(notes)})


def check_curve():
    c2, c3 = fred("T10Y2Y"), fred("T10Y3M")
    s2, s3 = chg(c2, 20) * 100, chg(c3, 20) * 100
    inverted = c2.iloc[-1] < 0 or c3.iloc[-1] < 0
    lvl = worst(grade(s2, 25, 50), grade(s3, 25, 50), AMBER if inverted else GREEN)
    return Result("Yield curve", lvl,
                  f"2s10s {c2.iloc[-1]*100:+.0f}bps ({s2:+.0f} in 20d), 3m10y {c3.iloc[-1]*100:+.0f}bps ({s3:+.0f} in 20d)",
                  {"2s10s": c2.iloc[-1], "3m10y": c3.iloc[-1], "2s10s_20d_bps": s2})


def check_vol():
    vix, move = yahoo("^VIX"), yahoo("^MOVE")
    v, m = float(vix.iloc[-1]), float(move.iloc[-1])
    lvl = worst(grade(v, 22, 30), grade(m, 110, 140))
    return Result("Volatility (VIX/MOVE)", lvl, f"VIX {v:.1f}, MOVE {m:.0f}", {"vix": v, "move": m})


def check_credit():
    hy, ig = fred("BAMLH0A0HYM2"), fred("BAMLC0A0CM")
    hy5, ig5 = chg(hy, 5) * 100, chg(ig, 5) * 100
    lvl = worst(grade(hy.iloc[-1], 4.5, 6.0), grade(hy5, 30, 50),
                grade(ig.iloc[-1], 1.3, 1.6), grade(ig5, 15, 25))
    return Result("Credit spreads", lvl,
                  f"HY {hy.iloc[-1]*100:.0f}bps ({hy5:+.0f} in 5d), IG {ig.iloc[-1]*100:.0f}bps ({ig5:+.0f} in 5d)",
                  {"hy": hy.iloc[-1], "hy_5d_bps": hy5, "ig": ig.iloc[-1]})


def check_fx():
    dxy, jpy = yahoo("DX-Y.NYB"), yahoo("JPY=X")
    d1, j1, j5 = pct(dxy), pct(jpy), pct(jpy, 5)
    lvl = worst(grade(abs(d1), 1.0, 1.5),
                grade(j1, -1.5, -3.0, higher_is_worse=False),
                grade(j5, -3.0, -5.0, higher_is_worse=False))
    return Result("Dollar and yen", lvl,
                  f"DXY {dxy.iloc[-1]:.2f} ({d1:+.2f}%), USDJPY {jpy.iloc[-1]:.2f} ({j1:+.2f}% 1d, {j5:+.2f}% 5d)",
                  {"dxy": dxy.iloc[-1], "dxy_1d": d1, "usdjpy_1d": j1})


def check_gold():
    gold, real = yahoo("GC=F"), fred("DFII10")
    g20, r20 = pct(gold, 20), chg(real, 20) * 100
    if g20 >= 8 and r20 >= 25:
        lvl = RED
    elif g20 >= 5 and r20 >= 15:
        lvl = AMBER
    else:
        lvl = GREEN
    return Result("Gold vs real yields", lvl,
                  f"Gold {gold.iloc[-1]:,.0f} ({g20:+.1f}% 20d), 10y real yield {real.iloc[-1]:.2f}% ({r20:+.0f}bps 20d)",
                  {"gold": gold.iloc[-1], "gold_20d": g20, "real_20d_bps": r20})


def check_oil():
    brent = yahoo("BZ=F")
    b1, b20 = pct(brent), pct(brent, 20)
    lvl = worst(grade(abs(b1), 5, 8), grade(b20, 15, 25))
    return Result("Brent crude", lvl,
                  f"${brent.iloc[-1]:.2f} ({b1:+.1f}% 1d, {b20:+.1f}% 20d)",
                  {"brent": brent.iloc[-1], "brent_1d": b1, "brent_20d": b20})


def check_funding():
    sofr, iorb, srf = fred("SOFR"), fred("IORB"), fred("RPONTSYD")
    spread = (sofr.iloc[-1] - iorb.iloc[-1]) * 100
    srf_use = float(srf.iloc[-1])
    lvl = worst(grade(spread, 5, 15), grade(srf_use, 10, 50))
    note = " (quarter/month-end spikes are normal)" if dt.date.today().day >= 26 or dt.date.today().day <= 2 else ""
    return Result("Funding markets", lvl,
                  f"SOFR-IORB {spread:+.0f}bps, Fed repo usage ${srf_use:.1f}bn{note}",
                  {"sofr_iorb_bps": spread, "srf_bn": srf_use})


def check_tech():
    ndx, nvda = yahoo("^NDX"), yahoo("NVDA")
    dd_ndx = (ndx.iloc[-1] / ndx.max() - 1) * 100
    dd_nvda = (nvda.iloc[-1] / nvda.max() - 1) * 100
    n1 = pct(ndx)
    lvl = worst(grade(dd_ndx, -10, -20, False), grade(dd_nvda, -15, -30, False),
                grade(n1, -3, -5, False))
    return Result("AI / mega-cap tech", lvl,
                  f"NDX {n1:+.1f}% 1d, {dd_ndx:.1f}% from 1y high; NVDA {dd_nvda:.1f}% from 1y high",
                  {"ndx_1d": n1, "ndx_dd": dd_ndx, "nvda_dd": dd_nvda})


CHECKS = [
    ("Treasury yields", check_yields,
     "Fast rises without good news mean buyers want more to hold US debt."),
    ("Treasury auctions", check_auctions,
     "Weak demand at auctions is the most direct sign of strain on government borrowing."),
    ("Yield curve", check_curve,
     "Rapid steepening from inversion often arrives just as a recession starts."),
    ("Volatility (VIX/MOVE)", check_vol,
     "Fear gauges for stocks (VIX) and bonds (MOVE)."),
    ("Credit spreads", check_credit,
     "Lenders demanding more for corporate risk. Credit usually cracks before stocks."),
    ("Dollar and yen", check_fx,
     "A dollar spike means a scramble for cash; a yen surge means carry trades unwinding."),
    ("Gold vs real yields", check_gold,
     "Gold rising with real yields points to lost confidence in sovereign debt."),
    ("Brent crude", check_oil,
     "Oil shocks feed inflation; oil collapses signal falling demand."),
    ("Funding markets", check_funding,
     "Overnight rates and Fed repo use show whether the financial plumbing is seizing."),
    ("AI / mega-cap tech", check_tech,
     "Growth leans heavily on the AI trade; a break here hits wealth and investment together."),
]
WHY = {name: why for name, _, why in CHECKS}


# ---------- clusters and scoring ----------
def clusters(results):
    by = {r.name: r for r in results}
    alerts = []
    y = by["Treasury yields"].metrics
    fx = by["Dollar and yen"].metrics
    auct = by["Treasury auctions"].level or 0
    if y.get("d10_bps", 0) >= 10 and fx.get("dxy_1d", 0) < 0 and auct >= AMBER:
        alerts.append("Debt confidence: yields up, dollar down and weak auction demand together.")
    if all((by[n].level or 0) >= AMBER for n in
           ["Volatility (VIX/MOVE)", "Credit spreads", "Funding markets"]):
        alerts.append("Liquidity stress: volatility, credit and funding are all flashing at once.")
    return alerts


def overall_level(results, alerts, score):
    if alerts or score >= 8:
        return RED
    if score >= 4 or any(r.level == RED for r in results):
        return AMBER
    return GREEN


# ---------- logging ----------
def log(results, score, overall, alerts):
    row = {"timestamp": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
           "score": score, "overall": LABEL[overall], "alerts": " | ".join(alerts)}
    for r in results:
        row[f"{r.name} level"] = LABEL[r.level]
        for k, v in r.metrics.items():
            row[f"{r.name} {k}"] = round(v, 4) if isinstance(v, float) else v
    exists = os.path.exists(LOG_FILE)
    fields = list(row.keys())
    if exists:
        with open(LOG_FILE, newline="") as f:
            fields = next(csv.reader(f), fields)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(row)


def load_history(n=90):
    if not os.path.exists(LOG_FILE):
        return pd.DataFrame()
    df = pd.read_csv(LOG_FILE, keep_default_na=False)
    return df.tail(n).reset_index(drop=True)


# ---------- page ----------
CLS = {GREEN: "g", AMBER: "a", RED: "r", NA: "n"}
FROM_LABEL = {"GREEN": "g", "AMBER": "a", "RED": "r"}
HEADLINE = {GREEN: "Calm. No meaningful stress.",
            AMBER: "Elevated. Some indicators need watching.",
            RED: "High stress. Check the alerts below."}


def gauge_svg(score):
    import math
    cx, cy, r = 110, 110, 88

    def pt(v, rad=r):
        a = math.pi * (1 - min(v, 20) / 20)
        return cx + rad * math.cos(a), cy - rad * math.sin(a)

    def arc(v1, v2, cls):
        x1, y1 = pt(v1)
        x2, y2 = pt(v2)
        return f'<path class="arc {cls}" d="M{x1:.1f} {y1:.1f} A{r} {r} 0 0 1 {x2:.1f} {y2:.1f}"/>'

    nx, ny = pt(score, 72)
    ticks = "".join(
        f'<text class="tick" x="{pt(v, 108)[0]:.0f}" y="{pt(v, 108)[1] + 4:.0f}">{v}</text>'
        for v in (0, 4, 8, 20))
    return f'''<svg class="gauge" viewBox="-14 0 248 128" role="img" aria-label="Stress score {score} out of 20">
  {arc(0, 3.9, "g")}{arc(4.1, 7.9, "a")}{arc(8.1, 20, "r")}
  <line class="needle" x1="{cx}" y1="{cy}" x2="{nx:.1f}" y2="{ny:.1f}"/>
  <circle class="hub" cx="{cx}" cy="{cy}" r="6"/>{ticks}
</svg>'''


def history_svg(hist):
    if len(hist) < 2:
        return '<p class="empty">The trend line appears after the monitor has run on at least two days.</p>'
    w, h, pad = 640, 160, 24
    scores = [float(x) for x in hist["score"]]
    top = max(12, max(scores) + 2)
    step = (w - 2 * pad) / (len(scores) - 1)
    y = lambda v: h - pad - (v / top) * (h - 2 * pad)
    pts = " ".join(f"{pad + i * step:.1f},{y(v):.1f}" for i, v in enumerate(scores))
    first = html.escape(str(hist["timestamp"].iloc[0])[:10])
    last = html.escape(str(hist["timestamp"].iloc[-1])[:10])
    return f'''<svg class="trend" viewBox="0 0 {w} {h}" role="img" aria-label="Stress score history">
  <line class="band a" x1="{pad}" x2="{w - pad}" y1="{y(4):.1f}" y2="{y(4):.1f}"/>
  <line class="band r" x1="{pad}" x2="{w - pad}" y1="{y(8):.1f}" y2="{y(8):.1f}"/>
  <text class="blabel" x="{w - pad}" y="{y(4) - 4:.1f}">amber</text>
  <text class="blabel" x="{w - pad}" y="{y(8) - 4:.1f}">red</text>
  <polyline class="line" points="{pts}"/>
  <circle class="dot" cx="{pad + (len(scores) - 1) * step:.1f}" cy="{y(scores[-1]):.1f}" r="4"/>
  <text class="axis" x="{pad}" y="{h - 4}">{first}</text>
  <text class="axis end" x="{w - pad}" y="{h - 4}">{last}</text>
</svg>'''


def strip(hist, name, n=14):
    col = f"{name} level"
    if hist.empty or col not in hist:
        return ""
    vals = list(hist[col].tail(n))
    dots = "".join(f'<i class="{FROM_LABEL.get(v, "n")}" title="{html.escape(str(v))}"></i>' for v in vals)
    return f'<span class="strip" aria-label="Last {len(vals)} readings">{dots}</span>'


def build_page(results, alerts, score, overall, hist):
    updated = dt.datetime.now(dt.timezone.utc).strftime("%a %d %b %Y, %H:%M UTC")
    rows = []
    for r in sorted(results, key=lambda r: -(r.level if r.level is not None else -1)):
        rows.append(f'''<li class="ind {CLS[r.level]}">
  <div class="ind-head"><h3>{html.escape(r.name)}</h3><span class="pill">{LABEL[r.level].title()}</span></div>
  <p class="detail">{html.escape(r.detail)}</p>
  <p class="why">{html.escape(WHY.get(r.name, ""))}</p>
  {strip(hist, r.name)}
</li>''')
    alert_html = ""
    if alerts:
        items = "".join(f"<li>{html.escape(a)}</li>" for a in alerts)
        alert_html = f'<section class="alerts" aria-label="Cluster alerts"><h2>Cluster alerts</h2><ul>{items}</ul></section>'
    counts = {lv: sum(1 for r in results if r.level == lv) for lv in (RED, AMBER, GREEN)}
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<title>Market stress | {LABEL[overall].title()}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,500;6..72,600&family=Public+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {{
  --bg:#E9EDF0; --paper:#F6F8F9; --ink:#1B2A3A; --muted:#5A6B7B; --rule:#CBD3DA;
  --g:#3F7D58; --a:#B97D12; --r:#B23A2E; --n:#98A4AE;
  box-sizing:border-box;
  padding-top:env(safe-area-inset-top,0px); padding-bottom:env(safe-area-inset-bottom,0px);
}}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#121C25; --paper:#18242F; --ink:#E4EBF0; --muted:#94A3B0; --rule:#2A3947;
    --g:#6FB38A; --a:#E0A93E; --r:#E46E60; --n:#5E6D79; }}
}}
*,*::before,*::after {{ box-sizing:inherit; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:16px/1.55 "Public Sans", system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width:880px; margin:0 auto; padding:32px 20px 56px; }}
h1,h2,h3 {{ font-family:"Newsreader", Georgia, serif; font-weight:600; margin:0; }}
.hero {{ display:grid; grid-template-columns:minmax(200px,280px) 1fr; gap:28px; align-items:center;
  background:var(--paper); border:1px solid var(--rule); border-radius:18px; padding:24px 28px; }}
.gauge {{ width:100%; height:auto; }}
.arc {{ fill:none; stroke-width:16; stroke-linecap:butt; }}
.arc.g {{ stroke:var(--g); }} .arc.a {{ stroke:var(--a); }} .arc.r {{ stroke:var(--r); opacity:.85; }}
.needle {{ stroke:var(--ink); stroke-width:3.5; stroke-linecap:round; }}
.hub {{ fill:var(--ink); }}
.tick {{ fill:var(--muted); font-size:10px; text-anchor:middle; }}
.score {{ font-family:"Newsreader", Georgia, serif; font-size:clamp(3rem,9vw,4.5rem); line-height:1; }}
.score small {{ font-size:.4em; color:var(--muted); }}
.hero h1 {{ font-size:clamp(1.4rem,3.5vw,1.9rem); line-height:1.2; margin:10px 0 8px; }}
.hero h1.g {{ color:var(--g); }} .hero h1.a {{ color:var(--a); }} .hero h1.r {{ color:var(--r); }}
.meta {{ color:var(--muted); margin:0; font-size:.93rem; }}
.alerts {{ margin-top:18px; border-left:5px solid var(--r); background:var(--paper);
  border-radius:0 12px 12px 0; padding:14px 20px; }}
.alerts h2 {{ font-size:1.15rem; color:var(--r); }}
.alerts ul {{ margin:6px 0 0; padding-left:18px; }}
section.block {{ margin-top:36px; }}
section.block > h2 {{ font-size:1.35rem; margin-bottom:12px; }}
.list {{ list-style:none; margin:0; padding:0; display:grid; gap:10px; }}
.ind {{ background:var(--paper); border:1px solid var(--rule); border-left:6px solid var(--n);
  border-radius:4px 12px 12px 4px; padding:14px 18px; }}
.ind.g {{ border-left-color:var(--g); }} .ind.a {{ border-left-color:var(--a); }} .ind.r {{ border-left-color:var(--r); }}
.ind-head {{ display:flex; justify-content:space-between; align-items:baseline; gap:12px; }}
.ind h3 {{ font-size:1.12rem; }}
.pill {{ font-size:.8rem; font-weight:600; padding:2px 10px; border-radius:99px; color:var(--paper); background:var(--n); }}
.g .pill {{ background:var(--g); }} .a .pill {{ background:var(--a); }} .r .pill {{ background:var(--r); }}
.detail {{ margin:6px 0 2px; font-variant-numeric:tabular-nums; }}
.why {{ margin:0; color:var(--muted); font-size:.9rem; }}
.strip {{ display:flex; gap:4px; margin-top:10px; }}
.strip i {{ width:10px; height:10px; border-radius:50%; background:var(--n); }}
.strip i.g {{ background:var(--g); }} .strip i.a {{ background:var(--a); }} .strip i.r {{ background:var(--r); }}
.trend-wrap {{ background:var(--paper); border:1px solid var(--rule); border-radius:12px; padding:12px; overflow-x:auto; }}
.trend {{ width:100%; min-width:420px; height:auto; display:block; }}
.line {{ fill:none; stroke:var(--ink); stroke-width:2.2; stroke-linejoin:round; }}
.dot {{ fill:var(--ink); }}
.band {{ stroke-width:1; stroke-dasharray:4 4; }} .band.a {{ stroke:var(--a); }} .band.r {{ stroke:var(--r); }}
.blabel {{ fill:var(--muted); font-size:11px; text-anchor:end; }}
.axis {{ fill:var(--muted); font-size:11px; }} .axis.end {{ text-anchor:end; }}
.empty {{ color:var(--muted); margin:8px; }}
footer {{ margin-top:36px; color:var(--muted); font-size:.85rem; }}
@media (max-width:620px) {{
  .hero {{ grid-template-columns:1fr; padding:20px; }}
  .gauge {{ max-width:260px; justify-self:center; }}
}}
</style>
</head>
<body>
<main>
  <section class="hero">
    {gauge_svg(score)}
    <div>
      <div class="score">{score}<small> / 20</small></div>
      <h1 class="{CLS[overall]}">{HEADLINE[overall]}</h1>
      <p class="meta">{counts[RED]} red, {counts[AMBER]} amber, {counts[GREEN]} green. Updated {updated}.</p>
    </div>
  </section>
  {alert_html}
  <section class="block">
    <h2>Indicators, worst first</h2>
    <ul class="list">
{''.join(rows)}
    </ul>
  </section>
  <section class="block">
    <h2>Stress score over time</h2>
    <div class="trend-wrap">{history_svg(hist)}</div>
  </section>
  <footer>
    Sources: FRED, US Treasury Fiscal Data, Yahoo Finance. FRED series reflect the previous close.
    Dots under each indicator show its last 14 readings, oldest on the left. Not investment advice.
  </footer>
</main>
</body>
</html>'''


def slack_text(results, alerts, score, overall):
    icon = {GREEN: ":large_green_circle:", AMBER: ":large_yellow_circle:", RED: ":red_circle:", NA: ":white_circle:"}
    lines = [f"{icon[overall]} *Market stress: {LABEL[overall]}* | score {score}/20"]
    lines += [f"• {a}" for a in alerts]
    lines += [f"{icon[r.level]} {r.name}: {r.detail}" for r in results if (r.level or 0) >= AMBER]
    return "\n".join(lines)


def main():
    if not FRED_KEY:
        print("Warning: FRED_API_KEY not set, FRED-based checks will fail.")
    results = []
    for name, check, _ in CHECKS:
        try:
            res = check()
            res.name = name
            results.append(res)
        except Exception as e:  # keep going if one source fails
            results.append(Result(name, NA, f"Data unavailable ({type(e).__name__})"))

    alerts = clusters(results)
    score = sum(r.level or 0 for r in results)
    overall = overall_level(results, alerts, score)

    log(results, score, overall, alerts)
    hist = load_history()
    os.makedirs(SITE_DIR, exist_ok=True)
    with open(os.path.join(SITE_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(build_page(results, alerts, score, overall, hist))
    print(f"Score {score}/20 ({LABEL[overall]}). Page written to {SITE_DIR}/index.html")

    if SLACK_WEBHOOK:
        requests.post(SLACK_WEBHOOK, json={"text": slack_text(results, alerts, score, overall)},
                      timeout=30).raise_for_status()


if __name__ == "__main__":
    main()
