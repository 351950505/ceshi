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

def fetch_comments(oid, header):
    url = "https://api.bilibili.com/x/v2/reply/main"
    params = {"oid": oid, "type": 1, "mode": 2}
    try:
        r = requests.get(url, headers=header, params=params, timeout=10)
        data = r.json()
        return data.get("data", {}).get("replies", []) or []
    except:
        return []

def fetch_sub_replies(oid, root_rpid, header):
    url = "https://api.bilibili.com/x/v2/reply/reply"
    params = {"oid": oid, "type": 1, "root": root_rpid, "pn": 1, "ps": 30}
    try:
        r = requests.get(url, headers=header, params=params, timeout=10)
        data = r.json()
        if data.get("code") == 0:
            return data.get("data", {}).get("replies", []) or []
        return []
    except:
        return []

def start_monitoring(header):
    last_video_check = 0
    oid, title = sync_latest_video(header)
    if not oid:
        logging.error("初始视频获取失败，程序退出")
        sys.exit(1)

    seen = set()
    replies = fetch_comments(oid, header)
    for r in replies:
        seen.add(r["rpid_str"])
    logging.info("已加载 %d 条历史主评论", len(seen))

    while True:
        try:
            now = datetime.datetime.now(china_tz)
            logging.info("检查时间: %s", now.strftime("%Y-%m-%d %H:%M:%S"))

            if is_work_time():
                replies = fetch_comments(oid, header)
                new_comments = []
                for r in replies:
                    rpid = r["rpid_str"]
                    if rpid in seen:
                        continue
                    seen.add(rpid)
                    comment = {
                        "user": r["member"]["uname"],
                        "message": r["content"]["message"],
                        "time": pd.to_datetime(r["ctime"], unit="s"),
                        "is_reply": False,
                        "reply_to": None
                    }
                    new_comments.append(comment)

                    sub_replies = fetch_sub_replies(oid, rpid, header)
                    for sub in sub_replies:
                        sub_rpid = sub["rpid_str"]
                        if sub_rpid in seen:
                            continue
                        seen.add(sub_rpid)
                        sub_comment = {
                            "user": sub["member"]["uname"],
                            "message": sub["content"]["message"],
                            "time": pd.to_datetime(sub["ctime"], unit="s"),
                            "is_reply": True,
                            "reply_to": r["member"]["uname"]
                        }
                        new_comments.append(sub_comment)

                if new_comments:
                    logging.info("发现 %d 条新评论/回复 - %s", len(new_comments), title)
                    for c in new_comments:
                        prefix = f"回复@{c['reply_to']} " if c["is_reply"] else ""
                        logging.info("%s%s : %s", prefix, c["user"], c["message"])
                    notifier.send_webhook_notification(title, new_comments)

                time.sleep(random.uniform(20, 40))
            else:
                time.sleep(3600)

            if time.time() - last_video_check > VIDEO_CHECK_INTERVAL:
                new_oid, new_title = sync_latest_video(header)
                if new_oid and new_oid != oid:
                    oid = new_oid
                    title = new_title
                    seen = set()
                    replies = fetch_comments(oid, header)
                    for r in replies:
                        seen.add(r["rpid_str"])
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
