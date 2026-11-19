#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RS Engine — 台股相對強度計算工具（v3）

計算方式（Mark Minervini 相對強度概念）：
  RS 原始分數 = 近3月報酬×40% + 近6月×30% + 近9月×20% + 近12月×10%（還原股價）
  RS 排名     = 原始分數在全市場上市櫃普通股中的百分位（1–99）

資料：證交所／櫃買官方每日行情 + 官方除權息/減資資料自行還原（免付費，見 market_data.py）

用法：
  python3 rs_engine.py 2330 3661 2454       # 指定個股
  python3 rs_engine.py --top 20             # 全市場前 20 名
  python3 rs_engine.py --all                # 全市場完整清單
  任一指令加 --cached                        # 不連網更新，直接用現有快取
"""

import sys
import json

import pandas as pd

import rs_core as core


def fmt(df, codes):
    out = []
    for c in codes:
        if c not in df.index:
            out.append({"stock_id": c, "rs_rank": None,
                        "rs_level": "無法計算（興櫃、ETF、上市未滿 3 個月、停牌中或代號不存在）"})
            continue
        r = df.loc[c]
        out.append({
            "stock_id": c, "name": r["name"], "industry": r["industry"],
            "rs_rank": int(r["rs"]), "rs_level": core.rs_level(int(r["rs"])),
            "returns": {k: (None if pd.isna(r[f"r{k}"]) else round(float(r[f"r{k}"]), 2))
                        for k in ("3m", "6m", "9m", "12m")},
        })
    out.sort(key=lambda x: x.get("rs_rank") or 0, reverse=True)
    return out


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cached = "--cached" in args
    df, ctx = core.compute_all(update=not cached)
    res = {"date": ctx["as_of"], "total_stocks_ranked": int(len(df))}
    if "--top" in args:
        i = args.index("--top")
        n = int(args[i + 1]) if i + 1 < len(args) and args[i + 1].isdigit() else 20
        res.update(mode="top", top_n=n, stocks=fmt(df, df.sort_values("raw", ascending=False).index[:n]))
    elif "--all" in args:
        res.update(mode="all", stocks=fmt(df, df.sort_values("raw", ascending=False).index))
    else:
        codes = [a for a in args if not a.startswith("--")]
        res.update(mode="specific", stocks=fmt(df, codes))
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
