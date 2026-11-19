#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rs_core.py — RS 與族群強度計算核心（v3）

修正重點（相對 v2）：
  1. 用還原股價（除權息、減資、面額變更都已調整）
  2. 排名母體只含上市櫃普通股：排除 ETF/ETN/DR/受益證券/指數/興櫃
  3. 無成交日不會以 0 價進入計算（停牌日沿用前一日收盤）
  4. 期間以「市場交易日曆」對齊（63/126/189/252 個交易日前），停牌股不會錯位
  5. 市值 = 實際發行股數 × 收盤價（不再假設面額 10 元）
  6. 增量快取（見 market_data.py）

RS 原始分數 = 3月報酬×40% + 6月×30% + 9月×20% + 12月×10%（上市未滿 12 個月者以既有期間權重正規化）
RS 排名     = 原始分數在全市場的百分位（1–99）
"""

import os
import sys
import json

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import market_data as md  # noqa: E402

PERIODS = {"3m": 63, "6m": 126, "9m": 189, "12m": 252}
WEIGHTS = {"3m": 0.4, "6m": 0.3, "9m": 0.2, "12m": 0.1}
MIN_STOCKS = 5          # 官方產業最少檔數
TH_MEDIAN_RS = 70
TH_PCT80 = 30
STALE_DAYS = 10         # 超過 10 個交易日沒成交 → 視為停牌，不列入排名
THEME_FILE = os.path.join(HERE, "細分族群.json")


def log(msg):
    print(f"[RS] {msg}", file=sys.stderr, flush=True)


def rs_level(rs):
    if rs is None:
        return "無法計算"
    return ("非常強勢（前 10%）" if rs >= 90 else "強勢（前 20%）" if rs >= 80
            else "中等偏強（前 30%）" if rs >= 70 else "中等（前 50%）" if rs >= 50 else "弱於市場平均")


def load_themes():
    try:
        with open(THEME_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def compute_all(update=True, max_seconds=None):
    """
    回傳 (df, ctx)
      df：index=代號，欄位 name, market, industry, rs, raw, r3m..r12m, tt, tt_checks, off_high,
          close, cap_yi, val20_m, turnover_pct, last_trade
      ctx：as_of, adj（還原價 DataFrame）, adj_report
    """
    if update:
        md.refresh(max_seconds=max_seconds)
    raw = md.load_daily()
    events = md.load_events()
    info = md.fetch_stock_info()

    adj, report = md.build_adjusted(raw, events)
    dates = adj.index
    as_of = dates[-1]

    # ---- 母體：上市櫃普通股 ----
    universe = [c for c in adj.columns if c in info.index]
    adj = adj[universe]
    close_raw = raw["close"][universe]
    money = raw["money"][universe].fillna(0.0)

    traded = close_raw.notna()
    last_pos = traded.values[::-1].argmax(axis=0)          # 距最新日幾天沒成交
    has_any = traded.values.any(axis=0)
    alive = pd.Series((last_pos <= STALE_DAYS) & has_any, index=universe)

    ff = adj.ffill()                                       # 停牌日沿用前一日（不會出現 0 價）
    n = len(dates)
    latest = ff.iloc[-1]

    # ---- 各期間報酬（以交易日曆對齊）----
    rets = {}
    for p, k in PERIODS.items():
        if n - 1 - k >= 0:
            base = ff.iloc[n - 1 - k]
            r = latest / base - 1
            r[base.isna() | (base <= 0)] = np.nan
            rets[p] = r
        else:
            rets[p] = pd.Series(np.nan, index=universe)
    R = pd.DataFrame(rets)
    W = pd.Series(WEIGHTS)
    wsum = R.notna().mul(W).sum(axis=1)
    raw_score = R.fillna(0).mul(W).sum(axis=1) / wsum.replace(0, np.nan)
    elig = alive & R["3m"].notna() & raw_score.notna()
    raw_score = raw_score[elig]
    rs = raw_score.rank(pct=True).apply(lambda x: int(max(1, min(99, round(x * 98 + 1)))))

    # ---- 趨勢模板（Minervini 7 項）----
    ma50 = ff.rolling(50, min_periods=50).mean()
    ma150 = ff.rolling(150, min_periods=150).mean()
    ma200 = ff.rolling(200, min_periods=200).mean()
    m50, m150, m200 = ma50.iloc[-1], ma150.iloc[-1], ma200.iloc[-1]
    m200_prev = ma200.iloc[-23] if n >= 23 else pd.Series(np.nan, index=universe)
    win = ff.iloc[-252:]
    hi, lo = win.max(), win.min()
    nobs = ff.notna().sum()
    c = latest
    checks = pd.DataFrame({
        "above_150_200": (c > m150) & (c > m200),
        "ma150_gt_200": m150 > m200,
        "ma200_rising": m200 > m200_prev,
        "ma50_gt_150_200": (m50 > m150) & (m50 > m200),
        "above_50": c > m50,
        "above_low_30": c >= lo * 1.3,
        "within_25_high": c >= hi * 0.75,
    })
    tt = checks.sum(axis=1).astype(int)
    tt[nobs < 60] = -1
    off_high = (c / hi - 1) * 100

    # ---- 流動性、市值 ----
    val20 = money.iloc[-20:].mean() / 1e6
    shares = pd.Series({**md.fetch_twse_shares(), **raw["shares_tpex"]}, dtype=float).reindex(universe)
    last_close_raw = close_raw.ffill().iloc[-1]
    cap_yi = shares * last_close_raw / 1e8
    turnover = val20 * 1e6 / (cap_yi * 1e8) * 100

    last_trade = pd.Series([dates[n - 1 - p].strftime("%Y-%m-%d") if h else None
                            for p, h in zip(last_pos, has_any)], index=universe)

    df = pd.DataFrame({
        "name": info["name"].reindex(universe),
        "market": info["market"].reindex(universe),
        "industry": info["industry"].reindex(universe),
        "rs": rs.reindex(universe),
        "raw": raw_score.reindex(universe),
        "r3m": R["3m"] * 100, "r6m": R["6m"] * 100, "r9m": R["9m"] * 100, "r12m": R["12m"] * 100,
        "tt": tt, "off_high": off_high, "close": last_close_raw,
        "cap_yi": cap_yi, "val20_m": val20, "turnover_pct": turnover, "last_trade": last_trade,
    })
    df["tt_checks"] = checks.apply(lambda r: [bool(x) for x in r.values], axis=1)
    df = df[df["rs"].notna()].copy()
    df["rs"] = df["rs"].astype(int)
    log(f"全市場 {len(df)} 檔完成 RS 排名（資料日 {as_of:%Y-%m-%d}）")
    ctx = {"as_of": as_of.strftime("%Y-%m-%d"), "adj": adj, "adj_report": report}
    return df, ctx


# ============================================================
# 族群統計
# ============================================================

def group_stats(g):
    if len(g) == 0:
        return None
    med = float(g["rs"].median())
    pct80 = round(float((g["rs"] >= 80).mean() * 100), 1)
    med3 = float(g["r3m"].median())
    strong = med >= TH_MEDIAN_RS and pct80 >= TH_PCT80
    if strong and med3 > 0:
        verdict = "真族群行情"
    elif strong and med3 <= -10:
        verdict = "假強勢（整群修正中，高 RS 是歷史遺產）"
    elif strong:
        verdict = "強勢但動能轉弱（近 3 月中位數報酬偏弱）"
    elif pct80 >= TH_PCT80:
        verdict = "族群分化（個股行情）"
    elif (g["rs"] >= 95).any():
        verdict = "個股獨走"
    else:
        verdict = "非強勢族群"
    return {
        "count": int(len(g)), "median_rs": med, "mean_rs": round(float(g["rs"].mean()), 1),
        "pct_rs80": pct80, "median_ret3m": round(med3, 1), "is_strong": bool(strong),
        "verdict": verdict, "small_sample": bool(len(g) < MIN_STOCKS),
    }


def member_row(df, c, rank=None):
    r = df.loc[c]

    def f(x, nd=1):
        return None if pd.isna(x) else round(float(x), nd)
    out = {
        "code": c, "name": r["name"], "rs": int(r["rs"]), "ret3m": f(r["r3m"]),
        "trend_template": f"{int(r['tt'])}/7" if r["tt"] >= 0 else "?/7",
        "off_52w_high_pct": f(r["off_high"]),
        "market_cap_yi": None if pd.isna(r["cap_yi"]) else int(round(r["cap_yi"])),
        "avg_val20_百萬": None if pd.isna(r["val20_m"]) else int(round(r["val20_m"])),
    }
    if rank is not None:
        out = {"rank": rank, **out}
    return out


def industry_ranking(df):
    rows = []
    for ind, g in df.groupby("industry"):
        if len(g) < MIN_STOCKS:
            continue
        s = group_stats(g)
        s["industry"] = ind
        s["top3"] = [{"code": c, "name": df.loc[c, "name"], "rs": int(df.loc[c, "rs"])}
                     for c in g.sort_values("raw", ascending=False).head(3).index]
        rows.append(s)
    rows.sort(key=lambda x: (x["median_rs"], x["pct_rs80"]), reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def theme_ranking(df):
    rows = []
    for name, codes in load_themes().items():
        g = df[df.index.isin(codes)]
        if len(g) < 2:
            continue
        s = group_stats(g)
        s["theme"] = name
        s["missing"] = [c for c in codes if c not in df.index]
        s["members"] = [member_row(df, c) for c in g.sort_values("raw", ascending=False).index]
        rows.append(s)
    rows.sort(key=lambda x: (x["median_rs"], x["pct_rs80"]), reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def group_view(df, codes, target=None, top_n=10):
    g = df[df.index.isin(codes)].sort_values("raw", ascending=False)
    members = [member_row(df, c, i) for i, c in enumerate(g.index, 1)]
    out = {"stats": group_stats(g), "members_top": members[:top_n],
           "pattern_candidates": [m for m in members if m["rs"] >= 80
                                  and m["trend_template"][0].isdigit() and int(m["trend_template"][0]) >= 6]}
    if target and target in g.index:
        t = next(m for m in members if m["code"] == target)
        n = len(members)
        out["leader_verdict"] = {
            "in_group_rank": f"{t['rank']}/{n}",
            "is_first_tier": bool(t["rank"] <= max(3, round(n * 0.03)) and t["rs"] >= 90),
            "stronger_members": [m for m in members if m["rank"] < t["rank"]][:5],
        }
        if t not in out["members_top"]:
            out["members_top"].append(t)
    return out


def stock_verdict(r):
    rs, tt, off = int(r["rs"]), int(r["tt"]), r["off_high"]
    notes = []
    if rs >= 80 and tt >= 6 and off >= -15:
        notes.append("真強勢：RS 高、趨勢模板完整、接近 52 週高點，值得看圖")
    elif rs >= 80 and tt <= 5 and off <= -30:
        notes.append("排名假象：RS 靠過去漲幅撐著，線型已破、距高點深")
    elif rs < 80 and tt >= 6 and r["r3m"] > 15:
        notes.append("落後補漲／修復中：位階還沒到")
    v = r["val20_m"]
    if pd.notna(v):
        if v < 50:
            notes.append("流動性不足（日均成交值 < 5,000 萬），實務上難以操作")
        elif v < 300:
            notes.append("流動性偏低，只能容納小部位")
    t = r["turnover_pct"]
    if pd.notna(t) and t >= 30:
        notes.append("日換手率 ≥ 30%，極端投機")
    return notes


def stock_report(df, ctx, code, top_n=10):
    if code not in df.index:
        return {"error": f"{code} 不在 RS 排名中（可能是興櫃、ETF、上市未滿 3 個月、停牌中或代號不存在）"}
    r = df.loc[code]
    target = member_row(df, code)
    target.update({"industry": r["industry"], "market": r["market"], "rs_level": rs_level(int(r["rs"])),
                   "returns": {k: (None if pd.isna(r[f"r{k}"]) else round(float(r[f"r{k}"]), 2))
                               for k in ("3m", "6m", "9m", "12m")},
                   "turnover_pct": None if pd.isna(r["turnover_pct"]) else round(float(r["turnover_pct"]), 2),
                   "notes": stock_verdict(r)})
    themes = load_themes()
    in_themes = [t for t, cs in themes.items() if code in cs]
    ind_codes = df.index[df["industry"] == r["industry"]]
    ind_rank = next((x for x in industry_ranking(df) if x["industry"] == r["industry"]), None)
    return {
        "as_of": ctx["as_of"],
        "total_stocks_ranked": int(len(df)),
        "target": target,
        "industry": r["industry"],
        "industry_rank": f"{ind_rank['rank']}/{len(industry_ranking(df))}" if ind_rank else None,
        "industry_view": group_view(df, ind_codes, code, top_n),
        "themes": in_themes or ["（未收錄於細分族群.json，可自行加入）"],
        "theme_views": {t: group_view(df, themes[t], code, top_n) for t in in_themes},
    }
