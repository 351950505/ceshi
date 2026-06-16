import sys
import os
import time
import random
import logging
import logging.handlers
import hashlib
import urllib.parse
import json
import requests
from requests.adapters import HTTPAdapter
import datetime
import threading
import queue
import signal
from collections import deque

try:
    from zoneinfo import ZoneInfo
except ImportError:
    import pytz
    def ZoneInfo(tz_str):
        return pytz.timezone(tz_str)

import notifier

# ================= 核心配置 =================
HEARTBEAT_INTERVAL = 60
FOLLOWING_REFRESH_INTERVAL = 3600
SOURCE_UID = 3706948578969654

FALLBACK_DYNAMIC_UIDS =[
    "3546905852250875",
    "3546961271589219",
    "3546610447419885",
    "285340365",
    "3706948578969654"
]

LOG_FILE = "bili_monitor.log"
DYNAMIC_STATE_FILE = "dynamic_state.json"
FOLLOWING_CACHE_FILE = "following_cache.json"

RUN_TZ = "Asia/Shanghai"
RUN_WEEKDAYS = {0, 1, 2, 3, 4}
RUN_START_HOUR = 9
RUN_START_MINUTE = 20
RUN_END_HOUR = 16
OFF_HOURS_SLEEP = 20

STATE_SAVE_INTERVAL = 30
BURST_MODE_DURATION = 12
BURST_COOLDOWN = 20
BURST_MAX_CHAIN = 3

BURST_INTERVAL_MIN = 1.6
BURST_INTERVAL_MAX = 2.2
NORMAL_INTERVAL_MIN = 2.0
NORMAL_INTERVAL_MAX = 3.0
IDLE_INTERVAL_MIN = 2.8
IDLE_INTERVAL_MAX = 3.8
IDLE_MODE_THRESHOLD = 300

FAILURE_EXIT_BURST = 2
FAILURE_SLOWDOWN_THRESHOLD = 3
FAILURE_SLOW_INTERVAL_MIN = 4.0
FAILURE_SLOW_INTERVAL_MAX = 6.0
NO_UPDATE_SLOWDOWN_THRESHOLD_1 = 20
NO_UPDATE_SLOWDOWN_THRESHOLD_2 = 50
NO_UPDATE_INTERVAL_1_MIN = 3.0
NO_UPDATE_INTERVAL_1_MAX = 4.0
NO_UPDATE_INTERVAL_2_MIN = 4.0
NO_UPDATE_INTERVAL_2_MAX = 5.5

MAX_SEEN_DYNAMIC_IDS = 3000
DYNAMIC_NEW_WINDOW = 3600
RECENT_PUSHED_IDS_LIMIT = 1000

ALLOWED_DYNAMIC_TYPES = {"", "MAJOR_TYPE_OPUS", "MAJOR_TYPE_ARCHIVE", "MAJOR_TYPE_ARTICLE", "MAJOR_TYPE_DRAW"}
ALLOWED_TOP_LEVEL_TYPES = {"DYNAMIC_TYPE_WORD", "DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_AV", "DYNAMIC_TYPE_ARTICLE", "DYNAMIC_TYPE_FORWARD"}
ALLOW_FORWARD_DYNAMIC = True

# ================= 全局状态与网络层 =================
IS_RUNNING = True
REQ_SESSION = requests.Session()
_adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=1)
REQ_SESSION.mount('http://', _adapter)
REQ_SESSION.mount('https://', _adapter)

notify_queue = queue.Queue(maxsize=1000)

burst_end_time = 0
last_burst_trigger_time = 0
burst_chain_count = 0
consecutive_failures = 0
last_new_dynamic_time = 0
consecutive_no_update_rounds = 0
last_state_save = time.time()
_last_notify_time = {}

WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}
mixinKeyEncTab =[46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]

def signal_handler(signum, frame):
    global IS_RUNNING
    logging.info("\n🛑 接收到关闭信号，准备保存数据安全退出...")
    IS_RUNNING = False

def atomic_write_json(path, data):
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)

def normalize_text(text):
    if not text: return ""
    text = str(text).replace("\r", "\n")
    lines =[line.strip() for line in text.split("\n") if line.strip()]
    return "\n".join(lines).strip()

def cut_text(text, max_len=800):
    text = normalize_text(text)
    return text if len(text) <= max_len else text[:max_len-3].rstrip() + "..."

def is_in_monitor_window(now_dt=None):
    if now_dt is None:
        try: now_dt = datetime.datetime.now(ZoneInfo(RUN_TZ))
        except Exception: now_dt = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    if now_dt.weekday() not in RUN_WEEKDAYS: return False
    current = now_dt.hour * 60 + now_dt.minute
    start = RUN_START_HOUR * 60 + RUN_START_MINUTE
    end = RUN_END_HOUR * 60
    return start <= current < end

class DingTalkFilter(logging.Filter):
    def filter(self, record): return "310000" not in record.getMessage()

def init_logging():
    root = logging.getLogger()
    if root.hasHandlers(): root.handlers.clear()
    formatter = logging.Formatter("[BILI] %(asctime)s [%(levelname)s] %(message)s")
    ding_filter = DingTalkFilter()
    file_handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=3, encoding="utf-8", delay=True)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(ding_filter) 
    root.addHandler(file_handler)
    if sys.stdout.isatty():
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        stream_handler.addFilter(ding_filter) 
        root.addHandler(stream_handler)
    root.setLevel(logging.INFO)
    root.propagate = False
    logging.info("=" * 60)
    logging.info("B站监控系统启动 (纯匿名雷达轮询版 - Zero Cookie)")
    logging.info("=" * 60)

def send_failure_notification(title, message):
    global _last_notify_time
    if len(_last_notify_time) > 200: _last_notify_time.clear() 
    key = f"{title}:{message[:100]}"
    if time.time() - _last_notify_time.get(key, 0) >= 600:
        _last_notify_time[key] = time.time()
        safe_enqueue_notify(title, [{"user": "系统", "message": message}], "system")

def safe_enqueue_notify(title, items, notify_type="dynamic"):
    try:
        notify_queue.put_nowait({"title": title, "items": items, "notify_type": notify_type})
        return True
    except queue.Full: return False

def notify_worker():
    while IS_RUNNING:
        try:
            task = notify_queue.get(timeout=1)
            title, items, ntype = task.get("title"), task.get("items"), task.get("notify_type")
            if ntype == "dynamic": logging.info(f"[排队发送] 正在推送新动态: {items[0].get('link', '')}")
            ok = notifier.send_webhook_notification(title, items, notify_type=ntype)
            if not ok and ntype != "system": logging.warning(f"[发送失败] 类型: {ntype}")
            time.sleep(2.5) 
        except queue.Empty: continue
        except Exception: pass

def get_header():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.bilibili.com/"
    }

def safe_request(url, params, header, retries=5):
    h = header.copy(); h.pop("Connection", None) 
    base_delay = 1
    for i in range(retries):
        try:
            r = REQ_SESSION.get(url, headers=h, params=params, timeout=12)
            try: data = r.json()
            except Exception: data = {"code": -500}
            
            code = data.get("code")
            
            # 【核心修复】：如果是纯匿名请求，返回 -101 是预期结果，直接放行秒过，不要原地发呆等重试！
            if code == -101: 
                return data
                
            if code in (-799, -352, -509): 
                time.sleep(base_delay * (2 ** i) + random.uniform(1.0, 3.0))
                continue
                
            if code != 0 and i < retries - 1: 
                time.sleep(base_delay * (2 ** i) + random.uniform(0.5, 1.5))
                continue
                
            return data
        except Exception: 
            time.sleep(base_delay * (2 ** i) + random.uniform(0.5, 1.5))
    return {"code": -500}

def getMixinKey(orig): return ''.join([orig[i] for i in mixinKeyEncTab])[:32]

def encWbi(params, img_key, sub_key):
    mixin_key = getMixinKey(img_key + sub_key)
    params["wts"] = int(time.time())
    filtered = {k: str(v).translate(str.maketrans('', '', "!'()*")) for k, v in sorted(params.items())}
    query = urllib.parse.urlencode(filtered, quote_via=urllib.parse.quote)
    filtered["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return filtered

def update_wbi_keys(header):
    try:
        data = safe_request("https://api.bilibili.com/x/web-interface/nav", None, header)
        # 【核心修复】：不再用 code == 0 判断，因为匿名请求会返回 -101，但依然带有 wbi_img 信息
        img = data.get("data", {}).get("wbi_img", {})
        img_url, sub_url = img.get("img_url", ""), img.get("sub_url", "")
        if img_url and sub_url:
            WBI_KEYS["img_key"] = img_url.rsplit("/", 1)[1].split(".")[0]
            WBI_KEYS["sub_key"] = sub_url.rsplit("/", 1)[1].split(".")[0]
            WBI_KEYS["last_update"] = time.time()
            logging.info("✅ 匿名 WBI 签名密钥已成功获取！")
    except Exception: pass

def wbi_request(url, params, header):
    if not WBI_KEYS["img_key"] or time.time() - WBI_KEYS["last_update"] > 21600: update_wbi_keys(header)
    if not WBI_KEYS["img_key"] or not WBI_KEYS["sub_key"]: return safe_request(url, params, header)
    try: return safe_request(url, encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"]), header)
    except Exception: return safe_request(url, params, header)

def get_scan_interval():
    global burst_end_time, consecutive_failures, last_new_dynamic_time, consecutive_no_update_rounds
    now = time.time()
    if consecutive_failures >= FAILURE_SLOWDOWN_THRESHOLD: return random.uniform(FAILURE_SLOW_INTERVAL_MIN, FAILURE_SLOW_INTERVAL_MAX)
    if now < burst_end_time: return random.uniform(BURST_INTERVAL_MIN, BURST_INTERVAL_MAX)
    if consecutive_no_update_rounds >= NO_UPDATE_SLOWDOWN_THRESHOLD_2: return random.uniform(NO_UPDATE_INTERVAL_2_MIN, NO_UPDATE_INTERVAL_2_MAX)
    if consecutive_no_update_rounds >= NO_UPDATE_SLOWDOWN_THRESHOLD_1: return random.uniform(NO_UPDATE_INTERVAL_1_MIN, NO_UPDATE_INTERVAL_1_MAX)
    if last_new_dynamic_time > 0 and now - last_new_dynamic_time >= IDLE_MODE_THRESHOLD: return random.uniform(IDLE_INTERVAL_MIN, IDLE_INTERVAL_MAX)
    return random.uniform(NORMAL_INTERVAL_MIN, NORMAL_INTERVAL_MAX)

def trigger_burst_mode():
    global burst_end_time, last_burst_trigger_time, burst_chain_count
    now = time.time()
    if now - last_burst_trigger_time < BURST_COOLDOWN:
        if now < burst_end_time and burst_chain_count < BURST_MAX_CHAIN:
            burst_end_time = max(burst_end_time, now + BURST_MODE_DURATION)
            burst_chain_count += 1
        return
    burst_end_time = now + BURST_MODE_DURATION
    last_burst_trigger_time = now; burst_chain_count = 1
    logging.info(f"🚀 捕获新动态，进入极速雷达扫描模式 {BURST_MODE_DURATION}s")

def exit_burst_mode(reason=""):
    global burst_end_time, burst_chain_count
    burst_end_time = 0; burst_chain_count = 0

def load_following_cache():
    if os.path.exists(FOLLOWING_CACHE_FILE):
        try:
            with open(FOLLOWING_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else[]
        except Exception: return []
    return[]

def save_following_cache(uids):
    try: atomic_write_json(FOLLOWING_CACHE_FILE, uids)
    except Exception: pass

def load_dynamic_state():
    default_state = {"uids": {}, "recent_pushed_ids":[]}
    if os.path.exists(DYNAMIC_STATE_FILE):
        try:
            with open(DYNAMIC_STATE_FILE, "r", encoding="utf-8") as f: state = json.load(f)
            if "uids" not in state: state["uids"] = {}
            if "recent_pushed_ids" not in state: state["recent_pushed_ids"] = []
            return state
        except Exception: return default_state
    return default_state

def save_dynamic_state(state):
    try:
        state["recent_pushed_ids"] = list(state.get("recent_pushed_ids",[]) or [])[:RECENT_PUSHED_IDS_LIMIT]
        atomic_write_json(DYNAMIC_STATE_FILE, state)
    except Exception: pass

def init_seen_cache(): return {"set": set(), "queue": deque()}

def add_seen_cache(cache, item_id, max_size):
    s, q = cache["set"], cache["queue"]
    if item_id in s: return False
    s.add(item_id); q.append(item_id)
    while len(q) > max_size: s.discard(q.popleft())
    return True

def add_recent_pushed_id(state, dyn_id):
    recent = list(state.get("recent_pushed_ids", []) or[])
    if dyn_id in recent: recent.remove(dyn_id)
    recent.insert(0, dyn_id)
    state["recent_pushed_ids"] = recent[:RECENT_PUSHED_IDS_LIMIT]

def is_allowed_dynamic(item):
    try:
        top_type = item.get("type", "")
        major_type = item.get("modules", {}).get("module_dynamic", {}).get("major", {}).get("type", "")
        if top_type == "DYNAMIC_TYPE_FORWARD": return ALLOW_FORWARD_DYNAMIC
        if top_type and top_type not in ALLOWED_TOP_LEVEL_TYPES: return False
        if major_type not in ALLOWED_DYNAMIC_TYPES: return False
        return True
    except Exception: return False

def extract_dynamic_text(item):
    try:
        dyn = item.get("modules", {}).get("module_dynamic", {})
        nodes = dyn.get("desc", {}).get("rich_text_nodes") or[]
        if nodes:
            text = "".join(n.get("text", "") for n in nodes if isinstance(n, dict) and "RICH_TEXT" in n.get("type", "")).strip()
            if text: return normalize_text(text)
        major = dyn.get("major", {})
        t = major.get("type", "")
        if t == "MAJOR_TYPE_ARCHIVE": return normalize_text(f"【视频】{major.get('archive', {}).get('title', '')}")
        if t == "MAJOR_TYPE_ARTICLE": return normalize_text(f"【专栏】{major.get('article', {}).get('title', '')}")
        if t == "MAJOR_TYPE_OPUS": return normalize_text(f"【图文】{major.get('opus', {}).get('title', '')}")
        return normalize_text(dyn.get("desc", {}).get("text", ""))
    except Exception: return ""

def format_dynamic_message(item):
    dyn_id = item.get("id_str", "")
    author = item.get("modules", {}).get("module_author", {}) or {}
    name = author.get("name", "未知UP")
    pub_ts = int(author.get("pub_ts", 0) or 0)
    text = cut_text(extract_dynamic_text(item), 900)
    if item.get("type") == "DYNAMIC_TYPE_FORWARD":
        orig = item.get("orig")
        if orig and isinstance(orig, dict):
            orig_text = cut_text(extract_dynamic_text(orig), 300)
            text = f"{text}\n\n【转发原文】\n{orig_text}" if text else f"【转发原文】\n{orig_text}"
            orig_id = orig.get("id_str")
            if orig_id: text = f"{text}\n\n原动态： https://t.bilibili.com/{orig_id}"
    if not text: text = "（该动态无可提取正文）"
    time_str = datetime.datetime.fromtimestamp(pub_ts).strftime("%Y-%m-%d %H:%M:%S") if pub_ts > 0 else "未知时间"
    cover = ""
    try:
        major = item.get("modules", {}).get("module_dynamic", {}).get("major", {}) or {}
        t = major.get("type")
        if t == "MAJOR_TYPE_DRAW": cover = major.get("draw", {}).get("items",[{}])[0].get("src", "")
        elif t == "MAJOR_TYPE_ARCHIVE": cover = major.get("archive", {}).get("cover", "")
        elif t == "MAJOR_TYPE_OPUS": cover = major.get("opus", {}).get("pics", [{}])[0].get("url", "") or major.get("opus", {}).get("cover", "")
    except Exception: cover = ""
    return {"user": name, "message": text, "time": time_str, "link": f"https://t.bilibili.com/{dyn_id}", "cover": cover, "kind": "dynamic"}

def fetch_space_feed(header, uid):
    params = {"host_mid": str(uid), "timezone_offset": "-480", "platform": "web", "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,decorationCard,onlyfansAssetsV2,forwardListHidden,ugcDelete", "web_location": "333.1365"}
    return wbi_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space", params, header)

def fetch_space_feed_retry(header, uid, retries=2):
    last = None
    for _ in range(retries + 1):
        data = fetch_space_feed(header, uid)
        last = data
        if data.get("code") == 0: return data
        time.sleep(random.uniform(0.8, 1.6))
    return last or {"code": -500}

def get_following_list(uid, header):
    following = []
    pn = 1; ps = 50
    while True:
        data = safe_request("https://api.bilibili.com/x/relation/followings", {"vmid": uid, "pn": pn, "ps": ps, "order": "desc", "order_type": "attention"}, header)
        if data.get("code") != 0: break
        items = data.get("data", {}).get("list",[])
        if not items: break
        for item in items:
            mid = item.get("mid")
            if mid: following.append(str(mid))
        if len(items) < ps: break
        pn += 1
        time.sleep(random.uniform(0.6, 1.2))
    return following

def init_space_state(header, target_uids):
    state = load_dynamic_state()
    seen_dynamic_ids = init_seen_cache()
    logging.info(f"🚀 开始初始化雷达矩阵，缓存 {len(target_uids)} 个目标的状态...")
    for uid in target_uids:
        data = fetch_space_feed_retry(header, uid)
        if data.get("code") == 0:
            items = data.get("data", {}).get("items", [])
            uid_state = state.setdefault("uids", {}).setdefault(str(uid), {"last_ts": 0})
            max_ts = uid_state["last_ts"]
            for item in items:
                dyn_id = item.get("id_str")
                if dyn_id: add_seen_cache(seen_dynamic_ids, dyn_id, MAX_SEEN_DYNAMIC_IDS)
                author = item.get("modules", {}).get("module_author", {}) or {}
                pub_ts = int(author.get("pub_ts", 0))
                if pub_ts > max_ts: max_ts = pub_ts
            uid_state["last_ts"] = max_ts
        time.sleep(random.uniform(0.3, 0.7))
    save_dynamic_state(state)
    logging.info("✅ 匿名雷达矩阵初始化完毕，准备进入巡航状态！")
    return seen_dynamic_ids, state

def process_space_items(items, uid, seen_dynamic_ids, state, now_ts):
    global last_new_dynamic_time, consecutive_failures, consecutive_no_update_rounds
    has_new = False
    uid_state = state.setdefault("uids", {}).setdefault(str(uid), {"last_ts": 0})
    max_ts = uid_state["last_ts"]
    candidate_items = []

    for item in items:
        if not isinstance(item, dict): continue
        dyn_id = item.get("id_str")
        if not dyn_id: continue
        author = item.get("modules", {}).get("module_author", {}) or {}
        pub_ts = int(author.get("pub_ts", 0))
        if not add_seen_cache(seen_dynamic_ids, dyn_id, MAX_SEEN_DYNAMIC_IDS): continue
        if pub_ts > max_ts: max_ts = pub_ts
        if dyn_id in state.get("recent_pushed_ids", []): continue
        if not is_allowed_dynamic(item): continue
        if now_ts - pub_ts > DYNAMIC_NEW_WINDOW: continue
        candidate_items.append((dyn_id, pub_ts, item))

    for dyn_id, pub_ts, item in reversed(candidate_items):
        push_data = format_dynamic_message(item)
        ok = safe_enqueue_notify(f"{push_data.get('user', '未知UP')} 发布了新动态", [push_data], "dynamic")
        if ok:
            has_new = True
            add_recent_pushed_id(state, dyn_id)

    if max_ts > uid_state["last_ts"]: uid_state["last_ts"] = max_ts
    if has_new:
        last_new_dynamic_time = time.time()
        consecutive_failures = 0
        consecutive_no_update_rounds = 0
        trigger_burst_mode()
    return has_new

def start_monitoring():
    global last_state_save, consecutive_no_update_rounds
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    header = get_header()
    
    logging.info("⏳ 正在获取匿名 WBI 签名密钥...")
    update_wbi_keys(header)
    
    last_hb = 0; last_following_refresh = 0; last_d_check = 0

    logging.info("⏳ 正在构建监控目标列表...")
    following_list = load_following_cache()
    if not following_list:
        following_list = get_following_list(SOURCE_UID, header)
    if not following_list:
        logging.info("⚠️ 匿名状态下无权读取公共关注列表，自动启用备用的内置监控白名单。")
        following_list = FALLBACK_DYNAMIC_UIDS[:]
        
    following_list = [str(uid) for uid in following_list]
    if str(SOURCE_UID) not in following_list: following_list.append(str(SOURCE_UID))
    save_following_cache(following_list)

    target_uids = list(set(following_list))
    logging.info(f"🎯 雷达矩阵设定：共锁定 {len(target_uids)} 个目标 UP 主。")
    if target_uids:
        est_time = len(target_uids) * ((NORMAL_INTERVAL_MIN + NORMAL_INTERVAL_MAX)/2)
        logging.info(f"⏱️ 单轮空间雷达扫描预估耗时：{est_time:.1f} 秒")
    
    seen_dynamic_ids, state = init_space_state(header, target_uids)
    threading.Thread(target=notify_worker, daemon=True).start()

    logging.info(f"🚀 系统点火完成，开始在工作日 {RUN_START_HOUR}:{RUN_START_MINUTE:02d}-{RUN_END_HOUR}:00 进行雷达隐形扫描！")

    current_uid_idx = 0

    while IS_RUNNING:
        try:
            now = time.time()
            try: china_now = datetime.datetime.now(ZoneInfo(RUN_TZ))
            except: china_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
            
            if not is_in_monitor_window(china_now):
                if now - last_hb >= HEARTBEAT_INTERVAL:
                    logging.info(f"⏸ 当前不在监听时段，中国时间={china_now.strftime('%Y-%m-%d %H:%M:%S')}，仅工作日 {RUN_START_HOUR}:{RUN_START_MINUTE:02d}-{RUN_END_HOUR}:00 运行")
                    last_hb = now
                time.sleep(OFF_HOURS_SLEEP)
                continue

            if target_uids and now - last_d_check >= get_scan_interval():
                uid = target_uids[current_uid_idx]
                current_uid_idx = (current_uid_idx + 1) % len(target_uids)
                
                try:
                    data = fetch_space_feed_retry(header, uid)
                    if data.get("code") == 0:
                        items = data.get("data", {}).get("items", [])
                        has_new = process_space_items(items, uid, seen_dynamic_ids, state, int(now))
                        if has_new or now - last_state_save > STATE_SAVE_INTERVAL:
                            save_dynamic_state(state)
                            last_state_save = now
                        global consecutive_failures
                        consecutive_failures = 0
                        if not has_new: consecutive_no_update_rounds += 1
                        else: consecutive_no_update_rounds = 0
                    else:
                        consecutive_failures += 1
                        if consecutive_failures >= FAILURE_EXIT_BURST: exit_burst_mode()
                except Exception as e:
                    logging.error(f"空间雷达扫描异常 (UID: {uid}): {repr(e)}")
                    
                last_d_check = now

            if now - last_following_refresh >= FOLLOWING_REFRESH_INTERVAL:
                try:
                    new_list = get_following_list(SOURCE_UID, header)
                    if new_list:
                        new_list = [str(u) for u in new_list]
                        if str(SOURCE_UID) not in new_list: new_list.append(str(SOURCE_UID))
                        old_set, new_set = set(target_uids), set(new_list)
                        if old_set != new_set:
                            target_uids = list(new_set)
                            save_following_cache(new_list)
                            logging.info(f"雷达矩阵目标已刷新，当前共锁定 {len(target_uids)} 个目标。")
                except Exception: pass
                last_following_refresh = now

            if now - last_hb >= HEARTBEAT_INTERVAL:
                logging.info(f"💓 匿名雷达隐形运转中 interval={get_scan_interval():.2f}s burst={'on' if time.time() < burst_end_time else 'off'} fail={consecutive_failures} no_update={consecutive_no_update_rounds}")
                last_hb = now

            time.sleep(0.5)

        except Exception:
            if IS_RUNNING:
                logging.error("主雷达循环异常")
                time.sleep(8)
            else: break

    save_dynamic_state(state)
    logging.info("💾 雷达矩阵进度已安全落盘，引擎完美熄火。")

if __name__ == "__main__":
    init_logging()
    start_monitoring()
EOF

pkill -f main.py
nohup python3 /opt/bilibili-comment/ceshi/main.py > /opt/bilibili-comment/ceshi/bili_monitor.log 2>&1 &
tail -f /opt/bilibili-comment/ceshi/bili_monitor.log
