# main.py
# ==========================================================
# 2026 B站动态监控 V7 终极版（仅动态监控版）
# 功能：
# 1. 多UID动态监控
# 2. 自动获取关注列表
# 3. 抗352 / -799 风控
# 4. Cookie失效自动重登
# 5. 断网自动恢复
# 6. 多人同时发动态不漏推送
# 7. 低频稳定轮询
# ==========================================================

import sys
import os
import time
import json
import random
import logging
import traceback
import hashlib
import urllib.parse
import subprocess
import requests

import notifier

# ==========================================================
# 配置区
# ==========================================================

SOURCE_UID = 3706948578969654     # 获取关注列表来源UID

FALLBACK_DYNAMIC_UIDS = [
    3546905852250875,
    3546961271589219,
    3546610447419885,
    285340365,
    3706948578969654
]

CHECK_INTERVAL = 15              # 动态总扫描间隔
FOLLOW_REFRESH_INTERVAL = 3600   # 关注列表刷新
UID_SLEEP_MIN = 0.8
UID_SLEEP_MAX = 1.6

TIME_OFFSET = -120              # 时间补偿

LOG_FILE = "bili_monitor.log"
STATE_FILE = "dynamic_state.json"
FOLLOW_FILE = "following_cache.json"

# ==========================================================
# 日志
# ==========================================================

def init_logging():
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        encoding="utf-8",
        filemode="w"
    )
    logging.info("=" * 60)
    logging.info("B站动态监控 V7 启动")
    logging.info("=" * 60)

# ==========================================================
# Cookie
# ==========================================================

def refresh_cookie():
    logging.warning("Cookie失效，尝试重新登录")
    try:
        subprocess.run(
            [sys.executable, "login_bilibili.py"],
            check=True
        )
        logging.info("Cookie刷新成功")
        return True
    except Exception as e:
        logging.error(f"Cookie刷新失败: {e}")
        return False

def get_header():
    if not os.path.exists("bili_cookie.txt"):
        refresh_cookie()

    with open("bili_cookie.txt", "r", encoding="utf-8") as f:
        cookie = f.read().strip()

    ua_list = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Mozilla/5.0 (X11; Linux x86_64)"
    ]

    return {
        "Cookie": cookie,
        "Referer": "https://www.bilibili.com/",
        "User-Agent": random.choice(ua_list),
        "Connection": "close"
    }

# ==========================================================
# 请求层
# ==========================================================

def safe_request(url, params=None, header=None, retry=3):
    for i in range(retry):
        try:
            r = requests.get(
                url,
                params=params,
                headers=header,
                timeout=10
            )

            if not r.text.strip():
                time.sleep(2)
                continue

            data = r.json()
            code = data.get("code", -999)

            if code == 0:
                return data

            if code == -101:
                if refresh_cookie():
                    return safe_request(url, params, get_header(), retry)
                return data

            if code in (-352, -799, -509):
                wait = (2 ** i) + random.uniform(1, 3)
                logging.warning(f"触发风控 {code}，等待 {wait:.1f}s")
                time.sleep(wait)
                continue

            return data

        except Exception as e:
            logging.error(f"请求异常: {e}")
            time.sleep(2)

    return {"code": -500}

# ==========================================================
# WBI
# ==========================================================

WBI = {
    "img": "",
    "sub": "",
    "time": 0
}

mixinKeyEncTab = [
46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,
27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,
37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,
22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52
]

def mixin(orig):
    return ''.join([orig[i] for i in mixinKeyEncTab])[:32]

def update_wbi(header):
    data = safe_request(
        "https://api.bilibili.com/x/web-interface/nav",
        header=header
    )

    if data.get("code") == 0:
        img = data["data"]["wbi_img"]
        WBI["img"] = img["img_url"].split("/")[-1].split(".")[0]
        WBI["sub"] = img["sub_url"].split("/")[-1].split(".")[0]
        WBI["time"] = time.time()

def sign(params):
    if time.time() - WBI["time"] > 21600:
        update_wbi(get_header())

    key = mixin(WBI["img"] + WBI["sub"])

    params["wts"] = int(time.time() + TIME_OFFSET)
    params = dict(sorted(params.items()))

    query = urllib.parse.urlencode(params)
    w_rid = hashlib.md5((query + key).encode()).hexdigest()

    params["w_rid"] = w_rid
    return params

def wbi_request(url, params, header):
    return safe_request(url, sign(params), header)

# ==========================================================
# 文件缓存
# ==========================================================

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return default
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ==========================================================
# 获取关注列表
# ==========================================================

def get_followings(uid, header):
    result = []
    pn = 1

    while True:
        data = safe_request(
            "https://api.bilibili.com/x/relation/followings",
            {
                "vmid": uid,
                "pn": pn,
                "ps": 50
            },
            header
        )

        if data.get("code") != 0:
            break

        items = data["data"].get("list", [])

        if not items:
            break

        for x in items:
            mid = x.get("mid")
            if mid:
                result.append(mid)

        pn += 1
        time.sleep(1)

    return result

# ==========================================================
# 动态接口
# ==========================================================

def fetch_dynamic(uid, header):
    return wbi_request(
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all",
        {
            "host_mid": uid,
            "type": "all",
            "offset": ""
        },
        header
    )

def get_text(item):
    try:
        mod = item["modules"]["module_dynamic"]

        desc = mod.get("desc", {})
        nodes = desc.get("rich_text_nodes", [])

        text = "".join(
            x.get("text", "")
            for x in nodes
            if isinstance(x, dict)
        ).strip()

        if text:
            return text

        major = mod.get("major", {})
        if major.get("type") == "MAJOR_TYPE_ARCHIVE":
            return "发布了视频"

        return "发布了新动态"

    except:
        return "发布了新动态"

# ==========================================================
# 主监控
# ==========================================================

def start():
    header = get_header()
    update_wbi(header)

    following = load_json(FOLLOW_FILE, [])

    if not following:
        following = get_followings(SOURCE_UID, header)

    if not following:
        following = FALLBACK_DYNAMIC_UIDS

    if SOURCE_UID not in following:
        following.append(SOURCE_UID)

    save_json(FOLLOW_FILE, following)

    seen = load_json(STATE_FILE, {})

    for uid in following:
        seen.setdefault(str(uid), [])

    last_refresh = 0

    logging.info(f"开始监控 {len(following)} 个UID")

    while True:
        try:
            now = time.time()

            # 刷新关注列表
            if now - last_refresh > FOLLOW_REFRESH_INTERVAL:
                new_list = get_followings(SOURCE_UID, header)
                if new_list:
                    following = list(set(new_list + [SOURCE_UID]))
                    save_json(FOLLOW_FILE, following)
                last_refresh = now

            # 随机顺序防风控
            uid_list = following[:]
            random.shuffle(uid_list)

            alerts = []

            for uid in uid_list:
                data = fetch_dynamic(uid, header)

                if data.get("code") != 0:
                    continue

                items = data.get("data", {}).get("items", [])

                if not items:
                    continue

                uid_key = str(uid)
                cache = set(seen.get(uid_key, []))

                for item in reversed(items):
                    did = item.get("id_str")

                    if not did:
                        continue

                    if did in cache:
                        continue

                    cache.add(did)

                    author = item["modules"]["module_author"]["name"]

                    msg = get_text(item)

                    alerts.append({
                        "user": author,
                        "message":
                            f"{msg}\n\n"
                            f"https://t.bilibili.com/{did}"
                    })

                seen[uid_key] = list(cache)[-50:]

                time.sleep(random.uniform(
                    UID_SLEEP_MIN,
                    UID_SLEEP_MAX
                ))

            if alerts:
                notifier.send_webhook_notification(
                    "💡 B站UP主发布新动态",
                    alerts
                )
                logging.info(f"推送 {len(alerts)} 条动态")

            save_json(STATE_FILE, seen)

            logging.info("心跳运行中")

            time.sleep(CHECK_INTERVAL)

        except Exception:
            logging.error(traceback.format_exc())
            time.sleep(30)

# ==========================================================
# 启动
# ==========================================================

if __name__ == "__main__":
    init_logging()
    start()
