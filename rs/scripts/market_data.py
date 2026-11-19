#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
market_data.py — 台股全市場資料層（v3，免付費）

資料來源（全部免費、官方為主）：
  - 每日收盤：證交所 MI_INDEX（上市）＋ 櫃買中心 otc（上櫃），逐日全市場
  - 除權息：證交所 TWT49U、櫃買 exDailyQ（官方「除權除息計算結果表」）
  - 減資：證交所 TWTAUU、櫃買 revivt（官方「減資恢復買賣參考價」）
  - 面額變更／分割：FinMind TaiwanStockSplitPrice（免費等級可用）
  - 產業分類：FinMind TaiwanStockInfo（免費等級可用）
  - 發行股數：證交所 openapi t187ap03_L、櫃買每日行情「發行股數」欄

還原股價：以官方「除權息前收盤價 → 參考價」比例向前調整（與一般看盤軟體的還原日線相同概念），
最新一天的還原價 = 實際收盤價。

快取：scripts/.cache/daily/YYYYMMDD.json.gz（每個交易日一檔，原始資料永不變動）
      → 增量更新：每天只補抓新的交易日，第一次建置約 15–20 分鐘，之後每天數秒。
"""

import os
import re
import sys
import json
import gzip
import time
from datetime import datetime, timedelta, date

import requests
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, ".cache")
DAILY_DIR = os.path.join(CACHE_DIR, "daily")
HOLIDAY_FILE = os.path.join(CACHE_DIR, "holidays.json")
EVENTS_FILE = os.path.join(CACHE_DIR, "events.json")

HISTORY_DAYS = 440          # 抓取的日曆天數（約 300 個交易日，足夠 12 個月報酬＋200MA 斜率）
TWSE_GAP = 3.5              # 證交所兩次請求最少間隔（秒），太快會被防火牆暫時封鎖
TPEX_GAP = 1.0

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

_last_call = {"twse": 0.0, "tpex": 0.0}


def log(msg):
    print(f"[資料] {msg}", file=sys.stderr, flush=True)


# ============================================================
# HTTP helpers
# ============================================================

def _throttle(host):
    gap = TWSE_GAP if host == "twse" else TPEX_GAP
    wait = _last_call[host] + gap - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.time()


def _get_json(url, params=None, host="twse", referer=None, tries=4):
    headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    if referer:
        headers["Referer"] = referer
    last_err = None
    for attempt in range(tries):
        if host in _last_call:
            _throttle(host)
        try:
            r = requests.get(url, params=params, headers=headers, timeout=60)
            txt = r.text.strip()
            if txt.startswith("<"):
                # 證交所防火牆頁面（請求太頻繁）→ 等久一點
                last_err = "回傳 HTML（防火牆擋下或網站維護）"
                time.sleep(30 * (attempt + 1))
                continue
            return r.json()
        except Exception as e:  # noqa
            last_err = str(e)
            time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"請求失敗：{url}（{last_err}）")


def _num(s):
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if s in ("", "--", "---", "----", "X", "除息", "除權", "除權息"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_date(s):
    """支援 115年09月16日 / 115/09/16 / 1150916 / 2026-09-16 / 20260916"""
    s = str(s).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    parts = re.findall(r"\d+", s)
    if len(parts) == 3:
        y, m, d = (int(p) for p in parts)
    elif len(parts) == 1 and len(parts[0]) in (7, 8):
        p = parts[0]
        y, m, d = (int(p[:-4]), int(p[-4:-2]), int(p[-2:]))
    else:
        return None
    if y < 1911:
        y += 1911
    return f"{y:04d}-{m:02d}-{d:02d}"


# ============================================================
# 每日行情
# ============================================================

def fetch_twse_day(ds):
    """ds=YYYYMMDD。回傳 {code: [close, chg, flag, money, volume, name]}；休市回傳 {}"""
    j = _get_json("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
                  {"date": ds, "type": "ALLBUT0999", "response": "json"},
                  host="twse", referer="https://www.twse.com.tw/zh/trading/historical/mi-index.html")
    if j.get("stat") != "OK":
        return {}
    for t in j.get("tables") or []:
        f = t.get("fields") or []
        if "證券代號" in f and "收盤價" in f:
            ic, iname, iclose = f.index("證券代號"), f.index("證券名稱"), f.index("收盤價")
            isign, ichg = f.index("漲跌(+/-)"), f.index("漲跌價差")
            imoney, ivol = f.index("成交金額"), f.index("成交股數")
            out = {}
            for row in t.get("data", []):
                code = str(row[ic]).strip()
                if not re.match(r"^\d{4}$", code):
                    continue
                close = _num(row[iclose])
                sign_raw = re.sub(r"<[^>]+>", "", str(row[isign])).strip()
                diff = _num(row[ichg])
                if sign_raw == "X":
                    chg, flag = None, "X"
                elif sign_raw == "-":
                    chg, flag = (-diff if diff is not None else None), ""
                elif sign_raw == "+":
                    chg, flag = diff, ""
                else:
                    chg, flag = (0.0 if diff is not None else None), ""
                if close is None:
                    flag = "-"
                out[code] = [close, chg, flag, _num(row[imoney]) or 0.0,
                             _num(row[ivol]) or 0.0, str(row[iname]).strip()]
            return out
    return {}


def fetch_tpex_day(ds):
    """回傳 {code: [close, chg, flag, money, volume, name, shares]}；休市或日期不符回傳 {}"""
    d = f"{ds[:4]}/{ds[4:6]}/{ds[6:]}"
    j = _get_json("https://www.tpex.org.tw/www/zh-tw/afterTrading/otc",
                  {"date": d, "type": "EW", "response": "json"}, host="tpex")
    for t in j.get("tables") or []:
        f = [str(x).strip() for x in (t.get("fields") or [])]
        if "代號" not in f:
            continue
        tdate = _parse_date(t.get("date", ""))
        if tdate != f"{ds[:4]}-{ds[4:6]}-{ds[6:]}":
            return {}
        ic, iname = f.index("代號"), f.index("名稱")
        iclose = next(i for i, x in enumerate(f) if x.startswith("收盤"))
        ichg = f.index("漲跌")
        imoney = next(i for i, x in enumerate(f) if "成交金額" in x)
        ivol = next(i for i, x in enumerate(f) if "成交股數" in x)
        ishares = next((i for i, x in enumerate(f) if "發行股數" in x), None)
        out = {}
        for row in t.get("data", []):
            code = str(row[ic]).strip()
            if not re.match(r"^\d{4}$", code):
                continue
            close = _num(row[iclose])
            raw_chg = str(row[ichg]).strip()
            if raw_chg in ("除息", "除權", "除權息") or "除" in raw_chg:
                chg, flag = None, "X"
            else:
                chg, flag = _num(raw_chg), ""
            if close is None:
                flag = "-"
            out[code] = [close, chg, flag, _num(row[imoney]) or 0.0, _num(row[ivol]) or 0.0,
                         str(row[iname]).strip(),
                         _num(row[ishares]) if ishares is not None else None]
        return out
    return {}


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def daily_path(ds):
    return os.path.join(DAILY_DIR, f"{ds}.json.gz")


def update_daily(today=None, max_seconds=None, newest_first=False):
    """增量補抓缺少的交易日。max_seconds：超過就先停（下次再接著抓）。回傳是否已完整。"""
    os.makedirs(DAILY_DIR, exist_ok=True)
    today = today or date.today()
    holidays = set(_load_json(HOLIDAY_FILE, []))
    start = today - timedelta(days=HISTORY_DAYS)
    todo = []
    d = start
    while d <= today:
        ds = d.strftime("%Y%m%d")
        if d.weekday() < 5 and ds not in holidays and not os.path.exists(daily_path(ds)):
            todo.append(d)
        d += timedelta(days=1)
    if not todo:
        return True
    if newest_first:
        todo.reverse()
    log(f"需補抓 {len(todo)} 個日期（上市＋上櫃）")
    t0 = time.time()
    now = datetime.now()
    for i, d in enumerate(todo):
        if max_seconds and time.time() - t0 > max_seconds:
            log(f"已達時間上限，先停在 {d}（再執行一次會接著抓）")
            return False
        ds = d.strftime("%Y%m%d")
        if i % 20 == 0:
            log(f"  進度 {i}/{len(todo)}（{ds}）")
        try:
            tw = fetch_twse_day(ds)
        except RuntimeError as e:
            log(f"  {ds} 抓取失敗，先停止（稍後再執行會接著抓）：{e}")
            return False
        if not tw:
            # 今天的收盤資料尚未公布（約 14:30 後才有）→ 不當成休市，下次再抓
            if d < today:
                holidays.add(ds)
                _save_json(HOLIDAY_FILE, sorted(holidays))
            continue
        try:
            tp = fetch_tpex_day(ds)
        except RuntimeError as e:
            log(f"  {ds} 上櫃抓取失敗：{e}")
            tp = {}
        if not tp:
            log(f"  {ds} 上櫃資料暫缺，下次再補")
            continue
        with gzip.open(daily_path(ds), "wt", encoding="utf-8") as f:
            json.dump({"twse": tw, "tpex": tp}, f, ensure_ascii=False)
    # 清掉過舊的檔案
    cutoff = (today - timedelta(days=HISTORY_DAYS + 10)).strftime("%Y%m%d")
    for fn in os.listdir(DAILY_DIR):
        if fn[:8] < cutoff:
            try:
                os.remove(os.path.join(DAILY_DIR, fn))
            except OSError:
                pass
    log("每日行情已是最新")
    return True


# ============================================================
# 除權息／減資／面額變更事件
# ============================================================

def _chunks(start, end, months=2):
    cur = start
    while cur <= end:
        nxt = min(end, (pd.Timestamp(cur) + pd.DateOffset(months=months) - pd.Timedelta(days=1)).date())
        yield cur, nxt
        cur = nxt + timedelta(days=1)


def _rows_from_table(fields, data, code_key, date_idx, before_kw, ref_kw):
    f = [str(x).strip() for x in fields]
    ic = next(i for i, x in enumerate(f) if code_key in x)
    ib = next(i for i, x in enumerate(f) if before_kw in x)
    ir = next(i for i, x in enumerate(f) if ref_kw in x)
    out = []
    for row in data:
        code = str(row[ic]).strip()
        dt = _parse_date(row[date_idx])
        b, r = _num(row[ib]), _num(row[ir])
        if re.match(r"^\d{4}$", code) and dt and b and r:
            out.append({"code": code, "date": dt, "before": b, "ref": r})
    return out


def fetch_events_range(start, end):
    ev = []
    for a, b in _chunks(start, end):
        sa, sb = a.strftime("%Y%m%d"), b.strftime("%Y%m%d")
        ra = f"{a.year - 1911}/{a:%m/%d}"
        rb = f"{b.year - 1911}/{b:%m/%d}"
        log(f"  事件 {a} ~ {b}")
        # 上市除權息
        j = _get_json("https://www.twse.com.tw/rwd/zh/exRight/TWT49U",
                      {"startDate": sa, "endDate": sb, "response": "json"}, host="twse",
                      referer="https://www.twse.com.tw/zh/exRight/TWT49U.html")
        if j.get("stat") == "OK":
            for e in _rows_from_table(j["fields"], j.get("data", []), "股票代號", 0, "前收盤", "參考價"):
                e["kind"] = "除權息"; ev.append(e)
        # 上市減資
        j = _get_json("https://www.twse.com.tw/rwd/zh/reducation/TWTAUU",
                      {"startDate": sa, "endDate": sb, "response": "json"}, host="twse",
                      referer="https://www.twse.com.tw/zh/reducation/TWTAUU.html")
        if j.get("stat") == "OK" and j.get("fields"):
            try:
                for e in _rows_from_table(j["fields"], j.get("data", []), "股票代號", 0, "收盤", "參考價"):
                    e["kind"] = "減資"; ev.append(e)
            except StopIteration:
                log(f"上市減資表欄位無法辨識：{j.get('fields')}")
        # 上櫃除權息
        j = _get_json("https://www.tpex.org.tw/web/stock/exright/dailyquo/exDailyQ_result.php",
                      {"l": "zh-tw", "d": ra, "ed": rb}, host="tpex")
        for t in j.get("tables") or []:
            if t.get("fields"):
                for e in _rows_from_table(t["fields"], t.get("data", []), "代號", 0, "前收盤", "除權息參考價"):
                    e["kind"] = "除權息"; ev.append(e)
        # 上櫃減資
        j = _get_json("https://www.tpex.org.tw/www/zh-tw/bulletin/revivt",
                      {"startDate": f"{a:%Y/%m/%d}", "endDate": f"{b:%Y/%m/%d}", "response": "json"}, host="tpex")
        for t in j.get("tables") or []:
            if t.get("fields"):
                for e in _rows_from_table(t["fields"], t.get("data", []), "股票代號", 0, "收盤", "開始日參考價"):
                    e["kind"] = "減資"; ev.append(e)
    # 面額變更／分割（FinMind 免費等級可查全市場）
    try:
        params = {"dataset": "TaiwanStockSplitPrice", "start_date": start.isoformat(), "end_date": end.isoformat()}
        tok = os.environ.get("FINMIND_TOKEN")
        if tok:
            params["token"] = tok
        r = requests.get(FINMIND_URL, params=params, timeout=60).json()
        for x in r.get("data", []):
            if re.match(r"^\d{4}$", str(x.get("stock_id"))) and x.get("before_price") and x.get("after_price"):
                ev.append({"code": x["stock_id"], "date": x["date"][:10], "before": float(x["before_price"]),
                           "ref": float(x["after_price"]), "kind": x.get("type") or "面額變更"})
    except Exception as e:  # noqa
        log(f"面額變更資料抓取失敗（略過）：{e}")
    return ev


def update_events(today=None, max_seconds=None):
    """分段抓取事件並逐段存檔（可中斷續抓）。回傳 (events, complete)"""
    today = today or date.today()
    store = _load_json(EVENTS_FILE, {"covered_to": None, "fetched_at": None, "events": []})
    start_all = today - timedelta(days=HISTORY_DAYS)
    last = store.get("fetched_at")
    if store.get("covered_to") == today.isoformat() and last and \
            datetime.now() - datetime.fromisoformat(last) < timedelta(hours=4):
        return store["events"], True
    if store.get("covered_to"):
        cursor = max(start_all, date.fromisoformat(store["covered_to"]) - timedelta(days=7))
    else:
        cursor = start_all
    t0 = time.time()
    keep = {(e["code"], e["date"]): e for e in store["events"] if e["date"] >= start_all.isoformat()}
    for a, b in _chunks(cursor, today):
        if max_seconds and time.time() - t0 > max_seconds:
            log("事件資料已達時間上限，下次接著抓")
            return sorted(keep.values(), key=lambda e: (e["date"], e["code"])), False
        for e in fetch_events_range(a, b):
            keep[(e["code"], e["date"])] = e
        store = {"covered_to": b.isoformat(), "fetched_at": datetime.now().isoformat(timespec="seconds"),
                 "events": sorted(keep.values(), key=lambda e: (e["date"], e["code"]))}
        _save_json(EVENTS_FILE, store)
    return store["events"], True


# ============================================================
# 產業分類、發行股數
# ============================================================

UMBRELLA = {"電子工業", "所有證券", "創新板股票", "創新版股票", "化學生技醫療"}
SYNONYM = {"綠能環保類": "綠能環保", "數位雲端類": "數位雲端", "居家生活類": "居家生活",
           "運動休閒類": "運動休閒", "其他電子類": "其他電子業", "觀光事業": "觀光餐旅",
           "金融業": "金融保險"}
NON_STOCK = {"ETF", "上櫃指數股票型基金(ETF)", "上櫃ETF", "ETN", "指數投資證券(ETN)",
             "Index", "大盤", "受益證券", "存託憑證"}


def fetch_stock_info():
    """回傳 DataFrame[code, name, market, industry]，僅上市櫃普通股（排除興櫃、ETF、ETN、DR、指數）"""
    path = os.path.join(CACHE_DIR, f"stock_info_{date.today():%Y-%m-%d}.json")
    rows = _load_json(path, None)
    if rows is None:
        params = {"dataset": "TaiwanStockInfo"}
        tok = os.environ.get("FINMIND_TOKEN")
        if tok:
            params["token"] = tok
        r = requests.get(FINMIND_URL, params=params, timeout=90).json()
        rows = r.get("data", [])
        if not rows:
            # 退回最近一次的快取
            olds = sorted(f for f in os.listdir(CACHE_DIR) if f.startswith("stock_info_") and f.endswith(".json"))
            if olds:
                rows = _load_json(os.path.join(CACHE_DIR, olds[-1]), [])
                log("產業分類抓取失敗，使用舊快取")
            else:
                raise RuntimeError(f"無法取得產業分類：{r.get('msg')}")
        else:
            for f in os.listdir(CACHE_DIR):
                if f.startswith("stock_info_"):
                    try:
                        os.remove(os.path.join(CACHE_DIR, f))
                    except OSError:
                        pass
            _save_json(path, rows)
    by = {}
    for x in rows:
        c = str(x.get("stock_id"))
        if not re.match(r"^[1-9]\d{3}$", c) or x.get("type") not in ("twse", "tpex"):
            continue
        by.setdefault(c, []).append(x)
    out = []
    for c, xs in by.items():
        xs.sort(key=lambda x: x.get("date") or "", reverse=True)
        cats = [SYNONYM.get(x["industry_category"], x["industry_category"]) for x in xs]
        if any(cat in NON_STOCK for cat in cats):
            continue
        spec = [cat for cat in cats if cat not in UMBRELLA]
        ind = spec[0] if spec else cats[0]
        out.append({"code": c, "name": xs[0]["stock_name"], "market": xs[0]["type"], "industry": ind})
    return pd.DataFrame(out).set_index("code")


def fetch_twse_shares():
    path = os.path.join(CACHE_DIR, f"twse_shares_{date.today():%Y-%m-%d}.json")
    got = _load_json(path, None)
    if got is not None:
        return got
    out = {}
    try:
        r = requests.get("https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
                         headers={"User-Agent": UA}, timeout=60)
        for row in r.json():
            n = _num(row.get("已發行普通股數或TDR原股發行股數"))
            if n:
                out[str(row.get("公司代號")).strip()] = n
    except Exception as e:  # noqa
        log(f"上市發行股數抓取失敗（市值欄位會缺）：{e}")
    if out:
        for f in os.listdir(CACHE_DIR):
            if f.startswith("twse_shares_"):
                try:
                    os.remove(os.path.join(CACHE_DIR, f))
                except OSError:
                    pass
        _save_json(path, out)
    return out


# ============================================================
# 組裝：原始價 → 還原價
# ============================================================

def load_daily():
    """回傳 dict：dates(list of Timestamp), close/chg/money(DataFrame), flag(DataFrame), names, shares_tpex"""
    files = sorted(f for f in os.listdir(DAILY_DIR) if f.endswith(".json.gz"))
    if not files:
        raise RuntimeError("沒有任何每日行情快取，請先執行更新（不加 --cached）")
    close, chg, money, flag = {}, {}, {}, {}
    names, shares_tpex, market = {}, {}, {}
    for fn in files:
        dt = pd.Timestamp(datetime.strptime(fn[:8], "%Y%m%d"))
        with gzip.open(os.path.join(DAILY_DIR, fn), "rt", encoding="utf-8") as f:
            day = json.load(f)
        c_, g_, m_, f_ = {}, {}, {}, {}
        for mk in ("twse", "tpex"):
            for code, row in day.get(mk, {}).items():
                c_[code], g_[code], f_[code], m_[code] = row[0], row[1], row[2], row[3]
                names[code] = row[5]
                market[code] = mk
                if mk == "tpex" and len(row) > 6 and row[6]:
                    shares_tpex[code] = row[6]
        close[dt], chg[dt], money[dt], flag[dt] = c_, g_, m_, f_
    close = pd.DataFrame.from_dict(close, orient="index").sort_index().astype(float)
    chg = pd.DataFrame.from_dict(chg, orient="index").reindex(index=close.index, columns=close.columns).astype(float)
    money = pd.DataFrame.from_dict(money, orient="index").reindex(index=close.index, columns=close.columns).astype(float)
    flag = pd.DataFrame.from_dict(flag, orient="index").reindex(index=close.index, columns=close.columns)
    return {"close": close, "chg": chg, "money": money, "flag": flag,
            "names": names, "shares_tpex": shares_tpex, "market": market}


def build_adjusted(raw, events):
    """
    回傳 (adj_close DataFrame, report dict)
    1. 官方事件：factor = 參考價 / 事件前一個交易日收盤
    2. 安全網：非事件日若「收盤 − 漲跌」≠ 前收盤（>0.3%），以官方隱含參考價推算 factor
    3. 標記 X（不比價）但查無事件的日子 → 列入 warnings
    """
    close = raw["close"]
    chg = raw["chg"]
    flag = raw["flag"]
    dates = close.index
    prev_close = close.ffill().shift(1)

    factors = {}   # (code, Timestamp) -> factor
    used = []
    ev_idx = {}
    for e in events:
        ev_idx[(e["code"], pd.Timestamp(e["date"]))] = e
    for (code, dt), e in ev_idx.items():
        if code not in close.columns or dt <= dates[0] or dt > dates[-1]:
            continue
        # 事件日若遇休市，落在下一個交易日
        pos = dates.searchsorted(dt)
        if pos >= len(dates):
            continue
        d = dates[pos]
        pc = prev_close.at[d, code]
        base = pc if pd.notna(pc) and pc > 0 else e["before"]
        f = e["ref"] / base
        if 0.02 < f < 50 and abs(f - 1) > 1e-6:
            factors[(code, d)] = factors.get((code, d), 1.0) * f
            used.append({**e, "factor": round(f, 6)})

    # 安全網：漲跌與前收盤對不起來
    implied = close - chg
    gap = (implied / prev_close - 1).abs()
    # 只在「前一交易日有成交」時比對：櫃買對前一日無成交的股票，漲跌是相對另一個參考價
    mask = (gap > 0.003) & ((implied - prev_close).abs() > 0.011) & close.notna() & \
        close.shift(1).notna() & chg.notna()
    inferred = []
    for d, code in zip(*np.where(mask.values)):
        dt, c = dates[d], close.columns[code]
        if (c, dt) in factors:
            continue
        f = implied.iat[d, code] / prev_close.iat[d, code]
        if 0.02 < f < 50:
            factors[(c, dt)] = f
            inferred.append({"code": c, "date": dt.strftime("%Y-%m-%d"), "factor": round(float(f), 6)})

    # X 但無事件
    warnings = []
    xs = (flag == "X")
    for d, code in zip(*np.where(xs.values)):
        dt, c = dates[d], close.columns[code]
        if (c, dt) not in factors and pd.notna(close.iat[d, code]):
            pc = prev_close.iat[d, code]
            if pd.notna(pc) and abs(close.iat[d, code] / pc - 1) > 0.12:
                warnings.append({"code": c, "date": dt.strftime("%Y-%m-%d"),
                                 "note": "不比價日但查無除權息/減資資料，且價格跳動 >12%"})

    adj = close.copy()
    by_code = {}
    for (c, dt), f in factors.items():
        by_code.setdefault(c, []).append((dt, f))
    for c, lst in by_code.items():
        col = adj[c].values.copy()
        for dt, f in lst:
            pos = dates.searchsorted(dt)
            col[:pos] = col[:pos] * f
        adj[c] = col
    report = {"events_used": len(used), "inferred": inferred, "warnings": warnings}
    return adj, report


def refresh(max_seconds=None):
    """抓最新資料（增量、可中斷續抓）。回傳是否完整。"""
    t0 = time.time()
    ok = update_daily(max_seconds=max_seconds)
    remain = None if max_seconds is None else max(5, max_seconds - (time.time() - t0))
    _, ok2 = update_events(max_seconds=remain)
    return ok and ok2


def load_events():
    return _load_json(EVENTS_FILE, {"events": []})["events"]


if __name__ == "__main__":
    # python3 market_data.py [--max-seconds N]  → 只做資料更新
    ms = None
    if "--max-seconds" in sys.argv:
        ms = float(sys.argv[sys.argv.index("--max-seconds") + 1])
    if "--prices-only" in sys.argv:
        done = update_daily(max_seconds=ms, newest_first="--newest-first" in sys.argv)
    elif "--events-only" in sys.argv:
        done = update_events(max_seconds=ms)[1]
    else:
        done = refresh(max_seconds=ms)
    print(json.dumps({"complete": done}, ensure_ascii=False))
