import sys
import os
import time
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
TARGET_UID = 1671203508
VIDEO_CHECK_INTERVAL = 21600
HEARTBEAT_INTERVAL = 600
EXTRA_DYNAMIC_UIDS = [
    3546905852250875,
    3546961271589219,
    3546610447419885,
    285340365,
    3706948578969654
]
DYNAMIC_CHECK_INTERVAL = 30
DYNAMIC_MAX_AGE = 600
LOG_FILE = "bili_monitor.log"

def init_logging():
    try:
        if os.path.exists(LOG_FILE):
            open(LOG_FILE, "w").close()
    except:
        pass
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", encoding="utf-8", filemode="w")
    logging.info("=" * 60)
    logging.info("B站监控系统启动")
    logging.info("=" * 60)

def safe_request(url, params, header, retries=3):
    h = header.copy()
    h["Connection"] = "close"
    for i in range(retries):
        try:
            r = requests.get(url, headers=h, params=params, timeout=10)
            if r.text.strip():
                return r.json()
        except:
            time.sleep(2 + i)
    return {"code": -500}

WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}
mixinKeyEncTab = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]

def getMixinKey(orig):
    return "".join([orig[i] for i in mixinKeyEncTab])[:32]

def encWbi(params, img_key, sub_key):
    mixin_key = getMixinKey(img_key + sub_key)
    params["wts"] = round(time.time())
    params = dict(sorted(params.items()))
    filtered = {k: str(v).translate({ord(c): None for c in "!'()*"}) for k, v in params.items()}
    query = urllib.parse.urlencode(filtered)
    sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
    filtered["w_rid"] = sign
    return filtered

def update_wbi_keys(header):
    try:
        data = safe_request("https://api.bilibili.com/x/web-interface/nav", None, header)
        if data.get("code") == 0:
            img = data["data"]["wbi_img"]
            WBI_KEYS["img_key"] = img["img_url"].rsplit("/", 1)[1].split(".")[0]
            WBI_KEYS["sub_key"] = img["sub_url"].rsplit("/", 1)[1].split(".")[0]
            WBI_KEYS["last_update"] = time.time()
    except:
        pass

def wbi_request(url, params, header):
    if not WBI_KEYS["img_key"] or time.time() - WBI_KEYS["last_update"] > 21600:
        update_wbi_keys(header)
    signed = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    return safe_request(url, signed, header)

def get_header():
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    except:
        subprocess.run([sys.executable, "login_bilibili.py"])
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    return {"Cookie": cookie, "User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}

def is_work_time():
    return True

# ---------------- 视频 ----------------
def get_latest_video(header):
    data = safe_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space", {"host_mid": TARGET_UID}, header)
    if data.get("code") != 0:
        return None
    items = (data.get("data") or {}).get("items", [])
    for item in items:
        try:
            if item.get("type") == "DYNAMIC_TYPE_AV":
                return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
        except:
            pass
    return None

def get_video_info(bv, header):
    data = safe_request(f"https://api.bilibili.com/x/web-interface/view?bvid={bv}", None, header)
    if data.get("code") == 0:
        return str(data["data"]["aid"]), data["data"]["title"]
    return None, None

def sync_latest_video(header):
    bv = get_latest_video(header)
    if not bv:
        return None, None
    videos = db.get_monitored_videos()
    if videos and videos[0][1] == bv:
        return videos[0][0], videos[0][2]
    oid, title = get_video_info(bv, header)
    if oid:
        db.clear_videos()
        db.add_video_to_db(oid, bv, title)
        return oid, title
    return None, None

# ---------------- 动态（极简+仅module_dynamic） ----------------
def deep_find_text(obj):
    result = []
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ["text", "content", "desc", "title"] and isinstance(v, str) and v.strip():
                    result.append(v.strip())
                walk(v)
        elif isinstance(x, list):
            for i in x:
                walk(i)
    walk(obj)
    return " ".join(dict.fromkeys(result)).strip()

def extract_dynamic_text(item):
    try:
        dyn = (item.get("modules") or {}).get("module_dynamic") or {}
        desc = dyn.get("desc") or {}
        text = desc.get("text") or ""
        if text:
            return str(text).strip()
        rich = desc.get("rich_text_nodes") or []
        if rich:
            texts = [str(n.get("orig_text") or n.get("text") or "").strip() for n in rich if isinstance(n, dict)]
            text = "\n".join(t for t in texts if t)
            if text:
                return text
        major = dyn.get("major") or {}
        if isinstance(major, dict) and major.get("type") in ["MAJOR_TYPE_OPUS", "MAJOR_TYPE_DRAW"]:
            opus = major.get("opus") or major.get("draw") or {}
            if isinstance(opus, dict):
                d = opus.get("desc") or {}
                if isinstance(d, dict):
                    t = d.get("text") or d.get("content") or ""
                    if t:
                        return str(t).strip()
        return deep_find_text(dyn) or "发布了新动态"
    except:
        return "发布了新动态"

def init_extra_dynamics(header):
    return {uid: set() for uid in EXTRA_DYNAMIC_UIDS}

def check_new_dynamics(header, seen_dynamics):
    alerts = []
    now_ts = time.time()
    for uid in EXTRA_DYNAMIC_UIDS:
        try:
            data = safe_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space", {"host_mid": uid}, header)
            if data.get("code") != 0:
                continue
            items = (data.get("data") or {}).get("items", [])
            if not items:
                continue
            item = items[0]
            id_str = item.get("id_str")
            if not id_str or id_str in seen_dynamics[uid]:
                continue
            modules = item.get("modules") or {}
            author = modules.get("module_author") or {}
            pub_ts = float(author.get("pub_ts", 0))
            if now_ts - pub_ts > DYNAMIC_MAX_AGE:
                continue
            seen_dynamics[uid].add(id_str)
            name = author.get("name", str(uid))
            text = extract_dynamic_text(item)
            final_msg = f"{text}\n\n🔗 https://t.bilibili.com/{id_str}"
            alerts.append({"user": name, "message": final_msg})
            logging.info(f"抓取新动态 [{name}]")
        except:
            pass
    if alerts:
        try:
            notifier.send_webhook_notification("💡 特别关注UP主发布新内容", alerts)
            logging.info(f"发送 {len(alerts)} 条动态通知")
        except Exception as e:
            logging.error(f"Webhook失败: {e}")
    return bool(alerts)

# ---------------- 评论（已完整复原原稳定版） ----------------
def scan_new_comments(oid, header, last_read_time, seen):
    new_list = []
    max_ctime = last_read_time
    safe_time = last_read_time - 300
    pn = 1
    while pn <= 10:
        data = wbi_request("https://api.bilibili.com/x/v2/reply", {"oid": oid, "type": 1, "sort": 0, "pn": pn, "ps": 20}, header)
        replies = (data.get("data") or {}).get("replies") or []
        if not replies:
            break
        page_old = True
        for r in replies:
            rpid = r["rpid_str"]
            ctime = r["ctime"]
            max_ctime = max(max_ctime, ctime)
            if ctime > safe_time:
                page_old = False
                if rpid not in seen:
                    seen.add(rpid)
                    new_list.append({"user": r["member"]["uname"], "message": r["content"]["message"], "ctime": ctime})
        if page_old:
            break
        pn += 1
        time.sleep(random.uniform(0.5, 1))
    return new_list, max_ctime

# ---------------- 主循环 ----------------
def start_monitoring(header):
    last_v_check = 0
    last_hb = time.time()
    last_d_check = 0
    burst_end = 0
    oid, title = sync_latest_video(header)
    last_read_time = int(time.time())
    seen_comments = set()
    seen_dynamics = init_extra_dynamics(header)
    logging.info("监控服务已启动")
    while True:
        try:
            now = time.time()
            if is_work_time():
                if oid:
                    new_c, new_t = scan_new_comments(oid, header, last_read_time, seen_comments)
                    if new_t > last_read_time:
                        last_read_time = new_t
                    if new_c:
                        new_c.sort(key=lambda x: x["ctime"])
                        try:
                            notifier.send_webhook_notification(title, new_c)
                        except Exception as e:
                            logging.error(f"评论通知失败: {e}")
                interval = 10 if now < burst_end else DYNAMIC_CHECK_INTERVAL
                if now - last_d_check >= interval:
                    if check_new_dynamics(header, seen_dynamics):
                        burst_end = now + 300
                    last_d_check = now
                if now - last_hb >= HEARTBEAT_INTERVAL:
                    try:
                        notifier.send_webhook_notification("心跳", [{"user": "系统", "message": "正常运行中"}])
                    except:
                        pass
                    last_hb = now
                time.sleep(random.uniform(10, 15))
            else:
                time.sleep(30)
            if now - last_v_check > VIDEO_CHECK_INTERVAL:
                res = sync_latest_video(header)
                if res:
                    oid, title = res
                last_v_check = now
        except:
            logging.error(traceback.format_exc())
            time.sleep(60)

if __name__ == "__main__":
    init_logging()
    db.init_db()
    h = get_header()
    update_wbi_keys(h)
    start_monitoring(h)
