import sys
import os
import time
import subprocess
import random
import logging
import traceback
import hashlib
import urllib.parse
import json
import requests
from collections import defaultdict
import datetime
import threading
import queue

import database as db
import notifier

# ================= 核心配置 =================
TARGET_UID = 1671203508
HEARTBEAT_INTERVAL = 30
FOLLOWING_REFRESH_INTERVAL = 3600
SOURCE_UID = 3706948578969654

FALLBACK_DYNAMIC_UIDS = [
    "3546905852250875", "3546961271589219", "3546610447419885",
    "285340365", "3706948578969654"
]

COMMENT_SCAN_INTERVAL = 5
COMMENT_MAX_PAGES = 3
COMMENT_SAFE_WINDOW = 60

LOG_FILE = "bili_monitor.log"
DYNAMIC_STATE_FILE = "dynamic_state.json"
FOLLOWING_CACHE_FILE = "following_cache.json"

# 优化参数
INIT_SLEEP_MIN = 3.5
INIT_SLEEP_MAX = 6.5
STATE_SAVE_INTERVAL = 25
SEEN_CLEAN_INTERVAL = 3600
# =============================================

_last_log_time = defaultdict(float)
_last_notify_time = defaultdict(float)
push_queue = queue.Queue()

last_state_save = 0
last_seen_clean = 0

def push_worker():
    while True:
        try:
            item = push_queue.get()
            if item:
                notifier.send_webhook_notification("💡 特别关注UP主发布新内容", [item])
        except Exception as e:
            logging.error(f"推送失败: {e}")
        time.sleep(0.2)

def should_log(key, interval=600):
    now = time.time()
    if now - _last_log_time[key] >= interval:
        _last_log_time[key] = now
        return True
    return False

def send_failure_notification(title, msg):
    key = f"{title}:{msg[:80]}"
    if time.time() - _last_notify_time.get(key, 0) >= 600:
        _last_notify_time[key] = time.time()
        try:
            notifier.send_webhook_notification(title, [{"user": "系统", "message": msg}])
        except:
            pass

def init_logging():
    if os.path.exists(LOG_FILE):
        open(LOG_FILE, "w").close()
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        encoding="utf-8", filemode="w")
    logging.info("="*70)
    logging.info("B站高频UP监控系统 - 最终优化版 (动态/评论带时间)")
    logging.info("="*70)

def safe_request(url, params, header, retries=5):
    h = header.copy()
    h["Connection"] = "close"
    base = 3
    for i in range(retries):
        try:
            r = requests.get(url, headers=h, params=params, timeout=12)
            data = r.json()
            code = data.get("code")
            if code == -101:
                send_failure_notification("Cookie失效", "需要重新登录")
                return {"code": -101}
            if code in (-799, -352, -509):
                wait = base * (2 ** i) + random.uniform(2, 5)
                if should_log(f"ratelimit_{code}"):
                    logging.warning(f"风控 {code}，等待 {wait:.1f}s")
                time.sleep(wait)
                continue
            if code != 0 and i < retries-1:
                time.sleep(base * (2 ** i) + random.uniform(0.5, 2))
                continue
            return data
        except:
            time.sleep(base * (2 ** i) + random.uniform(0.5, 2))
    send_failure_notification("API请求失败", "所有重试均失败")
    return {"code": -500}

# WBI相关函数（保持不变，略微精简）
WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}
mixinKeyEncTab = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,
                  37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]

def getMixinKey(orig): return ''.join([orig[i] for i in mixinKeyEncTab])[:32]

def encWbi(params, img_key, sub_key):
    mixin_key = getMixinKey(img_key + sub_key)
    params["wts"] = int(time.time())
    params = dict(sorted(params.items()))
    filtered = {k: str(v).replace("!", "").replace("'", "").replace("(", "").replace(")", "").replace("*", "") 
                for k, v in params.items()}
    query = urllib.parse.urlencode(filtered, quote_via=urllib.parse.quote)
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
    except Exception as e:
        logging.error(f"WBI更新异常: {e}")

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
        subprocess.run([sys.executable, "login_bilibili.py"], check=False)
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    return {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.bilibili.com/"
    }

# 其他函数（get_following_list, load/save cache, load/save state, extract_dynamic_text 等保持必要逻辑）
# 为保持简洁，这里只保留关键修改部分，完整逻辑与你上一版一致

def init_dynamic_states_for_uids(uids, header):
    seen = {}
    state = load_dynamic_state()
    for uid in uids:
        uid_str = str(uid)
        seen[uid_str] = set()
        if uid_str not in state or not isinstance(state[uid_str], dict):
            state[uid_str] = {"last_ts": 0, "baseline": "", "offset": ""}
        try:
            data = wbi_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all",
                             {"host_mid": uid_str, "type": "all", "platform": "web"}, header)
            # 初始化逻辑保持不变（省略具体解析，实际使用你之前版本）
            logging.info(f"初始化 UID {uid_str} 完成")
        except:
            logging.error(f"初始化 UID {uid_str} 失败")
        time.sleep(random.uniform(INIT_SLEEP_MIN, INIT_SLEEP_MAX))
    save_dynamic_state(state)
    return seen

def check_new_dynamics_for_uid(uid, header, seen_dynamics, state, now_ts):
    uid_str = str(uid)
    current = state.setdefault(uid_str, {"last_ts": 0, "baseline": "", "offset": ""})
    last_ts = current["last_ts"]
    offset = current.get("offset", "")

    data = wbi_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all",
                      {"host_mid": uid_str, "type": "all", "platform": "web", "offset": offset} if offset else 
                      {"host_mid": uid_str, "type": "all", "platform": "web"}, header)
    if data.get("code") != 0:
        return False

    items = (data.get("data") or {}).get("items") or []
    new_offset = data.get("data", {}).get("offset", offset)
    max_pub_ts = last_ts

    for item in items:
        if not isinstance(item, dict): continue
        modules = item.get("modules") or {}
        author = modules.get("module_author") or {}
        pub_ts = author.get("pub_ts", 0)
        dyn_id = item.get("id_str")
        if pub_ts > max_pub_ts:
            max_pub_ts = pub_ts
        if pub_ts > last_ts and (now_ts - pub_ts <= 300):
            name = author.get("name", uid_str)
            text = extract_dynamic_text(item)
            time_str = datetime.datetime.fromtimestamp(pub_ts).strftime('%Y-%m-%d %H:%M:%S')
            final_msg = f"{text}\n\n📅 发布于: {time_str}\n🔗 https://t.bilibili.com/{dyn_id}" if text else f"📅 发布于: {time_str}\n🔗 https://t.bilibili.com/{dyn_id}"
            push_queue.put({"user": name, "message": final_msg})
            logging.info(f"新动态 [{name}] {dyn_id}")

    if max_pub_ts > last_ts:
        current["last_ts"] = max_pub_ts
    if new_offset:
        current["offset"] = new_offset
    return True

def scan_new_comments(oid, header, last_read_time, seen):
    new_list = []
    max_ctime = last_read_time
    url = "https://api.bilibili.com/x/v2/reply"
    pn = 1
    for _ in range(COMMENT_MAX_PAGES):
        data = wbi_request(url, {"type":1, "oid":oid, "sort":0, "nohot":1, "ps":20, "pn":pn}, header)
        if data.get("code") != 0: break
        replies = (data.get("data") or {}).get("replies") or []
        if not replies: break
        for r in replies:
            ctime = r.get("ctime", 0)
            if ctime > max_ctime: max_ctime = ctime
            if ctime > last_read_time - COMMENT_SAFE_WINDOW:
                rpid = str(r.get("rpid", ""))
                if rpid not in seen:
                    seen.add(rpid)
                    comment_time = datetime.datetime.fromtimestamp(ctime).strftime('%H:%M:%S')
                    new_list.append({
                        "user": f"[{comment_time}] {r['member']['uname']}",
                        "message": r["content"]["message"]
                    })
        if len(replies) < 20: break
        pn += 1
        time.sleep(0.4)
    return new_list, max_ctime

def get_batch():
    hm = time.localtime().tm_hour * 100 + time.localtime().tm_min
    return 28 if 928 <= hm <= 1000 else 20

def is_work_time(now=None):
    if now is None: now = datetime.datetime.now()
    if now.weekday() >= 5: return False
    return datetime.time(8,30) <= now.time() <= datetime.time(17,0)

def get_sleep_until_work_time():
    now = datetime.datetime.now()
    target = datetime.datetime(now.year, now.month, now.day, 8, 30)
    if now > target: target += datetime.timedelta(days=1)
    while target.weekday() >= 5:
        target += datetime.timedelta(days=1)
    return max(1, (target - now).total_seconds())

def start_monitoring(header):
    global last_state_save, last_seen_clean
    oid, title = None, None
    last_read_time = int(time.time())
    seen_comments = set()
    following_list = load_following_cache() or get_following_list(SOURCE_UID, header) or FALLBACK_DYNAMIC_UIDS[:]
    if str(SOURCE_UID) not in following_list:
        following_list.append(str(SOURCE_UID))
    save_following_cache(following_list)

    logging.info(f"监控 {len(following_list)} 个UID")
    seen_dynamics = init_dynamic_states_for_uids(following_list, header)
    state = load_dynamic_state()

    batch_index = 0
    last_hb = time.time()
    last_comment_check = 0
    last_following_refresh = 0
    last_v_check = 0
    last_cleanup = time.time()

    threading.Thread(target=push_worker, daemon=True).start()

    while True:
        try:
            now_dt = datetime.datetime.now()
            if not is_work_time(now_dt):
                time.sleep(get_sleep_until_work_time())
                header = get_header()
                update_wbi_keys(header)
                continue

            now = time.time()

            # 定期保存状态
            if now - last_state_save > STATE_SAVE_INTERVAL:
                save_dynamic_state(state)
                last_state_save = now

            # 刷新关注列表
            if now - last_following_refresh >= FOLLOWING_REFRESH_INTERVAL:
                # 刷新逻辑略（保持你之前版本）
                last_following_refresh = now

            # 评论监控
            if oid and now - last_comment_check >= COMMENT_SCAN_INTERVAL:
                new_c, new_t = scan_new_comments(oid, header, last_read_time, seen_comments)
                last_comment_check = now
                if new_t > last_read_time:
                    last_read_time = new_t
                if new_c:
                    notifier.send_webhook_notification(title, new_c)

            # 动态监控
            batch_size = get_batch()
            batch = following_list[batch_index * batch_size : (batch_index + 1) * batch_size]
            for uid in batch:
                try:
                    check_new_dynamics_for_uid(uid, header, seen_dynamics, state, now)
                except:
                    pass
            batch_index = (batch_index + 1) % max(1, (len(following_list) + batch_size - 1) // batch_size)

            # 心跳
            if now - last_hb >= HEARTBEAT_INTERVAL:
                logging.info("💓 心跳正常运行")
                last_hb = now

            time.sleep(1.0)

        except Exception:
            logging.error(traceback.format_exc())
            time.sleep(10)

if __name__ == "__main__":
    init_logging()
    db.init_db()
    h = get_header()
    update_wbi_keys(h)
    start_monitoring(h)
