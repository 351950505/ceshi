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

# =====================================================
# B站监控系统（稳定版）
# 已修改：
# 1. 删除狂暴模式
# 2. 动态固定15秒扫描一次
# 3. 每个UID请求间隔1秒
# 4. 多人同时发动态可一起推送
# 5. 降低风控概率
# =====================================================

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

DYNAMIC_CHECK_INTERVAL = 15
DYNAMIC_MAX_AGE = 86400

COMMENT_SCAN_INTERVAL = 5
COMMENT_MAX_PAGES = 3
COMMENT_SAFE_WINDOW = 60

TIME_OFFSET = -120

LOG_FILE = "bili_monitor.log"
DYNAMIC_STATE_FILE = "dynamic_state.json"
FOLLOWING_CACHE_FILE = "following_cache.json"

LAST_ERROR_ALERT = {}
ERROR_ALERT_INTERVAL = 300


# ================= 日志 =================
def init_logging():
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        encoding="utf-8",
        filemode="w"
    )
    logging.info("=" * 60)
    logging.info("B站监控系统启动")
    logging.info("=" * 60)


# ================= Cookie =================
def refresh_cookie():
    try:
        logging.warning("Cookie失效，尝试重新登录")
        subprocess.run([sys.executable, "login_bilibili.py"], check=True)
        return True
    except:
        return False


def get_header():
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    except:
        subprocess.run([sys.executable, "login_bilibili.py"])
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()

    return {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.bilibili.com/"
    }


# ================= 通用请求 =================
def safe_request(url, params, header, retries=3):
    for i in range(retries):
        try:
            r = requests.get(
                url,
                headers=header,
                params=params,
                timeout=10
            )

            data = r.json()
            code = data.get("code")

            if code == -101:
                return {"code": -101}

            if code in (-352, -799, -509):
                wait = 3 + i * 3
                logging.warning(f"触发风控 {code}，等待 {wait} 秒")
                time.sleep(wait)
                continue

            return data

        except Exception as e:
            logging.error(str(e))
            time.sleep(2)

    return {"code": -500}


# ================= WBI =================
WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}

mixinKeyEncTab = [
    46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,
    27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,
    37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,
    22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52
]


def getMixinKey(orig):
    return ''.join([orig[i] for i in mixinKeyEncTab])[:32]


def encWbi(params, img_key, sub_key):
    mixin_key = getMixinKey(img_key + sub_key)

    params["wts"] = int(time.time() + TIME_OFFSET)

    params = dict(sorted(params.items()))

    query = urllib.parse.urlencode(params)
    sign = hashlib.md5((query + mixin_key).encode()).hexdigest()

    params["w_rid"] = sign
    return params


def update_wbi_keys(header):
    data = safe_request(
        "https://api.bilibili.com/x/web-interface/nav",
        None,
        header
    )

    if data.get("code") == 0:
        img = data["data"]["wbi_img"]

        WBI_KEYS["img_key"] = img["img_url"].split("/")[-1].split(".")[0]
        WBI_KEYS["sub_key"] = img["sub_url"].split("/")[-1].split(".")[0]
        WBI_KEYS["last_update"] = time.time()

        logging.info("WBI更新成功")


def wbi_request(url, params, header):
    if time.time() - WBI_KEYS["last_update"] > 21600:
        update_wbi_keys(header)

    params = encWbi(
        params,
        WBI_KEYS["img_key"],
        WBI_KEYS["sub_key"]
    )

    return safe_request(url, params, header)


# ================= 文件缓存 =================
def load_json(file):
    if os.path.exists(file):
        try:
            with open(file, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}


def save_json(file, data):
    with open(file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ================= 获取关注列表 =================
def get_following_list(uid, header):
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

        items = data["data"]["list"]

        if not items:
            break

        for i in items:
            result.append(i["mid"])

        pn += 1
        time.sleep(1)

    return result


# ================= 动态 =================
def fetch_latest_dynamics(uid, header):
    return wbi_request(
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all",
        {
            "host_mid": uid
        },
        header
    )


def extract_dynamic_text(item):
    try:
        return item["modules"]["module_dynamic"]["desc"]["text"]
    except:
        return "发布了新动态"


def check_uid_dynamic(uid, header, seen, adjusted_now):
    alerts = []

    data = fetch_latest_dynamics(uid, header)

    if data.get("code") != 0:
        return alerts

    items = data.get("data", {}).get("items", [])

    for item in reversed(items):
        dyn_id = item.get("id_str")

        if not dyn_id:
            continue

        if dyn_id in seen[uid]:
            continue

        seen[uid].add(dyn_id)

        author = item["modules"]["module_author"]

        pub_ts = author.get("pub_ts", 0)

        if adjusted_now - pub_ts > DYNAMIC_MAX_AGE:
            continue

        name = author.get("name", str(uid))
        text = extract_dynamic_text(item)

        msg = f"{text}\n\n🔗 https://t.bilibili.com/{dyn_id}"

        alerts.append({
            "user": name,
            "message": msg
        })

    return alerts


# ================= 视频 =================
def get_latest_video(header):
    data = safe_request(
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
        {"host_mid": TARGET_UID},
        header
    )

    if data.get("code") != 0:
        return None

    items = data.get("data", {}).get("items", [])

    for item in items:
        if item.get("type") == "DYNAMIC_TYPE_AV":
            return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]

    return None


def get_video_info(bv, header):
    data = safe_request(
        "https://api.bilibili.com/x/web-interface/view",
        {"bvid": bv},
        header
    )

    if data.get("code") == 0:
        return str(data["data"]["aid"]), data["data"]["title"]

    return None, None


def sync_latest_video(header):
    bv = get_latest_video(header)

    if not bv:
        return None, None

    oid, title = get_video_info(bv, header)

    if oid:
        db.clear_videos()
        db.add_video_to_db(oid, bv, title)

    return oid, title


# ================= 评论 =================
def scan_new_comments(oid, header, last_time, seen):
    result = []

    data = wbi_request(
        "https://api.bilibili.com/x/v2/reply",
        {
            "oid": oid,
            "type": 1,
            "pn": 1
        },
        header
    )

    if data.get("code") != 0:
        return result, last_time

    replies = data.get("data", {}).get("replies", [])

    for r in replies:
        ctime = r["ctime"]

        if ctime <= last_time:
            continue

        rid = r["rpid_str"]

        if rid in seen:
            continue

        seen.add(rid)

        result.append({
            "user": r["member"]["uname"],
            "message": r["content"]["message"],
            "ctime": ctime
        })

        if ctime > last_time:
            last_time = ctime

    return result, last_time


# ================= 主循环 =================
def start_monitoring(header):
    oid, title = sync_latest_video(header)

    last_read_time = int(time.time())
    seen_comments = set()

    following = get_following_list(SOURCE_UID, header)

    if not following:
        following = FALLBACK_DYNAMIC_UIDS

    if SOURCE_UID not in following:
        following.append(SOURCE_UID)

    seen = {}

    for uid in following:
        seen[uid] = set()

    last_dynamic = 0
    last_comment = 0
    last_video = 0
    last_hb = 0

    logging.info("监控开始")

    while True:
        try:
            now = time.time()
            adjusted_now = now + TIME_OFFSET

            # 评论
            if now - last_comment >= COMMENT_SCAN_INTERVAL:
                if oid:
                    new_comments, last_read_time = scan_new_comments(
                        oid,
                        header,
                        last_read_time,
                        seen_comments
                    )

                    if new_comments:
                        notifier.send_webhook_notification(
                            title,
                            new_comments
                        )

                last_comment = now

            # 动态（固定15秒）
            if now - last_dynamic >= 15:
                all_alerts = []

                for uid in following:
                    alerts = check_uid_dynamic(
                        uid,
                        header,
                        seen,
                        adjusted_now
                    )

                    if alerts:
                        all_alerts.extend(alerts)

                    # 每个UID间隔1秒
                    time.sleep(1)

                if all_alerts:
                    notifier.send_webhook_notification(
                        "💡 特别关注UP主发布新内容",
                        all_alerts
                    )

                    logging.info(
                        f"动态推送 {len(all_alerts)} 条"
                    )

                last_dynamic = now

            # 视频
            if now - last_video >= VIDEO_CHECK_INTERVAL:
                res = sync_latest_video(header)

                if res:
                    oid, title = res

                last_video = now

            # 心跳
            if now - last_hb >= HEARTBEAT_INTERVAL:
                logging.info("💓 系统运行正常")
                last_hb = now

            time.sleep(2)

        except Exception:
            logging.error(traceback.format_exc())
            time.sleep(60)


# ================= 启动 =================
if __name__ == "__main__":
    init_logging()
    db.init_db()

    header = get_header()

    update_wbi_keys(header)

    start_monitoring(header)
