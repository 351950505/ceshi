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
TARGET_UID = 1671203508           # 主监控视频评论的UP
VIDEO_CHECK_INTERVAL = 21600      # 6小时同步一次最新视频
HEARTBEAT_INTERVAL = 600          # 10分钟发一次运行心跳

# 动态监控名单
EXTRA_DYNAMIC_UIDS = [3546905852250875, 3546961271589219, 3546610447419885, 285340365]
DYNAMIC_CHECK_INTERVAL = 30       # 动态轮询频率
DYNAMIC_MAX_AGE = 300             # 动态时效性限制：300秒（5分钟）
# ==============================================

# 这里的 filemode='w' 确保每次启动程序都会清空 bili_monitor.log
logging.basicConfig(
    filename='bili_monitor.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    encoding='utf-8',
    filemode='w'
)

# ------------------------
# Wbi 签名算法模块
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
# 基础辅助模块
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
        url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
        r = requests.get(url, headers=header, params={"host_mid": TARGET_UID}, timeout=10)
        data = r.json()
        items = data.get("data", {}).get("items", [])
        for item in items:
            if item.get("type") == "DYNAMIC_TYPE_AV":
                arc = item.get("modules", {}).get("module_dynamic", {}).get("major", {}).get("archive", {})
                aid, bv, title = str(arc.get("aid")), arc.get("bvid"), arc.get("title")
                v = db.get_monitored_videos()
                if not v or v[0][0] != aid:
                    db.clear_videos()
                    db.add_video_to_db(aid, bv, title)
                    logging.info(f"监控目标切换: {title}")
                return aid, title
    except Exception: pass
    return None, None

# ------------------------
# 动态雷达
# ------------------------
def init_extra_dynamics(header):
    seen = {}
    for uid in EXTRA_DYNAMIC_UIDS:
        seen[uid] = set()
        try:
            url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
            r = requests.get(url, headers=header, params={"host_mid": uid}, timeout=10)
            items = r.json().get("data", {}).get("items", [])
            if items: seen[uid].add(items[0].get("id_str"))
        except Exception: pass
    return seen

def check_new_dynamics(header, seen_dynamics):
    new_alerts = []
    now_ts = time.time()
    for uid in EXTRA_DYNAMIC_UIDS:
        try:
            url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
            r = requests.get(url, headers=header, params={"host_mid": uid}, timeout=10)
            data = r.json()
            if data.get("code") != 0: continue
            items = data.get("data", {}).get("items", [])
            if not items: continue

            item = items[0]
            id_str = item.get("id_str")
            if not id_str or id_str in seen_dynamics[uid]: continue

            author_mod = item.get("modules", {}).get("module_author", {})
            try:
                pub_ts = float(author_mod.get("pub_ts", 0))
            except Exception: pub_ts = 0

            if now_ts - pub_ts > DYNAMIC_MAX_AGE:
                seen_dynamics[uid].add(id_str)
                continue

            seen_dynamics[uid].add(id_str)
            dyn_mod = item.get("modules", {}).get("module_dynamic", {})
            dyn_text = dyn_mod.get("desc", {}).get("text", "")
            major = dyn_mod.get("major", {})
            attach = ""
            if major.get("archive"): 
                attach = f"🎥 视频：《{major['archive'].get('title')}》"
            elif major.get("article"): 
                attach = f"📄 专栏：《{major['article'].get('title')}》"
            
            name = author_mod.get("name", str(uid))
            msg_body = f"【正文】: {dyn_text}\n【关联】: {attach}" if attach else dyn_text
            new_alerts.append({"user": name, "message": msg_body or "发布了新动态"})
            logging.info(f"动态扫描成功: {name}")
        except Exception: pass

    if new_alerts:
        try: notifier.send_webhook_notification("💡 特别关注UP主发布新内容", new_alerts)
        except Exception: pass

# ------------------------
# 视频评论扫描
# ------------------------
def scan_new_comments(oid, header, last_read_time, seen):
    new_list = []
    max_ctime = last_read_time
    safe_time = last_read_time - 300
    
    url = "https://api.bilibili.com/x/v2/reply"
    for pn in range(1, 4):
        data = wbi_request(url, {"oid": oid, "type": 1, "sort": 2, "pn": pn, "ps": 20}, header)
        if data.get("code") != 0: break
        
        replies = data.get("data", {}).get("replies") or []
        if not replies: break
        
        page_all_older = True
        for r in replies:
            ctime = r.get("ctime", 0)
            rpid = r.get("rpid_str")
            max_ctime = max(max_ctime, ctime)
            
            if ctime > safe_time:
                page_all_older = False
                if rpid not in seen:
                    seen.add(rpid)
                    uname = r.get("member", {}).get("uname", "未知用户")
                    message = r.get("content", {}).get("message", "")
                    logging.info(f"抓取评论成功: {uname}")
                    new_list.append({"user": uname, "message": message, "ctime": ctime})
        if page_all_older: break
        time.sleep(random.uniform(0.5, 1.0))
    return new_list, max_ctime

# ------------------------
# 主监控循环
# ------------------------
def start_monitoring(header):
    last_v_check = 0
    last_hb = time.time()
    last_d_check = 0
    
    oid, title = sync_latest_video(header)
    last_read_time = int(time.time())
    seen_comments = set()
    seen_dynamics = init_extra_dynamics(header)

    logging.info("B站监控程序已启动 (网络异常静默容错 & 日志自动重置版)")

    while True:
        try:
            now = time.time()
            if is_work_time():
                # 1. 评论监控
                if oid:
                    # 此处已修正所有中文字符逗号
                    new_c, new_t = scan_new_comments(oid, header, last_read_time, seen_comments)
                    if new_t > last_read_time:
                        last_read_time = new_t
                    if new_c:
                        new_c.sort(key=lambda x: x["ctime"])
                        try:
                            notifier.send_webhook_notification(title, new_c)
                        except Exception: pass

                # 2. 动态监控 (30秒频率)
                if now - last_d_check >= DYNAMIC_CHECK_INTERVAL:
                    check_new_dynamics(header, seen_dynamics)
                    last_d_check = now

                # 3. 心跳监控 (10分钟)
                if now - last_hb >= HEARTBEAT_INTERVAL:
                    try:
                        notifier.send_webhook_notification("心跳", [{"user": "系统", "message": "运行正常"}])
                    except Exception: pass
                    last_hb = now

                time.sleep(random.uniform(10, 15))
            else:
                time.sleep(30)

            # 定时更新监控视频 ID
            if now - last_v_check > VIDEO_CHECK_INTERVAL:
                res = sync_latest_video(header)
                if res:
                    oid, title = res
                last_v_check = now
        except Exception:
            # 记录主循环错误并静默重试
            logging.error(traceback.format_exc())
            time.sleep(60)

if __name__ == "__main__":
    db.init_db()
    h = get_header()
    # 第一次初始化密钥
    update_wbi_keys(h)
    start_monitoring(h)
