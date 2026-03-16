import sys
import requests
import time
import datetime
import subprocess
import random
import pandas as pd
import logging
import traceback
import pytz
import hashlib
import urllib.parse
import database as db
import notifier

TARGET_UID = 1671203508
VIDEO_CHECK_INTERVAL = 21600

logging.basicConfig(
    filename='bili_monitor.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    encoding='utf-8',
    filemode='a'
)

china_tz = pytz.timezone("Asia/Shanghai")

def md5(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()

def get_header():
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    except:
        logging.warning("未找到Cookie，启动扫码登录")
        subprocess.run([sys.executable, "login_bilibili.py"])
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    return {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.bilibili.com"
    }

def is_work_time():
    now = datetime.datetime.now(china_tz)
    return now.weekday() < 5 and 9 <= now.hour < 19

def get_video_info(bv, header):
    url = f"https://api.bilibili.com/x/web-interface/view?bvid={bv}"
    try:
        r = requests.get(url, headers=header, timeout=10)
        data = r.json()
        if data["code"] == 0:
            return str(data["data"]["aid"]), data["data"]["title"]
    except Exception as e:
        logging.error("获取视频信息失败: %s", e)
    return None, None

def get_latest_video(header):
    url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    params = {"host_mid": TARGET_UID}
    try:
        r = requests.get(url, headers=header, params=params, timeout=10)
        data = r.json()
        if data["code"] != 0:
            return None
        items = data["data"]["items"]
        for item in items:
            try:
                if item["type"] == "DYNAMIC_TYPE_AV":
                    return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
            except:
                continue
    except:
        return None
    return None

def sync_latest_video(header):
    bv = get_latest_video(header)
    if not bv:
        return None, None
    videos = db.get_monitored_videos()
    if videos and videos[0][1] == bv:
        return videos[0][0], videos[0][2]
    oid, title = get_video_info(bv, header)
    if not oid:
        return None, None
    db.clear_videos()
    db.add_video_to_db(oid, bv, title)
    logging.info("开始监控视频: %s", title)
    return oid, title

def fetch_latest_comments(oid, header):
    if not oid:
        return []
    mixin_key_salt = "ea1db124af3c7062474693fa704f4ff8"
    params = {
        'oid': oid,
        'type': 1,
        'mode': 2,
        'plat': 1,
        'web_location': 1315875,
        'wts': int(time.time())
    }
    query = urllib.parse.urlencode(sorted(params.items()))
    w_rid = md5(query + mixin_key_salt)
    params['w_rid'] = w_rid
    url = f"https://api.bilibili.com/x/v2/reply/wbi/main?{urllib.parse.urlencode(params)}"
    try:
        r = requests.get(url, headers=header, timeout=8)
        r.raise_for_status()
        data = r.json()
        if data.get("code") == 0:
            return data.get("data", {}).get("replies", []) or []
        return []
    except:
        return []

def fetch_all_sub_replies(oid, root_rpid, header):
    all_replies = []
    pn = 1
    while True:
        url = f"https://api.bilibili.com/x/v2/reply/reply?oid={oid}&type=1&root={root_rpid}&pn={pn}&ps=20"
        try:
            r = requests.get(url, headers=header, timeout=8)
            r.raise_for_status()
            data = r.json()
            if data.get("code") != 0 or not data.get("data"):
                break
            replies = data["data"].get("replies", [])
            if not replies:
                break
            all_replies.extend(replies)
            pn += 1
            time.sleep(0.8)
        except:
            break
    return all_replies

def start_monitoring(header):
    last_video_check = 0
    oid, title = sync_latest_video(header)
    if not oid:
        logging.error("初始视频获取失败")
        sys.exit(1)

    seen = set()
    main_comments = fetch_latest_comments(oid, header)
    for c in main_comments:
        seen.add(c["rpid_str"])
    logging.info("已加载 %d 条历史主评论", len(seen))

    while True:
        try:
            now = datetime.datetime.now(china_tz)
            logging.info("检查时间: %s", now.strftime("%Y-%m-%d %H:%M:%S"))

            if is_work_time():
                main_comments = fetch_latest_comments(oid, header)
                new_items = []
                for c in main_comments:
                    rpid = c["rpid_str"]
                    if rpid in seen:
                        continue
                    seen.add(rpid)
                    new_items.append({
                        "user": c["member"]["uname"],
                        "message": c["content"]["message"],
                        "time": pd.to_datetime(c["ctime"], unit="s"),
                        "is_reply": False,
                        "reply_to": None
                    })

                    # 获取所有子回复
                    subs = fetch_all_sub_replies(oid, rpid, header)
                    for sub in subs:
                        sub_rpid = sub["rpid_str"]
                        if sub_rpid in seen:
                            continue
                        seen.add(sub_rpid)
                        new_items.append({
                            "user": sub["member"]["uname"],
                            "message": sub["content"]["message"],
                            "time": pd.to_datetime(sub["ctime"], unit="s"),
                            "is_reply": True,
                            "reply_to": c["member"]["uname"]
                        })

                if new_items:
                    logging.info("发现 %d 条新评论/回复 - %s", len(new_items), title)
                    for item in new_items:
                        prefix = f"回复@{item['reply_to']} " if item["is_reply"] else ""
                        logging.info("%s%s : %s", prefix, item["user"], item["message"])
                    notifier.send_webhook_notification(title, new_items)

                time.sleep(random.uniform(20, 40))
            else:
                time.sleep(3600)

            if time.time() - last_video_check > VIDEO_CHECK_INTERVAL:
                new_oid, new_title = sync_latest_video(header)
                if new_oid and new_oid != oid:
                    oid = new_oid
                    title = new_title
                    seen = set()
                    main_comments = fetch_latest_comments(oid, header)
                    for c in main_comments:
                        seen.add(c["rpid_str"])
                    logging.info("切换新视频，已加载历史评论")
                last_video_check = time.time()

        except Exception as e:
            logging.error("主循环异常: %s", traceback.format_exc())
            time.sleep(10)

if __name__ == "__main__":
    db.init_db()
    header = get_header()
    logging.info("B站监控程序启动")
    start_monitoring(header)
