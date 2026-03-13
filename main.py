import sys
import requests
import time
import datetime
import subprocess
import random
import pandas as pd
import logging
import traceback

import database as db
import notifier

TARGET_UID = 1671203508
VIDEO_CHECK_INTERVAL = 21600   # 6小时

# ------------------------
# 初始化日志
# ------------------------
logging.basicConfig(
    filename='bilibili_monitor.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    encoding='utf-8'
)

# ------------------------
# 获取Cookie
# ------------------------
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

# ------------------------
# 判断是否工作时间（中国周一~周五 9~19）
# ------------------------
def is_work_time():
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    weekday = now.weekday()  # 0=周一
    hour = now.hour
    return weekday < 5 and 9 <= hour < 16

# ------------------------
# 获取视频信息
# ------------------------
def get_video_info(bv, header):
    url = f"https://api.bilibili.com/x/web-interface/view?bvid={bv}"
    try:
        r = requests.get(url, headers=header, timeout=10)
        data = r.json()
        if data["code"] == 0:
            aid = data["data"]["aid"]
            title = data["data"]["title"]
            return str(aid), title
    except Exception as e:
        logging.error("获取视频信息失败: %s", e)
    return None, None

# ------------------------
# 获取UP最新视频
# ------------------------
def get_latest_video(header):
    url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    params = {"host_mid": TARGET_UID}
    try:
        r = requests.get(url, headers=header, params=params, timeout=10)
        data = r.json()
        if data["code"] != 0:
            logging.warning("获取动态失败: %s", data)
            return None
        items = data["data"]["items"]
        for item in items:
            try:
                if item["type"] == "DYNAMIC_TYPE_AV":
                    bv = item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
                    return bv
            except:
                continue
    except Exception as e:
        logging.error("获取视频失败: %s", e)
    return None

# ------------------------
# 同步最新视频
# ------------------------
def sync_latest_video(header):
    bv = get_latest_video(header)
    if not bv:
        return None, None
    videos = db.get_monitored_videos()
    if videos and videos[0][1] == bv:
        return videos[0][0], videos[0][2]
    logging.info("发现UP新视频")
    oid, title = get_video_info(bv, header)
    if not oid:
        return None, None
    db.clear_videos()
    db.add_video_to_db(oid, bv, title)
    logging.info("当前监控视频: %s", title)
    return oid, title

# ------------------------
# 获取评论
# ------------------------
def fetch_comments(oid, header):
    url = "https://api.bilibili.com/x/v2/reply/main"
    params = {"oid": oid, "type": 1, "mode": 2}
    try:
        r = requests.get(url, headers=header, params=params, timeout=10)
        data = r.json()
        return data.get("data", {}).get("replies", []) or []
    except:
        return []

# ------------------------
# 初始化历史评论
# ------------------------
def init_seen_comments(oid, header):
    seen = set()
    replies = fetch_comments(oid, header)
    for r in replies:
        seen.add(r["rpid_str"])
    logging.info("历史评论已忽略")
    return seen

# ------------------------
# 主监控循环
# ------------------------
def start_monitoring(header):
    last_video_check = 0
    while True:
        try:
            oid, title = sync_latest_video(header)
            if not oid:
                time.sleep(60)
                continue
            seen = init_seen_comments(oid, header)
            last_video_check = time.time()
            while True:
                now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
                logging.info("当前中国时间: %s", now)
                # 工作时间评论检测
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
                            "time": pd.to_datetime(r["ctime"], unit="s")
                        }
                        new_comments.append(comment)
                    if new_comments:
                        logging.info("新评论: %s", title)
                        for c in new_comments:
                            logging.info("%s : %s", c["user"], c["message"])
                        notifier.send_webhook_notification(title, new_comments)
                    wait = random.uniform(20, 40)
                    logging.info("工作时间，等待 %.1f 秒", wait)
                    time.sleep(wait)
                else:
                    logging.info("非工作时间，不检测评论")
                    # 非工作时间只等待6小时
                    time.sleep(VIDEO_CHECK_INTERVAL)
                # 每6小时检测新视频
                if time.time() - last_video_check > VIDEO_CHECK_INTERVAL:
                    new_oid, new_title = sync_latest_video(header)
                    if new_oid and new_oid != oid:
                        oid = new_oid
                        title = new_title
                        seen = init_seen_comments(oid, header)
                    last_video_check = time.time()
        except Exception as e:
            logging.error("程序异常: %s", traceback.format_exc())
            time.sleep(10)  # 异常等待10秒重试

# ------------------------
# 主程序入口
# ------------------------
if __name__ == "__main__":
    db.init_db()
    header = get_header()
    start_monitoring(header)