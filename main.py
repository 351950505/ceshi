import sys
import os
import time
import datetime
import subprocess
import random
import logging
import traceback
import hashlib
import urllib.parse
import requests

import database as db
import notifier

# ================= 核心配置区 =================
TARGET_UID = 1671203508
VIDEO_CHECK_INTERVAL = 21600
HEARTBEAT_INTERVAL = 600

# 动态监控名单（UID）
EXTRA_DYNAMIC_UIDS = [
    3546905852250875,
    3546961271589219,
    3546610447419885,
    285340365,
    3706948578969654
]

DYNAMIC_CHECK_INTERVAL = 35
DYNAMIC_BURST_INTERVAL = 10
DYNAMIC_BURST_DURATION = 300
DYNAMIC_MAX_AGE = 300

LOG_FILE = "bili_monitor.log"

# ==============================================


# ------------------------
# 日志初始化（物理清空）
# ------------------------
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

    logging.info("=" * 50)
    logging.info("B站全能监控启动")
    logging.info("=" * 50)


# ------------------------
# 安全请求
# ------------------------
def safe_request(url, params, header, retries=3):
    safe_header = header.copy()
    safe_header["Connection"] = "close"

    for i in range(retries):
        try:
            r = requests.get(
                url,
                headers=safe_header,
                params=params,
                timeout=10
            )

            txt = r.text.strip()

            if not txt:
                time.sleep(2)
                continue

            return r.json()

        except:
            time.sleep(2 + i)

    return {"code": -500}


# ------------------------
# WBI签名模块
# ------------------------
WBI_KEYS = {
    "img_key": "",
    "sub_key": "",
    "last_update": 0
}

mixinKeyEncTab = [
    46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,
    27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,
    37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,
    22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52
]


def getMixinKey(orig: str):
    return "".join([orig[i] for i in mixinKeyEncTab])[:32]


def encWbi(params: dict, img_key: str, sub_key: str):
    mixin_key = getMixinKey(img_key + sub_key)

    params["wts"] = round(time.time())
    params = dict(sorted(params.items()))

    filtered = {}

    for k, v in params.items():
        v = str(v)
        for c in "!'()*":
            v = v.replace(c, "")
        filtered[k] = v

    query = urllib.parse.urlencode(filtered)
    sign = hashlib.md5((query + mixin_key).encode()).hexdigest()

    filtered["w_rid"] = sign
    return filtered


def update_wbi_keys(header):
    try:
        data = safe_request(
            "https://api.bilibili.com/x/web-interface/nav",
            None,
            header
        )

        if data.get("code") == 0:
            wbi = data["data"]["wbi_img"]

            WBI_KEYS["img_key"] = wbi["img_url"].rsplit("/", 1)[1].split(".")[0]
            WBI_KEYS["sub_key"] = wbi["sub_url"].rsplit("/", 1)[1].split(".")[0]
            WBI_KEYS["last_update"] = time.time()

            logging.info("WBI密钥更新成功")

    except:
        pass


def wbi_request(url, params, header):
    if (
        not WBI_KEYS["img_key"]
        or time.time() - WBI_KEYS["last_update"] > 21600
    ):
        update_wbi_keys(header)

    signed = encWbi(
        params.copy(),
        WBI_KEYS["img_key"],
        WBI_KEYS["sub_key"]
    )

    data = safe_request(url, signed, header)

    if data.get("code") == -400:
        update_wbi_keys(header)

        signed = encWbi(
            params.copy(),
            WBI_KEYS["img_key"],
            WBI_KEYS["sub_key"]
        )

        data = safe_request(url, signed, header)

    return data


# ------------------------
# Header
# ------------------------
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


# ------------------------
# 工作时间
# ------------------------
def is_work_time():
    now = datetime.datetime.now(
        datetime.timezone.utc
    ) + datetime.timedelta(hours=8)

    return now.weekday() < 5 and 9 <= now.hour < 19


# ------------------------
# 视频监控
# ------------------------
def get_video_info(bv, header):
    data = safe_request(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bv}",
        None,
        header
    )

    if data.get("code") == 0:
        return (
            str(data["data"]["aid"]),
            data["data"]["title"]
        )

    return None, None


def get_latest_video(header, target_uid):
    data = safe_request(
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
        {"host_mid": target_uid},
        header
    )

    if data.get("code") == 0:
        items = (data.get("data") or {}).get("items", [])

        for item in items:
            try:
                if item.get("type") == "DYNAMIC_TYPE_AV":
                    return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
            except:
                pass

    return None


def sync_latest_video(header):
    bv = get_latest_video(header, TARGET_UID)

    if not bv:
        return None, None

    videos = db.get_monitored_videos()

    if videos and videos[0][1] == bv:
        return videos[0][0], videos[0][2]

    oid, title = get_video_info(bv, header)

    if oid:
        db.clear_videos()
        db.add_video_to_db(oid, bv, title)
        logging.info(f"监控视频切换：{title}")
        return oid, title

    return None, None


# ------------------------
# 动态初始化
# ------------------------
def init_extra_dynamics(header):
    seen = {}
    active = {}

    for uid in EXTRA_DYNAMIC_UIDS:
        seen[uid] = set()
        active[uid] = {}

        data = safe_request(
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
            {"host_mid": uid},
            header
        )

        if data.get("code") == 0:
            items = (data.get("data") or {}).get("items", [])

            for item in items:
                if item.get("id_str"):
                    seen[uid].add(item["id_str"])

    return seen, active


# ------------------------
# 动态监控（增强版）
# ------------------------
def check_new_dynamics(header, seen_dynamics, active_dynamics):
    new_alerts = []
    has_new = False
    now_ts = time.time()

    for uid in EXTRA_DYNAMIC_UIDS:
        try:
            data = safe_request(
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
                {"host_mid": uid},
                header
            )

            code = data.get("code", -1)
            logging.info(f"动态检测 UID={uid} code={code}")

            if code != 0:
                continue

            inner = data.get("data") or {}

            items = (
                inner.get("items")
                or inner.get("cards")
                or inner.get("list")
                or []
            )

            if not isinstance(items, list):
                continue

            for item in items:
                try:
                    id_str = item.get("id_str")

                    if not id_str:
                        continue

                    if id_str in seen_dynamics[uid]:
                        continue

                    seen_dynamics[uid].add(id_str)

                    modules = item.get("modules") or {}
                    author = modules.get("module_author") or {}
                    dyn = modules.get("module_dynamic") or {}
                    major = dyn.get("major") or {}

                    try:
                        pub_ts = float(author.get("pub_ts", 0))
                    except:
                        pub_ts = 0

                    if now_ts - pub_ts > DYNAMIC_MAX_AGE:
                        continue

                    has_new = True

                    name = author.get("name", str(uid))

                    content = ""

                    desc = dyn.get("desc") or {}
                    content = desc.get("text", "")

                    if not content:
                        opus = major.get("opus") or {}
                        content = (
                            opus.get("summary", {})
                            .get("text", "")
                        )

                    attach = ""

                    archive = major.get("archive") or {}
                    if archive:
                        attach = f"视频：{archive.get('title','')}"

                    final_msg = content.strip()

                    if attach:
                        final_msg += f"\n{attach}"

                    if not final_msg:
                        final_msg = "发布了新动态"

                    new_alerts.append({
                        "user": name,
                        "message": final_msg[:300]
                    })

                    logging.info(f"抓到动态 [{name}]")

                    basic = item.get("basic") or {}

                    if basic.get("comment_id_str"):
                        active_dynamics[uid][id_str] = {
                            "oid": basic["comment_id_str"],
                            "type": basic["comment_type"],
                            "ctime": time.time()
                        }

                except Exception as e:
                    logging.error(f"单条动态解析失败:{e}")

        except Exception as e:
            logging.error(f"动态UID异常 {uid}: {e}")

        time.sleep(random.uniform(1.5, 3))

    if new_alerts:
        try:
            notifier.send_webhook_notification(
                "💡 特别关注UP发布新内容",
                new_alerts
            )
        except:
            pass

    return has_new


# ------------------------
# 动态补充评论
# ------------------------
def check_dynamic_up_replies(header, active_dynamics, seen_replies):
    alerts = []
    now = time.time()

    for uid, dyns in list(active_dynamics.items()):
        for did, info in list(dyns.items()):
            if now - info["ctime"] > 86400:
                del dyns[did]
                continue

            data = wbi_request(
                "https://api.bilibili.com/x/v2/reply",
                {
                    "oid": info["oid"],
                    "type": info["type"],
                    "sort": 0,
                    "pn": 1,
                    "ps": 20
                },
                header
            )

            if data.get("code") != 0:
                continue

            inner = data.get("data") or {}
            replies = inner.get("replies") or []

            top = (inner.get("upper") or {}).get("top")
            if top:
                replies.append(top)

            for r in replies:
                try:
                    if str((r.get("member") or {}).get("mid")) != str(uid):
                        continue

                    rpid = r.get("rpid_str")

                    if rpid in seen_replies:
                        continue

                    seen_replies.add(rpid)

                    alerts.append({
                        "user": r["member"]["uname"],
                        "message": "💬 UP主动态补充：\n" + r["content"]["message"]
                    })

                except:
                    pass

    if alerts:
        try:
            notifier.send_webhook_notification(
                "🔔 UP主本尊动态出没",
                alerts
            )
        except:
            pass


# ------------------------
# 评论监控（零修改）
# ------------------------
def scan_new_comments(oid, header, last_read_time, seen):
    new_list = []
    max_ctime = last_read_time
    safe_read_time = last_read_time - 300

    pn = 1

    while pn <= 10:
        data = wbi_request(
            "https://api.bilibili.com/x/v2/reply",
            {
                "oid": oid,
                "type": 1,
                "sort": 0,
                "pn": pn,
                "ps": 20
            },
            header
        )

        replies = (data.get("data") or {}).get("replies") or []

        if not replies:
            break

        page_old = True

        for r in replies:
            rpid = r["rpid_str"]
            ctime = r["ctime"]

            max_ctime = max(max_ctime, ctime)

            if ctime > safe_read_time:
                page_old = False

                if rpid not in seen:
                    seen.add(rpid)

                    new_list.append({
                        "user": r["member"]["uname"],
                        "message": r["content"]["message"],
                        "ctime": ctime
                    })

        if page_old:
            break

        pn += 1
        time.sleep(random.uniform(0.5, 1))

    return new_list, max_ctime


# ------------------------
# 主循环
# ------------------------
def start_monitoring(header):
    last_v_check = 0
    last_hb = time.time()
    last_d_check = 0
    burst_end = 0

    oid, title = sync_latest_video(header)

    last_read_time = int(time.time())
    seen_comments = set()

    seen_dyns, active_dyns = init_extra_dynamics(header)
    seen_dyn_replies = set()

    logging.info(f"启动成功 当前视频：{title}")

    while True:
        try:
            now = time.time()

            if is_work_time():

                # 评论监控
                if oid:
                    new_c, new_t = scan_new_comments(
                        oid,
                        header,
                        last_read_time,
                        seen_comments
                    )

                    if new_t > last_read_time:
                        last_read_time = new_t

                    if new_c:
                        new_c.sort(key=lambda x: x["ctime"])

                        notifier.send_webhook_notification(
                            title,
                            new_c
                        )

                # 动态监控
                interval = (
                    DYNAMIC_BURST_INTERVAL
                    if now < burst_end
                    else DYNAMIC_CHECK_INTERVAL
                )

                if now - last_d_check >= interval:

                    logging.info("动态监控运行中")

                    if check_new_dynamics(
                        header,
                        seen_dyns,
                        active_dyns
                    ):
                        burst_end = now + DYNAMIC_BURST_DURATION

                    check_dynamic_up_replies(
                        header,
                        active_dyns,
                        seen_dyn_replies
                    )

                    last_d_check = now

                # 心跳
                if now - last_hb >= HEARTBEAT_INTERVAL:
                    try:
                        notifier.send_webhook_notification(
                            "心跳",
                            [{
                                "user": "系统",
                                "message": "正常运行中"
                            }]
                        )
                    except:
                        pass

                    last_hb = now

                time.sleep(random.uniform(10, 15))

            else:
                time.sleep(30)

            # 视频刷新
            if now - last_v_check > VIDEO_CHECK_INTERVAL:
                res = sync_latest_video(header)

                if res:
                    oid, title = res

                last_v_check = now

        except Exception:
            logging.error(traceback.format_exc())
            time.sleep(60)


# ------------------------
# 启动入口
# ------------------------
if __name__ == "__main__":
    init_logging()
    db.init_db()

    h = get_header()

    update_wbi_keys(h)

    start_monitoring(h)
