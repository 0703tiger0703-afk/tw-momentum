#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台股動能篩選器 v1
=================
Minervini《超級績效》趨勢模板（硬性門檻）+ VCP / COH / HTF 型態評分（0–100）。

用法
----
  python tw_momentum_screener.py
      每日例行：全市場（上市＋上櫃）掃描，輸出 Excel 與 K 線圖

  python tw_momentum_screener.py --asof 2024-05-10
      回到過去某一天掃描（用來驗證歷史範例）

  python tw_momentum_screener.py --check 2330 3661 --asof 2024-05-10
      單檔診斷：逐項印出趨勢模板與三種型態的判斷細節，用來校準參數
      （若同一個 --asof 已跑過全市場，診斷時也會顯示 RS 百分位）

所有門檻都在下方 CONFIG，可自行調整。
"""

import argparse
import io
import re
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)

# =============================================================================
# 參數設定
# =============================================================================
CONFIG = {
    # ---- 資料 ----
    "years_of_history": 3.0,        # 下載幾年日線（200MA、SMA240、52 週，以及「右上角」要檢查型態之前的舊高點）
    "batch_size": 100,              # 每批下載檔數
    "output_dir": "output",
    "cache_dir": "cache",

    # ---- 流動性門檻 ----
    "min_price": 10.0,              # 最低股價
    "min_avg_turnover": 30_000_000, # 20 日平均成交金額（新台幣）
    "turnover_days": 20,

    # ---- 趨勢模板（Minervini 八條）----
    "tt_min_above_52w_low": 0.30,   # 6. 高於 52 週低點至少 30%
    "tt_max_below_52w_high": 0.30,  # 7. 距 52 週高點 30% 以內（書上為 25%，依由田範例放寬）
    "tt_ma200_rising_days": 22,     # 3. 200MA 高於約 1 個月前
    "tt_min_rs": 89,                # 8. RS 必須「大於」89（新股也適用）
    "rs_gate_here": False,          # False＝本程式不擋 RS；網站用「族群透視」官方資料算的 RS 再擋（> tt_min_rs）

    # ---- 新股（上市未滿一年，另走一套條件）----
    "ipo_calendar_days": 365,       # 上市未滿幾天算新股
    "ipo_min_bars": 30,             # 至少要有幾根 K 棒才評估
    "ipo_max_below_high": 0.25,     # 距上市後最高點 25% 以內
    "ipo_min_above_low": 0.20,      # 高於上市後最低點至少 20%

    # ---- VCP ----
    "vcp_lookback": 250,            # 往回找基底起點（最高點）的天數
    "vcp_min_base_days": 15,        # 基底至少 3 週
    "vcp_start_tolerance": 0.03,    # VCP 要「橫盤」：型態內的高點最多只能超過起點高點 3%（元大金、研華這種一路創高的是上升趨勢，不算）
    "vcp_zigzag": 0.05,             # 擺動判定門檻（5%，依真實資料校準，3% 會把雜訊切成多段）
    "vcp_merge_retrace": 0.60,        # 反彈不到前一段跌幅 60% 又破底 → 合併成同一段（2026/11：世禾、虹冠電兩段下跌其實是一次）
    "vcp_max_undercut": 0.04,        # VCP 低點要墊高：後面的低點最多比前面低 4%（2026/11 依華容放寬 1%）
    "vcp_min_contractions": 3,           # 至少收縮 3 次（2026/11：收縮 1–2 次的你多半判成杯柄或不是 VCP）
    "vcp_max_contractions": 6,
    "vcp_min_first_depth": 0.10,    # 第一段（最深）收縮至少 10%
    "vcp_max_first_depth": 0.45,    # 第一段收縮 ≤ 45% 給滿分（依範例：騰輝 45%、南亞 42%）
    "vcp_fail_first_depth": 0.55,   # 第一段收縮 ≥ 55% 直接淘汰
    "vcp_max_above_pivot": 0.10,    # 收盤高於 pivot 超過 10% 視為型態已結束
    "vcp_good_last_depth": 0.08,    # 最後一段 ≤ 8% 給滿分
    "vcp_bad_last_depth": 0.15,     # 最後一段 ≥ 15% 給 0 分

    # ---- COH 杯柄 ----
    "coh_min_cup_days": 15,          # 杯身至少 3 週（太短的杯子靠「柄要成形」擋掉）
    "coh_max_cup_days": 220,         # 杯身最長約 44 週（2026/11-20：信立 204 天的大底你判 COH；中再保 243 天仍排除）
    "coh_min_depth": 0.18,          # 杯深至少 18%（元大金 15% 只是上升趨勢中的回檔；奇鋐 33%、玉晶光 24%）
    "coh_ideal_max_depth": 0.33,
    "coh_max_depth": 0.50,
    "coh_max_handle_days": 25,
    "coh_ideal_handle_depth": 0.15,
    "coh_min_handle_depth": 0.04,
    "coh_min_handle_days": 1,             # 柄最短 1 天（2026/11-20 試過 3 天：會誤殺世禾、玉晶光、倫飛）
    "coh_quick_handle_depth": 0.08,
    "htf_short_flag_days": 10,      # 旗形 10 天內，回檔可放寬到 25%（益安、哲固）
    "htf_short_flag_depth": 0.25,
    "htf_max_prior_run": 1.00,        # 旗桿起點前 60 天已經漲超過 100%＝延續漲勢，不是新的旗桿（2026/11-20：興能高 122%、金居 249%；你判 HTF 的最多 66%）
    "coh_max_handle_depth": 0.20,
    "coh_min_handle_pos": 0.40,     # 柄的低點至少在杯子 40% 高度以上（2026/11 依倫飛 46%）
    "vcp_cup_first_days": 20,       # VCP 第一段收縮超過 20 個交易日（約 4 週）且杯柄也成立 → 歸杯柄（光聯）
    "dry_min_days": 3,              # 下跌量縮：拉回不到 3 天先不判斷
    "dry_max_ratio": 0.90,          # 下跌量縮：拉回期間均量 < 型態均量 × 0.9
    "coh_lip_ratio_min": 0.85,      # 右杯緣 / 左杯緣 下限
    "coh_lip_ratio_max": 1.12,      # 右杯緣可高於左杯緣 12%（依範例：奇鋐 3300 vs 3010）

    # ---- HTF 高窄旗形 ----
    "htf_min_gain": 0.40,        # 旗桿約 1 個月內漲 40% 以上（2026/11 依凱基金）
    "htf_min_above_prior": 0.25,        # 旗桿頂要比起漲前一年的高點再高 25% 以上，排除跌深反彈
    "htf_min_pole_days": 5,           # 旗桿最短 1 週
    "htf_max_pole_days": 22,          # 旗桿約 1 個月內完成（2026/11：台燿、宜鼎 2–3 個月的慢漲不算）
    "htf_min_flag_days": 5,
    "htf_max_flag_days": 25,          # 旗形最長約 5 週
    "htf_ideal_flag_depth": 0.20,
    "htf_max_flag_depth": 0.20,          # 旗形回檔上限（2026/11：宜鼎 23%、玉晶光 24% 太深，後者是杯子）

    # ---- 你的檢查清單 ----
    "req_vcp_last_max": 0.10,       # 必要：VCP 最後一次收縮 < 10%（以日內最高/最低計算）
    "req_dryup_days": 20,          # （舊算法用；2026/11 起下跌量縮改看型態最後一段）
    "bonus_breakout_vol": 1.5,      # 加分：大量突破 = 突破當天量 ≥ 50 日均量 × 1.5
    "bonus_wide_base_days": 35,     # 加分：寬底 = VCP 基底或 COH 杯身整理 ≥ 7 週（35 個交易日）
    "bonus_risk_pct": 0.07,         # 加分：危險點 = Pivot 下方 7% 以內有 EMA21 支撐
    "bonus_shallow": 0.25,          # 加分：回檔淺 = 型態最大回檔 ≤ 25%
    "bonus_ma_rising_days": 5,      # 加分：均線都往上 = EMA21、SMA50、SMA240 都高於 5 天前
    "bonus_mkt_lookback": 63,       # 加分：大盤大跌、型態小跌 = 近 3 個月
    "bonus_mkt_min_drop": 0.05,     #   大盤回檔至少 5% 才算「大盤大跌」
    "bonus_mkt_ratio": 0.5,         #   同期個股跌幅 < 大盤跌幅的一半

    # ---- 輸出 ----
    "min_score_to_list": 50,        # 型態分數達此才列入該型態分頁
    "chart_min_score": 50,          # 型態分數達此才畫圖（= 列入門檻，所以每檔列出的股票都有圖）
    "charts_per_pattern": 999,      # 每種型態最多畫幾張
    "chart_bars": 220,              # 圖上顯示幾根 K 棒
}

ISIN_SOURCES = [
    ("上市", "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2", ".TW"),
    ("上櫃", "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4", ".TWO"),
]


# =============================================================================
# 股票清單
# =============================================================================
def _http_get(url):
    import requests
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        return requests.get(url, headers=headers, timeout=30)
    except requests.exceptions.SSLError:
        # 證交所 ISIN 頁面的憑證鏈偶爾不完整，公開資料改用不驗證方式取得
        import urllib3
        urllib3.disable_warnings()
        return requests.get(url, headers=headers, timeout=30, verify=False)


def load_ticker_list(cfg):
    """從證交所 ISIN 頁面取得上市、上櫃普通股清單（含上市日、產業）。失敗時用快取。"""
    cache = Path(cfg["cache_dir"]) / "tickers.csv"
    cache.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    try:
        for market, url, suffix in ISIN_SOURCES:
            r = _http_get(url)
            html = r.content.decode("cp950", errors="ignore")
            raw = pd.read_html(io.StringIO(html), header=0)[0]
            # 依欄位位置讀取：0 代號及名稱、2 上市日、4 產業別、5 CFICode
            t = pd.DataFrame({"codename": raw.iloc[:, 0].astype(str), "上市日": raw.iloc[:, 2],
                              "產業別": raw.iloc[:, 4], "CFICode": raw.iloc[:, 5].astype(str).str.strip()})
            t = t[t["CFICode"] == "ESVUFR"].copy()          # 普通股
            parts = t["codename"].str.split(r"[\s\u3000]+", n=1, expand=True)
            t["code"] = parts[0].str.strip()
            t["name"] = parts[1].str.strip()
            t = t[t["code"].str.fullmatch(r"\d{4}")]
            t["market"] = market
            t["symbol"] = t["code"] + suffix
            t["list_date"] = pd.to_datetime(t["上市日"], errors="coerce")
            t["industry"] = t["產業別"]
            frames.append(t[["code", "name", "market", "symbol", "list_date", "industry"]])
        meta = pd.concat(frames, ignore_index=True)
        if len(meta) < 500:
            raise RuntimeError(f"清單筆數異常（{len(meta)}）")
        meta.to_csv(cache, index=False, encoding="utf-8-sig")
        print(f"[清單] 上市＋上櫃普通股 {len(meta)} 檔")
    except Exception as e:
        if cache.exists():
            print(f"[清單] 取得失敗（{e}），改用快取 {cache}")
            meta = pd.read_csv(cache, dtype={"code": str}, parse_dates=["list_date"])
        else:
            raise SystemExit(f"[清單] 無法取得股票清單：{e}")
    return meta.set_index("symbol")


# =============================================================================
# 價格下載（yfinance，還原權息）
# =============================================================================
def download_prices(symbols, start, end, cfg):
    import yfinance as yf
    out = {}
    symbols = list(symbols)
    bs = cfg["batch_size"]
    for i in range(0, len(symbols), bs):
        chunk = symbols[i:i + bs]
        df = None
        for attempt in range(3):
            try:
                df = yf.download(chunk, start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"),
                                 group_by="ticker", auto_adjust=True, threads=True, progress=False)
                break
            except Exception as e:
                print(f"  下載失敗，重試中（{e}）")
                time.sleep(5)
        if df is None or df.empty:
            continue
        multi = isinstance(df.columns, pd.MultiIndex)
        lvl0 = set(df.columns.get_level_values(0)) if multi else set()
        for s in chunk:
            if multi:
                if s not in lvl0:
                    continue
                d = df[s]
            else:
                d = df
            d = d[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"]).astype(float)
            if len(d) == 0:
                continue
            idx = pd.to_datetime(d.index)
            if idx.tz is not None:
                idx = idx.tz_localize(None)
            d.index = idx.normalize()
            out[s] = d
        print(f"  已下載 {min(i + bs, len(symbols))}/{len(symbols)}")
        time.sleep(1)
    return out


def load_or_download(symbols, start, end, cfg, use_cache=True):
    cache = Path(cfg["cache_dir"]) / f"prices_{end:%Y%m%d}.pkl"
    if use_cache and cache.exists():
        print(f"[價格] 使用快取 {cache}")
        return pd.read_pickle(cache)
    print(f"[價格] 下載 {len(symbols)} 檔日線 {start:%Y-%m-%d} ~ {end:%Y-%m-%d}")
    data = download_prices(symbols, start, end, cfg)
    pd.to_pickle(data, cache)
    return data


def split_indexes(data):
    """把大盤指數從價格資料中分出來（原地移除），回傳 {代碼: 收盤序列}"""
    out = {}
    for k in ["^TWII", "^TWOII"]:
        if k in data:
            out[k] = data.pop(k)["Close"]
    if "^TWOII" not in out and "^TWII" in out:
        out["^TWOII"] = out["^TWII"]                 # 櫃買指數抓不到時以加權指數代替
    return out


# =============================================================================
# 共用工具
# =============================================================================
def clip01(x):
    return float(min(1.0, max(0.0, x)))


def lin(x, zero_at, full_at):
    """線性給分：x 在 zero_at 為 0、在 full_at 為 1（兩端可任意方向）。"""
    if full_at == zero_at:
        return 1.0 if x == full_at else 0.0
    return clip01((x - zero_at) / (full_at - zero_at))


def zigzag_from_high(h, l, start, th):
    """從基底高點開始做擺動點偵測，回傳 [(idx, price, 'H'/'L'), ...]，最後一點為進行中的極值。"""
    piv = [(start, h[start], "H")]
    mode, ext_i, ext_p = "down", start, h[start]   # 從起點高點開始量，避免起點當根 K 棒被算成一段收縮
    for i in range(start + 1, len(h)):
        # 同一根 K 棒創新極值時不判定反轉（避免長 K 棒在當根來回翻轉）
        if mode == "down":
            if l[i] < ext_p:
                ext_p, ext_i = l[i], i
            elif h[i] >= ext_p * (1 + th):
                piv.append((ext_i, ext_p, "L"))
                mode, ext_i, ext_p = "up", i, h[i]
        else:
            if h[i] > ext_p:
                ext_p, ext_i = h[i], i
            elif l[i] <= ext_p * (1 - th):
                piv.append((ext_i, ext_p, "H"))
                mode, ext_i, ext_p = "down", i, l[i]
    if ext_i != piv[-1][0]:
        piv.append((ext_i, ext_p, "L" if mode == "down" else "H"))
    return piv


def merge_stairsteps(piv, retrace):
    """H_a,L_a,H_b,L_b：若反彈不到前段跌幅 retrace 且又破底，合併成 H_a→L_b。"""
    changed = True
    while changed:
        changed = False
        for k in range(len(piv) - 3):
            a, b, c, d = piv[k:k + 4]
            if a[2] == "H" and b[2] == "L" and c[2] == "H" and d[2] == "L":
                drop = a[1] - b[1]
                if drop > 0 and (c[1] - b[1]) / drop < retrace and d[1] <= b[1]:
                    piv = piv[:k + 1] + piv[k + 3:]
                    changed = True
                    break
    return piv


def fail(reason):
    return {"ok": False, "score": 0.0, "reason": reason}


# =============================================================================
# 趨勢模板
# =============================================================================
def trend_template(d, cfg):
    c = d["Close"]
    n = len(c)
    k = cfg["tt_ma200_rising_days"]
    if n < 200 + k:
        return None
    ma50, ma150, ma200 = (c.rolling(w).mean() for w in (50, 150, 200))
    last = c.iloc[-1]
    m50, m150, m200 = ma50.iloc[-1], ma150.iloc[-1], ma200.iloc[-1]
    m200_prev = ma200.iloc[-1 - k]
    diff = ma200.diff().values[::-1]
    streak = 0
    for x in diff:
        if x > 0:
            streak += 1
        else:
            break
    hi52 = d["High"].tail(252).max()
    lo52 = d["Low"].tail(252).min()
    above_low = last / lo52 - 1
    below_high = 1 - last / hi52
    crit = {
        "1 價>150MA & 200MA": bool(last > m150 and last > m200),
        "2 150MA>200MA": bool(m150 > m200),
        "3 200MA上升≥1月": bool(m200 > m200_prev),
        "4 50MA>150MA & 200MA": bool(m50 > m150 and m50 > m200),
        "5 價>50MA": bool(last > m50),
        f"6 高於52週低≥{cfg['tt_min_above_52w_low']:.0%}": bool(above_low >= cfg["tt_min_above_52w_low"]),
        f"7 距52週高≤{cfg['tt_max_below_52w_high']:.0%}": bool(below_high <= cfg["tt_max_below_52w_high"]),
    }
    return {
        "crit": crit,
        "pass_1_7": all(crit.values()),
        "ma50": m50, "ma150": m150, "ma200": m200,
        "ma200_rising_streak": streak,
        "above_52w_low": above_low, "below_52w_high": below_high,
    }


def ipo_template(d, cfg):
    c = d["Close"]
    n = len(c)
    w = 50 if n >= 60 else 20
    ma = c.rolling(w).mean()
    last = c.iloc[-1]
    hi, lo = d["High"].max(), d["Low"].min()
    below_high = 1 - last / hi
    above_low = last / lo - 1
    crit = {
        f"價>{w}MA": bool(last > ma.iloc[-1]),
        f"{w}MA上升": bool(ma.iloc[-1] > ma.iloc[-6]),
        "距上市後高點≤25%": bool(below_high <= cfg["ipo_max_below_high"]),
        "高於上市後低點≥20%": bool(above_low >= cfg["ipo_min_above_low"]),
    }
    return {"crit": crit, "pass": all(crit.values()), "below_high": below_high, "above_low": above_low}


# =============================================================================
# 型態偵測
# =============================================================================
def _vcp_from(d, bs, cfg):
    """以 bs 為基底起點評估 VCP。"""
    h, l, c, v = (d[x].values for x in ("High", "Low", "Close", "Volume"))
    n = len(c)
    base_len = n - 1 - bs
    th = cfg["vcp_zigzag"]
    piv = merge_stairsteps(zigzag_from_high(h, l, bs, th), cfg["vcp_merge_retrace"])
    cons = []
    for a, b in zip(piv[:-1], piv[1:]):
        if a[2] == "H" and b[2] == "L":
            cons.append({"hi_i": a[0], "hi": a[1], "lo_i": b[0], "lo": b[1], "depth": 1 - b[1] / a[1]})
    if cons:
        # 從最深的一段開始算（基底高點附近的小晃動不算第一段收縮）
        k = int(np.argmax([x["depth"] for x in cons]))
        cons = cons[k:]
    depths = [x["depth"] for x in cons]
    nC = len(cons)
    if nC and depths[0] < cfg["vcp_min_first_depth"]:
        return fail(f"最大回檔僅 {depths[0]:.0%}，尚未形成真正的基底")
    if nC < cfg["vcp_min_contractions"]:
        return fail(f"收縮次數不足（{nC}）")
    if nC > cfg["vcp_max_contractions"]:
        return fail(f"收縮次數過多（{nC}），結構鬆散")
    if depths[0] >= cfg["vcp_fail_first_depth"]:
        return fail(f"第一段收縮過深（{depths[0]:.0%}）")
    if depths[-1] >= depths[0]:
        return fail("最後一段沒有比第一段小")
    lows = [x["lo"] for x in cons]
    for i in range(1, nC):
        if lows[i] < min(lows[:i]) * (1 - cfg["vcp_max_undercut"]):
            return fail(f"第 {i + 1} 段低點跌破前面的低點（VCP 要低點墊高）")

    dec = np.mean([depths[i + 1] < depths[i] * 1.15 for i in range(nC - 1)])   # 相近（15% 內）也算
    s_dec = 20 * dec
    s_cnt = 10 * (1.0 if nC >= 3 else 0.4)           # 3T 以上滿分，2T 部分給分
    s_first = 10 * (1.0 if depths[0] <= cfg["vcp_max_first_depth"] else
                    lin(depths[0], cfg["vcp_fail_first_depth"], cfg["vcp_max_first_depth"]))
    s_last = 15 * lin(depths[-1], cfg["vcp_bad_last_depth"], cfg["vcp_good_last_depth"])
    pos = cons[-1]["hi"] / h[bs]                      # 最後一段的高點靠近基底高點（右側在基底上緣）
    s_pos = 10 * lin(pos, 0.80, 0.93)
    # 量縮：近 10 日內「最安靜的 3 天」均量 / 50 日均量
    v3 = pd.Series(v[-10:]).rolling(3).mean().min()
    vol_ratio = v3 / max(v[-50:].mean(), 1)
    s_vol = 20 * lin(vol_ratio, 0.9, 0.5)
    # 緊密：近 10 日內「收盤最緊的 3 天」的收盤價區間
    cs = pd.Series(c[-10:])
    tight = float(((cs.rolling(3).max() - cs.rolling(3).min()) / cs).min())
    s_tight = 15 * lin(tight, 0.08, 0.03)
    if c[-1] > cons[-1]["hi"] * (1 + cfg["vcp_max_above_pivot"]):
        return fail(f"已突破 pivot 超過 {cfg['vcp_max_above_pivot']:.0%}，型態已結束")
    score = s_dec + s_cnt + s_first + s_last + s_pos + s_vol + s_tight

    idx = d.index
    return {
        "ok": True, "score": round(score, 1),
        "pivot": cons[-1]["hi"], "stop": cons[-1]["lo"], "pivot_i": cons[-1]["hi_i"],
        "base_start": bs, "base_days": base_len, "max_depth": depths[0], "last_depth": depths[-1],
        "first_days": int(cons[0]["lo_i"] - cons[0]["hi_i"]), "n_con": len(cons),
        "rehigh": bool(max(x["hi"] for x in cons[1:]) >= h[bs] * 0.99),          # 右側漲回基底起點高度
        "first_ratio": float(depths[0] / max(max(depths[1:]), 1e-9)),          # 第一段深度 / 後面最深的一段
        "detail": f"{nC}T 收縮 " + "→".join(f"{x:.0%}" for x in depths)
                  + f"｜基底起點 {idx[bs]:%Y-%m-%d}（{base_len} 天）｜末段高點/基底高點 {pos:.0%}｜最低量比 {vol_ratio:.2f}｜最緊3日收盤 {tight:.1%}",
        "parts": {"遞減": s_dec, "次數": s_cnt, "首段": s_first, "末段": s_last, "位置": s_pos, "量縮": s_vol, "緊密": s_tight},
        "lines": [[(idx[x["hi_i"]], x["hi"]), (idx[x["lo_i"]], x["lo"])] for x in cons],
        "marks": [idx[bs]],
    }


def detect_vcp(d, cfg):
    """候選基底起點：回看期間內的局部高點，且不低於其後最高點的 (1 - 容差)。逐一評估取最高分。"""
    h = d["High"].values
    n = len(h)
    if n < 60:
        return fail("資料不足")
    lb = min(cfg["vcp_lookback"], n - 2)
    s0 = n - 1 - lb
    last_ok = n - 1 - cfg["vcp_min_base_days"]
    if last_ok < s0:
        return fail("資料不足")
    tol = cfg["vcp_start_tolerance"]
    hy = h[:n - 1]                                   # 不含今日
    suffix_max = np.maximum.accumulate(hy[::-1])[::-1]
    cands = []
    for i in range(last_ok, s0 - 1, -1):
        after = suffix_max[i + 1] if i + 1 < len(hy) else 0
        if h[i] >= after * (1 - tol) and h[i] == h[max(0, i - 5):i + 6].max():
            cands.append(i)
    if not cands:
        if hy[last_ok:].max() >= hy[s0:].max():
            return fail("近期正在創新高，尚無基底")
        return fail("找不到基底起點")
    oks, first_fail = [], None
    for bs in cands:
        r = _vcp_from(d, bs, cfg)
        if r["ok"]:
            oks.append((bs, r))
        elif first_fail is None:
            first_fail = r
    if not oks:
        return first_fail
    top = max(r["score"] for _, r in oks)
    # 分數相近（5 分內）時取最早、結構最完整的基底
    return min([(bs, r) for bs, r in oks if r["score"] >= top - 5], key=lambda x: x[0])[1]


def detect_coh(d, cfg):
    h, l, c, v = (d[x].values for x in ("High", "Low", "Close", "Volume"))
    n = len(c)
    t = n - 1
    if n < cfg["coh_min_cup_days"] + cfg["coh_min_handle_days"] + 15:
        return fail("資料不足")
    # 右杯緣候選：最近 30 天內、之後（到昨天）沒有更高點的高點；從最近的往回找，第一個能成杯又有柄的就用
    w0 = max(0, t - cfg["coh_max_handle_days"] - 5)
    last_reason = "找不到符合條件的杯身"
    for R in range(t - 1, w0 - 1, -1):
        if h[R] < h[R:t].max() or h[R] != h[max(0, R - 5):t].max():
            continue
        hd = t - R
        if hd < cfg["coh_min_handle_days"]:
            continue
        if hd > cfg["coh_max_handle_days"]:
            last_reason = f"柄過長（{hd} 天）"
            continue
        hR = h[R]
        handle_low = l[R + 1:].min()
        h_depth = 1 - handle_low / hR
        if h_depth > cfg["coh_max_handle_depth"]:
            last_reason = f"柄過深（{h_depth:.0%}）"
            continue
        if h_depth < cfg["coh_min_handle_depth"]:
            last_reason = f"柄還沒拉回（{h_depth:.0%}）"
            continue
        if hd < cfg["coh_min_handle_days"] and h_depth < cfg["coh_quick_handle_depth"]:
            last_reason = f"柄才 {hd} 天、拉回 {h_depth:.0%}，還沒成形"
            continue
        found = None
        lo_bound = max(10, R - cfg["coh_max_cup_days"])
        for L in range(R - cfg["coh_min_cup_days"], lo_bound - 1, -1):
            if h[L] != h[max(0, L - 10):L + 11].max():
                continue                                # 不是局部高點
            hL = h[L]
            ratio = hR / hL
            if not (cfg["coh_lip_ratio_min"] <= ratio <= cfg["coh_lip_ratio_max"]):
                continue
            if h[L + 1:R].max() > max(hL, hR) * 1.01:
                continue                                # 杯身中間有更高點，不成杯
            bot_i = L + int(np.argmin(l[L:R + 1]))
            if bot_i > L + 1 and h[L + 1:bot_i].max() > hL * 1.01:
                continue                                # 左側下跌過程中不能再創新高
            depth = 1 - l[bot_i] / hL
            if cfg["coh_min_depth"] <= depth <= cfg["coh_max_depth"]:
                if found is None or hL > found[4]:      # 取最高的合格左杯緣
                    found = (L, bot_i, depth, ratio, hL)
        if not found:
            continue
        L, bot_i, depth, ratio = found[:4]
        hL, bottom = h[L], l[bot_i]
        h_pos = (handle_low - bottom) / (max(hL, hR) - bottom)
        if h_pos < cfg["coh_min_handle_pos"]:
            last_reason = "柄落在杯子下半部"
            continue
        break
    else:
        return fail(last_reason)
    cup_len = R - L

    s_depth = 20 * (1.0 if depth <= cfg["coh_ideal_max_depth"] else lin(depth, cfg["coh_max_depth"], cfg["coh_ideal_max_depth"]))
    cup_c = c[L:R + 1]
    lower_third = bottom + (hL - bottom) / 3
    u_frac = np.mean(cup_c <= lower_third)
    s_u = 20 * lin(u_frac, 0.0, 0.25)
    s_lip = 15 * lin(ratio, cfg["coh_lip_ratio_min"], 0.95)
    s_hd = 15 * (1.0 if h_depth <= cfg["coh_ideal_handle_depth"] else lin(h_depth, cfg["coh_max_handle_depth"], cfg["coh_ideal_handle_depth"]))
    s_hpos = 10 * lin(h_pos, 0.5, 0.66)
    vol_ratio = v[R + 1:].mean() / max(v[L:R + 1].mean(), 1)
    s_vol = 20 * lin(vol_ratio, 1.0, 0.7)
    score = s_depth + s_u + s_lip + s_hd + s_hpos + s_vol

    prior = hL / l[max(0, L - 126):L + 1].min() - 1 if L > 20 else np.nan
    idx = d.index
    return {
        "ok": True, "score": round(score, 1),
        "pivot": hR, "stop": handle_low, "pivot_i": R,
        "base_start": L, "base_days": cup_len, "max_depth": depth,
        "detail": f"杯深 {depth:.0%}｜杯長 {cup_len} 天｜右/左杯緣 {ratio:.2f}｜柄深 {h_depth:.0%}、{hd} 天"
                  f"｜柄量/杯量 {vol_ratio:.2f}｜U型度 {u_frac:.0%}"
                  + (f"｜杯前漲幅 {prior:.0%}" if prior == prior else ""),
        "parts": {"杯深": s_depth, "U型": s_u, "杯緣": s_lip, "柄深": s_hd, "柄位置": s_hpos, "量縮": s_vol},
        "lines": [[(idx[L], hL), (idx[bot_i], bottom), (idx[R], hR), (idx[R + 1 + int(np.argmin(l[R + 1:]))], handle_low)]],
        "marks": [idx[L], idx[R]],
    }


def detect_htf(d, cfg):
    """高窄旗形：約 1 個月內急漲 40% 以上（旗桿），之後小幅拉回整理（旗形）。
    旗桿頂候選從最近的往回找；旗形之後已突破也算，但收盤不能超過旗桿頂 10%（型態已結束）。"""
    h, l, c, v = (d[x].values for x in ("High", "Low", "Close", "Volume"))
    n = len(c)
    t = n - 1
    if n < cfg["htf_min_pole_days"] + cfg["htf_min_flag_days"] + 5:
        return fail("資料不足")
    w0 = max(1, t - cfg["htf_max_flag_days"] - 10)
    last_reason = "近 1 個月沒有急漲的旗桿"
    for P in range(t - cfg["htf_min_flag_days"], w0 - 1, -1):
        if h[P] != h[max(0, P - 5):min(t, P + 6)].max():
            continue                                    # 不是局部高點
        s_lo = max(0, P - cfg["htf_max_pole_days"])
        if P - s_lo < cfg["htf_min_pole_days"]:
            continue
        S = s_lo + int(np.argmin(l[s_lo:P - cfg["htf_min_pole_days"] + 1]))
        gain = h[P] / l[S] - 1
        pole_days = P - S
        if gain < cfg["htf_min_gain"]:
            last_reason = f"旗桿漲幅不足（{gain:.0%}，{pole_days} 天）"
            continue
        prior = h[max(0, S - 252):S]
        if len(prior) and h[P] / prior.max() - 1 < cfg["htf_min_above_prior"]:
            last_reason = f"旗桿頂只比舊高高 {h[P] / prior.max() - 1:.0%}，屬於跌深反彈"
            continue
        # 旗形：旗桿頂之後到突破（收盤站上旗桿頂）前
        brk = [k for k in range(P + 1, t + 1) if c[k] > h[P]]
        f_end = brk[0] - 1 if brk else t
        fd = f_end - P
        if fd < cfg["htf_min_flag_days"]:
            last_reason = f"旗形還沒形成（旗桿頂後僅 {fd} 天）"
            continue
        if fd > cfg["htf_max_flag_days"]:
            last_reason = f"旗形過長（{fd} 天）"
            continue
        if brk and (h[brk[0]:t + 1].max() > h[P] * 1.10 or t - brk[0] > 10):
            last_reason = "突破後已漲超過旗桿頂 10% 或突破超過 10 天，型態已結束"
            continue
        flag_low = l[P + 1:f_end + 1].min()
        f_depth = 1 - flag_low / h[P]
        max_fd = cfg["htf_short_flag_depth"] if fd <= cfg["htf_short_flag_days"] else cfg["htf_max_flag_depth"]
        pre = l[max(0, S - 60):S]
        if len(pre) and c[S] / pre.min() - 1 > cfg["htf_max_prior_run"]:
            last_reason = f"旗桿起點前已漲 {c[S] / pre.min() - 1:.0%}，是延續漲勢不是新的旗桿"
            continue
        if f_depth > max_fd:
            last_reason = f"旗形回檔過深（{f_depth:.0%}）"
            continue
        break
    else:
        return fail(last_reason)

    s_gain = 25 * lin(gain, cfg["htf_min_gain"], 1.0)
    s_depth = 25 * (1.0 if f_depth <= cfg["htf_ideal_flag_depth"] else lin(f_depth, cfg["htf_max_flag_depth"], cfg["htf_ideal_flag_depth"]))
    s_len = 15 * (1.0 if 5 <= fd <= 20 else 0.5)
    vol_ratio = v[P + 1:f_end + 1].mean() / max(v[S:P + 1].mean(), 1)
    s_vol = 20 * lin(vol_ratio, 1.0, 0.5)
    tight = (h[-10:].max() - l[-10:].min()) / c[-1]
    s_tight = 15 * lin(tight, 0.18, 0.08)
    score = s_gain + s_depth + s_len + s_vol + s_tight

    pivot = h[P]
    idx = d.index
    fl_i = P + 1 + int(np.argmin(l[P + 1:f_end + 1]))
    return {
        "ok": True, "score": round(score, 1),
        "pivot": pivot, "stop": flag_low, "pivot_i": P,
        "base_start": P, "base_days": fd, "max_depth": f_depth, "pole_start": S, "flag_end": f_end,
        "detail": f"旗桿 +{gain:.0%}／{pole_days} 天｜旗形回檔 {f_depth:.0%}、{fd} 天｜旗量/桿量 {vol_ratio:.2f}"
                  f"｜近10日振幅 {tight:.1%}｜旗桿頂 {h[P]:.2f}" + ("｜已突破" if brk else ""),
        "parts": {"漲幅": s_gain, "回檔": s_depth, "天數": s_len, "量縮": s_vol, "緊密": s_tight},
        "lines": [[(idx[S], l[S]), (idx[P], h[P]), (idx[fl_i], flag_low)]],
        "marks": [idx[S], idx[P]],
    }


DETECTORS = {"VCP": detect_vcp, "COH": detect_coh, "HTF": detect_htf}
SHAPE_ORDER = ["HTF", "VCP", "COH"]      # 同時符合多種型態時的歸類順序（HTF 有強烈旗桿，最具辨識度）
INDEXES = {"上市": "^TWII", "上櫃": "^TWOII"}

REQ_NAMES = ["趨勢模板", "RS", "右上角", "下跌量縮", "末段<10%"]
BONUS_NAMES = ["大量突破", "寬底", "危險點", "回檔淺", "均線往上", "抗跌"]


def checklist(d, rec, pat, res, cfg, index_close=None):
    """依使用者的檢查清單評估。回傳 {"req": {名稱: (bool, 說明)}, "bonus": {名稱: (True/False/None, 說明)}, ...}
    None 代表「不適用」或「尚無法判斷」，不加分也不扣分。"""
    h, l, c, v = (d[x].values for x in ("High", "Low", "Close", "Volume"))
    n = len(c)
    bs = int(res["base_start"])
    idx = d.index
    req, bonus = {}, {}
    req["趨勢模板"] = (True, "新股條件" if rec["track"] == "新股" else "1–7 條全過")
    if cfg.get("rs_gate_here", True):
        req["RS"] = (True, f"RS {rec['RS']:.0f} > {cfg['tt_min_rs']}")

    # 右上角：52 週高點要包含在這次型態裡（型態起點往前延伸到 52 週高點），而且再往前不能有更高的舊高點
    w = min(252, n)
    i_hi = n - w + int(np.argmax(h[n - w:]))
    form_start = min(bs, i_hi)
    form_hi = h[form_start:].max()
    if form_start == 0:
        req["右上角"] = (True, "資料起點即型態內")
    else:
        w_lo = max(0, n - 252)                       # 只看 52 週內（2026/11 依考題：1 年以前的舊高不管）
        if form_start <= w_lo:
            req["右上角"] = (True, "52 週內都在型態裡")
        else:
            j = w_lo + int(np.argmax(h[w_lo:form_start]))
            ok = h[j] <= form_hi
            req["右上角"] = (bool(ok), f"型態 {idx[form_start]:%Y/%m/%d} 起，52 週內無更高舊高" if ok
                             else f"{idx[j]:%Y/%m/%d} 有舊高 {h[j]:.1f}，高於型態高點 {form_hi:.1f}")

    # 下跌量縮（2026/11 改）：只看型態最後一次拉回——VCP 最後一次收縮、COH 的柄、HTF 的旗形，
    # 從最後高點跌到之後最低點這段的平均量 < 整個型態平均量 × 0.9；拉回不到 3 天先不判斷
    hi = int(res["pivot_i"])
    end = int(res.get("flag_end", n - 1)) if pat == "HTF" else n - 1
    seg_name = {"VCP": "最後一次收縮", "COH": "柄", "HTF": "旗形"}[pat]
    if end > hi:
        lo = hi + 1 + int(np.argmin(l[hi + 1:end + 1]))
        seg = v[hi + 1:lo + 1]
        p0 = int(res.get("pole_start", res["base_start"]))
        pat_v = v[p0:hi + 1].mean() if hi >= p0 else np.nan
        if len(seg) < cfg["dry_min_days"] or not (pat_v == pat_v and pat_v > 0):
            req["下跌量縮"] = (True, f"{seg_name}拉回僅 {len(seg)} 天，量縮待觀察")
        else:
            r = seg.mean() / pat_v
            req["下跌量縮"] = (bool(r < cfg["dry_max_ratio"]), f"{seg_name}拉回 {len(seg)} 天均量 / 型態均量 = {r:.2f}")
    else:
        req["下跌量縮"] = (True, f"{seg_name}還沒拉回")

    if pat == "VCP":
        ld = res["last_depth"]
        req["末段<10%"] = (bool(ld < cfg["req_vcp_last_max"]), f"最後一次收縮 {ld:.0%}")

    # ---- 加分 ----
    pivot, pi = res["pivot"], int(res["pivot_i"])
    if c[-1] > pivot:
        js = [j for j in range(pi + 1, n) if c[j] > pivot]
        j = js[0] if js else n - 1
        avg = v[max(0, j - 50):j].mean() if j > 0 else np.nan
        ratio = v[j] / avg if avg and avg == avg else np.nan
        bonus["大量突破"] = (bool(ratio >= cfg["bonus_breakout_vol"]),
                         f"{idx[j]:%m/%d} 突破量 = 50 日均量 × {ratio:.1f}")
    else:
        bonus["大量突破"] = (None, "尚未突破")

    if pat in ("VCP", "COH"):
        bd = res["base_days"]
        bonus["寬底"] = (bool(bd >= cfg["bonus_wide_base_days"]), f"整理 {bd} 天（約 {bd / 5:.0f} 週）")
    else:
        bonus["寬底"] = (None, "HTF 不適用")

    ema21 = pd.Series(c).ewm(span=21, adjust=False).mean().values
    e = ema21[-1]
    dist = (pivot - e) / pivot
    stop7 = pivot * (1 - cfg["bonus_risk_pct"])
    if 0 <= dist <= cfg["bonus_risk_pct"]:
        bonus["危險點"] = (True, f"Pivot 下方 {dist:.1%} 有 EMA21（{e:.1f}），停損可縮到 EMA21 下方")
    else:
        bonus["危險點"] = (False, f"Pivot 距 EMA21 {dist:.1%}，用 7% 停損 {stop7:.1f}")

    md = res["max_depth"]
    bonus["回檔淺"] = (bool(md <= cfg["bonus_shallow"]), f"型態最大回檔 {md:.0%}")

    k = cfg["bonus_ma_rising_days"]
    if n >= 240 + k:
        s50 = pd.Series(c).rolling(50).mean().values
        s240 = pd.Series(c).rolling(240).mean().values
        ups = {"EMA21": ema21[-1] > ema21[-1 - k], "SMA50": s50[-1] > s50[-1 - k], "SMA240": s240[-1] > s240[-1 - k]}
        bad = [m for m, u in ups.items() if not u]
        bonus["均線往上"] = (not bad, "三條均線都往上" if not bad else "走平或下彎：" + "、".join(bad))
    else:
        bonus["均線往上"] = (None, "資料不足 240 天")

    if index_close is not None and len(index_close) > 20:
        ic = index_close[index_close.index <= idx[-1]].tail(cfg["bonus_mkt_lookback"])
        runmax = ic.cummax()
        ddser = ic / runmax - 1
        tr = ddser.idxmin()
        drop = -ddser.min()
        if drop >= cfg["bonus_mkt_min_drop"]:
            pk = ic[:tr].idxmax()
            sc = d["Close"]
            seg = sc[(sc.index >= pk) & (sc.index <= tr)]
            if len(seg) >= 2:
                sdrop = max(0.0, 1 - seg.min() / seg.iloc[0])
                bonus["抗跌"] = (bool(sdrop < drop * cfg["bonus_mkt_ratio"]),
                               f"大盤 {pk:%m/%d}–{tr:%m/%d} 跌 {drop:.1%}，個股同期最多跌 {sdrop:.1%}")
            else:
                bonus["抗跌"] = (None, "同期無個股資料")
        else:
            bonus["抗跌"] = (None, f"近 3 個月大盤最大回檔僅 {drop:.1%}")
    else:
        bonus["抗跌"] = (None, "無大盤資料")

    # 價格緊密（暫不列入門檻，只顯示）
    rng = (d["High"] - d["Low"]) / d["Close"]
    r10, r50 = rng.tail(10).mean(), rng.tail(50).mean()
    return {"req": req, "bonus": bonus, "passed": all(ok for ok, _ in req.values()),
            "bonus_n": sum(1 for b, _ in bonus.values() if b), "bonus_max": len(bonus),
            "range10": r10, "range50": r50, "ema21": e, "stop7": stop7}


# =============================================================================
# RS（IBD 式加權報酬，再於全市場排百分位）
# =============================================================================
RS_WEIGHTS = {63: 0.4, 126: 0.2, 189: 0.2, 252: 0.2}


def rs_raw(c):
    n = len(c)
    num, den = 0.0, 0.0
    for k, w in RS_WEIGHTS.items():
        if n > k:
            num += w * (c[-1] / c[-1 - k] - 1)
            den += w
    if den == 0:
        k = n - 1
        if k < 20:
            return np.nan
        return c[-1] / c[0] - 1
    return num / den


# =============================================================================
# 全市場分析
# =============================================================================
def analyze(data, meta, cfg, asof, indexes=None):
    indexes = indexes or {}
    rows = []
    trimmed = {}
    for sym, d in data.items():
        d = d[d.index <= asof]
        if len(d) < cfg["ipo_min_bars"]:
            continue
        trimmed[sym] = d
        c = d["Close"].values
        turnover = float((d["Close"] * d["Volume"]).tail(cfg["turnover_days"]).mean())
        rows.append({"symbol": sym, "close": c[-1], "bars": len(d), "turnover": turnover,
                     "rs_raw": rs_raw(c), "last_date": d.index[-1]})
    base = pd.DataFrame(rows).set_index("symbol")
    base["RS"] = (base["rs_raw"].rank(pct=True) * 99).clip(lower=1).round()
    stale = base["last_date"] < asof - pd.Timedelta(days=10)
    base.loc[stale, "RS"] = np.nan           # 停牌/無近期資料者不參與

    results = []
    for sym, row in base.iterrows():
        if stale.get(sym, False):
            continue
        d = trimmed[sym]
        info = meta.loc[sym] if sym in meta.index else None
        liq = row["close"] >= cfg["min_price"] and row["turnover"] >= cfg["min_avg_turnover"]
        if not liq:
            continue
        list_date = info["list_date"] if info is not None else pd.NaT
        is_ipo = pd.notna(list_date) and (asof - list_date).days < cfg["ipo_calendar_days"]
        rec = {"symbol": sym, "code": info["code"] if info is not None else sym.split(".")[0],
               "name": info["name"] if info is not None else "",
               "market": info["market"] if info is not None else "",
               "industry": info["industry"] if info is not None else "",
               "list_date": list_date, "close": row["close"], "RS": row["RS"],
               "turnover": row["turnover"], "track": "新股" if is_ipo else "一般"}
        if is_ipo:
            if cfg.get("rs_gate_here", True) and not (row["RS"] > cfg["tt_min_rs"]):
                continue
            it = ipo_template(d, cfg)
            if not it["pass"]:
                continue
            rec["below_high"] = it["below_high"]
        else:
            tt = trend_template(d, cfg)
            if tt is None:
                continue                      # 非新股但資料不足（長期停牌等）
            if not tt["pass_1_7"] or (cfg.get("rs_gate_here", True) and not (row["RS"] > cfg["tt_min_rs"])):
                continue
            rec["below_high"] = tt["below_52w_high"]
            rec["ma200_streak"] = tt["ma200_rising_streak"]
        any_ok = False
        for name, fn in DETECTORS.items():
            r = fn(d, cfg)
            if r["ok"]:
                ic = indexes.get(INDEXES.get(rec["market"], "^TWII"))
                r["check"] = checklist(d, rec, name, r, cfg, ic)
                r["listed"] = r["check"]["passed"]
                any_ok = any_ok or r["listed"]
            else:
                r["listed"] = False
            rec[name] = r
        rec["any_listed"] = any_ok
        # 型態歸類看結構，順序 HTF > VCP > COH；歸類後若必要條件沒過就不列（不會改列到別的型態）
        shape = next((k for k in SHAPE_ORDER if rec[k]["ok"]), None)
        if shape == "HTF" and rec["VCP"]["ok"] and rec["VCP"].get("n_con", 0) >= 3:
            shape = "VCP"                             # 旗桿後的整理裡已經收縮 3 次以上，算 VCP（2026/11-20 依金居）
        if shape == "VCP" and rec["COH"]["ok"]:
            vv = rec["VCP"]
            # 第一段下跌超過約 4 週（像圓底，光聯），或第一段深跌後右側漲回起點、後面只有小拉回（杯子＋柄，玉晶光）→ 歸杯柄
            if vv.get("first_days", 0) > cfg["vcp_cup_first_days"] or (vv.get("rehigh") and vv.get("first_ratio", 0) > 2.0):
                shape = "COH"
        rec["shape"] = shape
        rec["primary"] = shape if shape and rec[shape]["listed"] else None
        results.append(rec)
    return results, base, trimmed


# =============================================================================
# 輸出
# =============================================================================
def _cjk_font():
    try:
        from matplotlib import font_manager
        avail = {f.name for f in font_manager.fontManager.ttflist}
        for f in ["Microsoft JhengHei", "PingFang TC", "Heiti TC", "Noto Sans CJK TC", "Noto Sans TC",
                  "Noto Sans CJK JP", "Arial Unicode MS", "Microsoft YaHei", "SimHei"]:
            if f in avail:
                return f
    except Exception:
        pass
    return None


# TradingView 配色（依使用者圖表：紅漲綠跌、EMA21 粉紅、SMA50 橘、SMA240 黑、成交量 50 日均量橘線）
TV = {"up": "#F23645", "down": "#089981", "vol_up": "#F7A9A7", "vol_dn": "#8FD1C3",
      "ema21": "#F77C80", "ma50": "#FF9800", "ma240": "#434651",
      "vol_ma": "#FF9800", "grid": "#F0F3FA", "axis": "#787B86", "pivot": "#2962FF", "stop": "#9598A1",
      "struct": "#AB47BC"}


def plot_chart(d, rec, pattern, res, path, cfg, font):
    import matplotlib
    matplotlib.use("Agg")
    import mplfinance as mpf
    full = d.copy()
    full["EMA21"] = full["Close"].ewm(span=21, adjust=False).mean()
    for m in (50, 240):
        full[f"MA{m}"] = full["Close"].rolling(m).mean()
    full["VMA50"] = full["Volume"].rolling(50).mean()
    sub = full.tail(cfg["chart_bars"])
    lines_cfg = [("EMA21", TV["ema21"], 1.0), ("MA50", TV["ma50"], 1.2), ("MA240", TV["ma240"], 1.2)]   # 圖上只畫三條（篩選仍用 50/150/200）
    aps = [mpf.make_addplot(sub[c], color=col, width=w) for c, col, w in lines_cfg if sub[c].notna().any()]
    if sub["VMA50"].notna().any():
        aps.append(mpf.make_addplot(sub["VMA50"], panel=1, color=TV["vol_ma"], width=1.0))
    start = sub.index[0]
    lines = [seg for seg in res.get("lines", []) if all(p[0] >= start for p in seg)]
    marks = [x for x in res.get("marks", []) if x >= start]
    mc = mpf.make_marketcolors(up=TV["up"], down=TV["down"], edge="inherit", wick="inherit",
                               volume={"up": TV["vol_up"], "down": TV["vol_dn"]})
    rc = {"font.family": font} if font else {}
    rc.update({"axes.edgecolor": "#E0E3EB", "axes.labelcolor": TV["axis"], "xtick.color": TV["axis"],
               "ytick.color": TV["axis"], "font.size": 9})
    style = mpf.make_mpf_style(marketcolors=mc, facecolor="#FFFFFF", figcolor="#FFFFFF", edgecolor="#E0E3EB",
                               gridcolor=TV["grid"], gridstyle="-", y_on_right=True, rc=rc)
    name = rec["name"] if font else ""
    title = f"{rec['code']} {name}  {pattern}"     # 加分與 RS 由網站顯示（RS 用族群透視的資料）
    png = Path(str(path)).with_suffix(".tmp.png")
    kw = dict(type="candle", volume=True, addplot=aps, style=style, ylabel="", ylabel_lower="",
              figsize=(11, 6.2), panel_ratios=(3.2, 1), warn_too_much_data=100000, returnfig=True)
    fig, axes = mpf.plot(sub, **kw)
    axes[0].set_title(title, loc="left", fontsize=11, color="#131722", pad=8)
    # X 軸：仿 TradingView，每月第一個交易日標「九月」「十月」，一月標年份
    MONTHS = ["一月", "二月", "三月", "四月", "五月", "六月", "七月", "八月", "九月", "十月", "十一月", "十二月"]
    ticks, labels = [], []
    for i, dt in enumerate(sub.index):
        if i == 0 or dt.month != sub.index[i - 1].month:
            if i == 0 and sub.index[0].day > 7:
                continue                              # 第一個月不完整就不標
            ticks.append(i)
            labels.append(str(dt.year) if dt.month == 1 else MONTHS[dt.month - 1])
    for ax in axes:
        ax.set_xticks(ticks)
    axes[-2].set_xticklabels(labels, rotation=0, fontsize=9)
    for ax in axes:                                   # 成交量柱去掉外框，貼近 TradingView
        for patch in ax.patches:
            patch.set_edgecolor(patch.get_facecolor())
            patch.set_linewidth(0)
    axes[0].axhline(res["pivot"], color=TV["pivot"], linestyle="--", linewidth=1.0)
    axes[0].axhline(res["stop"], color=TV["stop"], linestyle="--", linewidth=1.0)
    fig.savefig(str(png), dpi=200, bbox_inches="tight", facecolor="#FFFFFF")   # 2026/11-20：200 dpi，手機與 Retina 螢幕放大也清楚
    import matplotlib.pyplot as plt
    plt.close(fig)
    # 轉成 JPEG 壓縮，讓網頁可以放下所有個股的圖
    from PIL import Image
    Image.open(png).convert("RGB").save(path, "JPEG", quality=90, optimize=True)
    png.unlink(missing_ok=True)


def sort_key(r, pat):
    return (-r[pat]["check"]["bonus_n"], -(r["RS"] if r["RS"] == r["RS"] else 0))


def pick_pattern(results, pat):
    sel = [r for r in results if r.get("primary") == pat]
    sel.sort(key=lambda r: sort_key(r, pat))
    return sel


def ipo_best(r):
    """新股：取歸類的型態"""
    return r.get("primary")


def _mark(b):
    return "✓" if b else ("—" if b is None else "✗")


def build_rows(results, pattern=None):
    out = []
    for r in results:
        row = {"代號": r["code"], "名稱": r["name"], "市場": r["market"], "產業": r["industry"],
               "路線": r["track"], "收盤": round(r["close"], 2),
               "RS": r["RS"], "20日均額(億)": round(r["turnover"] / 1e8, 2),
               "距高點%": round(r["below_high"] * 100, 1)}
        pat = pattern or ipo_best(r)
        if pat and r[pat]["ok"] and "check" in r[pat]:
            p, ck = r[pat], r[pat]["check"]
            if not pattern:
                row["型態"] = pat
            row.update({"加分": f'{ck["bonus_n"]}/{ck["bonus_max"]}'})
            for k, (bv, _) in ck["bonus"].items():
                row[k] = _mark(bv)
            row.update({"Pivot": round(p["pivot"], 2), "距Pivot%": round((r["close"] / p["pivot"] - 1) * 100, 1),
                        "狀態": "已突破" if r["close"] > p["pivot"] else "整理中",
                        "7%停損": round(ck["stop7"], 2), "EMA21": round(ck["ema21"], 2),
                        "近10日振幅%": round(ck["range10"] * 100, 1), "50日振幅%": round(ck["range50"] * 100, 1),
                        "型態結構": p["detail"],
                        "加分說明": "；".join(f"{k}：{t}" for k, (_, t) in ck["bonus"].items()),
                        "必要條件": "；".join(f"{k}：{t}" for k, (_, t) in ck["req"].items())})
        if r["track"] == "新股":
            row["上市日"] = r["list_date"].date() if pd.notna(r["list_date"]) else None
        out.append(row)
    return pd.DataFrame(out)


def write_outputs(results, trimmed, cfg, asof, n_scanned, make_charts=True):
    day = asof.strftime("%Y-%m-%d")
    outdir = Path(cfg["output_dir"]) / day
    chartdir = outdir / "charts"
    chartdir.mkdir(parents=True, exist_ok=True)
    regular = [r for r in results if r["track"] == "一般"]
    ipo = [r for r in results if r["track"] == "新股"]
    sheets = {}
    chart_jobs = []
    for pat in DETECTORS:
        sel = pick_pattern(regular, pat)
        df = build_rows(sel, pat)
        if len(df):
            df["圖檔"] = ""
            for i, r in enumerate(sel[:cfg["charts_per_pattern"]]):
                fn = f"{pat}_{r['code']}.jpg"
                df.loc[i, "圖檔"] = f"charts/{fn}"
                chart_jobs.append((r, pat, chartdir / fn))
        sheets[pat] = df
    ipo_rows = []
    for r in ipo:
        k = ipo_best(r)
        if k:
            ipo_rows.append(r)
            chart_jobs.append((r, k, chartdir / f"新股_{k}_{r['code']}.jpg"))
    ipo_rows.sort(key=lambda r: sort_key(r, ipo_best(r)))
    df_ipo = build_rows(ipo_rows)
    sheets["新股"] = df_ipo
    df_tt = pd.DataFrame([{"代號": r["code"], "名稱": r["name"], "市場": r["market"], "產業": r["industry"],
                           "收盤": round(r["close"], 2), "RS": r["RS"], "距高點%": round(r["below_high"] * 100, 1),
                           "歸類": r.get("primary") or "",
                           "加分": (str(r[r["primary"]]["check"]["bonus_n"]) if r.get("primary") else "")} for r in regular])
    if len(df_tt):
        df_tt = df_tt.sort_values("RS", ascending=False)
    sheets["趨勢模板通過"] = df_tt

    summary = pd.DataFrame([
        ["資料日期", day],
        ["掃描檔數", n_scanned],
        ["通過流動性＋趨勢模板", len(regular)],
        ["新股通過新股條件", len(ipo)],
        ["VCP（必要條件全過）", len(sheets["VCP"])],
        ["COH（必要條件全過）", len(sheets["COH"])],
        ["HTF（必要條件全過）", len(sheets["HTF"])],
        ["新股（必要條件全過）", len(df_ipo)],
    ] + [[f"參數 {k}", v] for k, v in cfg.items()], columns=["項目", "數值"])

    xlsx = outdir / f"動能篩選_{day}.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="摘要", index=False)
        for name in ["VCP", "COH", "HTF", "新股", "趨勢模板通過"]:
            df = sheets[name]
            if df.empty:
                df = pd.DataFrame({"說明": ["今日無符合標的"]})
            df.to_excel(xw, sheet_name=name, index=False)
        for ws in xw.book.worksheets:
            ws.freeze_panes = "C2"
            for col in ws.columns:
                width = max(len(str(c.value)) if c.value is not None else 0 for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(max(8, width * 1.6), 90)
    print(f"[輸出] {xlsx}")

    chart_map = {}
    if make_charts and chart_jobs:
        font = _cjk_font()
        for r, pat, path in chart_jobs:
            try:
                plot_chart(trimmed[r["symbol"]], r, pat, r[pat], path, cfg, font)
                chart_map[(r["symbol"], pat)] = _img64(path)
            except Exception as e:
                print(f"  畫圖失敗 {r['code']} {pat}：{e}")
        print(f"[輸出] {len(chart_jobs)} 張 K 線圖 → {chartdir}")
    write_site_json(results, cfg, day, chart_map, n_scanned)
    return xlsx


def write_site_json(results, cfg, day, chart_map, n_scanned):
    """輸出合併網站用的 output/site/screener.json（RS 門檻與族群領導股由網站端用族群透視的資料判斷）"""
    def J(x):
        if isinstance(x, (np.floating, float)):
            return None if x != x else round(float(x), 4)
        if isinstance(x, (np.integer,)):
            return int(x)
        if isinstance(x, (np.bool_,)):
            return bool(x)
        return x
    items = []
    for r in results:
        pat = r.get("primary")
        if not pat:
            continue
        p, ck = r[pat], r[pat]["check"]
        items.append({
            "code": r["code"], "name": r["name"], "market": r["market"], "industry": r["industry"],
            "track": r["track"], "pattern": pat,
            "list_date": (r["list_date"].strftime("%Y-%m-%d") if pd.notna(r["list_date"]) else None),
            "close": J(r["close"]), "pivot": J(p["pivot"]), "stop": J(p["stop"]),
            "status": "已突破" if r["close"] > p["pivot"] else "整理中",
            "below_high": J(r["below_high"]), "turnover": J(r["turnover"] / 1e8),
            "range10": J(ck["range10"]), "range50": J(ck["range50"]), "ema21": J(ck["ema21"]),
            "detail": p["detail"],
            "req": [[k, bool(b), t] for k, (b, t) in ck["req"].items()],
            "bonus": [[k, (None if b is None else bool(b)), t] for k, (b, t) in ck["bonus"].items()],
            "chart": chart_map.get((r["symbol"], pat)),
        })
    tt = [{"code": r["code"], "name": r["name"], "market": r["market"], "industry": r["industry"],
           "close": J(r["close"]), "below_high": J(r["below_high"]), "track": r["track"],
           "shape": r.get("shape")} for r in results]
    out = {"as_of": day, "generated_at": f"{pd.Timestamp.now():%Y-%m-%d %H:%M}", "scanned": int(n_scanned),
           "rs_min": cfg["tt_min_rs"], "items": items, "tt_pass": tt}
    site = Path(cfg["output_dir"]) / "site"
    site.mkdir(parents=True, exist_ok=True)
    import json
    (site / "screener.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"[輸出] 網站資料 {site / 'screener.json'}（{len(items)} 檔型態候選、{len(tt)} 檔通過趨勢模板）")


# =============================================================================
# 網頁報告
# =============================================================================
HTML_CSS = """
/* 版面：頂部摘要 → 黏性分頁列 → 卡片網格；台股紅漲綠跌 */
:root{--bg:#f5f6f8;--card:#ffffff;--ink:#1b2230;--mute:#667085;--line:#e3e6ec;--up:#d62728;--dn:#1f9d55;--acc:#0b5bd3;--chip:#eaf0fb;--onacc:#ffffff;--ipo:#7c3aed}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1319;--card:#171c25;--ink:#e6e9ef;--mute:#98a2b3;--line:#2a313d;--up:#f0524f;--dn:#34c27a;--acc:#5b9bff;--chip:#1d2a42;--onacc:#0b1220;--ipo:#a78bfa;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0f1319;--card:#171c25;--ink:#e6e9ef;--mute:#98a2b3;--line:#2a313d;--up:#f0524f;--dn:#34c27a;--acc:#5b9bff;--chip:#1d2a42;--onacc:#0b1220;--ipo:#a78bfa;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font-family:"PingFang TC","Microsoft JhengHei","Noto Sans TC",system-ui,sans-serif;font-size:15px}
header{padding-block:20px 8px;max-width:1200px;margin:auto}
.wrap{padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--mute);font-size:13px}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0 4px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 14px}
.stat b{font-size:20px;display:block}.stat span{font-size:12px;color:var(--mute)}
nav{position:sticky;top:env(safe-area-inset-top,0px);background:var(--bg);z-index:5;border-bottom:1px solid var(--line)}
.tabs{max-width:1200px;margin:auto;display:flex;gap:4px;padding-block:8px;overflow-x:auto;align-items:center}
.tab{border:1px solid var(--line);background:var(--card);color:var(--ink);padding:7px 14px;border-radius:999px;cursor:pointer;white-space:nowrap;font-size:14px}
.tab.on{background:var(--acc);color:var(--onacc);border-color:var(--acc)}
.tools{margin-left:auto;display:flex;gap:8px;align-items:center;font-size:13px;color:var(--mute)}
.tools input[type=search]{padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);width:130px}
main{max-width:1200px;margin:auto;padding-block:12px 40px}
.sec{display:none}.sec.on{display:block}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(520px,100%),1fr));gap:14px}
.card{min-width:0}
@media (max-width:600px){.grid{grid-template-columns:1fr}.tools input[type=search]{width:90px}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.ch{display:flex;align-items:baseline;gap:8px;padding:12px 14px 4px;flex-wrap:wrap}
.code{font-weight:700;font-size:18px}.name{font-weight:600}.ind{color:var(--mute);font-size:12px}
.score{margin-left:auto;font-weight:800;font-size:24px;color:var(--acc);font-variant-numeric:tabular-nums}
.score small{font-size:12px;font-weight:600;color:var(--mute);margin:0 2px}
.reqs{padding:4px 14px 0;font-size:12px;color:var(--mute);display:flex;gap:8px;flex-wrap:wrap}
.rq{color:var(--ink)}
.chip.y{background:var(--acc);color:var(--onacc)}.chip.n{background:transparent;border:1px solid var(--line);color:var(--mute)}
.chip.na{background:transparent;color:var(--mute);border:1px dashed var(--line)}
.det summary{cursor:pointer}.det ul{margin:6px 0 0;padding-left:18px}.det li{margin:2px 0}
.chips{display:flex;gap:6px;flex-wrap:wrap;padding:4px 14px}
.chip{background:var(--chip);border-radius:6px;padding:2px 8px;font-size:12.5px}
.chip.br{background:var(--up);color:var(--onacc)}.chip.ipo{background:var(--ipo);color:var(--onacc)}
.lbl{font-size:12.5px;color:var(--mute);align-self:center}
.kv{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:4px;padding:8px 14px;font-size:13px}
.kv div span{display:block;color:var(--mute);font-size:11.5px}
.det{padding:0 14px 10px;font-size:12.5px;color:var(--mute);line-height:1.6}
.card img{width:100%;max-width:100%;display:block;border-top:1px solid var(--line);cursor:zoom-in;background:#fff}
table{width:100%;border-collapse:collapse;background:var(--card);border-radius:12px;overflow:hidden;font-size:13.5px}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{cursor:pointer;background:var(--chip);position:sticky;top:52px}th:nth-child(-n+3),td:nth-child(-n+3){text-align:left}
.tw{overflow-x:auto}.empty{color:var(--mute);padding:30px;text-align:center}
.zoom{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;align-items:center;justify-content:center;z-index:20;cursor:zoom-out}
.zoom img{max-width:96vw;max-height:94vh}
footer{max-width:1200px;margin:auto;padding-block:8px 30px;color:var(--mute);font-size:13px}
.guide{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(300px,100%),1fr));gap:12px;border-top:1px solid var(--line);padding-top:16px}
.guide section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px;min-width:0}
.guide h3{font-size:14px;color:var(--ink);margin:0 0 8px}
.guide ul,.guide ol{margin:0;padding-left:20px;line-height:1.75}.guide ul{list-style:none;padding-left:0}
.guide b{color:var(--ink);font-weight:600}.guide .chip{display:inline-block;min-width:22px;text-align:center}
.disc{margin:14px 0 0;font-size:12px}
.tvtop p{color:var(--mute);font-size:13px;line-height:1.6;margin:4px 0 10px}
.tvb{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 14px;margin:12px 0}
.tvh{display:flex;align-items:center;gap:8px;margin-bottom:6px}.tvh .cp{margin-left:auto}
textarea{width:100%;font:12.5px ui-monospace,Menlo,monospace;border:1px solid var(--line);border-radius:8px;padding:8px;background:var(--bg);color:var(--ink);resize:vertical}
.cp,.dl{border:0;background:var(--acc);color:var(--onacc);border-radius:8px;padding:6px 12px;font-size:13px;cursor:pointer;text-decoration:none;display:inline-block;margin-bottom:8px}
.tab:focus-visible,.cp:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
select{padding:5px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink)}
"""

HTML_JS = """
const tabs=[...document.querySelectorAll('.tab')],secs=[...document.querySelectorAll('.sec')];
function show(id){tabs.forEach(t=>t.classList.toggle('on',t.dataset.t===id));secs.forEach(s=>s.classList.toggle('on',s.id===id));
 try{localStorage.setItem('tab',id)}catch(e){}}
tabs.forEach(t=>t.onclick=()=>show(t.dataset.t));
let saved=null;try{saved=localStorage.getItem('tab')}catch(e){}
show(saved&&document.getElementById(saved)?saved:tabs[0].dataset.t);
const q=document.getElementById('q'),ms=document.getElementById('ms'),msv=document.getElementById('msv');
function filt(){const k=q.value.trim(),m=+ms.value;msv.textContent=m;
 document.querySelectorAll('[data-s]').forEach(e=>{const ok=(!k||e.dataset.k.includes(k))&&(+e.dataset.s>=m);e.style.display=ok?'':'none'})}
q.oninput=filt;ms.oninput=filt;
const z=document.querySelector('.zoom'),zi=z.querySelector('img');
document.querySelectorAll('.card img').forEach(i=>i.onclick=()=>{zi.src=i.src;z.style.display='flex'});z.onclick=()=>z.style.display='none';
document.querySelectorAll('th').forEach((th,ci)=>th.onclick=()=>{const tb=th.closest('table').tBodies[0],rows=[...tb.rows];
 const asc=th.dataset.a!=='1';th.dataset.a=asc?'1':'0';
 rows.sort((a,b)=>{let x=a.cells[ci].dataset.v??a.cells[ci].textContent,y=b.cells[ci].dataset.v??b.cells[ci].textContent;
  const nx=parseFloat(x),ny=parseFloat(y);if(!isNaN(nx)&&!isNaN(ny)){x=nx;y=ny}return (x>y?1:x<y?-1:0)*(asc?1:-1)});
 rows.forEach(r=>tb.appendChild(r))});
document.querySelectorAll('.cp').forEach(b=>b.onclick=async()=>{const t=document.getElementById(b.dataset.src);
 try{await navigator.clipboard.writeText(t.value)}catch(e){t.select();document.execCommand('copy')}
 const o=b.textContent;b.textContent='已複製 ✓';setTimeout(()=>b.textContent=o,1500)});
const hs=document.getElementById('hist');if(hs)hs.onchange=()=>{if(hs.value)location.href=hs.value};
"""


def _img64(path):
    import base64
    try:
        mime = "image/jpeg" if str(path).lower().endswith((".jpg", ".jpeg")) else "image/png"
        return f"data:{mime};base64," + base64.b64encode(Path(path).read_bytes()).decode()
    except Exception:
        return None


def _card(r, pat, res, chart, ipo=False):
    from html import escape as E
    ck = res["check"]
    status = "已突破" if r["close"] > res["pivot"] else "整理中"
    dist = (r["close"] / res["pivot"] - 1) * 100
    chips = f'<span class="chip">{pat}</span>'
    if ipo:
        chips += '<span class="chip ipo">新股</span>'
    chips += f'<span class="chip{" br" if status == "已突破" else ""}">{status}</span>'
    req = "".join(f'<span class="rq" title="{E(t)}">✓ {E(k)}</span>' for k, (_, t) in ck["req"].items())
    bon = "".join(f'<span class="chip {"y" if b else ("na" if b is None else "n")}" title="{E(t)}">{_mark(b)} {E(k)}</span>'
                  for k, (b, t) in ck["bonus"].items())
    why = "".join(f'<li><b>{E(k)}</b> {E(t)}</li>' for k, (_, t) in ck["bonus"].items())
    img = f'<img loading="lazy" src="{chart}" alt="{r["code"]} {pat}">' if chart else ""
    rs = "—" if r["RS"] != r["RS"] else f'{r["RS"]:.0f}'
    key = E(f'{r["code"]} {r["name"]} {r["industry"]}')
    return f"""<div class="card" data-s="{ck['bonus_n']}" data-k="{key}">
<div class="ch"><span class="code">{r['code']}</span><span class="name">{E(str(r['name']))}</span>
<span class="ind">{E(str(r['market']))}・{E(str(r['industry']))}</span>
<span class="score"><small>加分</small>{ck['bonus_n']}<small>/{ck['bonus_max']}</small></span></div>
<div class="chips">{chips}</div>
<div class="reqs"><span class="lbl">必要：</span>{req}</div>
<div class="chips"><span class="lbl">加分：</span>{bon}</div>
<div class="kv"><div><span>RS</span>{rs}</div><div><span>近 10 日日振幅</span>{ck['range10'] * 100:.1f}%</div></div>
{img}</div>"""


def write_html(results, cfg, day, outdir, chart_map, n_scanned):
    from html import escape as E
    regular = [r for r in results if r["track"] == "一般"]
    ipo = [r for r in results if r["track"] == "新股"]
    sections, counts = [], {}
    for pat in DETECTORS:
        sel = pick_pattern(regular, pat)
        counts[pat] = len(sel)
        cards = "".join(_card(r, pat, r[pat], chart_map.get((r["symbol"], pat))) for r in sel)
        sections.append((pat, pat, cards))
    ipo_cards = []
    for r in ipo:
        k = ipo_best(r)
        if k:
            ipo_cards.append((sort_key(r, k), _card(r, k, r[k], chart_map.get((r["symbol"], k)), ipo=True)))
    ipo_cards.sort(key=lambda x: x[0])
    counts["新股"] = len(ipo_cards)
    sections.append(("IPO", "新股", "".join(c for _, c in ipo_cards)))

    rows = []
    for r in sorted(regular, key=lambda r: -(r["RS"] if r["RS"] == r["RS"] else 0)):
        sc = {k: (r[k]["check"]["bonus_n"] if r.get("primary") == k else -1) for k in DETECTORS}
        best = max(sc.values())
        cells = "".join(f'<td data-v="{sc[k]}">✓ 加分 {sc[k]}</td>' if sc[k] >= 0 else '<td data-v="-1">—</td>' for k in DETECTORS)
        rows.append(f'<tr data-s="{best}" data-k="{E(r["code"] + " " + str(r["name"]) + " " + str(r["industry"]))}">'
                    f'<td>{r["code"]}</td><td>{E(str(r["name"]))}</td><td>{E(str(r["industry"]))}</td>'
                    f'<td>{r["close"]:.2f}</td><td>{r["RS"]:.0f}</td><td>{r["below_high"] * 100:.1f}</td>'
                    f'<td>{r["turnover"] / 1e8:.2f}</td>{cells}</tr>')
    table = ('<div class="tw"><table><thead><tr><th>代號</th><th>名稱</th><th>產業</th><th>收盤</th><th>RS</th>'
             '<th>距高點%</th><th>均額(億)</th>' + "".join(f"<th>{k}</th>" for k in DETECTORS) +
             '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>') if rows else ""

    # ---- TradingView 代碼清單 ----
    def tv(r):
        return ("TPEX:" if r["market"] == "上櫃" or r["symbol"].endswith(".TWO") else "TWSE:") + r["code"]
    tv_groups = []
    for pat in DETECTORS:
        tv_groups.append((pat, [tv(r) for r in pick_pattern(regular, pat)]))
    ipo_sorted = sorted([r for r in ipo if ipo_best(r)], key=lambda r: sort_key(r, ipo_best(r)))
    tv_groups.append(("新股", [tv(r) for r in ipo_sorted]))
    tv_groups.append(("趨勢模板通過", [tv(r) for r in sorted(regular, key=lambda r: -r["RS"])]))
    tv_all = "\n".join(f"###{name}," + ",".join(codes) for name, codes in tv_groups if codes)
    (outdir / f"TradingView_{day}.txt").write_text(tv_all, encoding="utf-8")
    blocks = "".join(
        f'<div class="tvb"><div class="tvh"><b>{E(name)}</b>（{len(codes)}）'
        f'<button class="cp" data-src="tv{i}">複製</button></div>'
        f'<textarea id="tv{i}" readonly rows="3">{E(",".join(codes))}</textarea></div>'
        for i, (name, codes) in enumerate(tv_groups))
    tv_section = (
        '<section class="sec" id="TV"><div class="tvtop"><p>TradingView 匯入方式：觀察清單右上「⋯」→「匯入清單」選下載的 .txt（會依型態自動分組）；'
        '或複製下方任一段代碼，貼到觀察清單的「新增商品」欄位。</p>'
        '<button class="cp" data-src="tvall">複製全部（含分組）</button> '
        "__DL__"
        f'<textarea id="tvall" readonly rows="4">{E(tv_all)}</textarea></div>{blocks}</section>')

    root = Path(cfg["output_dir"])
    days = sorted([p.parent.name for p in root.glob("*/report.html")] + [day], reverse=True)
    days = list(dict.fromkeys(days))

    def page(prefix, artifact=False):
        hist = "".join(f'<option value="{prefix}{d}/report.html"{" selected" if d == day else ""}>{d}</option>' for d in days)
        tabs = "".join(f'<button class="tab" data-t="{sid}">{lab}（{counts[lab]}）</button>' for sid, lab, _ in sections)
        tabs += f'<button class="tab" data-t="TT">趨勢模板通過（{len(regular)}）</button>'
        tabs += '<button class="tab" data-t="TV">TradingView 代碼</button>'
        body = "".join(f'<section class="sec" id="{sid}"><div class="grid">{c or ""}</div>'
                       + ("" if c else '<div class="empty">今日無符合標的</div>') + "</section>"
                       for sid, lab, c in sections)
        body += f'<section class="sec" id="TT">{table or "<div class=empty>今日無符合標的</div>"}</section>'
        body += tv_section.replace("__DL__", "" if artifact else
            f'<a class="dl" download="TradingView_{day}.txt" href="data:text/plain;charset=utf-8,{E(__import__("urllib.parse").parse.quote(tv_all))}">下載 .txt</a>')
        histsel = "" if artifact else f'・歷史 <select id="hist">{hist}</select>'
        head = ('<title>台股動能篩選清單</title><meta name="claude:viewport-fit-sides" content="cover">' if artifact else
                '<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
                '<title>台股動能篩選清單</title>')
        tail = "" if artifact else "</body></html>"
        return head + f"""<style>{HTML_CSS}</style>{"" if artifact else "</head><body>"}
<header class="wrap"><h1>台股動能篩選清單</h1><div class="sub">資料日 {day}・趨勢模板（RS &gt; {cfg['tt_min_rs']}）+ VCP / COH / HTF・產生於 {pd.Timestamp.now():%Y-%m-%d %H:%M}
{histsel}</div>
<div class="stats"><div class="stat"><b>{n_scanned}</b><span>掃描檔數</span></div>
<div class="stat"><b>{len(regular)}</b><span>通過趨勢模板</span></div>
<div class="stat"><b>{counts['VCP']}</b><span>VCP</span></div><div class="stat"><b>{counts['COH']}</b><span>COH</span></div>
<div class="stat"><b>{counts['HTF']}</b><span>HTF</span></div><div class="stat"><b>{counts['新股']}</b><span>新股</span></div></div></header>
<nav><div class="tabs wrap">{tabs}<div class="tools"><input id="q" type="search" placeholder="代號/名稱/產業">
最少加分 <input id="ms" type="range" min="0" max="6" value="0"><b id="msv">0</b></div></div></nav>
<main class="wrap">{body}</main>
<footer class="wrap"><div class="guide">
<section><h3>排序</h3><ul><li>加分多的排前面</li><li>加分相同時，RS 高的排前面</li></ul></section>
<section><h3>必要條件（全部通過才列入）</h3><ol>
<li>趨勢模板（Mark 的 50／150／200 日，第 7 條放寬到距高點 30%）</li>
<li>RS &gt; {cfg['tt_min_rs']}</li>
<li>右上角：52 週高點在這次型態內，之前沒有更高的舊高</li>
<li>下跌量縮：近 20 日下跌日均量 &lt; 上漲日均量</li>
<li>VCP 最後一次收縮 &lt; 10%（只限 VCP，以日內高低點計算）</li></ol></section>
<section><h3>加分條件（每項 1 分，滿分 6）</h3><ul>
<li><b>大量突破</b>　突破當天量 ≥ 50 日均量 1.5 倍</li>
<li><b>寬底</b>　VCP 或杯身整理 ≥ 7 週</li>
<li><b>危險點</b>　Pivot 下方 7% 內有 EMA21 支撐</li>
<li><b>回檔淺</b>　型態最大回檔 ≤ 25%</li>
<li><b>均線往上</b>　EMA21、SMA50、SMA240 都往上</li>
<li><b>抗跌</b>　大盤回檔時，個股跌不到大盤的一半</li></ul></section>
<section><h3>型態歸類</h3><ul>
<li><b>VCP</b>　橫盤收縮：型態內高點不超過起點 3%，低點墊高</li>
<li><b>COH</b>　杯深 18–50%、杯身 ≥ 3 週，柄形成中也算</li>
<li><b>HTF</b>　旗桿 10 週內漲 ≥ 60%，且比起漲前舊高再高 25% 以上；旗形 1–8 週、回檔 ≤ 30%</li>
<li>同時符合多種時，依 HTF → VCP → COH 歸類</li></ul></section>
<section><h3>符號</h3><ul><li><span class="chip y">✓</span> 符合</li><li><span class="chip n">✗</span> 不符合</li>
<li><span class="chip na">—</span> 不適用或尚未發生（例如還沒突破）</li><li>滑鼠移到加分項目上可看原因</li></ul></section>
<section><h3>圖表</h3><ul><li>藍虛線：Pivot　灰虛線：停損參考</li>
<li>均線：粉紅 EMA21、橘 SMA50、黑 SMA240</li><li>成交量橘線：50 日均量</li><li>點圖可放大</li></ul></section>
</div><p class="disc">篩選結果是型態辨識的輔助工具，不是投資建議。</p></footer>
<div class="zoom"><img alt=""></div><script>{HTML_JS}</script>""" + tail

    (outdir / "report.html").write_text(page("../"), encoding="utf-8")
    (root / "index.html").write_text(page(""), encoding="utf-8")
    (root / "artifact.html").write_text(page("", artifact=True), encoding="utf-8")   # 線上發佈版
    print(f"[輸出] 網頁 {root / 'index.html'}")


# =============================================================================
# 單檔診斷
# =============================================================================
def run_check(codes, meta, cfg, asof, start, end, use_cache):
    syms = []
    for code in codes:
        code = code.upper().replace(".TWO", "").replace(".TW", "")
        hit = meta[meta["code"] == code]
        syms += list(hit.index) if len(hit) else [f"{code}.TW", f"{code}.TWO"]
    data = download_prices(syms + list(INDEXES.values()), start, end, cfg)
    indexes = split_indexes(data)
    universe_cache = Path(cfg["cache_dir"]) / f"prices_{end:%Y%m%d}.pkl"
    rs_table = None
    if universe_cache.exists():
        uni = pd.read_pickle(universe_cache)
        split_indexes(uni)
        _, rs_table, _ = analyze(uni, meta, cfg, asof)
    for sym, d in data.items():
        d = d[d.index <= asof]
        if len(d) < 30:
            continue
        name = meta.loc[sym, "name"] if sym in meta.index else ""
        print("\n" + "=" * 70)
        print(f"{sym} {name}  資料日 {d.index[-1]:%Y-%m-%d}  收盤 {d['Close'].iloc[-1]:.2f}  K棒 {len(d)}")
        to = (d["Close"] * d["Volume"]).tail(cfg["turnover_days"]).mean()
        print(f"20日均成交額 {to / 1e8:.2f} 億  {'✓' if to >= cfg['min_avg_turnover'] else '✗'}流動性")
        rs = rs_table.loc[sym, "RS"] if rs_table is not None and sym in rs_table.index else None
        print(f"RS 加權報酬 {rs_raw(d['Close'].values):.1%}  RS百分位 "
              + (f"{rs:.0f}" if rs is not None and rs == rs else "N/A（先用相同 --asof 跑一次全市場）"))
        tt = trend_template(d, cfg)
        if tt:
            print("趨勢模板：")
            for k, v in tt["crit"].items():
                print(f"   {'✓' if v else '✗'} {k}")
            if rs is not None and rs == rs:
                print(f"   {'✓' if rs > cfg['tt_min_rs'] else '✗'} 8 RS>{cfg['tt_min_rs']}")
            print(f"   距52週高 {tt['below_52w_high']:.1%}｜高於52週低 {tt['above_52w_low']:.0%}"
                  f"｜200MA 連續上升 {tt['ma200_rising_streak']} 天")
        else:
            it = ipo_template(d, cfg)
            print("新股條件：")
            for k, v in it["crit"].items():
                print(f"   {'✓' if v else '✗'} {k}")
        market = meta.loc[sym, "market"] if sym in meta.index else "上市"
        rec = {"RS": rs if rs is not None and rs == rs else float("nan"), "track": "一般" if tt else "新股", "market": market}
        for pat, fn in DETECTORS.items():
            r = fn(d, cfg)
            if r["ok"]:
                ck = checklist(d, rec, pat, r, cfg, indexes.get(INDEXES.get(market, "^TWII")))
                print(f"{pat}: 型態成立  pivot {r['pivot']:.2f}｜{r['detail']}")
                print(f"   必要條件：" + "　".join(f"{'✓' if b else '✗'}{k}（{t}）" for k, (b, t) in ck["req"].items()))
                print(f"   加分 {ck['bonus_n']}/{ck['bonus_max']}：" + "　".join(f"{_mark(b)}{k}（{t}）" for k, (b, t) in ck["bonus"].items()))
                print(f"   日振幅 近10日 {ck['range10']:.1%}／50日 {ck['range50']:.1%}")
            else:
                print(f"{pat}: ✗ {r['reason']}")


# =============================================================================
def main():
    ap = argparse.ArgumentParser(description="台股動能篩選：趨勢模板 + VCP/COH/HTF")
    ap.add_argument("--asof", help="以某日收盤為準掃描，格式 YYYY-MM-DD")
    ap.add_argument("--check", nargs="+", help="單檔診斷，例如 --check 2330 3661")
    ap.add_argument("--no-cache", action="store_true", help="強制重新下載價格")
    ap.add_argument("--no-charts", action="store_true", help="不畫圖")
    args = ap.parse_args()
    cfg = CONFIG
    Path(cfg["cache_dir"]).mkdir(parents=True, exist_ok=True)

    today = pd.Timestamp.today().normalize()
    asof = pd.Timestamp(args.asof) if args.asof else today
    end = asof + pd.Timedelta(days=1)
    start = end - pd.Timedelta(days=int(365 * cfg["years_of_history"]))
    meta = load_ticker_list(cfg)

    if args.check:
        run_check(args.check, meta, cfg, asof, start, end, not args.no_cache)
        return

    data = load_or_download(list(meta.index) + list(INDEXES.values()), start, end, cfg, use_cache=not args.no_cache)
    if not data:
        raise SystemExit("沒有取得任何價格資料")
    indexes = split_indexes(data)
    last_day = max(d.index[-1] for d in data.values())
    asof_eff = min(asof, last_day)
    results, base, trimmed = analyze(data, meta, cfg, asof_eff, indexes)
    n_tt = sum(r["track"] == "一般" for r in results)
    print(f"[分析] 資料日 {asof_eff:%Y-%m-%d}｜掃描 {len(base)} 檔｜趨勢模板通過 {n_tt}｜新股通過 {len(results) - n_tt}")
    write_outputs(results, trimmed, cfg, asof_eff, len(base), make_charts=not args.no_charts)
    try:                                              # 抗跌股（2026/11 新增）：失敗不影響型態篩選
        import pullback as PBM
        hist = PBM.fetch_index_hist(cfg["cache_dir"], asof_eff)
        pb = PBM.analyze_pullbacks(data, meta, cfg, asof_eff, hist, Path(cfg["output_dir"]) / "site" / "pullback.json")
        L = pb["legs"][-1]
        print(f"[輸出] 抗跌股 output/site/pullback.json（最近回檔 {L['peak']}～{L['end']} 加權 {L['twse']:.1%}，{len(pb['stocks'])} 檔）")
    except Exception as e:
        print(f"[抗跌股] 失敗：{e!r}")


if __name__ == "__main__":
    main()
