#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台股強勢族群計算引擎（v3）

用法：
  python3 strong_industry_engine.py --stock 2330 --top 10     # 個股族群透視（官方產業＋細分族群）
  python3 strong_industry_engine.py --themes                  # 細分族群強度排行榜
  python3 strong_industry_engine.py --theme 被動元件           # 單一細分族群完整檢視
  python3 strong_industry_engine.py                           # 官方產業強度排行榜
  python3 strong_industry_engine.py --industry 半導體業 --top 15
  python3 strong_industry_engine.py --theme-map-md            # 重新產生 references/細分族群地圖.md
  python3 strong_industry_engine.py --map-md                  # 重新產生 references/產業地圖.md
  python3 strong_industry_engine.py --map                     # 官方產業分類 JSON
  python3 strong_industry_engine.py --tv                      # TradingView 匯入清單
  任一指令加 --cached                                           # 不連網更新，直接用現有快取

強勢族群判定門檻：中位數 RS >= 70 且 RS>=80 佔比 >= 30%，並以近 3 月中位數報酬判斷真假強勢
"""

import os
import sys
import json
import argparse
from datetime import datetime

import rs_core as core
import market_data as md

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE)
REF_DIR = os.path.join(ROOT_DIR, "references")


def log(msg):
    print(f"[族群] {msg}", file=sys.stderr, flush=True)


def industry_map(info):
    out = {}
    for code, r in info.sort_index().iterrows():
        out.setdefault(r["industry"], []).append({"code": code, "name": r["name"]})
    return dict(sorted(out.items(), key=lambda kv: -len(kv[1])))


def write_map_md(info, path):
    m = industry_map(info)
    total = sum(len(v) for v in m.values())
    lines = ["---", "tags: [台股, 產業地圖]", f"updated: {datetime.now():%Y-%m-%d}", "---", "",
             "# 台股產業地圖（FinMind 分類）", "",
             f"> 共 {len(m)} 個產業、{total} 檔股票（上市＋上櫃普通股，排除興櫃/ETF/ETN/DR）。",
             "> 同一檔股票有多重分類時，優先取最細的產業別（例如台積電取「半導體業」而非「電子工業」）。",
             "> 重新產生：`python3 scripts/strong_industry_engine.py --map-md`", ""]
    for ind, stocks in m.items():
        lines += [f"## {ind}（{len(stocks)} 檔）", "", "、".join(f"{s['code']} {s['name']}" for s in stocks), ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log(f"產業地圖已寫入：{path}")


def write_theme_map_md(df, path):
    themes = core.load_themes()
    lines = ["---", "tags: [台股, 細分族群]", f"updated: {datetime.now():%Y-%m-%d}", "---", "",
             "# 台股細分族群地圖（自訂題材分類）", "",
             f"> 共 {len(themes)} 個細分族群。分類定義在 `scripts/細分族群.json`，可直接編輯增刪，改完執行",
             "> `python3 scripts/strong_industry_engine.py --theme-map-md --cached` 重新產生本檔。",
             "> 一檔股票可屬於多個族群。括號內為個股目前 RS。", ""]
    for name, codes in themes.items():
        g = df[df.index.isin(codes)].sort_values("raw", ascending=False)
        missing = [c for c in codes if c not in df.index]
        med = f"中位數 RS {g['rs'].median():.0f}" if len(g) else "無資料"
        parts = [f"{c} {g.loc[c, 'name']}（{int(g.loc[c, 'rs'])}）" for c in g.index]
        parts += [f"{c}（無排名資料）" for c in missing]
        lines += [f"## {name}（{len(codes)} 檔｜{med}）", "", "、".join(parts), ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log(f"細分族群地圖已寫入：{path}")


def write_tradingview_txt(info, path):
    lines, unknown = [], []
    for theme, codes in core.load_themes().items():
        syms = []
        for c in codes:
            if c not in info.index:
                unknown.append(c)
                continue
            syms.append(f"{'TWSE' if info.loc[c, 'market'] == 'twse' else 'TPEX'}:{c}")
        if syms:
            lines += [f"###{theme}", ",".join(syms)]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log(f"TradingView 清單已寫入：{path}")
    if unknown:
        log(f"⚠️ 不在上市櫃清單（未寫入）：{unknown}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock")
    ap.add_argument("--themes", action="store_true")
    ap.add_argument("--theme")
    ap.add_argument("--theme-map-md", action="store_true")
    ap.add_argument("--tv", action="store_true")
    ap.add_argument("--industry")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--map", action="store_true")
    ap.add_argument("--map-md", action="store_true")
    ap.add_argument("--cached", action="store_true")
    a = ap.parse_args()

    if a.tv or a.map or a.map_md:
        info = md.fetch_stock_info()
        if a.tv:
            write_tradingview_txt(info, os.path.join(ROOT_DIR, "TradingView 匯入清單.txt"))
        elif a.map_md:
            os.makedirs(REF_DIR, exist_ok=True)
            write_map_md(info, os.path.join(REF_DIR, "產業地圖.md"))
        else:
            print(json.dumps(industry_map(info), ensure_ascii=False, indent=2))
        return

    df, ctx = core.compute_all(update=not a.cached)
    as_of = ctx["as_of"]

    if a.stock:
        res = core.stock_report(df, ctx, a.stock, a.top)
    elif a.theme_map_md:
        os.makedirs(REF_DIR, exist_ok=True)
        write_theme_map_md(df, os.path.join(REF_DIR, "細分族群地圖.md"))
        return
    elif a.themes:
        rows = core.theme_ranking(df)
        res = {"as_of": as_of, "classification": "細分族群（自訂題材，見 細分族群.json）",
               "total_themes": len(rows),
               "threshold": f"強勢族群 = 中位數RS>={core.TH_MEDIAN_RS} 且 RS>=80佔比>={core.TH_PCT80}%",
               "strong_themes": [r["theme"] for r in rows if r["is_strong"]], "themes": rows}
    elif a.theme:
        themes = core.load_themes()
        if a.theme not in themes:
            res = {"error": f"找不到細分族群「{a.theme}」",
                   "did_you_mean": [t for t in themes if a.theme in t or t in a.theme],
                   "available": list(themes)}
        else:
            v = core.group_view(df, themes[a.theme], None, top_n=999)
            res = {"as_of": as_of, "theme": a.theme, **v["stats"],
                   "missing": [c for c in themes[a.theme] if c not in df.index],
                   "members": v["members_top"], "pattern_candidates": v["pattern_candidates"]}
    elif a.industry:
        codes = df.index[df["industry"] == a.industry]
        if len(codes) == 0:
            res = {"error": f"找不到產業「{a.industry}」（可執行 --map 查詢產業清單）"}
        else:
            v = core.group_view(df, codes, None, top_n=a.top)
            res = {"as_of": as_of, "industry": a.industry, **v["stats"], "stocks": v["members_top"]}
    else:
        rows = core.industry_ranking(df)
        res = {"as_of": as_of, "total_stocks": int(len(df)), "total_industries": len(rows),
               "threshold": f"強勢族群 = 中位數RS>={core.TH_MEDIAN_RS} 且 RS>=80佔比>={core.TH_PCT80}%（樣本<{core.MIN_STOCKS}檔不列入）",
               "strong_industries": [r["industry"] for r in rows if r["is_strong"]], "industries": rows}
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
