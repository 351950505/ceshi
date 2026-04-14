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

# ================= 核心配置区 =================
TARGET_UID = 1671203508           # 主监控UP主(监控最新视频主评论)
VIDEO_CHECK_INTERVAL = 21600      # 6小时刷新一次目标UP的最新视频
HEARTBEAT_INTERVAL = 600          # 10分钟发一次运行心跳

# 监听其他UP主动态的UID列表
EXTRA_DYNAMIC_UIDS = [3546905852250875, 3546961271589219, 3546610447419885]
DYNAMIC_CHECK_INTERVAL = 60       # 动态检查间隔
DYNAMIC_BURST_INTERVAL = 10       # 狂暴模式间隔
DYNAMIC_BURST_DURATION = 300      # 狂暴模式持续时间
# ==============================================

logging.basicConfig(
    filename='bili_monitor.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    encoding='utf-8',
    filemode='a'
)

# ------------------------
# Wbi 签名算法 (修复顺序校验)
# ------------------------
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
        for char in "!'()*": v_str = v_str.replace(char, '')
        filtered_params[k] = v_str
    query = urllib.parse.urlencode(filtered_params)
    wbi_sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
    filtered_params['w_rid'] = wbi_sign
    return filtered_params

def update_wbi_keys(header):
    try:
        r = requests.get("https://api.bilibili.com/x/web-interface/nav", headers=header, timeout=10)
        data = r.json()
        wbi_img = data["data"]["wbi_img"]
        WBI_KEYS["img_key"] = wbi_img["img_url"].rsplit('/', 1)[1].split('.')[0]
        WBI_KEYS["sub_key"] = wbi_img["sub_url"].rsplit('/', 1)[1].split('.')[0]
        WBI_KEYS["last_update"] = time.time()
        logging.info("Wbi 密钥已更新")
    except: logging.error("获取 Wbi 密钥失败")

def wbi_request(url, params, header):
    if time.time() - WBI_KEYS["last_update"] > 21600 or not WBI_KEYS["img_key"]:
        update_wbi_keys(header)
    
    signed_params = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    query_string = urllib.parse.urlencode(signed_params)
    full_url = f"{url}?{query_string}"
    
    try:
        r = requests.get(full_url, headers=header, timeout=10)
        return r.json()
    except: return {"code": -1}

# ------------------------
# 核心逻辑：获取最新视频/评论
# ------------------------
def get_header(oid=None):
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    except:
        subprocess.run([sys.executable, "login_bilibili.py"])
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    h = {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/"
    }
    if oid: h["Referer"] = f"https://www.bilibili.com/video/av{oid}"
    return h

def get_latest_video(header, target_uid):
    url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    try:
        r = requests.get(url, headers=header, params={"host_mid": target_uid}, timeout=10)
        data = r.json()
        for item in data.get("data", {}).get("items", []):
            if item.get("type") == "DYNAMIC_TYPE_AV":
                archive = item["modules"]["module_dynamic"]["major"]["archive"]
                return str(archive["aid"]), archive["title"], archive["bvid"]
    except: pass
    return None, None, None

def sync_latest_video(header):
    aid, title, bv = get_latest_video(header, TARGET_UID)
    if aid:
        videos = db.get_monitored_videos()
        if not videos or videos[0][0] != aid:
            db.clear_videos()
            db.add_video_to_db(aid, bv, title)
            logging.info(f"监控新视频: {title}")
        return aid, title
    return None, None

def scan_new_comments(oid, header, last_read_time, seen):
    """
    核心修复点：
    1. 使用 /reply/main 接口
    2. 使用 mode: 2 (最新排序)
    3. 只取 replies 第一层(主评论)
    """
    new_list = []
    max_ctime = last_read_time
    safe_time = last_read_time - 300 
    
    # 强制构建带 Referer 的 Header
    curr_header = get_header(oid)
    
    pn = 1
    while pn <= 3:
        params = {
            "oid": oid,
            "type": 1,
            "mode": 2, # 2=最新排序，0=热度排序
            "next": pn,
            "ps": 20
        }
        data = wbi_request("https://api.bilibili.com/x/v2/reply/main", params, curr_header)
        
        if data.get("code") != 0: break
        replies = data.get("data", {}).get("replies") or []
        if not replies: break
            
        page_all_older = True
        for r in replies:
            rpid = r["rpid_str"]
            ctime = r["ctime"]
            max_ctime = max(max_ctime, ctime)
            
            if ctime > safe_time:
                page_all_older = False
                if rpid not in seen:
                    seen.add(rpid)
                    new_list.append({
                        "user": r["member"]["uname"],
                        "message": r["content"]["message"],
                        "ctime": ctime
                    })
        if page_all_older: break
        pn += 1
        time.sleep(random.uniform(2, 3))
    
    return new_list, max_ctime

# ------------------------
# 动态雷达逻辑
# ------------------------
def init_extra_dynamics(header):
    seen, active = {}, {}
    for uid in EXTRA_DYNAMIC_UIDS:
        seen[uid], active[uid] = set(), {}
        try:
            r = requests.get("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space", 
                             headers=header, params={"host_mid": uid}, timeout=10)
            data = r.json()
            if data.get("code") == 0:
                for item in data.get("data", {}).get("items", []):
                    if item.get("id_str"): seen[uid].add(item["id_str"])
        except: pass
    return seen, active

def check_new_dynamics(header, seen, active):
    new_alerts, has_new = [], False
    for uid in EXTRA_DYNAMIC_UIDS:
        try:
            r = requests.get("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space", 
                             headers=header, params={"host_mid": uid}, timeout=10)
            data = r.json()
            for item in data.get("data", {}).get("items", []):
                id_str = item.get("id_str")
                if id_str and id_str not in seen[uid]:
                    seen[uid].add(id_str)
                    has_new = True
                    # 获取文案
                    txt = "发布了新动态"
                    try: txt = item["modules"]["module_dynamic"]["desc"]["text"]
                    except: pass
                    name = str(uid)
                    try: name = item["modules"]["module_author"]["name"]
                    except: pass
                    new_alerts.append({"user": name, "message": txt[:200]})
                    # 加入狂暴监控
                    basic = item.get("basic", {})
                    if basic.get("comment_id_str"):
                        active[uid][id_str] = {"oid": basic["comment_id_str"], "type": basic["comment_type"], "ctime": time.time()}
        except: continue
    if new_alerts: notifier.send_webhook_notification("💡 特别关注UP动态", new_alerts)
    return has_new

def check_dynamic_up_replies(header, active, seen_replies):
    new_alerts = []
    curr = time.time()
    for uid, dyns in list(active.items()):
        for did, info in list(dyns.items()):
            if curr - info["ctime"] > 86400:
                del dyns[did]
                continue
            params = {"oid": info["oid"], "type": info["type"], "mode": 2, "next": 1, "ps": 10}
            data = wbi_request("https://api.bilibili.com/x/v2/reply/main", params, header)
            if data.get("code") == 0:
                replies = (data.get("data", {}).get("replies") or [])
                top = data.get("data", {}).get("upper", {}).get("top")
                if top: replies.append(top)
                for r in replies:
                    if r and str(r["member"]["mid"]) == str(uid) and r["rpid_str"] not in seen_replies:
                        seen_replies.add(r["rpid_str"])
                        new_alerts.append({"user": r["member"]["uname"], "message": f"💬 补充动态评论：\n{r['content']['message']}"})
    if new_alerts: notifier.send_webhook_notification("🔔 UP主动态出没", new_alerts)

# ------------------------
# 主循环
# ------------------------
def is_work_time():
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
    return now.weekday() < 5 and 9 <= now.hour < 19

def start_monitoring(header):
    last_v_check = 0
    last_hb = time.time()
    last_d_check = 0
    burst_end = 0
    
    aid, title = sync_latest_video(header)
    last_read_time = int(time.time())
    seen_comments = set()
    
    seen_dyns, active_dyns = init_extra_dynamics(header)
    seen_dyn_replies = set()

    logging.info(f"监控启动。当前视频: {title}")

    while True:
        try:
            now = time.time()
            if not is_work_time():
                time.sleep(60)
                continue

            # 1. 心跳
            if now - last_hb >= HEARTBEAT_INTERVAL:
                notifier.send_webhook_notification("心跳", [{"user": "系统", "message": f"运行中\n监控视频: {title}"}])
                last_hb = now

            # 2. 扫描视频主评论 (修复后的核心)
            if aid:
                new_c, new_t = scan_new_comments(aid, header, last_read_time, seen_comments)
                if new_t > last_read_time: last_read_time = new_t
                if new_comments := new_c:
                    new_comments.sort(key=lambda x: x["ctime"])
                    notifier.send_webhook_notification(title, new_comments)

            # 3. 动态雷达
            d_interval = DYNAMIC_BURST_INTERVAL if now < burst_end else DYNAMIC_CHECK_INTERVAL
            if now - last_d_check >= d_interval:
                if check_new_dynamics(header, seen_dyns, active_dyns):
                    burst_end = now + DYNAMIC_BURST_DURATION
                check_dynamic_up_replies(header, active_dyns, seen_dyn_replies)
                last_d_check = now

            # 4. 定时刷新视频
            if now - last_v_check >= VIDEO_CHECK_INTERVAL:
                new_aid, new_title = sync_latest_video(header)
                if new_aid and new_aid != aid:
                    aid, title = new_aid, new_title
                    last_read_time = int(time.time())
                    seen_comments.clear()
                last_v_check = now

            time.sleep(random.uniform(10, 20))

        except Exception:
            logging.error(traceback.format_exc())
            time.sleep(30)

if __name__ == "__main__":
    db.init_db()
    h = get_header()
    update_wbi_keys(h)
    start_monitoring(h)
