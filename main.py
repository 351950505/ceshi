import sys
import time
import datetime
import subprocess
import random
import logging
import traceback
import pytz
import database as db
import notifier

TARGET_UID = 1671203508
VIDEO_CHECK_INTERVAL = 21600
HEARTBEAT_INTERVAL = 600

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
    return {"Cookie": cookie, "User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com"}

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
    except:
        pass
    return None, None

def get_latest_video(header):
    url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    params = {"host_mid": TARGET_UID}
    try:
        r = requests.get(url, headers=header, params=params, timeout=10)
        data = r.json()
        if data["code"] != 0: return None
        for item in data.get("data", {}).get("items", []):
            try:
                if item.get("type") == "DYNAMIC_TYPE_AV":
                    return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
            except:
                continue
    except:
        return None

def sync_latest_video(header):
    bv = get_latest_video(header)
    if not bv: return None, None
    videos = db.get_monitored_videos()
    if videos and videos[0][1] == bv:
        return videos[0][0], videos[0][2]
    oid, title = get_video_info(bv, header)
    if not oid: return None, None
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
    all_replies = []
    pn = 1
    while pn <= 6:
        params = {"oid": oid, "type": 1, "root": root_rpid, "pn": pn, "ps": 20}
        try:
            r = requests.get("https://api.bilibili.com/x/v2/reply/reply", headers=header, params=params, timeout=8)
            data = r.json()
            if data.get("code") != 0 or not data.get("data", {}).get("replies"):
                break
            all_replies.extend(data["data"]["replies"])
            pn += 1
            time.sleep(random.uniform(1.2, 2.0))
        except:
            break
    return all_replies

def send_exception_notification(error_msg):
    try:
        notifier.send_webhook_notification("程序异常", [{"user": "系统", "message": f"监控程序发生异常:\n{error_msg[:500]}"}])
    except:
        pass

def start_monitoring(header):
    last_check = time.time()
    last_heartbeat = time.time()
    oid, title = sync_latest_video(header)
    if not oid:
        send_exception_notification("初始视频获取失败")
        logging.error("初始视频获取失败，程序退出")
        sys.exit(1)

    seen = set(r["rpid_str"] for r in fetch_comments(oid, header))
    logging.info("程序启动成功，开始监控: %s", title)

    while True:
        try:
            current_time = time.time()

            # 10分钟心跳
            if is_work_time() and current_time - last_heartbeat >= HEARTBEAT_INTERVAL:
                now_str = datetime.datetime.now(china_tz).strftime("%Y-%m-%d %H:%M:%S")
                notifier.send_webhook_notification(
                    "监控心跳", 
                    [{"user": "系统", "message": f"程序运行正常\n时间: {now_str}\n监控视频: {title}"}]
                )
                last_heartbeat = current_time
                logging.info("已发送10分钟心跳")

            # 检测新评论
            if is_work_time():
                replies = fetch_comments(oid, header)
                new_list = []
                for r in replies:
                    rpid = r["rpid_str"]
                    if rpid in seen: continue
                    seen.add(rpid)
                    new_list.append({"user": r["member"]["uname"], "message": r["content"]["message"], "is_reply": False})

                    for sub in fetch_sub_replies(oid, rpid, header):
                        srpid = sub["rpid_str"]
                        if srpid in seen: continue
                        seen.add(srpid)
                        new_list.append({
                            "user": sub["member"]["uname"],
                            "message": sub["content"]["message"],
                            "is_reply": True,
                            "reply_to": r["member"]["uname"]
                        })

                if new_list:
                    logging.info("发现 %d 条新评论/回复", len(new_list))
                    for item in new_list:
                        prefix = f"回复@{item.get('reply_to','')} " if item["is_reply"] else ""
                        logging.info("%s%s : %s", prefix, item["user"], item["message"])
                    notifier.send_webhook_notification(title, new_list)

                time.sleep(random.uniform(25, 45))
            else:
                time.sleep(3600)

            if time.time() - last_check > VIDEO_CHECK_INTERVAL:
                new_oid, new_title = sync_latest_video(header)
                if new_oid and new_oid != oid:
                    oid, title = new_oid, new_title
                    seen = set(r["rpid_str"] for r in fetch_comments(oid, header))
                    logging.info("切换新视频")
                last_check = time.time()

        except Exception as e:
            err = traceback.format_exc()
            logging.error("程序异常: %s", err)
            send_exception_notification(err)
            time.sleep(60)

if __name__ == "__main__":
    db.init_db()
    header = get_header()
    logging.info("B站监控程序启动（10分钟心跳 + 异常提醒）")
    start_monitoring(header)
