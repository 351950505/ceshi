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
VIDEO_CHECK_INTERVAL = 21600
HEARTBEAT_INTERVAL = 30
FOLLOWING_REFRESH_INTERVAL = 3600
SOURCE_UID = 3706948578969654

FALLBACK_DYNAMIC_UIDS = ["3546905852250875","3546961271589219","3546610447419885","285340365","3706948578969654"]

COMMENT_SCAN_INTERVAL = 5
COMMENT_MAX_PAGES = 3
COMMENT_SAFE_WINDOW = 60

LOG_FILE = "bili_monitor.log"
DYNAMIC_STATE_FILE = "dynamic_state.json"
FOLLOWING_CACHE_FILE = "following_cache.json"

# 最稳 + 漏动态修复参数
INIT_SLEEP_MIN, INIT_SLEEP_MAX = 3.0, 6.0
STATE_SAVE_INTERVAL = 30
BURST_MODE_DURATION = 18
BURST_INTERVAL = 1.3          # 爆发模式（修复漏动态）
NORMAL_INTERVAL = 1.6         # 普通模式
MAX_SEEN_PER_UID = 800
# =============================================

push_queue = queue.Queue()
burst_end_time = 0
last_state_save = time.time()
last_seen_clean = time.time()

def push_worker():
    while True:
        try:
            item = push_queue.get(timeout=1)
            if item:
                notifier.send_webhook_notification("💡 特别关注UP主发布新内容", [item])
        except queue.Empty:
            continue
        except Exception as e:
            logging.error(f"推送失败: {e}")

def get_scan_interval():
    global burst_end_time
    return BURST_INTERVAL if time.time() < burst_end_time else NORMAL_INTERVAL

def init_logging():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", encoding="utf-8") as f: f.truncate()
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        encoding="utf-8", filemode="w")
    logging.info("="*60)
    logging.info("B站监控系统启动 (24小时运行 + 漏动态修复版)")
    logging.info("="*60)

def safe_request(url, params, header, retries=5):
    h = header.copy()
    h["Connection"] = "close"
    base_delay = 3
    for i in range(retries):
        try:
            r = requests.get(url, headers=h, params=params, timeout=12)
            data = r.json()
            code = data.get("code")
            if code == -101:
                logging.error("Cookie失效")
                send_failure_notification("Cookie 失效", "需要重新登录")
                return {"code": -101, "need_refresh": True}
            if code in (-799, -352, -509):
                wait = base_delay * (2 ** i) + random.uniform(2, 5)
                logging.warning(f"风控 {code}，等待 {wait:.1f}s")
                time.sleep(wait)
                continue
            if code != 0 and i < retries-1:
                time.sleep(base_delay * (2 ** i) + random.uniform(0.5, 2))
                continue
            return data
        except Exception:
            time.sleep(base_delay * (2 ** i) + random.uniform(0.5, 2))
    logging.error("请求最终失败")
    send_failure_notification("API 请求最终失败", "所有重试均失败")
    return {"code": -500}

WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}
mixinKeyEncTab = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]

def getMixinKey(orig):
    return ''.join([orig[i] for i in mixinKeyEncTab])[:32]

def encWbi(params, img_key, sub_key):
    mixin_key = getMixinKey(img_key + sub_key)
    params["wts"] = int(time.time())
    params = dict(sorted(params.items()))
    filtered = {}
    for k, v in params.items():
        v = str(v)
        for c in "!'()*": v = v.replace(c, "")
        filtered[k] = v
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
            logging.info("WBI密钥已更新")
    except Exception as e:
        logging.error(f"更新WBI异常: {e}")

def wbi_request(url, params, header):
    if not WBI_KEYS["img_key"] or time.time() - WBI_KEYS["last_update"] > 21600:
        update_wbi_keys(header)
    signed = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    return safe_request(url, signed, header)

def get_following_list(uid, header):
    following = []
    pn = 1
    ps = 50
    while True:
        params = {"vmid": uid, "pn": pn, "ps": ps, "order": "desc", "order_type": "attention"}
        data = safe_request("https://api.bilibili.com/x/relation/followings", params, header)
        if data.get("code") != 0: break
        items = data.get("data", {}).get("list", [])
        if not items: break
        for item in items:
            mid = item.get("mid")
            if mid: following.append(str(mid))
        if len(items) < ps: break
        pn += 1
        time.sleep(random.uniform(0.5, 1))
    return following

def load_following_cache():
    if os.path.exists(FOLLOWING_CACHE_FILE):
        try:
            with open(FOLLOWING_CACHE_FILE, "r") as f:
                return json.load(f)
        except: return []
    return []

def save_following_cache(uids):
    with open(FOLLOWING_CACHE_FILE, "w") as f:
        json.dump(uids, f)

def load_dynamic_state():
    if os.path.exists(DYNAMIC_STATE_FILE):
        try:
            with open(DYNAMIC_STATE_FILE, "r") as f:
                state = json.load(f)
            cleaned = {}
            for uid_str, v in state.items():
                if isinstance(v, dict):
                    cleaned[uid_str] = {"last_ts": v.get("last_ts", 0), "baseline": v.get("baseline", ""), "offset": v.get("offset", "")}
                else:
                    cleaned[uid_str] = {"last_ts": 0, "baseline": "", "offset": ""}
            return cleaned
        except: return {}
    return {}

def save_dynamic_state(state):
    with open(DYNAMIC_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def clean_old_seen(seen_dynamics):
    for uid in list(seen_dynamics.keys()):
        if len(seen_dynamics[uid]) > MAX_SEEN_PER_UID:
            seen_dynamics[uid] = set(list(seen_dynamics[uid])[-MAX_SEEN_PER_UID:])

def extract_dynamic_text(item):
    try:
        modules = item.get("modules") or {}
        dyn = modules.get("module_dynamic") or {}
        desc = dyn.get("desc") or {}
        nodes = desc.get("rich_text_nodes") or []
        if nodes:
            return "".join(n.get("text", "") for n in nodes if isinstance(n, dict) and n.get("type") in ("RICH_TEXT_NODE_TYPE_TEXT","RICH_TEXT_NODE_TYPE_TOPIC","RICH_TEXT_NODE_TYPE_AT","RICH_TEXT_NODE_TYPE_EMOJI","RICH_TEXT_NODE_TYPE_LOTTERY")).strip()
        major = dyn.get("major") or {}
        t = major.get("type", "")
        if t == "MAJOR_TYPE_ARCHIVE":
            a = major.get("archive") or {}
            return f"【视频】{a.get('title','')}\n{a.get('desc','')}".strip()
        if t == "MAJOR_TYPE_ARTICLE":
            return f"【专栏】{major.get('article',{}).get('title','')}".strip()
        if t == "MAJOR_TYPE_OPUS":
            return "".join(n.get("text", "") for n in (major.get("opus",{}).get("summary",{}).get("rich_text_nodes") or []) if isinstance(n, dict)).strip()
        return ""
    except:
        return ""

def fetch_dynamics_page(uid, offset, header):
    params = {"host_mid": uid, "type": "all", "timezone_offset": "-480", "platform": "web",
              "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,decorationCard,onlyfansAssetsV2,forwardListHidden,ugcDelete",
              "web_location": "333.1365"}
    if offset: params["offset"] = offset
    return wbi_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all", params, header)

def init_dynamic_states_for_uids(uids, header):
    seen = {}
    state = load_dynamic_state()
    for uid in uids:
        uid_str = str(uid)
        seen[uid_str] = set()
        if uid_str not in state: state[uid_str] = {"last_ts": 0, "baseline": "", "offset": ""}
        try:
            data = fetch_dynamics_page(uid_str, "", header)
            if data.get("code") == 0:
                feed = data.get("data", {})
                items = feed.get("items") or []
                offset = feed.get("offset", "")
                baseline = items[0].get("id_str", "") if items else ""
                max_ts = max((item.get("modules",{}).get("module_author",{}).get("pub_ts",0) for item in items if isinstance(item,dict)), default=0)
                state[uid_str]["last_ts"] = max_ts
                state[uid_str]["baseline"] = baseline
                state[uid_str]["offset"] = offset
                for item in items:
                    if isinstance(item,dict) and item.get("id_str"):
                        seen[uid_str].add(item["id_str"])
        except Exception as e:
            logging.error(f"初始化 UID {uid_str} 异常: {e}")
        time.sleep(random.uniform(INIT_SLEEP_MIN, INIT_SLEEP_MAX))
    save_dynamic_state(state)
    return seen

def check_new_dynamics_for_uid(uid, header, seen_dynamics, state, now_ts):
    global burst_end_time
    uid_str = str(uid)
    current = state.setdefault(uid_str, {"last_ts": 0, "baseline": "", "offset": ""})
    last_ts = current["last_ts"]
    offset = current["offset"]
    data = fetch_dynamics_page(uid_str, offset, header)
    if data.get("code") != 0: return False
    feed = data.get("data", {})
    items = feed.get("items") or []
    new_offset = feed.get("offset", offset)
    new_baseline = items[0].get("id_str", "") if items else ""
    new_items = []
    max_ts = last_ts
    seen_uid = seen_dynamics.setdefault(uid_str, set())
    for item in items:
        if not isinstance(item, dict): continue
        dyn_id = item.get("id_str")
        if not dyn_id: continue
        author = item.get("modules", {}).get("module_author", {})
        pub_ts = author.get("pub_ts", 0)
        if pub_ts > max_ts: max_ts = pub_ts
        if pub_ts > last_ts and (now_ts - pub_ts <= 300):
            new_items.append(item)
            seen_uid.add(dyn_id)
        else:
            seen_uid.add(dyn_id)
    if max_ts > last_ts: current["last_ts"] = max_ts
    if new_offset != offset: current["offset"] = new_offset
    if new_baseline: current["baseline"] = new_baseline
    has_new = False
    for item in new_items:
        dyn_id = item.get("id_str")
        author = item.get("modules", {}).get("module_author", {})
        name = author.get("name", uid_str)
        pub_ts = author.get("pub_ts", 0)
        text = extract_dynamic_text(item)
        if item.get("type") == "DYNAMIC_TYPE_FORWARD":
            orig = item.get("orig")
            if orig:
                orig_text = extract_dynamic_text(orig)
                if orig_text: text = f"{text}\n【转发原文】{orig_text}" if text else f"【转发原文】{orig_text}"
                orig_id = orig.get("id_str")
                if orig_id: text = f"{text}\n【原动态链接】https://t.bilibili.com/{orig_id}"
        time_str = datetime.datetime.fromtimestamp(pub_ts).strftime('%Y-%m-%d %H:%M:%S') if pub_ts > 0 else "未知时间"
        final_msg = f"{text}\n\n📅 发布于: {time_str}\n🔗 直达链接: https://t.bilibili.com/{dyn_id}" if text else f"📅 发布于: {time_str}\n🔗 直达链接: https://t.bilibili.com/{dyn_id}"
        push_queue.put({"user": name, "message": final_msg})
        has_new = True
        logging.info(f"✅ 新动态 [{name}] {dyn_id}")
    if has_new:
        burst_end_time = time.time() + BURST_MODE_DURATION
        logging.info(f"🚀 爆发模式启动 {BURST_MODE_DURATION}s")
    return has_new

def scan_new_comments(oid, header, last_read_time, seen):
    new_list = []
    max_ctime = last_read_time
    safe_time = last_read_time - COMMENT_SAFE_WINDOW
    url = "https://api.bilibili.com/x/v2/reply"
    pn = 1
    fetched = 0
    while fetched < COMMENT_MAX_PAGES:
        params = {"type": 1, "oid": oid, "sort": 0, "nohot": 1, "ps": 20, "pn": pn}
        data = wbi_request(url, params, header)
        if data.get("code") != 0: break
        replies = data.get("data", {}).get("replies", [])
        if not replies: break
        all_old = True
        for r in replies:
            ctime = r.get("ctime", 0)
            if ctime > max_ctime: max_ctime = ctime
            if ctime > safe_time:
                all_old = False
                rpid = str(r.get("rpid", ""))
                if rpid and rpid not in seen:
                    seen.add(rpid)
                    comment_time = datetime.datetime.fromtimestamp(ctime).strftime('%H:%M:%S')
                    new_list.append({"user": f"[{comment_time}] {r.get('member',{}).get('uname','')}", "message": r.get("content",{}).get("message",""), "ctime": ctime})
        if all_old or len(replies) < 20: break
        pn += 1
        fetched += 1
        time.sleep(random.uniform(0.3, 0.6))
    return new_list, max_ctime

def get_latest_video(header):
    data = safe_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space", {"host_mid": TARGET_UID}, header)
    if data.get("code") == -101:
        if refresh_cookie(): return get_latest_video(get_header())
        return None
    if data.get("code") != 0: return None
    for item in (data.get("data", {}).get("items") or []):
        if item.get("type") == "DYNAMIC_TYPE_AV":
            try: return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
            except: pass
    return None

def get_video_info(bv, header):
    data = safe_request(f"https://api.bilibili.com/x/web-interface/view?bvid={bv}", None, header)
    if data.get("code") == -101 and refresh_cookie():
        return get_video_info(bv, get_header())
    if data.get("code") == 0:
        d = data["data"]
        return str(d["aid"]), d["title"]
    return None, None

def sync_latest_video(header):
    bv = get_latest_video(header)
    if not bv: return None, None
    videos = db.get_monitored_videos()
    if videos and videos[0][1] == bv: return videos[0][0], videos[0][2]
    oid, title = get_video_info(bv, header)
    if oid:
        db.clear_videos()
        db.add_video_to_db(oid, bv, title)
        return oid, title
    return None, None

def get_header():
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    except:
        subprocess.run([sys.executable, "login_bilibili.py"])
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    return {"Cookie": cookie, "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Referer": "https://www.bilibili.com/"}

def refresh_cookie():
    logging.warning("Cookie失效，尝试重新登录...")
    try:
        subprocess.run([sys.executable, "login_bilibili.py"], check=True)
        logging.info("重新登录成功")
        return True
    except Exception as e:
        msg = f"重新登录失败: {e}"
        logging.error(msg)
        send_failure_notification("Cookie 刷新失败", msg)
        return False

def send_failure_notification(title, message):
    key = f"{title}:{message[:100]}"
    if time.time() - _last_notify_time.get(key, 0) >= 600:
        _last_notify_time[key] = time.time()
        try:
            notifier.send_webhook_notification(title, [{"user": "系统", "message": message}])
        except: pass

def start_monitoring(header):
    global last_state_save, last_seen_clean, burst_end_time
    last_v_check = last_hb = last_comment_check = last_following_refresh = last_cleanup_check = last_d_check = 0
    oid, title = sync_latest_video(header)
    last_read_time = int(time.time())
    seen_comments = set()
    following_list = load_following_cache() or get_following_list(SOURCE_UID, header) or FALLBACK_DYNAMIC_UIDS[:]
    if str(SOURCE_UID) not in following_list: following_list.append(str(SOURCE_UID))
    save_following_cache(following_list)
    logging.info(f"监控 {len(following_list)} 个 UID")
    seen_dynamics = init_dynamic_states_for_uids(following_list, header)
    state = load_dynamic_state()
    threading.Thread(target=push_worker, daemon=True).start()
    logging.info("✅ 扫描与推送线程已彻底分离（24小时 + 漏动态修复）")
    while True:
        try:
            now_dt = datetime.datetime.now()
            # 24小时运行（已取消工作时间限制）
            now = time.time()
            # 动态扫描（修复漏动态：每轮扫描所有UID）
            if now - last_d_check >= get_scan_interval():
                state_updated = False
                for uid in following_list:
                    try:
                        if check_new_dynamics_for_uid(uid, header, seen_dynamics, state, now):
                            state_updated = True
                    except Exception as e:
                        logging.error(f"UID {uid} 检查异常: {e}")
                if state_updated and now - last_state_save > STATE_SAVE_INTERVAL:
                    save_dynamic_state(state)
                    last_state_save = now
                last_d_check = now
            # 刷新关注列表（新UID立即初始化）
            if now - last_following_refresh >= FOLLOWING_REFRESH_INTERVAL:
                new_list = get_following_list(SOURCE_UID, header)
                if new_list:
                    new_list = [str(uid) for uid in new_list]
                    if str(SOURCE_UID) not in new_list:
                        new_list.append(str(SOURCE_UID))
                    old_set = set(following_list)
                    new_set = set(new_list)
                    added = new_set - old_set
                    removed = old_set - new_set
                    if added or removed:
                        for uid in added:
                            uid_str = str(uid)
                            seen_dynamics[uid_str] = set()
                            state[uid_str] = {"last_ts": 0, "baseline": "", "offset": ""}
                            logging.info(f"新UID {uid_str} 已加入扫描")
                        for uid in removed:
                            seen_dynamics.pop(uid, None)
                            state.pop(uid, None)
                        following_list = new_list
                        save_following_cache(following_list)
                        save_dynamic_state(state)
                last_following_refresh = now
            if oid and now - last_comment_check >= COMMENT_SCAN_INTERVAL:
                new_c, new_t = scan_new_comments(oid, header, last_read_time, seen_comments)
                last_comment_check = now
                if new_t > last_read_time: last_read_time = new_t
                if new_c:
                    new_c.sort(key=lambda x: x["ctime"])
                    notifier.send_webhook_notification(title, new_c)
                    logging.info(f"💬 发送 {len(new_c)} 条评论")
            if now - last_hb >= HEARTBEAT_INTERVAL:
                logging.info("💓 心跳正常")
                last_hb = now
            if now - last_v_check > VIDEO_CHECK_INTERVAL:
                res = sync_latest_video(header)
                if res: oid, title = res
                last_v_check = now
            if now - last_seen_clean > 3600:
                clean_old_seen(seen_dynamics)
                last_seen_clean = now
            time.sleep(0.2)
        except Exception:
            logging.error(traceback.format_exc())
            time.sleep(5)

if __name__ == "__main__":
    init_logging()
    db.init_db()
    h = get_header()
    update_wbi_keys(h)
    start_monitoring(h)
