import sys
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

# Wbi 签名模块
WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}
mixinKeyEncTab = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52
]

def getMixinKey(orig: str):
    return "".join([orig[i] for i in mixinKeyEncTab])[:32]

def encWbi(params: dict, img_key: str, sub_key: str):
    mixin_key = getMixinKey(img_key + sub_key)
    curr_time = round(time.time())
    params['wts'] = curr_time
    params = dict(sorted(params.items()))
    filtered_params = {}
    for k, v in params.items():
        v_str = str(v)
        for char in "!'()*":
            v_str = v_str.replace(char, '')
        filtered_params[k] = v_str
    query = urllib.parse.urlencode(filtered_params)
    wbi_sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
    filtered_params['w_rid'] = wbi_sign
    return filtered_params

def update_wbi_keys(header):
    url = "https://api.bilibili.com/x/web-interface/nav"
    try:
        r = requests.get(url, headers=header, timeout=10)
        data = r.json()
        wbi_img = data["data"]["wbi_img"]
        WBI_KEYS["img_key"] = wbi_img["img_url"].rsplit('/', 1)[1].split('.')[0]
        WBI_KEYS["sub_key"] = wbi_img["sub_url"].rsplit('/', 1)[1].split('.')[0]
        WBI_KEYS["last_update"] = time.time()
        logging.info("Wbi 密钥已自动更新")
    except Exception as e:
        logging.error("获取 Wbi 密钥失败: %s", e)

def wbi_request(url, params, header):
    if time.time() - WBI_KEYS["last_update"] > 21600 or not WBI_KEYS["img_key"]:
        update_wbi_keys(header)
        time.sleep(1)
    signed_params = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    try:
        r = requests.get(url, headers=header, params=signed_params, timeout=10)
        data = r.json()
    except Exception:
        return {"code": -1}
    if data.get("code") == -400:
        update_wbi_keys(header)
        signed_params = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
        try:
            r = requests.get(url, headers=header, params=signed_params, timeout=10)
            data = r.json()
        except Exception:
            return {"code": -1}
    return data

def get_header():
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    except:
        subprocess.run([sys.executable, "login_bilibili.py"])
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    return {"Cookie": cookie, "User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com"}

def is_work_time():
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
    return now.weekday() < 5 and 9 <= now.hour < 19

def get_video_info(bv, header):
    url = f"https://api.bilibili.com/x/web-interface/view?bvid={bv}"
    try:
        r = requests.get(url, headers=header, timeout=10)
        data = r.json()
        if data["code"] == 0:
            return str(data["data"]["aid"]), data["data"]["title"]
    except: pass
    return None, None

def get_latest_video(header):
    url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    params = {"host_mid": TARGET_UID}
    try:
        r = requests.get(url, headers=header, params=params, timeout=10)
        data = r.json()
        if data.get("code") != 0: return None
        for item in data.get("data", {}).get("items",[]):
            if item.get("type") == "DYNAMIC_TYPE_AV":
                return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
    except: pass
    return None

def sync_latest_video(header):
    for i in range(5):
        bv = get_latest_video(header)
        if bv:
            videos = db.get_monitored_videos()
            if videos and videos[0][1] == bv:
                return videos[0][0], videos[0][2]
            oid, title = get_video_info(bv, header)
            if oid:
                db.clear_videos()
                db.add_video_to_db(oid, bv, title)
                logging.info("开始监控视频: %s", title)
                return oid, title
        time.sleep(10)
    return None, None

def send_exception_notification(msg):
    try:
        notifier.send_webhook_notification("程序异常", [{"user": "系统", "message": msg}])
    except: pass

# 只监控主评论（已取消子评论功能）
def scan_new_comments(oid, header, last_read_time, seen):
    new_list = []
    max_ctime_in_this_round = last_read_time
    safe_read_time = last_read_time - 300
   
    pn = 1
    while pn <= 10:
        params = {"oid": oid, "type": 1, "sort": 0, "pn": pn, "ps": 20}
        try:
            data = wbi_request("https://api.bilibili.com/x/v2/reply", params, header)
            replies = data.get("data", {}).get("replies") or []
            if not replies:
                break
            page_all_older = True
            for r_obj in replies:
                rpid = r_obj["rpid_str"]
                r_ctime = r_obj["ctime"]
                max_ctime_in_this_round = max(max_ctime_in_this_round, r_ctime)
                if r_ctime > safe_read_time:
                    page_all_older = False
                    if rpid not in seen:
                        seen.add(rpid)
                        new_list.append({
                            "user": r_obj["member"]["uname"],
                            "message": r_obj["content"]["message"],
                            "ctime": r_ctime
                        })
            if page_all_older:
                break
            pn += 1
            time.sleep(random.uniform(0.5, 1.0))
        except Exception:
            break
    return new_list, max_ctime_in_this_round

def start_monitoring(header):
    last_check = time.time()
    last_heartbeat = time.time()
    oid, title = sync_latest_video(header)
    if not oid:
        send_exception_notification("初始视频获取失败（已重试5次）")
        oid, title = None, "待获取视频"
    last_read_time = int(time.time())
    seen = set()
   
    logging.info("程序启动成功，仅监控主评论")
    while True:
        try:
            current = time.time()
            if is_work_time() and current - last_heartbeat >= HEARTBEAT_INTERVAL:
                notifier.send_webhook_notification(
                    "监控心跳",
                    [{"user": "系统", "message": f"程序运行正常\n监控视频: {title or '待获取'}"}]
                )
                last_heartbeat = current
            if is_work_time() and oid:
                new_list, new_last_read_time = scan_new_comments(oid, header, last_read_time, seen)
                if new_last_read_time > last_read_time:
                    last_read_time = new_last_read_time
                if new_list:
                    new_list.sort(key=lambda x: x["ctime"])
                    logging.info("发现 %d 条新主评论", len(new_list))
                    try:
                        notifier.send_webhook_notification(title, new_list)
                    except:
                        pass
                time.sleep(random.uniform(10, 20))
            else:
                time.sleep(30)
            if time.time() - last_check > VIDEO_CHECK_INTERVAL:
                new_oid, new_title = sync_latest_video(header)
                if new_oid and new_oid != oid:
                    oid, title = new_oid, new_title
                    last_read_time = int(time.time())
                    seen.clear()
                last_check = time.time()
        except Exception as e:
            err = traceback.format_exc()
            logging.error("程序异常: %s", err)
            time.sleep(60)

if __name__ == "__main__":
    db.init_db()
    header = get_header()
    update_wbi_keys(header)
    logging.info("B站监控程序启动（仅主评论版）")
    start_monitoring(header)
