#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_web.py — 產生查詢網站用的資料檔（web/data.json）

用法：
  python3 scripts/export_web.py            # 先增量更新資料，再輸出
  python3 scripts/export_web.py --cached   # 不連網，直接用現有快取輸出
"""

import os
import sys
import json
import math
from datetime import datetime, timezone, timedelta

import pandas as pd

import rs_core as core

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(os.path.dirname(HERE), "web")
TW = timezone(timedelta(hours=8))


def num(x, nd=1):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) or pd.isna(x):
        return None
    return round(float(x), nd)


def main():
    cached = "--cached" in sys.argv
    df, ctx = core.compute_all(update=not cached)
    adj = ctx["adj"]

    # 近 52 週還原收盤（每 3 個交易日取一點，做走勢小圖）
    win = adj.iloc[-252:].ffill()
    idx = list(range(len(win) - 1, -1, -3))[::-1]
    spark_dates = [win.index[i].strftime("%Y-%m-%d") for i in idx]

    stocks = {}
    for c, r in df.iterrows():
        s = win[c].iloc[idx] if c in win.columns else None
        stocks[c] = {
            "n": r["name"], "m": r["market"], "i": r["industry"],
            "rs": int(r["rs"]), "raw": num(r["raw"], 5),
            "r": [num(r["r3m"]), num(r["r6m"]), num(r["r9m"]), num(r["r12m"])],
            "tt": int(r["tt"]), "ttc": [int(b) for b in r["tt_checks"]],
            "oh": num(r["off_high"]), "c": num(r["close"], 2),
            "cap": None if pd.isna(r["cap_yi"]) else int(round(r["cap_yi"])),
            "v20": None if pd.isna(r["val20_m"]) else int(round(r["val20_m"])),
            "to": num(r["turnover_pct"], 2), "lt": r["last_trade"],
            "notes": core.stock_verdict(r),
            "sp": None if s is None else [num(v, 2) for v in s.values],
        }

    themes = core.load_themes()
    data = {
        "as_of": ctx["as_of"],
        "generated_at": datetime.now(TW).strftime("%Y-%m-%d %H:%M"),
        "total": int(len(df)),
        "thresholds": {"median_rs": core.TH_MEDIAN_RS, "pct80": core.TH_PCT80, "min_stocks": core.MIN_STOCKS},
        "spark_dates": spark_dates,
        "stocks": stocks,
        "industries": [{k: v for k, v in r.items() if k != "top3"} | {"top3": [t["code"] for t in r["top3"]]}
                       for r in core.industry_ranking(df)],
        "themes": [{k: v for k, v in r.items() if k != "members"} | {"codes": [m["code"] for m in r["members"]]}
                   for r in core.theme_ranking(df)],
        "theme_def": themes,
        "adj_report": {"events_used": ctx["adj_report"]["events_used"],
                       "inferred": len(ctx["adj_report"]["inferred"]),
                       "warnings": ctx["adj_report"]["warnings"][:50]},
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "data.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(json.dumps({"ok": True, "path": path, "as_of": data["as_of"], "stocks": len(stocks),
                      "bytes": os.path.getsize(path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
