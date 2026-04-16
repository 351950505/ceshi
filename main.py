import sys
import os
import time
import subprocess
import random
import logging
import traceback
import hashlib
import urllib.parse
import json
import requests

import database as db
import notifier

# ================= 核心配置 =================
TARGET_UID = 1671203508
VIDEO_CHECK_INTERVAL = 21600
HEARTBEAT_INTERVAL = 10

SOURCE_UID = 3706948578969654
FOLLOWING_REFRESH_INTERVAL = 3600

FALLBACK_DYNAMIC_UIDS = [
    3546905852250875,
    3546961271589219,
    3546610447419885,
    285340365,
    3706948578969654
]

DYNAMIC_CHECK_INTERVAL = 12  # 平滑轮询
DYNAMIC_MAX_AGE = 300

COMMENT_SCAN_INTERVAL = 5
COMMENT_MAX_PAGES = 3
COMMENT_SAFE_WINDOW = 60

TIME_OFFSET = -120

LOG_FILE = "bili_monitor.log"
DYNAMIC_STATE_FILE = "dynamic_state.json"
FOLLOWING_CACHE_FILE = "following_cache.json"
# =============================================


def init_logging():
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.truncate()
    except:
        pass

    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        encoding="utf-8",
        filemode="w"
    )
    logging.info("=" * 60)
    logging.info("B站监控系统启动（动态简化稳定版）")
    logging.info("=" * 60)


def refresh_cookie():
    logging.warning("Cookie 失效，重新登录...")
    try:
        subprocess.run([sys.executable, "login_bilibili.py"], check=True)
        return True
    except Exception as e:
        logging.error(f"登录失败: {e}")
        return False


def safe_request(url, params, header, retries=3):
    h = header.copy()
    h["Connection"] = "close"

    for i in range(retries):
        try:
            r = requests.get(url, headers=h, params=params, timeout=10)
            data = r.json()

            code = data.get("code")

            if code == -101:
                return {"code": -101, "need_refresh": True}

            if code in (-799, -352, -509):
                time.sleep(2 + i * 2 + random.uniform(0, 1))
                continue

            return data

        except Exception:
            time.sleep(2 + i * 2)

    return {"code": -500}


# ================= WBI =================
WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}

mixinKeyEncTab = [
    46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,
    37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52
]


def getMixinKey(orig):
    return ''.join([orig[i] for i in mixinKeyEncTab])[:32]


def encWbi(params, img_key, sub_key):
    mixin_key = getMixinKey(img_key + sub_key)
    params["wts"] = int(time.time() + TIME_OFFSET)

    params = dict(sorted(params.items()))
    for k in params:
        params[k] = str(params[k]).replace("!", "").replace("'", "").replace("(", "").replace(")", "").replace("*", "")

    query = urllib.parse.urlencode(params)
    sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
    params["w_rid"] = sign
    return params


def update_wbi_keys(header):
    data = safe_request("https://api.bilibili.com/x/web-interface/nav", None, header)
    if data.get("code") == 0:
        img = data["data"]["wbi_img"]
        WBI_KEYS["img_key"] = img["img_url"].split("/")[-1].split(".")[0]
        WBI_KEYS["sub_key"] = img["sub_url"].split("/")[-1].split(".")[0]
        WBI_KEYS["last_update"] = time.time()


def wbi_request(url, params, header):
    if time.time() - WBI_KEYS["last_update"] > 21600:
        update_wbi_keys(header)

    signed = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    return safe_request(url, signed, header)


# ================= 关注列表 =================
def get_following_list(uid, header):
    result = []
    pn = 1
    ps = 50

    while True:
        params = {
            "vmid": uid,
            "pn": pn,
            "ps": ps,
            "order": "desc"
        }

        data = safe_request(
            "https://api.bilibili.com/x/relation/followings",
            params,
            header
        )

        if data.get("code") != 0:
            break

        items = data.get("data", {}).get("list", [])
        if not items:
            break

        for i in items:
            result.append(i.get("mid"))

        if len(items) < ps:
            break

        pn += 1
        time.sleep(0.5)

    return result


def load_following_cache():
    if os.path.exists(FOLLOWING_CACHE_FILE):
        try:
            return json.load(open(FOLLOWING_CACHE_FILE, "r"))
        except:
            return []
    return []


def save_following_cache(data):
    json.dump(data, open(FOLLOWING_CACHE_FILE, "w"))


# ================= 动态（已简化） =================
def load_dynamic_state():
    if os.path.exists(DYNAMIC_STATE_FILE):
        try:
            return json.load(open(DYNAMIC_STATE_FILE, "r"))
        except:
            return {}
    return {}


def save_dynamic_state(state):
    json.dump(state, open(DYNAMIC_STATE_FILE, "w"), indent=2)


def extract_text(item):
    try:
        modules = item.get("modules", {})
        desc = modules.get("module_dynamic", {}).get("desc", {})
        nodes = desc.get("rich_text_nodes", [])

        return "".join(n.get("text", "") for n in nodes if isinstance(n, dict)).strip()
    except:
        return ""


def fetch_dynamic(uid, offset, header):
    params = {
        "host_mid": uid,
        "type": "all",
        "offset": offset,
        "platform": "web"
    }

    return wbi_request(
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all",
        params,
        header
    )


def check_dynamic(uid, header, seen, state):
    uid = str(uid)
    offset = state.get(uid, "")

    data = fetch_dynamic(uid, offset, header)
    if data.get("code") != 0:
        return [], offset

    feed = data.get("data", {})
    items = feed.get("items", [])
    new_offset = feed.get("offset", offset)

    alerts = []

    for it in items:
        dyn_id = it.get("id_str")
        if not dyn_id or dyn_id in seen[uid]:
            continue

        seen[uid].add(dyn_id)

        text = extract_text(it)
        msg = f"{text}\nhttps://t.bilibili.com/{dyn_id}"

        alerts.append({
            "user": uid,
            "message": msg
        })

    state[uid] = new_offset
    return alerts, new_offset


# ================= 评论（原样保留不动） =================
def scan_new_comments(oid, header, last_read_time, seen):
    new_list = []
    max_ctime = last_read_time
    safe_time = last_read_time - COMMENT_SAFE_WINDOW

    params = {"type": 1, "oid": oid}

    data = wbi_request(
        "https://api.bilibili.com/x/v2/reply/wbi/main",
        params,
        header
    )

    if data.get("code") != 0:
        return [], last_read_time

    replies = data.get("data", {}).get("replies", [])

    for r in replies:
        ctime = r.get("ctime", 0)
        if ctime > max_ctime:
            max_ctime = ctime

        if ctime > safe_time:
            rpid = r.get("rpid_str")
            if rpid and rpid not in seen:
                seen.add(rpid)
                new_list.append({
                    "user": r["member"]["uname"],
                    "message": r["content"]["message"],
                    "ctime": ctime
                })

    return new_list, max_ctime


# ================= 主循环 =================
def get_header():
    cookie = open("bili_cookie.txt", "r", encoding="utf-8").read().strip()

    return {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0"
    }


def start_monitoring(header):
    following = load_following_cache()

    if not following:
        following = get_following_list(SOURCE_UID, header)
        save_following_cache(following)

    if SOURCE_UID not in following:
        following.append(SOURCE_UID)

    state = load_dynamic_state()
    seen = {str(uid): set() for uid in following}

    last_comment = 0
    last_dynamic = 0
    last_follow = 0
    seen_comments = set()

    while True:
        now = time.time()

        # 更新关注列表
        if now - last_follow > FOLLOWING_REFRESH_INTERVAL:
            new_list = get_following_list(SOURCE_UID, header)
            if new_list:
                following = new_list
                save_following_cache(following)
            last_follow = now

        # 评论（不动）
        oid = db.get_monitored_videos()[0][0] if db.get_monitored_videos() else None

        if oid and now - last_comment > COMMENT_SCAN_INTERVAL:
            new_c, last_comment = scan_new_comments(oid, header, last_comment, seen_comments)
            if new_c:
                notifier.send_webhook_notification("评论", new_c)

        # 动态（简化版）
        if now - last_dynamic > DYNAMIC_CHECK_INTERVAL:
            all_alerts = []

            for uid in following:
                alerts, offset = check_dynamic(uid, header, seen, state)
                all_alerts.extend(alerts)
                time.sleep(random.uniform(0.3, 0.8))

            save_dynamic_state(state)

            if all_alerts:
                notifier.send_webhook_notification("动态更新", all_alerts)

            last_dynamic = now

        time.sleep(2)


# ================= 启动 =================
if __name__ == "__main__":
    init_logging()
    db.init_db()

    h = get_header()
    update_wbi_keys(h)

    start_monitoring(h)
