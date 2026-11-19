"""抗跌股：大盤回檔時，找同一段期間跌得少、甚至逆勢上漲的股票。
由 tw_momentum_screener.py 每天呼叫，輸出 output/site/pullback.json。

規則（2026/11 與使用者討論定案）：
- 大盤回檔＝加權指數收盤從波段高點拉回 5% 以上（反彈 5% 才算這段結束）；還在跌的也算，比到最新一天
- 上市股比加權指數、上櫃股比櫃買指數，都用同一段日期
- 抗跌＝這段期間個股跌幅不到指數的一半（含逆勢上漲）
- 範圍：股價 ≥ 10 元、20 日均成交額 ≥ 3,000 萬
- 另外標出最近 3 次回檔都抗跌的股票
"""
import json, time, urllib.request, datetime as dt
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0 (Macintosh) tw-screener"}
PULLBACK = 0.05          # 回檔門檻
REBOUND = 0.05           # 從低點反彈多少才算這段回檔結束
N_LEGS = 3               # 最近幾次回檔


def _get(url):
    for k in range(3):
        try:
            try:
                import requests                      # Mac 上的篩選程式環境有 requests（附憑證），優先使用
                return requests.get(url, headers=UA, timeout=30).json()
            except ImportError:
                with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                    return json.loads(r.read().decode("utf-8"))
        except Exception:
            if k == 2:
                raise
            time.sleep(3 + 3 * k)


def _num(x):
    return float(str(x).replace(",", ""))


def fetch_index_hist(cache_dir, asof, months=40):
    """加權（證交所）與櫃買指數（櫃買中心）每日收盤，逐月抓、存快取；最近 2 個月每次重抓。"""
    path = Path(cache_dir) / "index_hist.json"
    H = {"twse": {}, "tpex": {}, "done": []}
    if path.exists():
        try:
            H = json.load(open(path, encoding="utf-8"))
        except Exception:
            pass
    asof = dt.date(asof.year, asof.month, asof.day)
    ms = []
    y, m = asof.year, asof.month
    for _ in range(months):
        ms.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    recent = set(ms[:2])
    for (y, m) in ms:
        key = f"{y}{m:02d}"
        if key in H["done"] and (y, m) not in recent:
            continue
        d = _get(f"https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?date={y}{m:02d}01&response=json")
        for r in d.get("data", []) or []:
            ry, rm, rd = (int(x) for x in r[0].split("/"))
            H["twse"][f"{ry + 1911}-{rm:02d}-{rd:02d}"] = _num(r[4])
        time.sleep(1.2)
        d = _get(f"https://www.tpex.org.tw/www/zh-tw/indexInfo/inx?date={y}/{m:02d}/01&response=json")
        for t in d.get("tables", []) or []:
            for r in t.get("data", []) or []:
                H["tpex"][r[0].replace("/", "-")] = _num(r[4])
        time.sleep(1.2)
        if key not in H["done"]:
            H["done"].append(key)
    json.dump(H, open(path, "w", encoding="utf-8"))
    return H


def find_legs(dates, closes, asof_i=None):
    """加權收盤的 5% 波段：回傳 [(peak_i, end_i, trough_i, ongoing)]，由舊到新。"""
    n = len(closes) if asof_i is None else asof_i + 1
    legs = []
    state, p, q = "up", 0, 0
    for i in range(1, n):
        c = closes[i]
        if state == "up":
            if c > closes[p]:
                p = i
            elif c <= closes[p] * (1 - PULLBACK):
                state, q = "down", i
        else:
            if c < closes[q]:
                q = i
            elif c >= closes[q] * (1 + REBOUND):
                legs.append((p, q, q, False))
                state, p = "up", i
    if state == "down":
        legs.append((p, n - 1, q, True))      # 還在跌：比到最新一天
    return legs


def analyze_pullbacks(data, meta, cfg, asof, hist, out_path):
    import numpy as np
    import pandas as pd
    tw = pd.Series(hist["twse"]).sort_index(); tw.index = pd.to_datetime(tw.index)
    tp = pd.Series(hist["tpex"]).sort_index(); tp.index = pd.to_datetime(tp.index)
    tw = tw[tw.index <= asof]; tp = tp[tp.index <= asof]
    if len(tw) < 60:
        raise RuntimeError("加權指數歷史資料不足")
    legs = find_legs(tw.index, tw.values)
    legs = legs[-N_LEGS:]
    if not legs:
        raise RuntimeError("近幾年沒有 5% 以上的回檔")

    def at(s, d):                              # d 當天或之前最近一筆
        s2 = s[s.index <= d]
        return float(s2.iloc[-1]) if len(s2) else np.nan

    leg_info = []
    for (p, e, q, ongoing) in legs:
        d0, d1, dq = tw.index[p], tw.index[e], tw.index[q]
        leg_info.append({
            "peak": f"{d0:%Y-%m-%d}", "end": f"{d1:%Y-%m-%d}", "trough": f"{dq:%Y-%m-%d}", "ongoing": ongoing,
            "twse": round(at(tw, d1) / at(tw, d0) - 1, 4), "tpex": round(at(tp, d1) / at(tp, d0) - 1, 4) if len(tp) else None,
            "twse_rebound": round(float(tw.iloc[-1]) / at(tw, dq) - 1, 4),
            "tpex_rebound": round(float(tp.iloc[-1]) / at(tp, dq) - 1, 4) if len(tp) else None,
            "days": int(e - p),
        })

    rows = []
    for sym, d in data.items():
        if sym.startswith("^") or sym not in meta.index:
            continue
        d = d[d.index <= asof]
        if len(d) < 30 or d.index[-1] < asof - pd.Timedelta(days=10):
            continue
        close = d["Close"]
        last = float(close.iloc[-1])
        turnover = float((close * d["Volume"]).tail(cfg["turnover_days"]).mean())
        if last < cfg["min_price"] or turnover < cfg["min_avg_turnover"]:
            continue
        is_tpex = sym.endswith(".TWO")
        r = []
        for L in leg_info:
            d0, d1 = pd.Timestamp(L["peak"]), pd.Timestamp(L["end"])
            if close.index[0] > d0:
                r.append(None); continue
            r.append(round(at(close, d1) / at(close, d0) - 1, 4))
        if r[-1] is None:
            continue
        hi52 = float(d["High"].tail(252).max())
        dq = pd.Timestamp(leg_info[-1]["trough"])
        reb = last / at(close, dq) - 1 if close.index[0] <= dq else None
        m = meta.loc[sym]
        rows.append({"c": m["code"], "n": m["name"], "m": "tpex" if is_tpex else "twse", "i": m.get("industry", ""),
                     "r": r, "oh": round(last / hi52 - 1, 4), "rb": None if reb is None else round(reb, 4),
                     "nh": bool(float(d["High"].iloc[-5:].max()) >= hi52 * 0.999)})
    out = {"as_of": f"{asof:%Y-%m-%d}", "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
           "rule": {"pullback": PULLBACK, "resist": 0.5}, "legs": leg_info, "stocks": rows}
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    return out


if __name__ == "__main__":       # 單獨測試抓指數：python3 pullback.py
    import sys
    h = fetch_index_hist(sys.argv[1] if len(sys.argv) > 1 else "cache", dt.date.today())
    print("加權", len(h["twse"]), "天，櫃買", len(h["tpex"]), "天")
