# main.py
# B站监控系统（2026极简抗风控版）

import sys
import os
import time
import random
import logging
import traceback
import hashlib
import urllib.parse
import subprocess
import requests

import database as db
import notifier


# ==================================================
# 配置区
# ==================================================
TARGET_UID = 1671203508

EXTRA_DYNAMIC_UIDS = [
    3546905852250875,
    3546961271589219,
    3546610447419885,
    285340365,
    3706948578969654
]

LOG_FILE = "bili_monitor.log"

VIDEO_REFRESH_INTERVAL = 21600
COMMENT_INTERVAL = 60
DYNAMIC_INTERVAL = 60   # ↓ 降低单次扫描压力
HEARTBEAT_INTERVAL = 1800

COMMENT_MAX_PAGE = 3    # ↓ 降低
DYNAMIC_MAX_AGE = 300

REQUEST_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0"

# ==================================================
# 日志
# ==================================================
def init_logging():
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        encoding="utf-8",
        filemode="w"
    )


# ==================================================
# Header
# ==================================================
def get_header():
    if not os.path.exists("bili_cookie.txt"):
        subprocess.run([sys.executable, "login_bilibili.py"])

    with open("bili_cookie.txt", "r", encoding="utf-8") as f:
        cookie = f.read().strip()

    return {
        "Cookie": cookie,
        "User-Agent": USER_AGENT,
        "Referer": "https://www.bilibili.com/"
    }


# ==================================================
# 安全请求（核心抗-352优化）
# ==================================================
def safe_request(url, params=None, header=None, retries=3):
    for _ in range(retries):
        try:
            r = requests.get(url, headers=header, params=params, timeout=REQUEST_TIMEOUT)

            if r.status_code != 200:
                time.sleep(random.uniform(2, 5))
                continue

            txt = r.text

            if "验证码" in txt or "风险控制" in txt:
                logging.warning("触发页面风控 -> sleep 120s")
                time.sleep(120)
                continue

            data = r.json()
            if data.get("code") == -352:
                logging.warning("触发 -352 -> sleep 180s + jitter")
                time.sleep(180 + random.randint(0, 30))
                continue

            return data

        except Exception:
            time.sleep(3)

    return {"code": -500}


# ==================================================
# WBI（保持不变）
# ==================================================
WBI = {"img_key": "", "sub_key": "", "ts": 0}

mixinKeyEncTab = [
46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,
27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,
37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,
22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52
]


def getMixinKey(orig):
    return "".join([orig[i] for i in mixinKeyEncTab])[:32]


def update_wbi(header):
    data = safe_request(
        "https://api.bilibili.com/x/web-interface/nav",
        header=header
    )

    if data.get("code") == 0:
        img = data["data"]["wbi_img"]
        WBI["img_key"] = img["img_url"].split("/")[-1].split(".")[0]
        WBI["sub_key"] = img["sub_url"].split("/")[-1].split(".")[0]
        WBI["ts"] = time.time()


def sign_wbi(params):
    mixin = getMixinKey(WBI["img_key"] + WBI["sub_key"])
    params["wts"] = int(time.time())
    params = dict(sorted(params.items()))
    query = urllib.parse.urlencode(params)
    params["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return params


def wbi_request(url, params, header):
    if time.time() - WBI["ts"] > 21600:
        update_wbi(header)

    return safe_request(url, sign_wbi(params), header)


# ==================================================
# 视频（不动）
# ==================================================
def get_latest_video(header):
    data = safe_request(
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
        {"host_mid": TARGET_UID},
        header
    )

    items = (data.get("data") or {}).get("items", [])

    for item in items:
        if item.get("type") == "DYNAMIC_TYPE_AV":
            try:
                return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
            except:
                pass

    return None


def get_video_info(bv, header):
    data = safe_request(
        "https://api.bilibili.com/x/web-interface/view",
        {"bvid": bv},
        header
    )

    if data.get("code") == 0:
        d = data["data"]
        return str(d["aid"]), d["title"]

    return None, None


def sync_video(header):
    bv = get_latest_video(header)
    if not bv:
        return None, None

    videos = db.get_monitored_videos()

    if videos and videos[0][1] == bv:
        return videos[0][0], videos[0][2]

    oid, title = get_video_info(bv, header)

    if oid:
        db.clear_videos()
        db.add_video_to_db(oid, bv, title)

    return oid, title


# ==================================================
# 评论（轻量）
# ==================================================
def scan_comments(oid, header, last_time, seen):
    new_list = []
    max_time = last_time

    for pn in range(1, COMMENT_MAX_PAGE + 1):
        data = wbi_request(
            "https://api.bilibili.com/x/v2/reply",
            {"oid": oid, "type": 1, "sort": 0, "pn": pn, "ps": 20},
            header
        )

        replies = (data.get("data") or {}).get("replies") or []

        if not replies:
            break

        for r in replies:
            ctime = r["ctime"]
            rid = r["rpid_str"]

            if ctime > max_time:
                max_time = ctime

            if ctime > last_time and rid not in seen:
                seen.add(rid)
                new_list.append({
                    "user": r["member"]["uname"],
                    "message": r["content"]["message"],
                    "ctime": ctime
                })

        time.sleep(1)

    return new_list, max_time


# ==================================================
# 动态（🔥核心重写）
# ==================================================
def extract_text(item):
    try:
        dyn = item["modules"]["module_dynamic"]
        desc = dyn.get("desc", {})
        nodes = desc.get("rich_text_nodes", [])

        if nodes:
            return "".join(x.get("text", "") for x in nodes).strip()

        return "发布了新动态"
    except:
        return "发布了新动态"


def check_dynamics(header, seen):
    alerts = []
    now = time.time()

    for uid in EXTRA_DYNAMIC_UIDS:
        try:
            data = safe_request(
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
                {"host_mid": uid},
                header
            )

            items = (data.get("data") or {}).get("items", [])

            # 🔥 只取最新1条（彻底降压）
            if not items:
                continue

            item = items[0]
            did = item.get("id_str")

            if not did or did in seen[uid]:
                continue

            modules = item.get("modules") or {}
            author = modules.get("module_author") or {}

            pub_ts = float(author.get("pub_ts", 0))

            # 🔥 只允许5分钟内
            if now - pub_ts > DYNAMIC_MAX_AGE:
                continue

            seen[uid].add(did)

            name = author.get("name", str(uid))
            text = extract_text(item)

            alerts.append({
                "user": name,
                "message": f"{text}\nhttps://t.bilibili.com/{did}"
            })

        except Exception:
            logging.error(traceback.format_exc())

        time.sleep(random.uniform(5, 10))  # 🔥 人类级延迟

    if alerts:
        notifier.send_webhook_notification(
            "特别关注UP主动态",
            alerts
        )


# ==================================================
# 主循环（无初始化风暴）
# ==================================================
def start(header):
    oid, title = sync_video(header)

    seen_comments = set()
    seen_dynamics = {uid: set() for uid in EXTRA_DYNAMIC_UIDS}

    last_comment = 0
    last_dynamic = 0
    last_video = 0
    last_heart = 0
    last_time = int(time.time())

    while True:
        try:
            now = time.time()

            if oid and now - last_comment >= COMMENT_INTERVAL:
                new_list, new_time = scan_comments(
                    oid, header, last_time, seen_comments
                )

                if new_time > last_time:
                    last_time = new_time

                if new_list:
                    notifier.send_webhook_notification(title, new_list)

                last_comment = now

            if now - last_dynamic >= DYNAMIC_INTERVAL:
                check_dynamics(header, seen_dynamics)
                last_dynamic = now

            if now - last_video >= VIDEO_REFRESH_INTERVAL:
                oid, title = sync_video(header)
                last_video = now

            if now - last_heart >= HEARTBEAT_INTERVAL:
                notifier.send_webhook_notification(
                    "心跳",
                    [{"user": "system", "message": "running"}]
                )
                last_heart = now

            time.sleep(5)

        except Exception:
            logging.error(traceback.format_exc())
            time.sleep(60)


# ==================================================
# 启动
# ==================================================
if __name__ == "__main__":
    init_logging()
    db.init_db()

    header = get_header()
    update_wbi(header)

    start(header)
