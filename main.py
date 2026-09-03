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
HEARTBEAT_INTERVAL = 30
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
# ===== 动态参数 / 智能爆发模式 =====
STATE_SAVE_INTERVAL = 30
# 正常间隔（无更新时）
NORMAL_INTERVAL_MIN = 8.0
NORMAL_INTERVAL_MAX = 15.0
# 每天 9:20–10:20 强制高频爆发
MORNING_BURST_START_HOUR = 9
MORNING_BURST_START_MINUTE = 20
MORNING_BURST_END_HOUR = 10
MORNING_BURST_END_MINUTE = 20
MORNING_BURST_INTERVAL_MIN = 3.0
MORNING_BURST_INTERVAL_MAX = 5.0
# 智能爆发（检测到新动态后）
BURST_MODE_DURATION = 90
BURST_COOLDOWN = 30
BURST_MAX_CHAIN = 3
BURST_INTERVAL_MIN = 3.0
BURST_INTERVAL_MAX = 5.0
IDLE_INTERVAL_MIN = 20.0
IDLE_INTERVAL_MAX = 30.0
IDLE_MODE_THRESHOLD = 300
FAILURE_EXIT_BURST = 2
FAILURE_SLOWDOWN_THRESHOLD = 3
FAILURE_SLOW_INTERVAL_MIN = 12.0
FAILURE_SLOW_INTERVAL_MAX = 20.0
NO_UPDATE_SLOWDOWN_THRESHOLD_1 = 10
NO_UPDATE_SLOWDOWN_THRESHOLD_2 = 30
NO_UPDATE_INTERVAL_1_MIN = 12.0
NO_UPDATE_INTERVAL_1_MAX = 18.0
NO_UPDATE_INTERVAL_2_MIN = 20.0
NO_UPDATE_INTERVAL_2_MAX = 30.0
# 内存微调：由 6000 降为 2000，极致节省 128MB 服务器内存
MAX_SEEN_DYNAMIC_IDS = 2000
DYNAMIC_NEW_WINDOW = 6 * 3600          # 6小时，覆盖短时扫描间隙
FEED_FETCH_MAX_PAGES = 8               # 一次多翻页，覆盖短时爆发
FEED_INIT_PAGES = 2
RECENT_PUSHED_IDS_LIMIT = 1000
LAST_TS_IDS_LIMIT = 100
# Cookie 连续失效阈值后停止
COOKIE_FAIL_EXIT_THRESHOLD = 3
# ===== 动态类型过滤 =====
ALLOWED_DYNAMIC_TYPES = {"", "MAJOR_TYPE_OPUS", "MAJOR_TYPE_ARCHIVE", "MAJOR_TYPE_ARTICLE", "MAJOR_TYPE_DRAW"}
ALLOWED_TOP_LEVEL_TYPES = {"DYNAMIC_TYPE_WORD", "DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_AV", "DYNAMIC_TYPE_ARTICLE", "DYNAMIC_TYPE_FORWARD"}
ALLOW_FORWARD_DYNAMIC = True
# ================= 全局运行标识 =================
IS_RUNNING = True
# 网络层：连接池设为 2，减少网络缓冲占用的物理内存
REQ_SESSION = requests.Session()
_adapter = HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=3)
REQ_SESSION.mount('http://', _adapter)
REQ_SESSION.mount('https://', _adapter)
REQ_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive"
})
notify_queue = queue.Queue(maxsize=100)
_last_notify_time = {}
WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}
mixinKeyEncTab = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]
# ================== 统一状态管理 ==================
@dataclass
class MonitorState:
    burst_end_time: float = 0.0
    last_burst_trigger_time: float = 0.0
    burst_chain_count: int = 0
    consecutive_failures: int = 0
    consecutive_cookie_failures: int = 0
    last_new_dynamic_time: float = 0.0
    consecutive_no_update_rounds: int = 0
    last_state_save: float = field(default_factory=time.time)
    last_checkin_date: str = ""  # 记录上一次发早安打卡的日期 (YYYY-MM-DD)
    def trigger_burst(self, duration: float = BURST_MODE_DURATION):
        now = time.time()
        if now - self.last_burst_trigger_time < BURST_COOLDOWN:
            if now < self.burst_end_time and self.burst_chain_count < BURST_MAX_CHAIN:
                self.burst_end_time = max(self.burst_end_time, now + duration)
                self.burst_chain_count += 1
                logging.info(f"🚀 爆发续期，chain={self.burst_chain_count}, until={int(self.burst_end_time)}")
            return
        self.burst_end_time = now + duration
        self.last_burst_trigger_time = now
        self.burst_chain_count = 1
        logging.info(f"🚀 进入智能爆发模式 {duration}s, chain={self.burst_chain_count}")
    def exit_burst(self, reason: str = ""):
        self.burst_end_time = 0.0
        self.burst_chain_count = 0
        if reason:
            logging.info(f"退出爆发模式: {reason}")
    def is_in_burst(self) -> bool:
        return time.time() < self.burst_end_time
    def is_morning_burst_window(self, now_dt: Optional[datetime.datetime] = None) -> bool:
        if now_dt is None:
            try:
                now_dt = datetime.datetime.now(ZoneInfo(RUN_TZ))
            except Exception:
                now_dt = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        if now_dt.weekday() not in RUN_WEEKDAYS:
            return False
        current = now_dt.hour * 60 + now_dt.minute
        start = MORNING_BURST_START_HOUR * 60 + MORNING_BURST_START_MINUTE
        end = MORNING_BURST_END_HOUR * 60 + MORNING_BURST_END_MINUTE
        return start <= current < end
STATE = MonitorState()
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
    root.setLevel(logging.INFO)
    root.propagate = False
    logging.info("=" * 60)
    logging.info("B站监控系统启动 (关注流防风控 - 128M极限内存优化版)")
    logging.info("=" * 60)
def send_failure_notification(title, message):
    global _last_notify_time
    if len(_last_notify_time) > 200:
        _last_notify_time.clear()
    key = f"{title}:{message[:100]}"
    if time.time() - _last_notify_time.get(key, 0) >= 600:
        _last_notify_time[key] = time.time()
        safe_enqueue_notify(title, [{"user": "系统", "message": message}], "system")
def safe_enqueue_notify(title, items, notify_type="dynamic"):
    try:
        notify_queue.put_nowait({"title": title, "items": items, "notify_type": notify_type})
        return True
    except queue.Full:
        return False
def notify_worker():
    while IS_RUNNING:
        try:
            task = notify_queue.get(timeout=1)
            title = task.get("title")
            items = task.get("items")
            ntype = task.get("notify_type")
            
            if ntype == "dynamic":
                logging.info(f"[排队发送] 正在推送新动态: {items[0].get('link', '')}")
            elif ntype == "system":
                logging.info(f"[排队发送] 正在推送系统通知: {title}")
            ok = notifier.send_webhook_notification(title, items, notify_type=ntype)
            if not ok and ntype != "system":
                logging.warning(f"[发送失败] 类型: {ntype}")
            time.sleep(2.5)
        except queue.Empty:
            continue
        except Exception as e:
            logging.error(f"推送消费失败: {repr(e)}")
def safe_request(url, params, retries=5):
    """核心请求函数。Cookie 已决定退出时直接短路，不再发网络请求。"""
    global IS_RUNNING
    if not IS_RUNNING and STATE.consecutive_cookie_failures >= COOKIE_FAIL_EXIT_THRESHOLD:
        return {"code": -101}
    base_delay = 3
    for i in range(retries):
        try:
            r = REQ_SESSION.get(url, params=params, timeout=12)
            try:
                data = r.json()
            except Exception:
                data = {"code": -500}
            code = data.get("code")
            # Cookie 失效处理
            if code == -101:
                STATE.consecutive_cookie_failures += 1
                logging.error(f"❌ B站 Cookie 已失效！连续失败 {STATE.consecutive_cookie_failures}/{COOKIE_FAIL_EXIT_THRESHOLD}")
                send_failure_notification(
                    "❌ B站 Cookie 失效预警",
                    f"Cookie 验证失败（连续第 {STATE.consecutive_cookie_failures} 次）。\n"
                    f"请立即重新获取并覆盖 bili_cookie.txt。\n"
                    f"达到 {COOKIE_FAIL_EXIT_THRESHOLD} 次后程序将自动停止。"
                )
                if STATE.consecutive_cookie_failures >= COOKIE_FAIL_EXIT_THRESHOLD:
                    logging.critical("🛑 Cookie 连续失效达到阈值，停止监控，进入人工干预模式。")
                    IS_RUNNING = False
                return data
            else:
                if STATE.consecutive_cookie_failures > 0:
                    STATE.consecutive_cookie_failures = 0
            # 风控状态码：强制刷新 WBI
            if code in (-799, -352, -509, -412):
                wait = base_delay * (2 ** i) + random.uniform(2.5, 6)
                logging.warning(f"⚠️ 触发风控 {code}，自动避让等待 {wait:.1f}s，并强制刷新 WBI")
                force_update_wbi_keys()
                send_failure_notification(
                    "🚨 B站风控安全预警",
                    f"服务器 IP 疑似被 B 站安全网关风控锁定。\n"
                    f"- 异常状态码: **{code}**\n"
                    f"- 自动避让延迟: **{wait:.1f} 秒**"
                )
                time.sleep(wait)
                continue
            if code != 0 and i < retries - 1:
                wait = base_delay * (2 ** i) + random.uniform(0.8, 2.5)
                logging.warning(f"[请求重试] url={url} code={code} wait={wait:.1f}s")
                time.sleep(wait)
                continue
            return data
        except Exception:
            time.sleep(base_delay * (2 ** i) + random.uniform(0.8, 2.5))
    logging.error(f"请求最终失败: {url}")
    send_failure_notification("❌ B站 API 请求最终失败", f"接口 {url} 连续重试 {retries} 次均失败。")
    return {"code": -500}
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
    now = time.time()
    try:
        china_now = datetime.datetime.now(ZoneInfo(RUN_TZ))
    except Exception:
        china_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    # 1. 每天 9:20–10:20 强制高频爆发
    if STATE.is_morning_burst_window(china_now):
        return random.uniform(MORNING_BURST_INTERVAL_MIN, MORNING_BURST_INTERVAL_MAX)
    # 2. 连续失败降速
    if STATE.consecutive_failures >= FAILURE_SLOWDOWN_THRESHOLD:
        return random.uniform(FAILURE_SLOW_INTERVAL_MIN, FAILURE_SLOW_INTERVAL_MAX)
    # 3. 智能爆发中
    if STATE.is_in_burst():
        return random.uniform(BURST_INTERVAL_MIN, BURST_INTERVAL_MAX)
    # 4. 长时间无更新降速
    if STATE.consecutive_no_update_rounds >= NO_UPDATE_SLOWDOWN_THRESHOLD_2:
        return random.uniform(NO_UPDATE_INTERVAL_2_MIN, NO_UPDATE_INTERVAL_2_MAX)
    if STATE.consecutive_no_update_rounds >= NO_UPDATE_SLOWDOWN_THRESHOLD_1:
        return random.uniform(NO_UPDATE_INTERVAL_1_MIN, NO_UPDATE_INTERVAL_1_MAX)
    # 5. 空闲模式
    if STATE.last_new_dynamic_time > 0 and now - STATE.last_new_dynamic_time >= IDLE_MODE_THRESHOLD:
        return random.uniform(IDLE_INTERVAL_MIN, IDLE_INTERVAL_MAX)
    # 6. 正常间隔
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
        "feed": {
            "last_ts": 0,
            "last_ts_ids": [],
            "baseline": "",
            "offset": "",
            "recent_pushed_ids": []
        }
    }
    if os.path.exists(DYNAMIC_STATE_FILE):
        try:
            with open(DYNAMIC_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            bak = DYNAMIC_STATE_FILE + ".bak"
            if os.path.exists(bak):
                try:
                    with open(bak, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    pass
            return default_state
    return default_state
def save_dynamic_state(state):
    try:
        feed = state.setdefault("feed", {})
        feed["last_ts_ids"] = list(feed.get("last_ts_ids", []) or [])[:LAST_TS_IDS_LIMIT]
        feed["recent_pushed_ids"] = list(feed.get("recent_pushed_ids", []) or [])[:RECENT_PUSHED_IDS_LIMIT]
        atomic_write_json(DYNAMIC_STATE_FILE, state)
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
    if dyn_id in recent:
        recent.remove(dyn_id)
    recent.insert(0, dyn_id)
    feed["recent_pushed_ids"] = recent[:RECENT_PUSHED_IDS_LIMIT]
def is_recent_pushed(state, dyn_id):
    feed = state.setdefault("feed", {})
    recent = feed.get("recent_pushed_ids", []) or []
    return dyn_id in recent
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
def is_new_dynamic_candidate(feed_state, dyn_id, pub_ts, now_ts):
    last_ts = int(feed_state.get("last_ts", 0) or 0)
    last_ts_ids = set(feed_state.get("last_ts_ids", []) or [])

    # 比当前游标新的，一律认为是新动态（优先保证不漏）
    if pub_ts > last_ts:
        return True
    if pub_ts == last_ts and dyn_id not in last_ts_ids:
        return True

    # 只有比游标旧的，才用时间窗口兜底过滤
    if now_ts - pub_ts > DYNAMIC_NEW_WINDOW:
        return False
    return False
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
    following = []
    pn = 1
    ps = 50
    while IS_RUNNING:
        data = safe_request("https://api.bilibili.com/x/relation/followings", {
            "vmid": uid, "pn": pn, "ps": ps, "order": "desc", "order_type": "attention"
        })
        if data.get("code") != 0:
            break
        items = data.get("data", {}).get("list", [])
        if not items:
            break
        for item in items:
            mid = item.get("mid")
            if mid:
                following.append(str(mid))
        if len(items) < ps:
            break
        pn += 1
        time.sleep(random.uniform(0.6, 1.2))
    return following
def init_feed_state(target_uids):
    state = load_dynamic_state()
    seen_dynamic_ids = init_seen_cache()
    if not IS_RUNNING:
        return seen_dynamic_ids, state
    try:
        max_ts = int(state.get("feed", {}).get("last_ts", 0) or 0)
        max_ts_ids = set(state.get("feed", {}).get("last_ts_ids", []) or [])
        offset = ""
        baseline = state.get("feed", {}).get("baseline", "")
        for page_idx in range(FEED_INIT_PAGES):
            if not IS_RUNNING:
                break
            data = fetch_following_feed_retry(offset=offset)
            if data.get("code") != 0:
                logging.warning(f"关注流初始化第 {page_idx + 1} 页失败 code={data.get('code')}")
                break
            feed = data.get("data", {}) or {}
            items = feed.get("items") or []
            if page_idx == 0:
                baseline = feed.get("update_baseline", "") or baseline
            for item in items:
                if not isinstance(item, dict):
                    continue
                dyn_id = item.get("id_str")
                if dyn_id:
                    add_seen_cache(seen_dynamic_ids, dyn_id, MAX_SEEN_DYNAMIC_IDS)
                author = item.get("modules", {}).get("module_author", {}) or {}
                author_mid = str(author.get("mid", ""))
                pub_ts = int(author.get("pub_ts", 0) or 0)
                if author_mid in target_uids:
                    if pub_ts > max_ts:
                        max_ts = pub_ts
                        max_ts_ids = {dyn_id} if dyn_id else set()
                    elif pub_ts == max_ts and dyn_id:
                        max_ts_ids.add(dyn_id)
            offset = feed.get("offset", "")
            if not offset or not items:
                break
            time.sleep(random.uniform(0.4, 0.8))
        state["feed"]["baseline"] = baseline
        state["feed"]["offset"] = offset
        state["feed"]["last_ts"] = max_ts
        state["feed"]["last_ts_ids"] = list(max_ts_ids)[:LAST_TS_IDS_LIMIT]
        if "recent_pushed_ids" not in state["feed"]:
            state["feed"]["recent_pushed_ids"] = []
        save_dynamic_state(state)
        STATE.last_new_dynamic_time = time.time()
        logging.info(f"关注流初始化完成 baseline={baseline} last_ts={max_ts}")
    except Exception as e:
        logging.error(f"关注流初始化异常: {repr(e)}")
    return seen_dynamic_ids, state
def process_feed_items(items, target_uids, seen_dynamic_ids, state, now_ts):
    has_new = False
    feed_state = state.setdefault("feed", {
        "last_ts": 0,
        "last_ts_ids": [],
        "baseline": "",
        "offset": "",
        "recent_pushed_ids": []
    })
    candidate_items = {}
    new_items = set()
    for item in items:
        try:
            if not isinstance(item, dict):
                continue
            dyn_id = item.get("id_str")
            if not dyn_id:
                continue
            add_seen_cache(seen_dynamic_ids, dyn_id, MAX_SEEN_DYNAMIC_IDS)
            author = item.get("modules", {}).get("module_author", {}) or {}
            author_mid = str(author.get("mid", ""))
            pub_ts = int(author.get("pub_ts", 0) or 0)
            if author_mid not in target_uids:
                continue
            if not is_allowed_dynamic(item):
                continue
            if is_recent_pushed(state, dyn_id):
                update_last_ts_state(feed_state, dyn_id, pub_ts)
                continue
            if is_new_dynamic_candidate(feed_state, dyn_id, pub_ts, now_ts):
                new_items.add(dyn_id)
                candidate_items[dyn_id] = item
        except Exception:
            pass

    # ===== 优化1：按发布时间从旧到新排序后再推送，避免游标跳跃 =====
    candidates = []
    for dyn_id in new_items:
        item = candidate_items.get(dyn_id)
        if item:
            author = item.get("modules", {}).get("module_author", {}) or {}
            pub_ts = int(author.get("pub_ts", 0) or 0)
            candidates.append((pub_ts, dyn_id, item))
    candidates.sort(key=lambda x: x[0])  # 从旧到新

    pushed_ids = set()
    for pub_ts, dyn_id, item in candidates:
        if dyn_id in pushed_ids:
            continue
        try:
            push_data = format_dynamic_message(item)
            ok = safe_enqueue_notify(f"{push_data.get('user', '未知UP')} 发布了新动态", [push_data], "dynamic")
            if ok:
                pushed_ids.add(dyn_id)
                add_recent_pushed_id(state, dyn_id)
                update_last_ts_state(feed_state, dyn_id, pub_ts)
                has_new = True
        except Exception as e:
            logging.error(f"动态推送异常 dyn_id={dyn_id} err={repr(e)}")
    if has_new:
        STATE.last_new_dynamic_time = time.time()
        STATE.consecutive_failures = 0
        STATE.consecutive_no_update_rounds = 0
        STATE.trigger_burst()
    return has_new
def scan_following_feed(target_uids, seen_dynamic_ids, state, now_ts):
    if not IS_RUNNING:
        return False
    feed_state = state.setdefault("feed", {
        "last_ts": 0,
        "last_ts_ids": [],
        "baseline": "",
        "offset": "",
        "recent_pushed_ids": []
    })
    baseline = feed_state.get("baseline", "")
    old_baseline = baseline
    update_data = check_feed_update(baseline)
    direct_fallback = False
    if update_data.get("code") != 0:
        STATE.consecutive_failures += 1
        direct_fallback = True
        if STATE.consecutive_failures >= FAILURE_EXIT_BURST:
            STATE.exit_burst("update_failed")
    else:
        update_num = update_data.get("data", {}).get("update_num", 0)
        STATE.consecutive_failures = 0
        if update_num <= 0:
            STATE.consecutive_no_update_rounds += 1
            return False
        STATE.consecutive_no_update_rounds = 0
    has_new = False
    offset = ""
    page_count = 0
    candidate_baseline = baseline
    completed = True
    any_success_page = False
    while page_count < FEED_FETCH_MAX_PAGES and IS_RUNNING:
        data = fetch_following_feed_retry(offset=offset)
        if data.get("code") != 0:
            STATE.consecutive_failures += 1
            completed = False
            if STATE.consecutive_failures >= FAILURE_EXIT_BURST:
                STATE.exit_burst("feed_page_failed")
            break
        STATE.consecutive_failures = 0
        any_success_page = True
        feed = data.get("data", {}) or {}
        items = feed.get("items") or []
        if not items:
            break
        if page_count == 0:
            first_page_baseline = feed.get("update_baseline", "") or (items[0].get("id_str", "") if items else "")
            if first_page_baseline:
                candidate_baseline = first_page_baseline
        page_has_new = process_feed_items(items, target_uids, seen_dynamic_ids, state, now_ts)
        if page_has_new:
            has_new = True
        reached_old = False
        if old_baseline:
            for item in items:
                if isinstance(item, dict) and item.get("id_str") == old_baseline:
                    reached_old = True
                    break
        offset = feed.get("offset", "")
        page_count += 1

        # ===== 优化2：只要本页还有新动态，就继续翻页，直到页数上限 =====
        if not offset:
            break
        if reached_old and not page_has_new:
            break
        # 有新动态时继续翻页
        time.sleep(random.uniform(0.4, 0.8))
    if not has_new and not direct_fallback and IS_RUNNING:
        try:
            time.sleep(1.0)
            retry_data = fetch_following_feed_retry(offset="")
            if retry_data.get("code") == 0:
                retry_items = (retry_data.get("data", {}) or {}).get("items") or []
                if retry_items:
                    retry_has_new = process_feed_items(
                        retry_items, target_uids, seen_dynamic_ids, state, int(time.time())
                    )
                    if retry_has_new:
                        has_new = True
        except Exception:
            pass
    if completed and any_success_page:
        if candidate_baseline:
            feed_state["baseline"] = candidate_baseline
        feed_state["offset"] = offset
    return has_new
def start_monitoring():
    global IS_RUNNING
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    last_hb = 0
    last_following_refresh = 0
    last_d_check = 0
    # 1. 模拟设备指纹
    activate_session_cookies()
    # 2. 载入 Cookie
    if not load_cookies_into_session():
        logging.critical("❌ Cookie 文件不存在或为空，程序退出")
        return
    # 3. 更新 WBI
    update_wbi_keys()
    # ★ 关键：Cookie 已失效则立刻退出，不再继续初始化
    if not IS_RUNNING:
        logging.critical("因 Cookie 失效，跳过后续初始化，程序退出")
        return
    # 4. 获取关注列表
    following_list = load_following_cache() or get_following_list(SOURCE_UID) or FALLBACK_DYNAMIC_UIDS[:]
    if not IS_RUNNING:
        logging.critical("因 Cookie 失效，跳过关注流初始化，程序退出")
        return
    following_list = [str(uid) for uid in following_list]
    if str(SOURCE_UID) not in following_list:
        following_list.append(str(SOURCE_UID))
    save_following_cache(following_list)
    target_uids = set(following_list)
    logging.info(f"监控 {len(target_uids)} 个 UID（关注流模式）")
    # 5. 初始化关注流状态
    seen_dynamic_ids, state = init_feed_state(target_uids)
    if not IS_RUNNING:
        save_dynamic_state(state)
        logging.critical("因 Cookie 失效，初始化中止，程序退出")
        return
    if STATE.last_new_dynamic_time == 0:
        STATE.last_new_dynamic_time = time.time()
    # 开启推送消费线程
    threading.Thread(target=notify_worker, daemon=True).start()
    logging.info(f"✅ 系统初始化完成，开始在工作日 {RUN_START_HOUR}:{RUN_START_MINUTE:02d}-{RUN_END_HOUR}:00 运行监听")
    logging.info(f"   每天 {MORNING_BURST_START_HOUR}:{MORNING_BURST_START_MINUTE:02d}-"
                 f"{MORNING_BURST_END_HOUR}:{MORNING_BURST_END_MINUTE:02d} 强制高频爆发（3~5秒）")
    while IS_RUNNING:
        try:
            now = time.time()
            try:
                china_now = datetime.datetime.now(ZoneInfo(RUN_TZ))
            except Exception:
                china_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
            if not is_in_monitor_window(china_now):
                if now - last_hb >= HEARTBEAT_INTERVAL:
                    logging.info(
                        f"⏸ 当前不在监听时段，中国时间={china_now.strftime('%Y-%m-%d %H:%M:%S')}，"
                        f"仅工作日 {RUN_START_HOUR}:{RUN_START_MINUTE:02d}-{RUN_END_HOUR}:00 运行"
                    )
                    last_hb = now
                time.sleep(OFF_HOURS_SLEEP)
                continue
            # 每日 09:20 首次进入监控窗口时自动向钉钉发送打卡通知
            today_str = china_now.strftime("%Y-%m-%d")
            if STATE.last_checkin_date != today_str:
                STATE.last_checkin_date = today_str
                safe_enqueue_notify(
                    "☀️ B站动态监控系统打卡上班（发布了新动态）",
                    [{"user": "系统雷达", "message": f"今天({today_str})工作日打卡成功！B站动态监控已锁定 {len(target_uids)} 个目标 UP 主，开始隐形巡航！"}],
                    "system"
                )
            if now - last_d_check >= get_scan_interval():
                try:
                    state_updated = scan_following_feed(target_uids, seen_dynamic_ids, state, int(now))
                    if state_updated or now - STATE.last_state_save > STATE_SAVE_INTERVAL:
                        save_dynamic_state(state)
                        STATE.last_state_save = now
                except Exception as e:
                    logging.error(f"关注流扫描异常: {repr(e)}")
                last_d_check = now
            if now - last_following_refresh >= FOLLOWING_REFRESH_INTERVAL:
                try:
                    new_list = get_following_list(SOURCE_UID)
                    if new_list and IS_RUNNING:
                        new_list = [str(uid) for uid in new_list]
                        if str(SOURCE_UID) not in new_list:
                            new_list.append(str(SOURCE_UID))
                        old_set = set(following_list)
                        new_set = set(new_list)
                        if new_set != old_set:
                            following_list = new_list
                            target_uids = set(following_list)
                            save_following_cache(following_list)
                            logging.info(f"过滤UID已刷新，当前共 {len(target_uids)} 个")
                except Exception:
                    pass
                last_following_refresh = now
            if now - last_hb >= HEARTBEAT_INTERVAL:
                interval = get_scan_interval()
                logging.info(
                    f"💓 心跳正常 interval={interval:.2f}s "
                    f"burst={'on' if STATE.is_in_burst() else 'off'} "
                    f"morning_burst={'on' if STATE.is_morning_burst_window(china_now) else 'off'} "
                    f"fail={STATE.consecutive_failures} no_update={STATE.consecutive_no_update_rounds} "
                    f"cookie_fail={STATE.consecutive_cookie_failures}"
                )
                last_hb = now
            time.sleep(0.5)
        except Exception:
            if IS_RUNNING:
                logging.error("主循环异常")
                time.sleep(8)
            else:
                break
    save_dynamic_state(state)
    logging.info("💾 内存读取进度已安全落盘，主程序完美退出。")
if __name__ == "__main__":
    init_logging()
    start_monitoring()
