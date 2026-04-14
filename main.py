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
# Wbi 签名算法模块 (防风控核心)
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
            if videos and videos[0][1] == bv: return videos[0][0], videos[0][2]
            oid, title = get_video_info(bv, header)
            if oid:
                db.clear_videos(); db.add_video_to_db(oid, bv, title)
                logging.info("开始监控视频: %s", title)
                return oid, title
        time.sleep(10)
    return None, None

# ------------------------
# 动态轮询：修复内容提取 + 时效性过滤
# ------------------------
def init_extra_dynamics(header):
    return {uid: set() for uid in EXTRA_DYNAMIC_UIDS}

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

            # 只取最新一条进行判断
            item = items[0]
            id_str = item.get("id_str")
            if not id_str or id_str in seen_dynamics[uid]: continue

            # --- 时效性校验（修复 float-str 报错点） ---
            raw_pub_ts = item.get("modules", {}).get("module_author", {}).get("pub_ts", 0)
            try:
                pub_ts = float(raw_pub_ts)
            except (ValueError, TypeError):
                pub_ts = 0

            if now_ts - pub_ts > DYNAMIC_MAX_AGE:
                seen_dynamics[uid].add(id_str) # 标记为已读，防止重复扫描旧内容
                continue
            # ---------------------------------------

            seen_dynamics[uid].add(id_str)

            # 内容抓取：修复只能抓到“新动态”三字的逻辑
            dyn_text = ""
            try:
                module_dyn = item.get("modules", {}).get("module_dynamic", {})
                if module_dyn.get("desc"):
                    dyn_text = module_dyn["desc"].get("text", "")
                elif module_dyn.get("major", {}).get("archive", {}).get("desc"):
                    dyn_text = module_dyn["major"]["archive"].get("desc", "")
            except: pass

            dyn_type = item.get("type")
            attach_str = ""
            try:
                if dyn_type == "DYNAMIC_TYPE_AV":
                    title = item["modules"]["module_dynamic"]["major"]["archive"]["title"]
                    attach_str = f"🎥 视频：《{title}》"
                elif dyn_type == "DYNAMIC_TYPE_ARTICLE":
                    title = item["modules"]["module_dynamic"]["major"]["article"]["title"]
                    attach_str = f"📄 专栏：《{title}》"
                elif dyn_type == "DYNAMIC_TYPE_FORWARD":
                    attach_str = "🔄 转发了动态"
                elif dyn_type == "DYNAMIC_TYPE_LIVE_RCMD":
                    attach_str = "🔴 开启了直播"
            except: pass

            final_desc = ""
            if dyn_text: final_desc += f"【正文】:\n{dyn_text}\n"
            if attach_str: final_desc += f"【附带】: {attach_str}"
            if not final_desc: final_desc = "发布了新动态 (无文字描述)"

            name = str(uid)
            try: name = item["modules"]["module_author"]["name"]
            except: pass

            new_alerts.append({"user": name, "message": final_desc})
            logging.info("成功抓取动态 - %s: %s", name, final_desc.replace('\n', ' '))

        except Exception as e:
            logging.error(f"动态轮询出错 (UID: {uid}): {e}")

    if new_alerts:
        try: notifier.send_webhook_notification("💡 特别关注UP主发布新内容", new_alerts)
        except: pass

# ------------------------
# 视频评论监控：仅主评论
# ------------------------
def scan_new_comments(oid, header, last_read_time, seen):
    new_list = []
    max_ctime_in_this_round = last_read_time
    safe_read_time = last_read_time - 300
    
    pn = 1
    while pn <= 10:
        params = {"oid": oid, "type": 1, "sort": 0, "pn": pn, "ps": 20}
        try:
            data = wbi_request("https://api.bilibili.com/x/v2/reply", params, header)
            if data.get("code") != 0: break
            replies = data.get("data", {}).get("replies") or []
            if not replies: break
                
            page_all_older = True
            for r_obj in replies:
                rpid = r_obj["rpid_str"]
                r_ctime = r_obj["ctime"]
                max_ctime_in_this_round = max(max_ctime_in_this_round, r_ctime)
                
                if r_ctime > safe_read_time:
                    page_all_older = False
                    if rpid not in seen:
                        seen.add(rpid)
                        user = r_obj["member"]["uname"]
                        msg = r_obj["content"]["message"]
                        logging.info("成功抓取主评论 - %s: %s", user, msg[:30])
                        new_list.append({"user": user, "message": msg, "ctime": r_ctime})
                        
            if page_all_older: break
            pn += 1
            time.sleep(random.uniform(0.5, 1.0))
        except: break
    return new_list, max_ctime_in_this_round

# ------------------------
# 主循环守护
# ------------------------
def start_monitoring(header):
    last_check_video = time.time()
    last_heartbeat = time.time()
    oid, title = sync_latest_video(header)
    last_read_time = int(time.time()); seen_comments = set()
    seen_dynamics = init_extra_dynamics(header)

    logging.info("B站监控程序已启动 (优化版：仅最新动态 + 2分钟过滤 + 详细内容抓取)")

    while True:
        try:
            current = time.time()
            if is_work_time():
                # 1. 扫描视频主评论
                if oid:
                    new_list, new_last_read_time = scan_new_comments(oid, header, last_read_time, seen_comments)
                    if new_last_read_time > last_read_time: last_read_time = new_last_read_time
                    if new_list:
                        new_list.sort(key=lambda x: x["ctime"])
                        try: notifier.send_webhook_notification(title, new_list)
                        except: pass

                # 2. 扫描特别关注动态
                check_new_dynamics(header, seen_dynamics)

                # 3. 发送心跳
                if current - last_heartbeat >= HEARTBEAT_INTERVAL:
                    notifier.send_webhook_notification("监控心跳", [{"user": "系统", "message": f"程序运行正常\n监控视频: {title or '待获取'}"}])
                    last_heartbeat = current

                time.sleep(random.uniform(10, 20))
            else:
                time.sleep(30)

            # 4. 定时切换视频监控
            if current - last_check_video > VIDEO_CHECK_INTERVAL:
                new_oid, new_title = sync_latest_video(header)
                if new_oid and new_oid != oid:
                    oid, title = new_oid, new_title; last_read_time = int(time.time()); seen_comments.clear()
                last_check_video = current

        except Exception:
            logging.error(traceback.format_exc()); time.sleep(60)

if __name__ == "__main__":
    db.init_db(); h = get_header(); update_wbi_keys(h)
    start_monitoring(h)
