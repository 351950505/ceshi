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
DYNAMIC_CHECK_INTERVAL = 60       # 动态日常检查间隔(秒)
DYNAMIC_BURST_INTERVAL = 10       # 发现新动态后的狂暴模式刷新间隔(秒)
DYNAMIC_BURST_DURATION = 300      # 狂暴模式持续时间(5分钟)
# ==============================================

logging.basicConfig(
    filename='bili_monitor.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    encoding='utf-8',
    filemode='a'
)

# ------------------------
# Wbi 签名算法加密模块
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
    # 严格按照键名排序，这是 Wbi 签名的核心
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
    """极致稳健：手动拼接 Query 防止 requests 重排参数顺序导致签名失效"""
    if time.time() - WBI_KEYS["last_update"] > 21600 or not WBI_KEYS["img_key"]:
        update_wbi_keys(header)
        time.sleep(1) 
        
    signed_params = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    query_string = urllib.parse.urlencode(signed_params)
    full_url = f"{url}?{query_string}"
    
    try:
        r = requests.get(full_url, headers=header, timeout=10)
        return r.json()
    except Exception:
        return {"code": -1}

# ------------------------
# 辅助工具模块
# ------------------------
def get_header(oid=None):
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    except:
        logging.warning("未找到Cookie，启动扫码登录")
        subprocess.run([sys.executable, "login_bilibili.py"])
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    
    headers = {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*"
    }
    if oid:
        headers["Referer"] = f"https://www.bilibili.com/video/av{oid}"
    return headers

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

def get_latest_video(header, target_uid):
    url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    params = {"host_mid": target_uid}
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
        bv = get_latest_video(header, TARGET_UID)
        if bv:
            videos = db.get_monitored_videos()
            if videos and videos[0][1] == bv:
                return videos[0][0], videos[0][2]
            oid, title = get_video_info(bv, header)
            if oid:
                db.clear_videos()
                db.add_video_to_db(oid, bv, title)
                logging.info("开始监控新视频: %s", title)
                return oid, title
        time.sleep(10)
    return None, None

# ------------------------
# 动态监控模块
# ------------------------
def init_extra_dynamics(header):
    seen_dynamics, active_dynamics = {}, {}
    for uid in EXTRA_DYNAMIC_UIDS:
        seen_dynamics[uid], active_dynamics[uid] = set(), {}
        url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
        try:
            r = requests.get(url, headers=header, params={"host_mid": uid}, timeout=10)
            data = r.json()
            if data.get("code") == 0:
                for item in data.get("data", {}).get("items",[]):
                    id_str = item.get("id_str")
                    if id_str:
                        seen_dynamics[uid].add(id_str)
                        basic = item.get("basic", {})
                        if basic.get("comment_id_str"):
                            active_dynamics[uid][id_str] = {
                                "oid": basic["comment_id_str"], "type": basic["comment_type"], "ctime": time.time()
                            }
        except: pass
    return seen_dynamics, active_dynamics

def check_new_dynamics(header, seen_dynamics, active_dynamics):
    new_alerts, has_new = [], False
    for uid in EXTRA_DYNAMIC_UIDS:
        try:
            r = requests.get("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space", 
                             headers=header, params={"host_mid": uid}, timeout=10)
            data = r.json()
            if data.get("code") != 0: continue
            for item in data.get("data", {}).get("items",[]):
                id_str = item.get("id_str")
                if id_str and id_str not in seen_dynamics[uid]:
                    seen_dynamics[uid].add(id_str)
                    has_new = True
                    # 获取文案
                    msg = "发布了新内容"
                    try: msg = item["modules"]["module_dynamic"]["desc"]["text"]
                    except: pass
                    name = str(uid)
                    try: name = item["modules"]["module_author"]["name"]
                    except: pass
                    new_alerts.append({"user": name, "message": msg[:200]})
                    # 加入狂暴评论监控
                    basic = item.get("basic", {})
                    if basic.get("comment_id_str"):
                        active_dynamics[uid][id_str] = {
                            "oid": basic["comment_id_str"], "type": basic["comment_type"], "ctime": time.time()
                        }
        except: continue
    if new_alerts: notifier.send_webhook_notification("💡 特别关注UP新动态", new_alerts)
    return has_new

def check_dynamic_up_replies(header, active_dynamics, seen_replies):
    new_alerts = []
    curr = time.time()
    for uid, dyns in list(active_dynamics.items()):
        for did, info in list(dyns.items()):
            if curr - info["ctime"] > 86400: # 超过24小时停止监控回复
                del dyns[did]
                continue
            params = {"oid": info["oid"], "type": info["type"], "mode": 2, "next": 1, "ps": 10}
            data = wbi_request("https://api.bilibili.com/x/v2/reply/main", params, header)
            if data.get("code") == 0:
                replies = (data.get("data", {}).get("replies") or []) + ([data.get("data",{}).get("upper",{}).get("top")] if data.get("data",{}).get("upper",{}).get("top") else [])
                for r in replies:
                    if r and str(r["member"]["mid"]) == str(uid) and r["rpid_str"] not in seen_replies:
                        seen_replies.add(r["rpid_str"])
                        new_alerts.append({"user": r["member"]["uname"], "message": f"💬 补充动态评论：\n{r['content']['message']}"})
    if new_alerts: notifier.send_webhook_notification("🔔 UP主本尊动态回复", new_alerts)

# ------------------------
# 核心扫描逻辑：仅视频主评论
# ------------------------
def scan_new_comments(oid, header, last_read_time, seen):
    new_list = []
    max_ctime = last_read_time
    safe_time = last_read_time - 300 
    
    # 获取特定视频的 Header (带精准 Referer)
    current_header = get_header(oid)
    
    pn = 1
    while pn <= 3: # 时间排序下，最新评论都在前几页
        params = {
            "oid": oid,
            "type": 1,
            "mode": 2,  # 2 = 时间排序 (最新在前)
            "next": pn,
            "ps": 20
        }
        try:
            data = wbi_request("https://api.bilibili.com/x/v2/reply/main", params, current_header)
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
        except: break
    return new_list, max_ctime

# ------------------------
# 主循环控制
# ------------------------
def start_monitoring(header):
    last_check_video = time.time()
    last_heartbeat = time.time()
    last_dynamic_check = time.time()
    burst_end = 0
    
    oid, title = sync_latest_video(header)
    last_read_time = int(time.time())
    seen_comments = set()
    seen_dynamics, active_dynamics = init_extra_dynamics(header)
    seen_dyn_replies = set()

    logging.info("监控启动成功，当前目标: %s", title or "等待获取")

    while True:
        try:
            now = time.time()
            # 1. 心跳
            if is_work_time() and now - last_heartbeat >= HEARTBEAT_INTERVAL:
                notifier.send_webhook_notification("监控心跳", [{"user": "系统", "message": f"正在运行\n目标: {title}"}])
                last_heartbeat = now
            
            # 2. 视频主评论监控
            if is_work_time() and oid:
                new_comments, new_time = scan_new_comments(oid, header, last_read_time, seen_comments)
                if new_time > last_read_time: last_read_time = new_time
                if new_comments:
                    new_comments.sort(key=lambda x: x["ctime"])
                    notifier.send_webhook_notification(title, new_comments)
            
            # 3. 动态雷达 (含狂暴模式)
            interval = DYNAMIC_BURST_INTERVAL if now < burst_end else DYNAMIC_CHECK_INTERVAL
            if now - last_dynamic_check >= interval:
                if check_new_dynamics(header, seen_dynamics, active_dynamics):
                    burst_end = now + DYNAMIC_BURST_DURATION
                    logging.info("🔥 发现动态，激活狂暴模式")
                check_dynamic_up_replies(header, active_dynamics, seen_dyn_replies)
                last_dynamic_check = time.time()

            # 4. 定时刷新监控视频
            if now - last_check_video > VIDEO_CHECK_INTERVAL:
                new_oid, new_title = sync_latest_video(header)
                if new_oid and new_oid != oid:
                    oid, title, last_read_time = new_oid, new_title, int(time.time())
                    seen_comments.clear()
                last_check_video = now

            time.sleep(random.uniform(10, 20) if is_work_time() else 60)
            
        except Exception:
            logging.error(traceback.format_exc())
            time.sleep(60)

if __name__ == "__main__":
    db.init_db()
    current_header = get_header()
    update_wbi_keys(current_header)
    start_monitoring(current_header)
