# -*- coding: utf-8 -*-
"""
冒险岛怀旧服金价抓取器
数据源: https://mxdc.dvg.cn/tools/price-trend/ (源自 G买卖)

流程:
  1. GET 页面 -> 拿到 PHP session cookie + PRICE_TREND_TOKEN
  2. 带 cookie + token 请求 index.php?action=data
  3. 保存 data.json 供看板读取, 同时追加 CSV 历史记录

用法:
  python fetcher.py             # 抓一次
  python fetcher.py --loop 300  # 每 300 秒循环抓取
  python fetcher.py --hourly    # 对齐整点: 每小时第 2 分钟抓一次(数据源每小时整点更新)
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.request
import http.cookiejar
from datetime import datetime, timezone, timedelta

BASE = "https://mxdc.dvg.cn/tools/price-trend/"
PAGE_URL = BASE
API_URL = BASE + "index.php"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_JSON = os.path.join(HERE, "data.json")
DATA_CSV = os.path.join(HERE, "price_history.csv")
ITEMS_JSON = os.path.join(HERE, "items.json")
ITEMS_CSV = os.path.join(HERE, "item_history.csv")
AUTH_STATE = os.path.join(HERE, "auth_state.json")

# 监控的道具 (id, 名称)
WATCH_ITEMS = [
    ("5152053", "皇家整容券"),
    ("5151036", "万能高级染发卡"),
    ("5150040", "皇家理发券"),
]
ITEM_API = "https://mxdc.dvg.cn/api/item-auction-market.php?id=%s"

CST = timezone(timedelta(hours=8))


def make_opener():
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def get(opener, url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with opener.open(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def fetch_once():
    opener = make_opener()
    # 1. 抓页面: 拿 token (cookie 自动存入 opener)
    html = get(opener, PAGE_URL, {"Referer": BASE})
    m = re.search(r'PRICE_TREND_TOKEN\s*=\s*"([^"]+)"', html)
    if not m:
        raise RuntimeError("页面中未找到 PRICE_TREND_TOKEN, 页面结构可能已变化")
    token = m.group(1)

    # 2. 请求 JSON 数据接口
    url = "%s?action=data&v=%d" % (API_URL, int(time.time() * 1000))
    data = get(opener, url, {
        "X-Requested-With": "XMLHttpRequest",
        "X-Price-Trend-Token": token,
        "Referer": PAGE_URL,
        "Accept": "application/json",
    })
    obj = json.loads(data)
    if not obj.get("ok"):
        raise RuntimeError("接口返回失败: %s" % data[:200])
    return obj


def csv_last_col(path):
    """读取 CSV 最后一行的第一列(时间戳), 用于按快照去重"""
    try:
        with open(path, encoding="utf-8-sig") as f:
            rows = [r for r in csv.reader(f) if r]
        return rows[-1][0] if len(rows) > 1 else None
    except FileNotFoundError:
        return None


def save(obj):
    latest = obj.get("latest", {})
    collected = latest.get("collected_at", "")
    items = latest.get("items", [])

    with open(DATA_JSON, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)

    # 去重: 同一小时快照已入库则不重复追加 (云端定时 20 分钟一次, 需要防重)
    if csv_last_col(DATA_CSV) != collected:
        exists = os.path.exists(DATA_CSV)
        with open(DATA_CSV, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(["collected_at", "area", "yuan_to_wan_gold",
                            "wan_gold_to_yuan", "price_yuan", "stock"])
            for it in items:
                q = it.get("quote", {})
                w.writerow([collected, it.get("area_name", ""),
                            q.get("yuan_to_wan_gold", ""),
                            q.get("wan_gold_to_yuan", ""),
                            q.get("price_yuan", ""),
                            q.get("stock", "")])
    return collected, items, True


def seconds_until_next_fetch(offset_min=2):
    """距离下一个整点后 offset_min 分钟的秒数"""
    now = datetime.now()
    target = now.replace(minute=offset_min, second=10, microsecond=0)
    if target <= now:
        target += timedelta(hours=1)
    return (target - now).total_seconds()


def auth_cookie_header():
    """从 playwright 保存的登录态中提取 Cookie 头; 未登录返回空"""
    if not os.path.exists(AUTH_STATE):
        return ""
    try:
        state = json.load(open(AUTH_STATE, encoding="utf-8"))
        return "; ".join("%s=%s" % (c["name"], c["value"]) for c in state.get("cookies", []))
    except Exception:
        return ""


def fetch_items(cookie_header):
    """抓取监控道具的各区服最低金币价"""
    if not cookie_header:
        raise RuntimeError("无登录态, 请运行 login_save.py 扫码登录")
    out_items = []
    for item_id, item_name in WATCH_ITEMS:
        req = urllib.request.Request(ITEM_API % item_id, headers={
            "User-Agent": UA,
            "Cookie": cookie_header,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://mxdc.dvg.cn/item_info.php?id=%s" % item_id,
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            obj = json.loads(resp.read().decode("utf-8", "replace"))
        if not obj.get("ok"):
            raise RuntimeError("道具 %s 接口失败(%s): 登录态可能已失效, 请重新运行 login_save.py"
                               % (item_id, obj.get("code", "?")))
        for m in obj.get("markets", []):
            out_items.append({
                "item_id": item_id, "item_name": item_name,
                "server_id": m.get("server_id"), "server_name": m.get("server_name"),
                "lowest_price": m.get("lowest_price"), "rmb_value": m.get("rmb_value"),
                "yuan_to_wan_gold": m.get("yuan_to_wan_gold"),
                "observed_at": m.get("observed_at"),
            })
    collected = max((it["observed_at"] or "" for it in out_items), default="")
    return {"ok": True, "collected_at": collected, "count": len(out_items), "items": out_items}


def save_items(items_obj):
    with open(ITEMS_JSON, "w", encoding="utf-8") as f:
        json.dump(items_obj, f, ensure_ascii=False)
    ts = items_obj["collected_at"]
    last = csv_last_col(ITEMS_CSV)
    # 去重: 同一小时内已有入库则跳过 (items.json 仍会更新为最新)
    if last and ts and last[:13] == ts[:13]:
        return
    exists = os.path.exists(ITEMS_CSV)
    with open(ITEMS_CSV, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["collected_at", "item_id", "item_name", "server",
                        "lowest_price", "rmb_value"])
        for it in items_obj["items"]:
            w.writerow([ts, it["item_id"], it["item_name"], it["server_name"],
                        it["lowest_price"], it["rmb_value"]])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="循环间隔秒数, 0=只抓一次")
    ap.add_argument("--hourly", action="store_true",
                    help="对齐整点抓取: 每小时第 2 分钟各抓一次(数据源每小时整点更新)")
    args = ap.parse_args()

    while True:
        cookie_header = auth_cookie_header()
        try:
            obj = fetch_once()
            collected, items, _ = save(obj)
            ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
            print("[%s] OK 金价 采集时间=%s 区服数=%d" % (ts, collected, len(items)))
        except Exception as e:
            ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
            print("[%s] FAIL 金价 %s" % (ts, e), file=sys.stderr)
        try:
            items_obj = fetch_items(cookie_header)
            save_items(items_obj)
            ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
            print("[%s] OK 道具物价 %d 条" % (ts, items_obj["count"]))
        except Exception as e:
            ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
            print("[%s] FAIL 道具物价 %s" % (ts, e), file=sys.stderr)
        if args.hourly:
            wait = seconds_until_next_fetch()
            print("下次抓取: %d 秒后 (整点+%d分)" % (wait, 2))
            time.sleep(wait)
        elif args.loop:
            time.sleep(args.loop)
        else:
            break


if __name__ == "__main__":
    main()
