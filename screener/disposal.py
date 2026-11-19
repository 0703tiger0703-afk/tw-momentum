#!/usr/bin/env python3
"""抓證交所／櫃買中心的注意股與處置股公告，算出每檔的處置累計，輸出 output/site/disposal.json。
只用標準函式庫，Python 3.8+。用法：python3 disposal.py [輸出路徑]"""
import json, re, sys, time, datetime as dt, urllib.request, urllib.parse, os

UA = {"User-Agent": "Mozilla/5.0 (Macintosh) tw-screener"}
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "site", "disposal.json")
CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
CODE_RE = re.compile(r"^\d{4}[A-Z]?$")


def get(url, tries=4):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if k == tries - 1:
                raise
            time.sleep(4 + 4 * k)


def roc(s):
    s = re.sub(r"[^\d/.]", "", str(s)).replace(".", "/")
    y, m, d = (int(x) for x in s.split("/")[:3])
    return dt.date(y + 1911, m, d)


def kuan(text):
    out = set()
    for m in re.finditer(r"第([一二三四五六七八九十]+)款", text):
        t = m.group(1)
        if "十" in t:
            a, _, b = t.partition("十")
            n = (CN.get(a, 1) if a else 1) * 10 + (CN.get(b, 0) if b else 0)
        else:
            n = CN.get(t, 0)
        out.add(n)
    return sorted(out)


def clean(s):
    s = re.sub(r"<br\s*/?>", " ", str(s))
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\([^)]*\.html[^)]*\)", "", s)
    return re.sub(r"\s+", " ", s).strip()


def trading_days(today):
    days = set()
    for back in (0, 1, 2, 3):
        y, m = today.year, today.month - back
        while m <= 0:
            m += 12; y -= 1
        d = get(f"https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?date={y}{m:02d}01&response=json")
        for r in d.get("data", []):
            days.add(roc(r[0]))
        time.sleep(1.5)
    return sorted(days)


def main():
    today = dt.date.today()
    tdays = trading_days(today)
    as_of = tdays[-1]
    t30 = tdays[-30:]
    n_start = tdays[-45]
    p_start = as_of - dt.timedelta(days=100)
    f = lambda d: d.strftime("%Y%m%d")
    g = lambda d: d.strftime("%Y/%m/%d")

    S = {}  # code -> {...}

    def rec(code, name, mkt):
        r = S.setdefault(code, {"name": name, "m": mkt, "notes": {}, "disp": []})
        return r

    # ---- 上市注意 ----
    d = get(f"https://www.twse.com.tw/rwd/zh/announcement/notice?querytype=1&stockNo=&selectType=&startDate={f(n_start)}&endDate={f(today)}&sortKind=STKNO&response=json")
    for r in d.get("data", []):
        code = str(r[1]).strip()
        if not CODE_RE.match(code):
            continue
        day = roc(r[5]); txt = clean(r[4])
        if day > as_of: continue
        x = rec(code, str(r[2]).strip(), "twse")["notes"].setdefault(day, [set(), []])
        x[0].update(kuan(txt)); x[1].append(txt)
    time.sleep(2)
    # ---- 上市處置 ----
    d = get(f"https://www.twse.com.tw/rwd/zh/announcement/punish?startDate={f(p_start)}&endDate={f(today)}&response=json")
    for r in d.get("data", []):
        code = str(r[2]).strip()
        if not CODE_RE.match(code):
            continue
        a, b = [roc(x) for x in re.split(r"[～~]", str(r[6]))[:2]]
        rec(code, str(r[3]).strip(), "twse")["disp"].append({"pub": roc(r[1]), "start": a, "end": b, "why": clean(r[5]), "kind": clean(r[7]),
                                                               "how": clean(r[8]).split("３處置措施：")[-1][:400]})
    time.sleep(2)
    # ---- 上櫃注意 ----
    d = get(f"https://www.tpex.org.tw/www/zh-tw/bulletin/attention?startDate={g(n_start)}&endDate={g(today)}&response=json")
    for t in d.get("tables", []):
        for r in t.get("data", []):
            code = str(r[1]).strip()
            if not CODE_RE.match(code):
                continue
            day = roc(r[5]); txt = clean(r[4])
            if day > as_of: continue
            x = rec(code, str(r[2]).strip(), "tpex")["notes"].setdefault(day, [set(), []])
            x[0].update(kuan(txt)); x[1].append(txt)
    time.sleep(2)
    # ---- 上櫃處置 ----
    d = get(f"https://www.tpex.org.tw/www/zh-tw/bulletin/disposal?startDate={g(p_start)}&endDate={g(today)}&response=json")
    for t in d.get("tables", []):
        for r in t.get("data", []):
            code = str(r[2]).strip()
            if not CODE_RE.match(code):
                continue
            a, b = [roc(x) for x in re.split(r"[～~]", str(r[5]))[:2]]
            how = clean(r[8])
            how = how.split("處置措施：")[-1][:400] if "處置措施：" in how else ""
            rec(code, re.sub(r"\(.*", "", str(r[3])).strip(), "tpex")["disp"].append({"pub": roc(r[1]), "start": a, "end": b, "why": clean(r[6]), "kind": clean(r[7]), "how": how})

    out = {}
    for code, r in S.items():
        disp = sorted(r["disp"], key=lambda x: x["pub"])
        last_pub = max([x["pub"] for x in disp if x["pub"] <= as_of], default=None)
        eff = {day: k for day, (k, _) in r["notes"].items() if (last_pub is None or day > last_pub)}
        def has(day, first_only=False):
            k = eff.get(day)
            if not k:
                return False
            return 1 in k if first_only else any(1 <= n <= 8 for n in k)
        def streak(first_only):
            c = 0
            for day in reversed(tdays):
                if has(day, first_only):
                    c += 1
                else:
                    break
            return c
        c3, c5 = streak(True), streak(False)
        c10 = sum(has(day) for day in tdays[-10:])
        c30 = sum(has(day) for day in t30)
        cur = [x for x in disp if x["start"] <= as_of <= x["end"]]
        nxt = [x for x in disp if x["start"] > as_of]
        soon = c3 >= 2 or c5 >= 4 or c10 >= 5 or c30 >= 11
        if nxt:
            st = "next"
        elif cur:
            st = "disp"
        elif soon:
            st = "soon"
        elif as_of in r["notes"]:
            st = "notice"
        elif c5 or c10 or c30:
            st = "watch"
        else:
            st = "ok"
        notes = [[day.isoformat(), sorted(k), " ／ ".join(t), (last_pub is None or day > last_pub)] for day, (k, t) in sorted(r["notes"].items(), reverse=True) if day >= t30[0]]
        disp_out = [{"pub": x["pub"].isoformat(), "start": x["start"].isoformat(), "end": x["end"].isoformat(), "why": x["why"], "kind": x["kind"], "how": x["how"]}
                    for x in sorted(disp, key=lambda x: x["pub"], reverse=True)]
        if st == "ok" and not notes and not disp_out:
            continue
        out[code] = {"n": r["name"], "m": r["m"], "st": st, "c": [c3, c5, c10, c30], "notes": notes, "disp": disp_out}

    res = {"as_of": as_of.isoformat(), "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
           "tdays": [x.isoformat() for x in t30], "stocks": out}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, separators=(",", ":"))
    cnt = {}
    for v in out.values():
        cnt[v["st"]] = cnt.get(v["st"], 0) + 1
    print(json.dumps({"ok": True, "as_of": res["as_of"], "stocks": len(out), "status": cnt}, ensure_ascii=False))


if __name__ == "__main__":
    main()
