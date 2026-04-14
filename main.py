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
DYNAMIC_MAX_AGE = 120             # 动态时效性限制：120秒（2分钟）
# ==============================================

logging.basicConfig(
    filename='bili_monitor.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    encoding='utf-8',
    filemode='a'
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
        time.sleep(1)
    signed_params = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    try:
        r = requests.get(url, headers=header, params=signed_params, timeout=10)
        data = r.json()
    except Exception: return {"code": -1}
    if data.get("code") == -400:
        update_wbi_keys(header)
        signed_params = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
        try:
            r = requests.get(url, headers=header, params=signed_params, timeout=10)
            data = r.json()
        except Exception: return {"code": -1}
    return data

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
        r = requests.get(f"https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?host_mid={TARGET_UID}", headers=header, timeout=10)
        data = r.json()
        for item in data.get("data", {}).get("items", []):
            if item.get("type") == "DYNAMIC_TYPE_AV":
                archive = item["modules"]["module_dynamic"]["major"]["archive"]
                aid, bv, title = str(archive["aid"]), archive["bvid"], archive["title"]
                v = db.get_monitored_videos()
                if not v or v[0][0] != aid:
                    db.clear_videos(); db.add_video_to_db(aid, bv, title)
                    logging.info(f"监控目标切换: {title}")
                return aid, title
    except Exception: pass
    return None, None

# ------------------------
# 动态轮询逻辑 (修复 NameError，确保定义在调用前)
# ------------------------
def init_extra_dynamics(header):
    seen = {}
    for uid in EXTRA_DYNAMIC_UIDS:
        seen[uid] = set()
        try:
            r = requests.get(f"https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?host_mid={uid}", headers=header, timeout=10)
            data = r.json()
            items = data.get("data", {}).get("items", [])
            if items:
                seen[uid].add(items[0].get("id_str"))
        except Exception: pass
    return seen

def check_new_dynamics(header, seen_dynamics):
    new_alerts = []
    now_ts = time.time()

    for uid in EXTRA_DYNAMIC_UIDS:
        url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
        params = {"host_mid": uid}
        try:
            r = requests.get(url, headers=header, params=params, timeout=10)
            data = r.json()
            if data.get("code") != 0: continue

            items = data.get("data", {}).get("items", [])
            if not items: continue

            item = items[0] 
            id_str = item.get("id_str")
            if not id_str or id_str in seen_dynamics[uid]: continue

            # 时效性校验
            try:
                pub_ts = float(item.get("modules", {}).get("module_author", {}).get("pub_ts", 0))
            except Exception: pub_ts = 0

            if now_ts - pub_ts > DYNAMIC_MAX_AGE:
                seen_dynamics[uid].add(id_str)
                continue

            seen_dynamics[uid].add(id_str)

            # 深度内容抓取
            dyn_text = ""
            module_dyn = item.get("modules", {}).get("module_dynamic", {})
            desc_node = module_dyn.get("desc")
            if desc_node and desc_node.get("text"):
                dyn_text = desc_node["text"]
            
            major = module_dyn.get("major", {})
            attach_str = ""
            if major:
                if major.get("archive"): 
                    arc = major["archive"]
                    attach_str = f"🎥 视频：《{arc.get('title')}》"
                elif major.get("article"): 
                    art = major["article"]
                    attach_str = f"📄 专栏：《{art.get('title')}》"
                elif major.get("live_rcmd"):
                    live = major["live_rcmd"]["content"]["live_play_info"]
                    attach_str = f"🔴 直播中：{live.get('title')}"

            final_desc = ""
            if dyn_text: final_desc += f"【正文】:\n{dyn_text}\n"
            if attach_str: final_desc += f"【关联】: {attach_str}"
            if not final_desc: final_desc = "发布了新动态"

            name = str(uid)
            try: name = item["modules"]["module_author"]["name"]
            except Exception: pass

            new_alerts.append({"user": name, "message": final_desc})
            logging.info(f"动态抓取成功 - {name}: {final_desc.replace(chr(10), ' ')}")

        except Exception as e:
            logging.error(f"动态扫描异常 (UID:{uid}): {e}")

    if new_alerts:
        try: notifier.send_webhook_notification("💡 特别关注UP主发布新内容", new_alerts)
        except Exception: pass

# ------------------------
# 视频评论扫描模块 (修复 SyntaxError 重点区)
# ------------------------
def scan_new_comments(oid, header, last_read_time, seen):
    new_list = []
    max_ctime = last_read_time
    safe_time = last_read_time - 300
    pn = 1
    while pn <= 5:
        params = {"oid": oid, "type": 1, "sort": 2, "pn": pn, "ps": 20}
        data = wbi_request("https://api.bilibili.com/x/v2/reply", params, header)
        if data.get("code") != 0: break
        replies = data.get("data", {}).get("replies") or []
        if not replies: break
        page_all_older = True
        for r in replies:
            rpid, ctime = r["rpid_str"], r["ctime"]
            max_ctime = max(max_ctime, ctime)
            if ctime > safe_time:
                page_all_older = False
                if rpid not in seen:
                    seen.add(rpid)
                    # --- 修复后的代码，确保引号和结构完全正确 ---
                    u_name = r["member"]["uname"]
                    u_msg = r["content"]["message"]
                    logging.info(f"成功抓取主评论: [{u_name}]")
                    new_list.append({
                        "user": u_name,
                        "message": u_msg,
                        "ctime": ctime
                    })
        if page_all_older: break
        pn += 1
        time.sleep(random.uniform(0.5, 1.0))
    return new_list, max_ctime

# ------------------------
# 主监控循环
# ------------------------
def start_monitoring(header):
    last_v_check = 0; last_hb = time.time(); last_d_check = 0
    oid, title = sync_latest_video(header)
    last_read_time = int(time.time()); seen_comments = set()
    
    # 初始化动态名单
    seen_dynamics = init_extra_dynamics(header)

    logging.info("B站监控程序已启动 (最终语法修正版)")

    while True:
        try:
            now = time.time()
            if is_work_time():
                # 1. 扫描评论
                if oid:
                    new_c, new_t = scan_new_comments(oid, header, last_read_time, seen_comments)
                    if new_t > last_read_time: last_read_time = new_t
                    if new_c:
                        new_c.sort(key=lambda x: x["ctime"])
                        try: notifier.send_webhook_notification(title, new_c)
                        except Exception: pass

                # 2. 扫描动态
                check_new_dynamics(header, seen_dynamics)

                # 3. 心跳
                if now - last_hb >= HEARTBEAT_INTERVAL:
                    notifier.send_webhook_notification("心跳", [{"user": "系统", "message": f"运行正常 | 视频: {title or '无'}"}])
                    last_hb = now

                time.sleep(random.uniform(10, 20))
            else:
                time.sleep(30)

            if now - last_v_check > VIDEO_CHECK_INTERVAL:
                res = sync_latest_video(header)
                if res: oid, title = res
                last_v_check = now
        except Exception:
            logging.error(traceback.format_exc()); time.sleep(60)

if __name__ == "__main__":
    db.init_db()
    h = get_header()
    update_wbi_keys(h)
    start_monitoring(h)
