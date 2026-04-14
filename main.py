import sys
import time
import datetime
import subprocess
import random
import logging
import traceback
import requests
import hashlib
import urllib.parse
import database as db
import notifier

# ================= 核心配置区 =================
TARGET_UID = 1671203508           # 主监控视频评论的UP
VIDEO_CHECK_INTERVAL = 21600      # 6小时同步一次最新视频
HEARTBEAT_INTERVAL = 600          # 10分钟发一次运行心跳

# 动态监控名单
EXTRA_DYNAMIC_UIDS = [3546905852250875, 3546961271589219, 3546610447419885]
DYNAMIC_CHECK_INTERVAL = 60       # 动态日常检查间隔
DYNAMIC_BURST_INTERVAL = 10       # 发现新动态后的狂暴刷新间隔
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
# Wbi 签名算法加密模块 (防风控核心)
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
    url = "https://api.bilibili.com/x/web-interface/nav"
    try:
        r = requests.get(url, headers=header, timeout=10)
        data = r.json()
        wbi_img = data["data"]["wbi_img"]
        WBI_KEYS["img_key"] = wbi_img["img_url"].rsplit('/', 1)[1].split('.')[0]
        WBI_KEYS["sub_key"] = wbi_img["sub_url"].rsplit('/', 1)[1].split('.')[0]
        WBI_KEYS["last_update"] = time.time()
        logging.info("Wbi 密钥已自动更新")
    except Exception: pass

def wbi_request(url, params, header):
    if time.time() - WBI_KEYS["last_update"] > 21600 or not WBI_KEYS["img_key"]:
        update_wbi_keys(header)
    signed_params = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    try:
        r = requests.get(url, headers=header, params=signed_params, timeout=10)
        return r.json()
    except Exception: return {"code": -1}

# ------------------------
# 核心扫描逻辑（仅主评论 + 详细日志）
# ------------------------
def scan_new_comments(oid, header, last_read_time, seen):
    new_list = []
    max_ctime = last_read_time
    safe_time = last_read_time - 300 
    
    pn = 1
    while pn <= 5:  
        # 使用 sort=2 (时间排序) 确保抓取最新主评论
        params = {"oid": oid, "type": 1, "sort": 2, "pn": pn, "ps": 20}
        data = wbi_request("https://api.bilibili.com/x/v2/reply", params, header)
        if data.get("code") != 0: break
        replies = data.get("data", {}).get("replies") or []
        if not replies: break
            
        page_all_older = True  
        for r_obj in replies:
            rpid = r_obj["rpid_str"]
            r_ctime = r_obj["ctime"]
            max_ctime = max(max_ctime, r_ctime)
            
            if r_ctime > safe_time:
                page_all_older = False
                if rpid not in seen:
                    seen.add(rpid)
                    user = r_obj["member"]["uname"]
                    msg = r_obj["content"]["message"]
                    logging.info(f"成功抓取主评论: [{user}] {msg[:50]}...")
                    new_list.append({"user": user, "message": msg, "ctime": r_ctime})
        
        if page_all_older: break
        pn += 1
    return new_list, max_ctime

# ------------------------
# 动态监控模块
# ------------------------
def check_new_dynamics(header, seen_dyns, active_dyns):
    new_alerts, has_new = [], False
    for uid in EXTRA_DYNAMIC_UIDS:
        try:
            r = requests.get("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space", 
                             headers=header, params={"host_mid": uid}, timeout=10)
            data = r.json()
            if data.get("code") != 0: continue
            for item in data.get("data", {}).get("items", []):
                id_str = item.get("id_str")
                if id_str and id_str not in seen_dyns[uid]:
                    seen_dyns[uid].add(id_str)
                    has_new = True
                    txt = "发布了新动态"; name = "UP主"
                    try: txt = item["modules"]["module_dynamic"]["desc"]["text"]
                    except: pass
                    try: name = item["modules"]["module_author"]["name"]
                    except: pass
                    logging.info(f"成功发现新动态: [{name}] {txt[:50]}...")
                    new_alerts.append({"user": name, "message": txt[:200]})
                    basic = item.get("basic", {})
                    if basic.get("comment_id_str"):
                        active_dyns[uid][id_str] = {"oid": basic["comment_id_str"], "type": basic["comment_type"], "ctime": time.time()}
        except: continue
    if new_alerts: notifier.send_webhook_notification("💡 关注UP新动态", new_alerts)
    return has_new

def check_dynamic_replies(header, active_dyns, seen_replies):
    new_alerts = []
    curr = time.time()
    for uid, dyns in list(active_dyns.items()):
        for did, info in list(dyns.items()):
            if curr - info["ctime"] > 86400:
                del dyns[did]; continue
            params = {"oid": info["oid"], "type": info["type"], "sort": 2, "pn": 1, "ps": 5}
            data = wbi_request("https://api.bilibili.com/x/v2/reply", params, header)
            if data.get("code") == 0:
                reps = data.get("data", {}).get("replies") or []
                top = data.get("data", {}).get("upper", {}).get("top")
                if top: reps.append(top)
                for r in reps:
                    if r and str(r["member"]["mid"]) == str(uid) and r["rpid_str"] not in seen_replies:
                        seen_replies.add(r["rpid_str"])
                        logging.info(f"成功捕捉UP动态回复: [{r['member']['uname']}] {r['content']['message'][:50]}...")
                        new_alerts.append({"user": r["member"]["uname"], "message": f"💬 补充回复：\n{r['content']['message']}"})
    if new_alerts: notifier.send_webhook_notification("🔔 UP主动态出没", new_alerts)

# ------------------------
# 基础功能
# ------------------------
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

def sync_latest_video(header):
    try:
        r = requests.get("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space", 
                         headers=header, params={"host_mid": TARGET_UID}, timeout=10)
        data = r.json()
        for item in data.get("data", {}).get("items", []):
            if item.get("type") == "DYNAMIC_TYPE_AV":
                bv = item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
                # 获取详细aid
                v_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bv}"
                v_data = requests.get(v_url, headers=header).json()
                if v_data["code"] == 0:
                    aid, title = str(v_data["data"]["aid"]), v_data["data"]["title"]
                    videos = db.get_monitored_videos()
                    if not videos or videos[0][0] != aid:
                        db.clear_videos(); db.add_video_to_db(aid, bv, title)
                        logging.info(f"监控目标已切换: {title}")
                    return aid, title
    except: pass
    return None, None

def start_monitoring(header):
    last_v_check = 0; last_hb = time.time(); last_d_check = 0; burst_end = 0
    oid, title = sync_latest_video(header)
    last_read_time = int(time.time()); seen_c = set()
    seen_d = {uid: set() for uid in EXTRA_DYNAMIC_UIDS}; active_d = {uid: {} for uid in EXTRA_DYNAMIC_UIDS}; seen_dr = set()
    
    # 初始化动态ID
    for uid in EXTRA_DYNAMIC_UIDS:
        try:
            r = requests.get("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space", headers=header, params={"host_mid": uid}, timeout=10)
            for i in r.json().get("data", {}).get("items", []):
                if i.get("id_str"): seen_d[uid].add(i["id_str"])
        except: pass

    logging.info("监控服务启动成功（仅主评论版）...")

    while True:
        try:
            now = time.time()
            if not is_work_time(): time.sleep(30); continue

            # 1. 评论监控 (仅一级主评论)
            if oid:
                nc, nt = scan_new_comments(oid, header, last_read_time, seen_c)
                if nt > last_read_time: last_read_time = nt
                if nc:
                    nc.sort(key=lambda x: x["ctime"])
                    notifier.send_webhook_notification(title, nc)

            # 2. 动态监控
            d_iv = DYNAMIC_BURST_INTERVAL if now < burst_end else DYNAMIC_CHECK_INTERVAL
            if now - last_d_check >= d_iv:
                if check_new_dynamics(header, seen_d, active_d):
                    burst_end = now + DYNAMIC_BURST_DURATION
                check_dynamic_replies(header, active_d, seen_dr)
                last_d_check = now

            # 3. 辅助
            if now - last_hb >= HEARTBEAT_INTERVAL:
                notifier.send_webhook_notification("运行状态", [{"user": "系统", "message": f"正在监控: {title}"}])
                last_hb = now
            if now - last_v_check >= VIDEO_CHECK_INTERVAL:
                new_aid, new_title = sync_latest_video(header)
                if new_aid and new_aid != oid:
                    oid, title = new_aid, new_title; seen_c.clear(); last_read_time = int(time.time())
                last_v_check = now

            time.sleep(random.uniform(15, 25))
        except Exception:
            logging.error(traceback.format_exc()); time.sleep(60)

if __name__ == "__main__":
    db.init_db(); h = get_header(); update_wbi_keys(h)
    start_monitoring(h)
