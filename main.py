import sys
import os
import time
import random
import logging
import logging.handlers
import traceback
import hashlib
import urllib.parse
import json
import requests
from requests.adapters import HTTPAdapter
import datetime
import threading
import queue
import signal
import uuid
import shutil
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
# 兼容 CentOS 7 及低版本 Python
try:
    from zoneinfo import ZoneInfo
except ImportError:
    import pytz
    def ZoneInfo(tz_str):
        return pytz.timezone(tz_str)
import notifier
# ================= 核心配置 =================
VIDEO_CHECK_INTERVAL = 21600

# 用户明确要求：关注列表与心跳均为 1 小时一次
HEARTBEAT_INTERVAL = 3600
FOLLOWING_REFRESH_INTERVAL = 3600

SOURCE_UID = 3707011984264075
FALLBACK_DYNAMIC_UIDS = [
    "3546905852250875",
    "3546961271589219",
    "3546610447419885",
    "285340365",
    "3707011984264075"
]

LOG_FILE = "bili_monitor.log"
DYNAMIC_STATE_FILE = "dynamic_state.json"
FOLLOWING_CACHE_FILE = "following_cache.json"

# ===== 运行时间窗口（中国时间）=====
RUN_TZ = "Asia/Shanghai"
RUN_WEEKDAYS = {0, 1, 2, 3, 4}
RUN_START_HOUR = 9
RUN_START_MINUTE = 20
RUN_END_HOUR = 16
OFF_HOURS_SLEEP = 20

# ===== 动态扫描 =====
# 主关注流只负责“快速发现”，不要靠提高频率解决漏报。
NORMAL_INTERVAL_MIN = 20.0
NORMAL_INTERVAL_MAX = 35.0
FEED_FETCH_BASE_PAGES = 1      # 没有更新时至少检查第一页
FEED_FETCH_MAX_PAGES = 6       # 检测到更新时最多翻 6 页
FEED_INIT_PAGES = 3

# ===== 动态补漏 =====
# 某 UID 长时间没在主关注流出现，就独立访问 feed/space。
UID_STALE_GLOBAL_THRESHOLD = 15 * 60      # 15 分钟没被主流看到，进入高优先级
UID_DIRECT_CHECK_INTERVAL = 5 * 60        # 同一 UID 两次独立检查至少间隔 5 分钟
UID_DIRECT_CHECK_BATCH = 2                # 每轮最多检查 2 个 UID，控制请求量
UID_DIRECT_MAX_PAGES = 3                  # 单 UID 最多翻 3 页
DYNAMIC_NEW_WINDOW = 6 * 3600             # 延迟/补漏最多追溯 6 小时
NEW_UID_BOOTSTRAP_WINDOW = 30 * 60        # 新加入监控的 UID 首次只追 30 分钟

# ===== 状态与内存 =====
STATE_SAVE_INTERVAL = 300   # 5分钟保存一次，降低磁盘写频率
MAX_SEEN_DYNAMIC_IDS = 2000
UID_STATE_SEEN_LIMIT = 120
RECENT_PUSHED_IDS_LIMIT = 1500
LAST_TS_IDS_LIMIT = 100

# ===== 推送可靠性 =====
NOTIFY_QUEUE_MAXSIZE = 100
NOTIFY_SEND_RETRIES = 3
NOTIFY_SEND_DELAY = 2.0
NOTIFY_RETRY_COOLDOWN = 300

# Cookie 连续失效阈值后停止
COOKIE_FAIL_EXIT_THRESHOLD = 3

# ===== 请求 / 风控 =====
REQUEST_TIMEOUT = 12
REQUEST_RETRIES = 3
WBI_REFRESH_INTERVAL = 21600

# ===== 动态类型过滤 =====
ALLOWED_DYNAMIC_TYPES = {"", "MAJOR_TYPE_OPUS", "MAJOR_TYPE_ARCHIVE", "MAJOR_TYPE_ARTICLE", "MAJOR_TYPE_DRAW"}
ALLOWED_TOP_LEVEL_TYPES = {"DYNAMIC_TYPE_WORD", "DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_AV", "DYNAMIC_TYPE_ARTICLE", "DYNAMIC_TYPE_FORWARD"}
ALLOW_FORWARD_DYNAMIC = True

# ================= 全局运行标识 =================
IS_RUNNING = True

# 线程共享
STATE_LOCK = threading.RLock()
ACTIVE_STATE = None
PENDING_PUSH_IDS = set()

# 网络层：只保留业务层重试，避免 Session 层重试 + safe_request 重试叠加。
REQ_SESSION = requests.Session()
_adapter = HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=0)
REQ_SESSION.mount('http://', _adapter)
REQ_SESSION.mount('https://', _adapter)
REQ_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive"
})
notify_queue = queue.Queue(maxsize=NOTIFY_QUEUE_MAXSIZE)
_last_notify_time = {}
WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}
mixinKeyEncTab = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]
# ================== 统一状态管理 ==================
@dataclass
class MonitorState:
    consecutive_failures: int = 0
    consecutive_cookie_failures: int = 0
    last_new_dynamic_time: float = 0.0
    consecutive_no_update_rounds: int = 0
    last_state_save: float = field(default_factory=time.time)
    last_checkin_date: str = ""
STATE = MonitorState()
def mark_state_dirty(state):
    if state is not None:
        state["_meta"] = state.setdefault("_meta", {})
        state["_meta"]["dirty"] = True

def get_state_counts(state):
    with STATE_LOCK:
        feed = state.get("feed", {}) if state else {}
        uid_state = state.get("uid_state", {}) if state else {}
        sent = feed.get("recent_pushed_ids", []) or []
        pending = len(PENDING_PUSH_IDS)
        retry_map = feed.get("push_retry_after", {}) or {}
        retrying = sum(1 for _, ts in retry_map.items() if float(ts or 0) > time.time())
        return {"uids": len(uid_state), "sent_cache": len(sent), "pending": pending, "retrying": retrying}

# ================== 工具与风控指纹模块 ==================
def signal_handler(signum, frame):
    global IS_RUNNING
    logging.info("\n🛑 接收到关闭信号 (SIGTERM/SIGINT)，准备保存数据安全退出...")
    IS_RUNNING = False
def atomic_write_json(path, data):
    """写入前自动备份上一份，防止崩溃丢失状态"""
    if os.path.exists(path):
        try:
            shutil.copy2(path, path + ".bak")
        except Exception as e:
            logging.warning(f"备份 {path} 失败: {e}")
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)
def normalize_text(text):
    if not text:
        return ""
    text = str(text).replace("\r", "\n")
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    return "\n".join(lines).strip()
def cut_text(text, max_len=800):
    text = normalize_text(text)
    if len(text) <= max_len:
        return text
    return text[:max_len - 3].rstrip() + "..."
def is_in_monitor_window(now_dt=None):
    if now_dt is None:
        try:
            now_dt = datetime.datetime.now(ZoneInfo(RUN_TZ))
        except Exception:
            now_dt = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    if now_dt.weekday() not in RUN_WEEKDAYS:
        return False
    current = now_dt.hour * 60 + now_dt.minute
    start = RUN_START_HOUR * 60 + RUN_START_MINUTE
    end = RUN_END_HOUR * 60
    return start <= current < end
def activate_session_cookies():
    try:
        logging.info("⏳ 正在模拟设备指纹并向 B 站注册安全 Cookie...")
        resp = REQ_SESSION.get("https://www.bilibili.com/", timeout=10)
        resp.close()
        uuid_sec = str(uuid.uuid4())
        time_sec = str(int(time.time() * 1000 % 1e5)).ljust(5, "0")
        _uuid = f"{uuid_sec}{time_sec}infoc"
        REQ_SESSION.cookies.set("_uuid", _uuid, domain=".bilibili.com")
        REQ_SESSION.cookies.set("CURRENT_FNVAL", "4048", domain=".bilibili.com")
        REQ_SESSION.cookies.set("blackside_state", "1", domain=".bilibili.com")
        logging.info("✅ 浏览器安全指纹及 buvid3 风控 Cookie 激活成功！")
        return True
    except Exception as e:
        logging.warning(f"⚠️ 模拟首页指纹激活失败 (可能影响高频防封): {e}")
        return False
def load_cookies_into_session():
    try:
        if not os.path.exists("bili_cookie.txt"):
            logging.error("❌ 未找到 bili_cookie.txt，关注流需要 Cookie 支持！")
            return False
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie_str = f.read().strip()
        if not cookie_str:
            logging.error("❌ bili_cookie.txt 内容为空！")
            return False
        for item in cookie_str.split(";"):
            item = item.strip()
            if not item or "=" not in item:
                continue
            k, v = item.split("=", 1)
            REQ_SESSION.cookies.set(k.strip(), v.strip(), domain=".bilibili.com")
        logging.info("✅ 账号登录 Cookie 载入成功！")
        return True
    except Exception as e:
        logging.error(f"❌ 加载 Cookie 异常: {e}")
        return False
class DingTalkFilter(logging.Filter):
    def filter(self, record):
        if "310000" in record.getMessage():
            return False
        return True
def init_logging():
    root = logging.getLogger()
    if root.hasHandlers():
        root.handlers.clear()
    formatter = logging.Formatter("[BILI] %(asctime)s[%(levelname)s] %(message)s")
    ding_filter = DingTalkFilter()
    # 128MB 机器日志轮转限制为最大 5MB，保留 2 个历史文件
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8", delay=True
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(ding_filter)
    root.addHandler(file_handler)
    if sys.stdout.isatty():
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        stream_handler.addFilter(ding_filter)
        root.addHandler(stream_handler)
    root.setLevel(logging.WARNING)
    root.propagate = False
    logging.info("=" * 60)
    logging.info("B站动态监控系统启动（可靠防漏 + UID独立补漏 + 低请求风控版）")
    logging.info("=" * 60)
def send_failure_notification(title, message):
    global _last_notify_time
    if len(_last_notify_time) > 200:
        _last_notify_time.clear()
    key = f"{title}:{message[:100]}"
    if time.time() - _last_notify_time.get(key, 0) >= 600:
        _last_notify_time[key] = time.time()
        safe_enqueue_notify(title, [{"user": "系统", "message": message}], "system")
def safe_enqueue_notify(title, items, notify_type="dynamic", dyn_id="", uid="", pub_ts=0):
    """统一入队。动态只有进入队列，不立即标记为已发送。"""
    task = {
        "title": title,
        "items": items,
        "notify_type": notify_type,
        "dyn_id": str(dyn_id or ""),
        "uid": str(uid or ""),
        "pub_ts": int(pub_ts or 0),
        "attempt": 0,
    }
    if notify_type == "dynamic" and task["dyn_id"]:
        dyn_id = task["dyn_id"]
        with STATE_LOCK:
            if dyn_id in PENDING_PUSH_IDS:
                return False
            if ACTIVE_STATE is not None and is_recent_pushed(ACTIVE_STATE, dyn_id):
                return False
            PENDING_PUSH_IDS.add(dyn_id)
    try:
        notify_queue.put_nowait(task)
        return True
    except queue.Full:
        if task["notify_type"] == "dynamic" and task["dyn_id"]:
            with STATE_LOCK:
                PENDING_PUSH_IDS.discard(task["dyn_id"])
        return False

def mark_dynamic_sent(state, dyn_id, uid, pub_ts):
    if not dyn_id:
        return
    with STATE_LOCK:
        add_recent_pushed_id(state, dyn_id)
        uid_state = get_uid_state(state, uid)
        remember_uid_dynamic(uid_state, dyn_id, pub_ts)
        feed = state.setdefault("feed", {})
        retry_after = feed.setdefault("push_retry_after", {})
        retry_after.pop(str(dyn_id), None)
        mark_state_dirty(state)

def schedule_dynamic_retry(state, dyn_id):
    if not dyn_id:
        return
    with STATE_LOCK:
        feed = state.setdefault("feed", {})
        retry_after = feed.setdefault("push_retry_after", {})
        retry_after[str(dyn_id)] = time.time() + NOTIFY_RETRY_COOLDOWN
        mark_state_dirty(state)

def is_push_retry_blocked(state, dyn_id, now_ts):
    feed = state.setdefault("feed", {})
    retry_after = feed.get("push_retry_after", {}) or {}
    try:
        return float(retry_after.get(str(dyn_id), 0) or 0) > now_ts
    except Exception:
        return False

def notify_worker():
    while IS_RUNNING or not notify_queue.empty():
        try:
            task = notify_queue.get(timeout=1)
        except queue.Empty:
            continue
        title = task.get("title")
        items = task.get("items") or []
        ntype = task.get("notify_type")
        dyn_id = str(task.get("dyn_id") or "")
        uid = str(task.get("uid") or "")
        pub_ts = int(task.get("pub_ts") or 0)
        attempt = int(task.get("attempt") or 0)
        try:
            if ntype == "dynamic":
                logging.info(f"[排队发送] {items[0].get('link', '') if items else dyn_id}")
            else:
                logging.info(f"[排队发送] 系统通知: {title}")
            ok = bool(notifier.send_webhook_notification(title, items, notify_type=ntype))
            if ok:
                if ntype == "dynamic" and dyn_id and ACTIVE_STATE is not None:
                    with STATE_LOCK:
                        PENDING_PUSH_IDS.discard(dyn_id)
                        mark_dynamic_sent(ACTIVE_STATE, dyn_id, uid, pub_ts)
                        save_dynamic_state(ACTIVE_STATE)
                logging.info(f"[发送成功] 类型={ntype} dyn_id={dyn_id or '-'}")
            else:
                if ntype == "dynamic" and dyn_id and attempt + 1 < NOTIFY_SEND_RETRIES and ACTIVE_STATE is not None:
                    task["attempt"] = attempt + 1
                    time.sleep(NOTIFY_SEND_DELAY)
                    try:
                        notify_queue.put_nowait(task)
                    except queue.Full:
                        with STATE_LOCK:
                            PENDING_PUSH_IDS.discard(dyn_id)
                            schedule_dynamic_retry(ACTIVE_STATE, dyn_id)
                elif ntype == "dynamic" and dyn_id and ACTIVE_STATE is not None:
                    with STATE_LOCK:
                        PENDING_PUSH_IDS.discard(dyn_id)
                        schedule_dynamic_retry(ACTIVE_STATE, dyn_id)
                    logging.error(f"[发送最终失败] dyn_id={dyn_id}，{NOTIFY_RETRY_COOLDOWN}s 后允许再次补发")
                else:
                    logging.warning(f"[发送失败] 类型={ntype}")
        except Exception as e:
            logging.error(f"推送消费失败: {repr(e)}")
            if ntype == "dynamic" and dyn_id and ACTIVE_STATE is not None:
                with STATE_LOCK:
                    PENDING_PUSH_IDS.discard(dyn_id)
                    schedule_dynamic_retry(ACTIVE_STATE, dyn_id)
        finally:
            notify_queue.task_done()
        time.sleep(NOTIFY_SEND_DELAY)

def safe_request(url, params, retries=REQUEST_RETRIES):
    """单层业务重试；Session 本身不再自动重试，避免请求次数失控。"""
    global IS_RUNNING
    if not IS_RUNNING and STATE.consecutive_cookie_failures >= COOKIE_FAIL_EXIT_THRESHOLD:
        return {"code": -101}
    base_delay = 3.0
    last_data = None
    for i in range(retries):
        try:
            response = REQ_SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
            try:
                data = response.json()
            finally:
                response.close()
            last_data = data
            code = data.get("code")
            if code == -101:
                STATE.consecutive_cookie_failures += 1
                logging.error(f"❌ B站 Cookie 已失效！连续失败 {STATE.consecutive_cookie_failures}/{COOKIE_FAIL_EXIT_THRESHOLD}")
                send_failure_notification(
                    "❌ B站 Cookie 失效预警",
                    f"Cookie 验证失败（连续第 {STATE.consecutive_cookie_failures} 次）。\n请立即重新获取并覆盖 bili_cookie.txt。"
                )
                if STATE.consecutive_cookie_failures >= COOKIE_FAIL_EXIT_THRESHOLD:
                    logging.critical("🛑 Cookie 连续失效达到阈值，停止监控。")
                    IS_RUNNING = False
                return data
            STATE.consecutive_cookie_failures = 0

            if code in (-799, -352, -509, -412):
                wait = min(120.0, base_delay * (2 ** i)) + random.uniform(2.0, 5.0)
                logging.warning(f"⚠️ 触发风控 {code}，等待 {wait:.1f}s 后重试并刷新 WBI")
                force_update_wbi_keys()
                send_failure_notification(
                    "🚨 B站风控安全预警",
                    f"状态码={code}，本次自动退避 {wait:.1f} 秒。"
                )
                time.sleep(wait)
                continue
            if code == 0:
                return data
            if i < retries - 1:
                wait = base_delay * (2 ** i) + random.uniform(0.8, 2.5)
                logging.warning(f"[请求重试] code={code} wait={wait:.1f}s url={url}")
                time.sleep(wait)
            else:
                return data
        except Exception as e:
            last_data = {"code": -500, "message": repr(e)}
            if i < retries - 1:
                wait = base_delay * (2 ** i) + random.uniform(0.8, 2.5)
                logging.warning(f"[网络重试] {repr(e)} wait={wait:.1f}s url={url}")
                time.sleep(wait)
    logging.error(f"请求最终失败: {url}")
    send_failure_notification("❌ B站 API 请求最终失败", f"接口 {url} 连续重试 {retries} 次失败。")
    return last_data or {"code": -500}
def getMixinKey(orig):
    return ''.join([orig[i] for i in mixinKeyEncTab])[:32]
def encWbi(params, img_key, sub_key):
    mixin_key = getMixinKey(img_key + sub_key)
    params["wts"] = int(time.time())
    params = dict(sorted(params.items()))
    filtered = {}
    for k, v in params.items():
        v = str(v)
        for c in "!'()*":
            v = v.replace(c, "")
        filtered[k] = v
    query = urllib.parse.urlencode(filtered, quote_via=urllib.parse.quote)
    sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
    filtered["w_rid"] = sign
    return filtered
def force_update_wbi_keys():
    """独立轻量刷新，避免与 safe_request 互相调用"""
    try:
        r = REQ_SESSION.get("https://api.bilibili.com/x/web-interface/nav", timeout=8)
        data = r.json()
        if data.get("code") in (0, -101):
            img = data.get("data", {}).get("wbi_img", {})
            img_url = img.get("img_url", "")
            sub_url = img.get("sub_url", "")
            if img_url and sub_url:
                WBI_KEYS["img_key"] = img_url.rsplit("/", 1)[1].split(".")[0]
                WBI_KEYS["sub_key"] = sub_url.rsplit("/", 1)[1].split(".")[0]
                WBI_KEYS["last_update"] = time.time()
                logging.info("✅ WBI 密钥已强制刷新成功")
                return True
    except Exception as e:
        logging.error(f"强制更新WBI异常: {e}")
    return False
def update_wbi_keys():
    if WBI_KEYS["img_key"] and time.time() - WBI_KEYS["last_update"] < 21600:
        return
    force_update_wbi_keys()
def wbi_request(url, params):
    update_wbi_keys()
    if not WBI_KEYS["img_key"] or not WBI_KEYS["sub_key"]:
        return safe_request(url, params)
    try:
        signed = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
        return safe_request(url, signed)
    except Exception:
        return safe_request(url, params)
# ================== 业务控制与状态流 ==================
def get_scan_interval():
    """主关注流扫描频率。保持随机抖动，避免固定周期；不再使用高频 burst。"""
    if STATE.consecutive_failures >= 2:
        return random.uniform(20.0, 35.0)
    return random.uniform(NORMAL_INTERVAL_MIN, NORMAL_INTERVAL_MAX)

def load_following_cache():
    if os.path.exists(FOLLOWING_CACHE_FILE):
        try:
            with open(FOLLOWING_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []
    return []
def save_following_cache(uids):
    try:
        atomic_write_json(FOLLOWING_CACHE_FILE, uids)
    except Exception:
        pass
def load_dynamic_state():
    default_state = {
        "version": 3,
        "feed": {
            "baseline": "",
            "offset": "",
            "last_ts": 0,
            "last_ts_ids": [],
            "recent_pushed_ids": [],
            "push_retry_after": {}
        },
        "uid_state": {},
        "_meta": {"dirty": False}
    }
    if not os.path.exists(DYNAMIC_STATE_FILE):
        return default_state
    try:
        with open(DYNAMIC_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            return default_state
    except Exception:
        bak = DYNAMIC_STATE_FILE + ".bak"
        try:
            with open(bak, "r", encoding="utf-8") as f:
                state = json.load(f)
            if not isinstance(state, dict):
                return default_state
        except Exception:
            return default_state

    feed = state.setdefault("feed", {})
    feed.setdefault("baseline", "")
    feed.setdefault("offset", "")
    feed.setdefault("last_ts", 0)
    feed.setdefault("last_ts_ids", [])
    # 兼容旧版本字段
    if "recent_pushed_ids" not in feed:
        feed["recent_pushed_ids"] = []
    feed.setdefault("push_retry_after", {})
    state.setdefault("uid_state", {})
    state.setdefault("_meta", {"dirty": False})
    state["version"] = max(3, int(state.get("version", 1) or 1))
    return state

def save_dynamic_state(state):
    if not state:
        return
    try:
        with STATE_LOCK:
            feed = state.setdefault("feed", {})
            feed["last_ts_ids"] = list(feed.get("last_ts_ids", []) or [])[-LAST_TS_IDS_LIMIT:]
            feed["recent_pushed_ids"] = list(feed.get("recent_pushed_ids", []) or [])[:RECENT_PUSHED_IDS_LIMIT]
            retry_after = feed.get("push_retry_after", {}) or {}
            now = time.time()
            feed["push_retry_after"] = {k: v for k, v in retry_after.items() if float(v or 0) > now}
            # 保证 UID 状态不会无限膨胀
            uid_state = state.setdefault("uid_state", {})
            if len(uid_state) > 3000:
                ordered = sorted(uid_state.items(), key=lambda kv: float((kv[1] or {}).get("last_direct_check", 0) or 0), reverse=True)
                state["uid_state"] = dict(ordered[:3000])
            atomic_write_json(DYNAMIC_STATE_FILE, state)
            state.setdefault("_meta", {})["dirty"] = False
    except Exception as e:
        logging.error(f"保存 dynamic_state 失败: {repr(e)}")

def init_seen_cache():
    return {"set": set(), "queue": deque()}

def add_seen_cache(cache, item_id, max_size):
    s, q = cache["set"], cache["queue"]
    if item_id in s:
        return False
    s.add(item_id)
    q.append(item_id)
    while len(q) > max_size:
        s.discard(q.popleft())
    return True

def add_recent_pushed_id(state, dyn_id):
    feed = state.setdefault("feed", {})
    recent = list(feed.get("recent_pushed_ids", []) or [])
    dyn_id = str(dyn_id)
    if dyn_id in recent:
        recent.remove(dyn_id)
    recent.insert(0, dyn_id)
    feed["recent_pushed_ids"] = recent[:RECENT_PUSHED_IDS_LIMIT]

def is_recent_pushed(state, dyn_id):
    feed = state.setdefault("feed", {})
    return str(dyn_id) in set(feed.get("recent_pushed_ids", []) or [])

def get_uid_state(state, uid):
    uid = str(uid)
    root = state.setdefault("uid_state", {})
    item = root.get(uid)
    if not isinstance(item, dict):
        item = {}
    item.setdefault("last_ts", 0)
    item.setdefault("seen_ids", [])
    item.setdefault("last_global_seen", 0)
    item.setdefault("last_direct_check", 0)
    root[uid] = item
    return item

def remember_uid_dynamic(uid_state, dyn_id, pub_ts):
    if not dyn_id:
        return
    dyn_id = str(dyn_id)
    ids = list(uid_state.get("seen_ids", []) or [])
    if dyn_id in ids:
        ids.remove(dyn_id)
    ids.insert(0, dyn_id)
    uid_state["seen_ids"] = ids[:UID_STATE_SEEN_LIMIT]
    if pub_ts > int(uid_state.get("last_ts", 0) or 0):
        uid_state["last_ts"] = int(pub_ts)

def uid_has_seen(uid_state, dyn_id):
    return str(dyn_id) in set(uid_state.get("seen_ids", []) or [])

def is_new_uid_dynamic(uid_state, dyn_id, pub_ts, now_ts):
    """UID 独立判断；允许短时间迟到的动态补回来。"""
    if not dyn_id:
        return False
    if uid_has_seen(uid_state, dyn_id):
        return False
    last_ts = int(uid_state.get("last_ts", 0) or 0)
    if last_ts <= 0:
        return pub_ts >= now_ts - NEW_UID_BOOTSTRAP_WINDOW
    if pub_ts >= last_ts:
        return True
    # 动态可能晚于主关注流出现；6小时内没见过的 ID 仍允许补发。
    return pub_ts >= now_ts - DYNAMIC_NEW_WINDOW

def is_uid_candidate_blocked(state, dyn_id, now_ts):
    with STATE_LOCK:
        if dyn_id in PENDING_PUSH_IDS:
            return True
        if is_recent_pushed(state, dyn_id):
            return True
        return is_push_retry_blocked(state, dyn_id, now_ts)

def update_last_ts_state(feed_state, dyn_id, pub_ts):
    last_ts = int(feed_state.get("last_ts", 0) or 0)
    if pub_ts > last_ts:
        feed_state["last_ts"] = pub_ts
        feed_state["last_ts_ids"] = [dyn_id]
    elif pub_ts == last_ts:
        last_ts_ids = list(feed_state.get("last_ts_ids", []) or [])
        if dyn_id not in last_ts_ids:
            last_ts_ids.append(dyn_id)
            feed_state["last_ts_ids"] = last_ts_ids[:LAST_TS_IDS_LIMIT]

def is_allowed_dynamic(item):
    try:
        top_type = item.get("type", "")
        modules = item.get("modules", {}) or {}
        major_type = modules.get("module_dynamic", {}).get("major", {}).get("type", "")
        if top_type == "DYNAMIC_TYPE_FORWARD":
            return ALLOW_FORWARD_DYNAMIC
        if top_type and top_type not in ALLOWED_TOP_LEVEL_TYPES:
            return False
        if major_type not in ALLOWED_DYNAMIC_TYPES:
            return False
        return True
    except Exception:
        return False
def extract_dynamic_text(item):
    try:
        modules = item.get("modules") or {}
        dyn = modules.get("module_dynamic") or {}
        desc = dyn.get("desc") or {}
        nodes = desc.get("rich_text_nodes") or []
        if nodes:
            text = "".join(
                n.get("text", "")
                for n in nodes
                if isinstance(n, dict) and n.get("type") in (
                    "RICH_TEXT_NODE_TYPE_TEXT",
                    "RICH_TEXT_NODE_TYPE_TOPIC",
                    "RICH_TEXT_NODE_TYPE_AT",
                    "RICH_TEXT_NODE_TYPE_EMOJI",
                    "RICH_TEXT_NODE_TYPE_LOTTERY"
                )
            ).strip()
            text = normalize_text(text)
            if text:
                return text
        major = dyn.get("major") or {}
        t = major.get("type", "")
        if t == "MAJOR_TYPE_ARCHIVE":
            a = major.get("archive") or {}
            title = normalize_text(a.get("title", ""))
            desc_text = normalize_text(a.get("desc", ""))
            if title and desc_text:
                return f"【视频】{title}\n{desc_text}"
            return f"【视频】{title or desc_text}".strip()
        if t == "MAJOR_TYPE_ARTICLE":
            a = major.get("article", {}) or {}
            title = normalize_text(a.get("title", ""))
            desc_text = normalize_text(a.get("desc", ""))
            if title and desc_text:
                return f"【专栏】{title}\n{desc_text}"
            return f"【专栏】{title or desc_text}".strip()
        if t == "MAJOR_TYPE_OPUS":
            opus = major.get("opus", {}) or {}
            summary = opus.get("summary", {}) or {}
            nodes = summary.get("rich_text_nodes") or []
            text = "".join(n.get("text", "") for n in nodes if isinstance(n, dict)).strip()
            text = normalize_text(text)
            title = normalize_text(opus.get("title") or "")
            if title and text:
                return f"【图文】{title}\n{text}"
            return text or f"【图文】{title}".strip()
        if t == "MAJOR_TYPE_DRAW":
            desc_text = normalize_text(desc.get("text", ""))
            return desc_text or "【图片动态】"
        if t == "MAJOR_TYPE_COMMON":
            common = major.get("common", {}) or {}
            title = normalize_text(common.get("title", ""))
            desc_text = normalize_text(common.get("desc", ""))
            if title and desc_text:
                return f"【卡片】{title}\n{desc_text}"
            return f"【卡片】{title or desc_text}".strip()
        if t == "MAJOR_TYPE_LIVE":
            live = major.get("live", {}) or {}
            title = normalize_text(live.get("title", ""))
            desc_text = normalize_text(live.get("desc_second", ""))
            if title and desc_text:
                return f"【直播】{title}\n{desc_text}"
            return f"【直播】{title or desc_text}".strip()
        return normalize_text(desc.get("text", ""))
    except Exception:
        return ""
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
            if orig_id:
                text = f"{text}\n\n原动态： https://t.bilibili.com/{orig_id}"
    if not text:
        text = "（该动态无可提取正文）"
    time_str = datetime.datetime.fromtimestamp(pub_ts).strftime("%Y-%m-%d %H:%M:%S") if pub_ts > 0 else "未知时间"
    cover = ""
    try:
        modules = item.get("modules", {}) or {}
        dyn_module = modules.get("module_dynamic", {}) or {}
        major = dyn_module.get("major", {}) or {}
        if major.get("type") == "MAJOR_TYPE_DRAW":
            cover = major.get("draw", {}).get("items", [{}])[0].get("src", "")
        elif major.get("type") == "MAJOR_TYPE_ARCHIVE":
            cover = major.get("archive", {}).get("cover", "")
        elif major.get("type") == "MAJOR_TYPE_OPUS":
            cover = major.get("opus", {}).get("pics", [{}])[0].get("url", "") or major.get("opus", {}).get("cover", "")
    except Exception:
        cover = ""
    return {
        "user": name,
        "message": text,
        "time": time_str,
        "link": f"https://t.bilibili.com/{dyn_id}",
        "cover": cover,
        "kind": "dynamic"
    }
# ================== 核心关注流监测 ==================
def fetch_following_feed(offset="", update_baseline=""):
    params = {
        "type": "all",
        "timezone_offset": "-480",
        "platform": "web",
        "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,decorationCard,onlyfansAssetsV2,forwardListHidden,ugcDelete",
        "web_location": "333.1365"
    }
    if offset:
        params["offset"] = offset
    if update_baseline:
        params["update_baseline"] = update_baseline
    return wbi_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all", params)
def fetch_following_feed_retry(offset="", update_baseline="", retries=2):
    last = None
    for _ in range(retries + 1):
        data = fetch_following_feed(offset=offset, update_baseline=update_baseline)
        last = data
        if data.get("code") == 0:
            return data
        if not IS_RUNNING:
            return last or {"code": -101}
        time.sleep(random.uniform(0.8, 1.6))
    return last or {"code": -500}
def check_feed_update(update_baseline):
    params = {
        "type": "all",
        "update_baseline": update_baseline or "0",
        "web_location": "333.1365"
    }
    return safe_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all/update", params)
def get_following_list(uid):
    """完整获取关注列表；任何分页失败都返回 None，绝不拿“半截列表”覆盖正常缓存。"""
    following = []
    pn = 1
    ps = 50
    max_pages = 200
    while IS_RUNNING and pn <= max_pages:
        data = safe_request("https://api.bilibili.com/x/relation/followings", {
            "vmid": uid, "pn": pn, "ps": ps, "order": "desc", "order_type": "attention"
        })
        if data.get("code") != 0:
            logging.warning(f"关注列表第 {pn} 页获取失败 code={data.get('code')}，本次刷新放弃覆盖")
            return None
        page_data = data.get("data") or {}
        items = page_data.get("list") or []
        for item in items:
            mid = item.get("mid") if isinstance(item, dict) else None
            if mid is not None:
                following.append(str(mid))
        if len(items) < ps:
            break
        pn += 1
        time.sleep(random.uniform(0.6, 1.2))
    if not following:
        logging.warning("关注列表获取结果为空，本次刷新放弃覆盖")
        return None
    return list(dict.fromkeys(following))

def init_feed_state(target_uids):
    state = load_dynamic_state()
    seen_dynamic_ids = init_seen_cache()
    if not IS_RUNNING:
        return seen_dynamic_ids, state
    try:
        state.setdefault("uid_state", {})
        for uid in target_uids:
            get_uid_state(state, uid)

        feed_state = state.setdefault("feed", {})
        baseline = str(feed_state.get("baseline") or "")
        offset = ""
        max_ts = int(feed_state.get("last_ts", 0) or 0)
        max_ts_ids = set(feed_state.get("last_ts_ids", []) or [])

        for page_idx in range(FEED_INIT_PAGES):
            if not IS_RUNNING:
                break
            data = fetch_following_feed_retry(offset=offset)
            if data.get("code") != 0:
                logging.warning(f"关注流初始化第 {page_idx + 1} 页失败 code={data.get('code')}")
                break
            feed = data.get("data", {}) or {}
            items = feed.get("items") or []
            if page_idx == 0 and items:
                baseline = str(feed.get("update_baseline") or items[0].get("id_str") or baseline)
            for item in items:
                try:
                    dyn_id = str(item.get("id_str") or "")
                    if dyn_id:
                        add_seen_cache(seen_dynamic_ids, dyn_id, MAX_SEEN_DYNAMIC_IDS)
                    author = item.get("modules", {}).get("module_author", {}) or {}
                    uid = str(author.get("mid", ""))
                    pub_ts = int(author.get("pub_ts", 0) or 0)
                    if uid not in target_uids:
                        continue
                    us = get_uid_state(state, uid)
                    us["last_global_seen"] = time.time()
                    if dyn_id:
                        remember_uid_dynamic(us, dyn_id, pub_ts)
                    if pub_ts > max_ts:
                        max_ts = pub_ts
                        max_ts_ids = {dyn_id} if dyn_id else set()
                    elif pub_ts == max_ts and dyn_id:
                        max_ts_ids.add(dyn_id)
                except Exception:
                    continue
            offset_next = str(feed.get("offset") or "")
            has_more = bool(feed.get("has_more"))
            if not offset_next or not has_more or not items:
                break
            offset = offset_next
            time.sleep(random.uniform(0.4, 0.8))

        feed_state["baseline"] = baseline
        feed_state["offset"] = offset
        # 保留兼容字段，不再作为 UID 新旧判断依据。
        feed_state["last_ts"] = max_ts
        feed_state["last_ts_ids"] = list(max_ts_ids)[:LAST_TS_IDS_LIMIT]
        feed_state.setdefault("recent_pushed_ids", [])
        feed_state.setdefault("push_retry_after", {})
        save_dynamic_state(state)
        logging.info(f"关注流初始化完成 baseline={baseline} 监控UID={len(target_uids)}")
    except Exception as e:
        logging.error(f"关注流初始化异常: {repr(e)}")
    return seen_dynamic_ids, state

def process_feed_items(items, target_uids, seen_dynamic_ids, state, now_ts):
    has_new = False
    candidate_items = {}
    for item in items:
        try:
            if not isinstance(item, dict):
                continue
            dyn_id = str(item.get("id_str") or "")
            if not dyn_id:
                continue
            add_seen_cache(seen_dynamic_ids, dyn_id, MAX_SEEN_DYNAMIC_IDS)
            author = item.get("modules", {}).get("module_author", {}) or {}
            uid = str(author.get("mid", ""))
            pub_ts = int(author.get("pub_ts", 0) or 0)
            if uid not in target_uids:
                continue

            # 主关注流只要看到该 UID，就更新健康时间；不要求动态类型必须是可推送类型。
            uid_state = get_uid_state(state, uid)
            uid_state["last_global_seen"] = now_ts

            if not is_allowed_dynamic(item):
                continue
            if is_uid_candidate_blocked(state, dyn_id, now_ts):
                if is_recent_pushed(state, dyn_id):
                    remember_uid_dynamic(uid_state, dyn_id, pub_ts)
                continue
            if is_new_uid_dynamic(uid_state, dyn_id, pub_ts, now_ts):
                candidate_items[dyn_id] = (pub_ts, uid, item)
        except Exception:
            logging.debug("处理关注流条目异常", exc_info=True)

    candidates = sorted(
        ((pub_ts, dyn_id, uid, item) for dyn_id, (pub_ts, uid, item) in candidate_items.items()),
        key=lambda x: (x[0], x[1])
    )
    for pub_ts, dyn_id, uid, item in candidates:
        try:
            push_data = format_dynamic_message(item)
            title = f"{push_data.get('user', '未知UP')} 发布了新动态"
            ok = safe_enqueue_notify(title, [push_data], "dynamic", dyn_id=dyn_id, uid=uid, pub_ts=pub_ts)
            if ok:
                # 只记录内存中的“待发送”，真正 sent 由 notify_worker 在 Webhook 成功后落盘。
                has_new = True
                remember_uid_dynamic(get_uid_state(state, uid), dyn_id, pub_ts)
                logging.info(f"📥 发现新动态 uid={uid} dyn_id={dyn_id} pub_ts={pub_ts}")
        except Exception as e:
            logging.error(f"动态入队异常 dyn_id={dyn_id} err={repr(e)}")
    return has_new

def fetch_user_dynamic_feed(uid, offset=""):
    params = {
        "host_mid": str(uid),
        "type": "all",
        "timezone_offset": "-480",
        "platform": "web",
        "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,decorationCard,onlyfansAssetsV2,forwardListHidden,ugcDelete",
        "web_location": "333.1387"
    }
    if offset:
        params["offset"] = offset
    return wbi_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space", params)

def fetch_user_dynamic_feed_retry(uid, offset="", retries=2):
    last = None
    for _ in range(retries + 1):
        data = fetch_user_dynamic_feed(uid, offset)
        last = data
        if data.get("code") == 0:
            return data
        if not IS_RUNNING:
            break
        time.sleep(random.uniform(0.8, 1.6))
    return last or {"code": -500}

def repair_one_uid(uid, state, now_ts):
    uid = str(uid)
    uid_state = get_uid_state(state, uid)
    uid_state["last_direct_check"] = now_ts

    bootstrap = int(uid_state.get("last_ts", 0) or 0) <= 0 and not uid_state.get("seen_ids")
    bootstrap_cutoff = now_ts - NEW_UID_BOOTSTRAP_WINDOW
    offset = ""
    page_count = 0
    found_new = []
    seen_any = False

    while page_count < UID_DIRECT_MAX_PAGES and IS_RUNNING:
        data = fetch_user_dynamic_feed_retry(uid, offset=offset)
        if data.get("code") != 0:
            logging.warning(f"UID 独立补漏失败 uid={uid} code={data.get('code')}")
            return False
        page_count += 1
        feed = data.get("data", {}) or {}
        items = feed.get("items") or []
        if not items:
            break

        reached_old = False
        for item in items:
            try:
                if not isinstance(item, dict):
                    continue
                dyn_id = str(item.get("id_str") or "")
                author = item.get("modules", {}).get("module_author", {}) or {}
                author_uid = str(author.get("mid", ""))
                pub_ts = int(author.get("pub_ts", 0) or 0)
                if author_uid != uid or not dyn_id:
                    continue
                seen_any = True
                uid_state["last_global_seen"] = now_ts

                if is_allowed_dynamic(item) and not is_uid_candidate_blocked(state, dyn_id, now_ts):
                    if bootstrap:
                        candidate = pub_ts >= bootstrap_cutoff
                    else:
                        candidate = is_new_uid_dynamic(uid_state, dyn_id, pub_ts, now_ts)
                    if candidate:
                        found_new.append((pub_ts, dyn_id, uid, item))

                # 对已看到的历史 ID 建索引；但 bootstrap 模式下不要用历史项推进到当前未来。
                if not bootstrap or pub_ts < bootstrap_cutoff:
                    if uid_has_seen(uid_state, dyn_id) is False:
                        remember_uid_dynamic(uid_state, dyn_id, pub_ts)
            except Exception:
                continue

        offset_next = str(feed.get("offset") or "")
        has_more = bool(feed.get("has_more"))
        if not offset_next or not has_more:
            break
        offset = offset_next
        time.sleep(random.uniform(0.4, 0.8))

    # 去重 + 按发布时间升序推送，避免补漏时顺序反了。
    unique = {}
    for pub_ts, dyn_id, uid, item in found_new:
        unique[dyn_id] = (pub_ts, dyn_id, uid, item)
    for pub_ts, dyn_id, uid, item in sorted(unique.values(), key=lambda x: (x[0], x[1])):
        try:
            push_data = format_dynamic_message(item)
            title = f"{push_data.get('user', '未知UP')} 发布了新动态"
            ok = safe_enqueue_notify(title, [push_data], "dynamic", dyn_id=dyn_id, uid=uid, pub_ts=pub_ts)
            if ok:
                remember_uid_dynamic(uid_state, dyn_id, pub_ts)
                logging.info(f"🔧 UID补漏发现 uid={uid} dyn_id={dyn_id} pub_ts={pub_ts}")
        except Exception as e:
            logging.error(f"UID补漏入队异常 uid={uid} dyn_id={dyn_id}: {repr(e)}")

    if bootstrap and seen_any:
        # 第一次建立 UID 基线，不把老历史全量推送；但保留最近窗口内已经入队的动态。
        uid_state["bootstrap_done"] = True
    return bool(found_new)

def repair_stale_uids(target_uids, state, now_ts):
    candidates = []
    for uid in target_uids:
        uid = str(uid)
        us = get_uid_state(state, uid)
        last_direct = int(us.get("last_direct_check", 0) or 0)
        last_global = int(us.get("last_global_seen", 0) or 0)
        direct_due = last_direct == 0 or now_ts - last_direct >= UID_DIRECT_CHECK_INTERVAL
        stale = last_global == 0 or now_ts - last_global >= UID_STALE_GLOBAL_THRESHOLD
        if not direct_due:
            continue
        # 只有从未看到、或长时间没在主流出现的 UID 才进入独立补漏；避免每轮扫描全量打 API。
        if stale or not us.get("seen_ids"):
            priority = (0 if stale else 1, last_global, last_direct, uid)
            candidates.append((priority, uid))

    candidates.sort(key=lambda x: x[0])
    has_new = False
    for _, uid in candidates[:UID_DIRECT_CHECK_BATCH]:
        if not IS_RUNNING:
            break
        try:
            if repair_one_uid(uid, state, now_ts):
                has_new = True
        except Exception as e:
            logging.warning(f"UID补漏异常 uid={uid} err={repr(e)}")
    return has_new

def scan_following_feed(target_uids, seen_dynamic_ids, state, now_ts):
    if not IS_RUNNING:
        return False
    feed_state = state.setdefault("feed", {})
    baseline = str(feed_state.get("baseline") or "")
    update_data = check_feed_update(baseline)
    update_ok = update_data.get("code") == 0
    update_num = 0
    if update_ok:
        update_num = int((update_data.get("data") or {}).get("update_num", 0) or 0)
        if update_num > 0:
            STATE.consecutive_no_update_rounds = 0
        else:
            # 关键修复：update_num=0 仍然检查第一页。
            STATE.consecutive_no_update_rounds += 1
    else:
        STATE.consecutive_failures += 1

    has_new = False
    offset = ""
    page_count = 0
    completed = True
    first_page_baseline = baseline
    max_pages = FEED_FETCH_MAX_PAGES if update_num > 0 else FEED_FETCH_BASE_PAGES

    while page_count < max_pages and IS_RUNNING:
        data = fetch_following_feed_retry(offset=offset)
        if data.get("code") != 0:
            STATE.consecutive_failures += 1
            completed = False
            break
        STATE.consecutive_failures = 0
        feed = data.get("data", {}) or {}
        items = feed.get("items") or []
        page_count += 1
        if not items:
            break
        if page_count == 1:
            first_page_baseline = str(feed.get("update_baseline") or items[0].get("id_str") or baseline)
        if process_feed_items(items, target_uids, seen_dynamic_ids, state, now_ts):
            has_new = True

        offset_next = str(feed.get("offset") or "")
        has_more = bool(feed.get("has_more"))
        if not offset_next or not has_more:
            break
        offset = offset_next
        # 没更新时不要翻页，减少请求；有更新时最多到 MAX_PAGES。
        if page_count >= max_pages:
            break
        time.sleep(random.uniform(0.4, 0.8))

    # 只有至少第一页成功，才允许推进 baseline。失败则保留旧 baseline，下一轮重试。
    if completed and page_count > 0 and first_page_baseline:
        feed_state["baseline"] = first_page_baseline
        feed_state["offset"] = offset

    # 独立 UID 补漏是完整性的保险通道。
    if IS_RUNNING:
        try:
            if repair_stale_uids(target_uids, state, now_ts):
                has_new = True
        except Exception as e:
            logging.warning(f"UID补漏总流程异常: {repr(e)}")

    return has_new

def start_monitoring():
    global IS_RUNNING, ACTIVE_STATE
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    last_hb = 0
    last_following_refresh = 0
    last_d_check = 0

    activate_session_cookies()
    if not load_cookies_into_session():
        logging.critical("❌ Cookie 文件不存在或为空，程序退出")
        return
    update_wbi_keys()
    if not IS_RUNNING:
        logging.critical("因 Cookie 失效，跳过后续初始化，程序退出")
        return

    # 启动时优先实时获取完整关注列表；失败才使用缓存；缓存再失败才 fallback。
    following_list = get_following_list(SOURCE_UID)
    list_source = "实时"
    if following_list is None:
        following_list = load_following_cache()
        list_source = "缓存"
    if not following_list:
        following_list = FALLBACK_DYNAMIC_UIDS[:]
        list_source = "fallback"
    following_list = [str(uid) for uid in following_list]
    if str(SOURCE_UID) not in following_list:
        following_list.append(str(SOURCE_UID))
    following_list = list(dict.fromkeys(following_list))
    save_following_cache(following_list)
    target_uids = set(following_list)
    last_following_refresh = time.time()

    logging.info(f"监控 {len(target_uids)} 个 UID（关注流模式，列表来源={list_source}）")

    seen_dynamic_ids, state = init_feed_state(target_uids)
    ACTIVE_STATE = state
    if not IS_RUNNING:
        save_dynamic_state(state)
        return

    # 推送线程放在 ACTIVE_STATE 设置之后，避免线程拿到空状态。
    threading.Thread(target=notify_worker, daemon=True, name="notify-worker").start()
    STATE.last_new_dynamic_time = time.time()
    logging.info(
        f"✅ 系统初始化完成：工作日 {RUN_START_HOUR}:{RUN_START_MINUTE:02d}-{RUN_END_HOUR}:00；"
        f"关注列表1小时刷新；心跳1小时一次；主流{NORMAL_INTERVAL_MIN:g}~{NORMAL_INTERVAL_MAX:g}s随机扫描；UID补漏启用"
    )

    while IS_RUNNING:
        try:
            now = time.time()
            try:
                china_now = datetime.datetime.now(ZoneInfo(RUN_TZ))
            except Exception:
                china_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)

            # 每小时心跳独立运行，不受工作窗口影响。
            if now - last_hb >= HEARTBEAT_INTERVAL:
                counts = get_state_counts(state)
                logging.info(
                    f"💓 心跳正常 | UID={len(target_uids)} | 主流扫描间隔={get_scan_interval():.1f}s | "
                    f"失败={STATE.consecutive_failures} | 无更新轮数={STATE.consecutive_no_update_rounds} | "
                    f"Cookie失败={STATE.consecutive_cookie_failures} | 待发送={counts['pending']} | "
                    f"重试冷却={counts['retrying']} | 已发送缓存={counts['sent_cache']}"
                )
                last_hb = now

            # 工作时间外：不跑动态扫描，但主循环继续维持状态、关注列表和心跳。
            if not is_in_monitor_window(china_now):
                if now - last_following_refresh >= FOLLOWING_REFRESH_INTERVAL:
                    new_list = get_following_list(SOURCE_UID)
                    if new_list is not None and IS_RUNNING:
                        new_list = [str(uid) for uid in new_list]
                        if str(SOURCE_UID) not in new_list:
                            new_list.append(str(SOURCE_UID))
                        new_list = list(dict.fromkeys(new_list))
                        old_set = set(following_list)
                        new_set = set(new_list)
                        following_list = new_list
                        target_uids = new_set
                        save_following_cache(following_list)
                        for uid in target_uids:
                            get_uid_state(state, uid)
                        if new_set != old_set:
                            logging.info(f"过滤UID已刷新，当前共 {len(target_uids)} 个")
                        else:
                            logging.info(f"关注列表1小时刷新完成，UID数量不变={len(target_uids)}")
                        mark_state_dirty(state)
                    else:
                        logging.warning("关注列表本轮刷新失败，继续使用旧列表")
                    last_following_refresh = now
                if now - STATE.last_state_save >= STATE_SAVE_INTERVAL:
                    save_dynamic_state(state)
                    STATE.last_state_save = now
                time.sleep(2.0)
                continue

            # 每日打卡
            today_str = china_now.strftime("%Y-%m-%d")
            if STATE.last_checkin_date != today_str:
                STATE.last_checkin_date = today_str
                safe_enqueue_notify(
                    "☀️ B站动态监控系统打卡上班（发布了新动态）",
                    [{"user": "系统雷达", "message": f"今天({today_str})工作日打卡成功！B站动态监控已锁定 {len(target_uids)} 个目标 UP 主。"}],
                    "system"
                )

            # 动态主扫描
            if now - last_d_check >= get_scan_interval():
                try:
                    scan_following_feed(target_uids, seen_dynamic_ids, state, int(now))
                except Exception as e:
                    STATE.consecutive_failures += 1
                    logging.error(f"关注流扫描异常: {repr(e)}")
                last_d_check = now

            # 关注列表严格每小时刷新；失败绝不覆盖旧列表。
            if now - last_following_refresh >= FOLLOWING_REFRESH_INTERVAL:
                try:
                    new_list = get_following_list(SOURCE_UID)
                    if new_list is None:
                        logging.warning("关注列表刷新失败：保留当前列表，不使用残缺数据覆盖")
                    elif IS_RUNNING:
                        new_list = [str(uid) for uid in new_list]
                        if str(SOURCE_UID) not in new_list:
                            new_list.append(str(SOURCE_UID))
                        new_list = list(dict.fromkeys(new_list))
                        old_set = set(following_list)
                        new_set = set(new_list)
                        following_list = new_list
                        target_uids = new_set
                        save_following_cache(following_list)
                        for uid in target_uids:
                            get_uid_state(state, uid)
                        # 清理已经取消关注 UID 的推送冷却键，但保留 uid_state 历史。
                        logging.info(
                            f"🔄 关注列表每小时刷新完成：{len(old_set)} → {len(new_set)} UID"
                            if old_set != new_set else
                            f"🔄 关注列表每小时检查完成：UID数量={len(new_set)}，无变化"
                        )
                        mark_state_dirty(state)
                except Exception as e:
                    logging.warning(f"关注列表刷新异常：{repr(e)}，保留旧列表")
                last_following_refresh = now

            if now - STATE.last_state_save >= STATE_SAVE_INTERVAL:
                save_dynamic_state(state)
                STATE.last_state_save = now
            time.sleep(1.0)

        except Exception as e:
            if IS_RUNNING:
                logging.error(f"主循环异常: {repr(e)}", exc_info=True)
                time.sleep(8)
            else:
                break

    if state:
        save_dynamic_state(state)
    logging.info("💾 监控状态已安全落盘，程序退出。")

if __name__ == "__main__":
    init_logging()
    start_monitoring()
