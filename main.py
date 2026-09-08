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
import uuid
import shutil
from dataclasses import dataclass, field
from collections import deque

try:
    from zoneinfo import ZoneInfo
except ImportError:
    import pytz
    def ZoneInfo(tz_str):
        return pytz.timezone(tz_str)

import notifier

# =========================================================
# 核心配置
# =========================================================
VIDEO_CHECK_INTERVAL = 21600
HEARTBEAT_INTERVAL = 3600
FOLLOWING_REFRESH_INTERVAL = 3600
SOURCE_UID = 3707011984264075
FALLBACK_DYNAMIC_UIDS = [
    "3546905852250875",
    "3546961271589219",
    "3546610447419885",
    "285340365",
    "3707011984264075",
]
MAX_MONITOR_UIDS = 20

LOG_FILE = "bili_monitor.log"
DYNAMIC_STATE_FILE = "dynamic_state.json"
FOLLOWING_CACHE_FILE = "following_cache.json"

# 全天运行测试模式
RUN_TZ = "Asia/Shanghai"
RUN_WEEKDAYS = {0, 1, 2, 3, 4}
ALWAYS_RUN = True
RUN_START_HOUR = 0
RUN_START_MINUTE = 0
RUN_END_HOUR = 24
OFF_HOURS_SLEEP = 20

# =========================================================
# App 关注流模拟参数
# =========================================================
NORMAL_INTERVAL_MIN = 20.0
NORMAL_INTERVAL_MAX = 35.0

# 发现新动态后，追加一次“整体刷新确认”，仍然是 feed/nav
VERIFY_DELAY_MIN = 1.5
VERIFY_DELAY_MAX = 3.0

# 每 5 分钟整体深扫一次；不按 UID 拆分
DEEP_SCAN_INTERVAL = 300
DEEP_SCAN_MAX_PAGES = 20
DEEP_SCAN_STOP_STABLE_PAGES = 2

# 当发现新动态时，二次整体刷新最多检查更多页面，仍然不拆 UID
VERIFY_MAX_PAGES = 6

# 延迟动态保护窗口
DYNAMIC_NEW_WINDOW = 6 * 3600
RECENT_DISCOVERY_WINDOW = 15 * 60

# 状态
STATE_SAVE_INTERVAL = 60
RECENT_SNAPSHOT_LIMIT = 400
SEEN_DYNAMIC_LIMIT = 12000
RECENT_PUSHED_IDS_LIMIT = 12000
OUTBOX_MAX = 500

# Webhook
NOTIFY_QUEUE_MAXSIZE = 100
NOTIFY_SEND_RETRIES = 3
NOTIFY_SEND_DELAY = 2.0
NOTIFY_RETRY_BASE = 60
NOTIFY_RETRY_MAX = 1800

# API
REQUEST_TIMEOUT = 12
REQUEST_RETRIES = 3
WBI_REFRESH_INTERVAL = 21600

# 15:30 报告
HEALTH_REPORT_HOUR = 15
HEALTH_REPORT_MINUTE = 30

# 动态类型过滤
ALLOWED_DYNAMIC_TYPES = {
    "", "MAJOR_TYPE_OPUS", "MAJOR_TYPE_ARCHIVE", "MAJOR_TYPE_ARTICLE",
    "MAJOR_TYPE_DRAW", "MAJOR_TYPE_COMMON", "MAJOR_TYPE_LIVE"
}
ALLOWED_TOP_LEVEL_TYPES = {
    "DYNAMIC_TYPE_WORD", "DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_AV",
    "DYNAMIC_TYPE_ARTICLE", "DYNAMIC_TYPE_FORWARD", "DYNAMIC_TYPE_LIVE"
}
ALLOW_FORWARD_DYNAMIC = True

# =========================================================
# 全局运行对象
# =========================================================
IS_RUNNING = True
STATE_LOCK = threading.RLock()
ACTIVE_STATE = None
PENDING_PUSH_IDS = set()
notify_queue = queue.Queue(maxsize=NOTIFY_QUEUE_MAXSIZE)

REQ_SESSION = requests.Session()
_adapter = HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=0)
REQ_SESSION.mount("http://", _adapter)
REQ_SESSION.mount("https://", _adapter)
REQ_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                  "Chrome/120.0.0.0 Mobile Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
})

_last_notify_time = {}
WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}
mixinKeyEncTab = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52
]


@dataclass
class MonitorState:
    consecutive_failures: int = 0
    consecutive_cookie_failures: int = 0
    consecutive_no_update_rounds: int = 0
    last_new_dynamic_time: float = 0.0
    last_state_save: float = field(default_factory=time.time)
    last_checkin_date: str = ""
    last_report_date: str = ""
    last_deep_scan: float = 0.0
    refresh_seq: int = 0


STATE = MonitorState()


# =========================================================
# 通用工具
# =========================================================
def signal_handler(signum, frame):
    global IS_RUNNING
    logging.info("🛑 收到停止信号，准备安全退出并保存状态...")
    IS_RUNNING = False


def atomic_write_json(path, data):
    backup_path = path + ".bak"
    tmp_path = path + ".tmp"
    if os.path.exists(path):
        try:
            shutil.copy2(path, backup_path)
        except Exception:
            pass
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


def now_cn():
    try:
        return datetime.datetime.now(ZoneInfo(RUN_TZ))
    except Exception:
        return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def is_in_monitor_window(dt=None):
    dt = dt or now_cn()
    if dt.weekday() not in RUN_WEEKDAYS:
        return False
    minute = dt.hour * 60 + dt.minute
    start = RUN_START_HOUR * 60 + RUN_START_MINUTE
    end = RUN_END_HOUR * 60
    return start <= minute < end


def normalize_text(text):
    if not text:
        return ""
    text = str(text).replace("\r", "\n")
    return "\n".join(x.strip() for x in text.split("\n") if x.strip()).strip()


def cut_text(text, max_len=900):
    text = normalize_text(text)
    if len(text) <= max_len:
        return text
    return text[:max_len - 3].rstrip() + "..."


def ts_to_str(ts):
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "未知时间"


def mark_state_dirty(state):
    state.setdefault("_meta", {})["dirty"] = True


def random_main_interval():
    if STATE.consecutive_failures >= 2:
        return random.uniform(35.0, 60.0)
    return random.uniform(NORMAL_INTERVAL_MIN, NORMAL_INTERVAL_MAX)


# =========================================================
# Cookie / Logging / WBI
# =========================================================
def activate_session_cookies():
    try:
        resp = REQ_SESSION.get("https://www.bilibili.com/", timeout=10)
        resp.close()
        uuid_sec = str(uuid.uuid4())
        time_sec = str(int(time.time() * 1000 % 1e5)).ljust(5, "0")
        _uuid = f"{uuid_sec}{time_sec}infoc"
        REQ_SESSION.cookies.set("_uuid", _uuid, domain=".bilibili.com")
        REQ_SESSION.cookies.set("CURRENT_FNVAL", "4048", domain=".bilibili.com")
        REQ_SESSION.cookies.set("blackside_state", "1", domain=".bilibili.com")
        logging.info("✅ B站首页会话 Cookie 激活完成")
        return True
    except Exception as e:
        logging.warning(f"⚠️ 首页会话激活失败: {e}")
        return False


def load_cookies_into_session():
    try:
        if not os.path.exists("bili_cookie.txt"):
            logging.error("❌ 未找到 bili_cookie.txt")
            return False
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie_str = f.read().strip()
        if not cookie_str:
            return False
        for item in cookie_str.split(";"):
            item = item.strip()
            if not item or "=" not in item:
                continue
            k, v = item.split("=", 1)
            REQ_SESSION.cookies.set(k.strip(), v.strip(), domain=".bilibili.com")
        logging.info("✅ 登录 Cookie 已加载")
        return True
    except Exception as e:
        logging.error(f"❌ 加载 Cookie 异常: {e}")
        return False


class DingTalkFilter(logging.Filter):
    def filter(self, record):
        return "310000" not in record.getMessage()


def init_logging():
    root = logging.getLogger()
    if root.hasHandlers():
        root.handlers.clear()
    formatter = logging.Formatter("[BILI] %(asctime)s [%(levelname)s] %(message)s")
    filt = DingTalkFilter()
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=2,
        encoding="utf-8", delay=True
    )
    handler.setFormatter(formatter)
    handler.addFilter(filt)
    root.addHandler(handler)
    if sys.stdout.isatty():
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        stream.addFilter(filt)
        root.addHandler(stream)
    root.setLevel(logging.INFO)
    root.propagate = False
    logging.info("=" * 70)
    logging.info("B站关注动态监控启动（App关注流模拟最终版）")
    logging.info("=" * 70)


def force_update_wbi_keys():
    try:
        r = REQ_SESSION.get("https://api.bilibili.com/x/web-interface/nav", timeout=8)
        data = r.json()
        r.close()
        if data.get("code") not in (0, -101):
            return False
        img = data.get("data", {}).get("wbi_img", {}) or {}
        img_url = img.get("img_url", "")
        sub_url = img.get("sub_url", "")
        if not img_url or not sub_url:
            return False
        WBI_KEYS["img_key"] = img_url.rsplit("/", 1)[1].split(".")[0]
        WBI_KEYS["sub_key"] = sub_url.rsplit("/", 1)[1].split(".")[0]
        WBI_KEYS["last_update"] = time.time()
        logging.info("✅ WBI 密钥刷新成功")
        return True
    except Exception as e:
        logging.warning(f"WBI 刷新失败: {e}")
        return False


def update_wbi_keys():
    if WBI_KEYS["img_key"] and time.time() - WBI_KEYS["last_update"] < WBI_REFRESH_INTERVAL:
        return True
    return force_update_wbi_keys()


def enc_wbi(params, img_key, sub_key):
    mixin_key = "".join((img_key + sub_key)[i] for i in mixinKeyEncTab)[:32]
    params = dict(params)
    params["wts"] = int(time.time())
    params = dict(sorted(params.items()))
    filtered = {}
    for k, v in params.items():
        v = str(v)
        for c in "!'()*":
            v = v.replace(c, "")
        filtered[k] = v
    query = urllib.parse.urlencode(filtered, quote_via=urllib.parse.quote)
    filtered["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return filtered


# =========================================================
# API 请求 / 风控
# =========================================================
def notify_system_once(title, message):
    key = f"{title}:{message[:120]}"
    now = time.time()
    if now - _last_notify_time.get(key, 0) < 600:
        return
    _last_notify_time[key] = now
    safe_enqueue_notify(title, [{"user": "系统", "message": message}], "system")


def safe_request(url, params=None, retries=REQUEST_RETRIES):
    params = params or {}
    last = {"code": -500, "message": "unknown"}
    for i in range(retries):
        try:
            resp = REQ_SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
            try:
                data = resp.json()
            finally:
                resp.close()
            last = data
            code = data.get("code")

            if code == -101:
                STATE.consecutive_cookie_failures += 1
                logging.error(
                    f"❌ Cookie 验证失败 {STATE.consecutive_cookie_failures}/3"
                )
                notify_system_once(
                    "❌ B站 Cookie 失效预警",
                    "Cookie 验证失败，请检查 bili_cookie.txt。"
                )
                if STATE.consecutive_cookie_failures >= 3:
                    logging.critical("🛑 Cookie 连续失效，停止程序。")
                    globals()["IS_RUNNING"] = False
                return data

            STATE.consecutive_cookie_failures = 0

            if code in (-799, -352, -509, -412) or resp.status_code in (412, 429):
                wait = min(300.0, 15.0 * (2 ** i)) + random.uniform(3, 8)
                logging.warning(
                    f"⚠️ B站风控/限流 code={code} http={resp.status_code}，退避 {wait:.1f}s"
                )
                force_update_wbi_keys()
                notify_system_once(
                    "🚨 B站风控预警",
                    f"code={code}, http={resp.status_code}，已自动退避 {wait:.1f} 秒。"
                )
                time.sleep(wait)
                continue

            if code == 0:
                return data

            if i < retries - 1:
                wait = 3.0 * (2 ** i) + random.uniform(1, 3)
                logging.warning(f"[API重试] code={code} wait={wait:.1f}s url={url}")
                time.sleep(wait)
            else:
                return data

        except Exception as e:
            last = {"code": -500, "message": repr(e)}
            if i < retries - 1:
                wait = 3.0 * (2 ** i) + random.uniform(1, 3)
                logging.warning(f"[网络重试] {repr(e)} wait={wait:.1f}s")
                time.sleep(wait)
    logging.error(f"❌ 请求最终失败: {url}")
    notify_system_once("❌ B站 API 请求失败", f"接口连续失败: {url}")
    return last


def wbi_request(url, params):
    update_wbi_keys()
    if WBI_KEYS["img_key"] and WBI_KEYS["sub_key"]:
        try:
            return safe_request(url, enc_wbi(params, WBI_KEYS["img_key"], WBI_KEYS["sub_key"]))
        except Exception:
            pass
    return safe_request(url, params)


# =========================================================
# 状态持久化
# =========================================================
def default_state():
    return {
        "version": 5,
        "feed": {
            "baseline": "",
            "last_snapshot_ids": [],
            "recent_snapshot_history": [],
            "recent_pushed_ids": [],
            "push_retry_after": {},
            "outbox": {},
            "last_refresh_time": 0,
            "last_success_refresh": 0,
        },
        "uid_stats": {},
        "daily": {
            "date": "",
            "refreshes": 0,
            "deep_scans": 0,
            "items_seen": 0,
            "new_found": 0,
            "primary_found": 0,
            "verify_rounds": 0,
            "verify_recovered": 0,
            "deep_recovered": 0,
            "delayed_found": 0,
            "webhook_success": 0,
            "webhook_fail": 0,
            "api_fail": 0,
            "rate_limit": 0,
        },
        "_meta": {"dirty": False},
    }


def load_dynamic_state():
    if not os.path.exists(DYNAMIC_STATE_FILE):
        return default_state()
    try:
        with open(DYNAMIC_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise ValueError("state not dict")
    except Exception:
        try:
            with open(DYNAMIC_STATE_FILE + ".bak", "r", encoding="utf-8") as f:
                state = json.load(f)
            if not isinstance(state, dict):
                return default_state()
        except Exception:
            return default_state()

    base = default_state()
    for k, v in base.items():
        if k not in state:
            state[k] = v
    for k, v in base["feed"].items():
        state["feed"].setdefault(k, v)
    for k, v in base["daily"].items():
        state["daily"].setdefault(k, v)
    state.setdefault("uid_stats", {})
    return state


def save_dynamic_state(state):
    if not state:
        return
    with STATE_LOCK:
        feed = state.setdefault("feed", {})
        feed["last_snapshot_ids"] = list(dict.fromkeys(feed.get("last_snapshot_ids", [])))[:RECENT_SNAPSHOT_LIMIT]
        feed["recent_pushed_ids"] = list(dict.fromkeys(feed.get("recent_pushed_ids", [])))[:RECENT_PUSHED_IDS_LIMIT]
        history = feed.get("recent_snapshot_history", []) or []
        feed["recent_snapshot_history"] = history[-20:]
        retry = feed.get("push_retry_after", {}) or {}
        now = time.time()
        feed["push_retry_after"] = {k: v for k, v in retry.items() if float(v or 0) > now}

        outbox = feed.get("outbox", {}) or {}
        if len(outbox) > OUTBOX_MAX:
            ordered = sorted(outbox.items(), key=lambda kv: float((kv[1] or {}).get("created_at", 0) or 0))
            feed["outbox"] = dict(ordered[-OUTBOX_MAX:])

        for uid, info in list(state.get("uid_stats", {}).items()):
            if isinstance(info, dict):
                seen = info.get("seen_ids", []) or []
                info["seen_ids"] = list(dict.fromkeys(seen))[:500]
        atomic_write_json(DYNAMIC_STATE_FILE, state)
        state.setdefault("_meta", {})["dirty"] = False


def reset_daily_stats(state, date_str):
    daily = state.setdefault("daily", {})
    if daily.get("date") != date_str:
        for _uid, info in state.setdefault("uid_stats", {}).items():
            if isinstance(info, dict):
                info["daily_new"] = 0
                info["daily_delayed"] = 0
                info["daily_verify_recovered"] = 0
                info["daily_deep_recovered"] = 0
                info["max_delay_today"] = 0
        state["daily"] = {
            "date": date_str,
            "refreshes": 0,
            "deep_scans": 0,
            "items_seen": 0,
            "new_found": 0,
            "primary_found": 0,
            "verify_rounds": 0,
            "verify_recovered": 0,
            "deep_recovered": 0,
            "delayed_found": 0,
            "webhook_success": 0,
            "webhook_fail": 0,
            "api_fail": 0,
            "rate_limit": 0,
        }


# =========================================================
# 关注列表
# =========================================================
def load_following_cache():
    try:
        if not os.path.exists(FOLLOWING_CACHE_FILE):
            return []
        with open(FOLLOWING_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [str(x) for x in data] if isinstance(data, list) else []
    except Exception:
        return []


def save_following_cache(uids):
    try:
        atomic_write_json(FOLLOWING_CACHE_FILE, [str(x) for x in uids])
    except Exception as e:
        logging.warning(f"保存关注缓存失败: {e}")


def get_following_list(uid):
    result = []
    ps = 50
    max_pages = 100
    for pn in range(1, max_pages + 1):
        data = safe_request("https://api.bilibili.com/x/relation/followings", {
            "vmid": uid,
            "pn": pn,
            "ps": ps,
            "order": "desc",
            "order_type": "attention",
        })
        if data.get("code") != 0:
            logging.warning(f"关注列表第 {pn} 页失败 code={data.get('code')}，放弃本次覆盖")
            return None
        items = (data.get("data") or {}).get("list") or []
        for item in items:
            if isinstance(item, dict) and item.get("mid") is not None:
                result.append(str(item["mid"]))
        if len(items) < ps:
            break
        time.sleep(random.uniform(0.5, 1.0))
    result = list(dict.fromkeys(result))
    if not result:
        return None
    if len(result) > MAX_MONITOR_UIDS:
        logging.error(f"❌ 关注列表实际为 {len(result)} 个，超过 MAX_MONITOR_UIDS={MAX_MONITOR_UIDS}，本次拒绝覆盖")
        notify_system_once(
            "⚠️ 关注列表异常",
            f"检测到 {len(result)} 个关注目标，超过安全上限 {MAX_MONITOR_UIDS}，已保留旧列表。"
        )
        return None
    return result


# =========================================================
# 动态解析
# =========================================================
def is_allowed_dynamic(item):
    try:
        top_type = item.get("type", "")
        modules = item.get("modules", {}) or {}
        major_type = (modules.get("module_dynamic", {}) or {}).get("major", {}).get("type", "")
        if top_type == "DYNAMIC_TYPE_FORWARD":
            return ALLOW_FORWARD_DYNAMIC
        if top_type and top_type not in ALLOWED_TOP_LEVEL_TYPES:
            return False
        if major_type and major_type not in ALLOWED_DYNAMIC_TYPES:
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
                n.get("text", "") for n in nodes
                if isinstance(n, dict) and n.get("type") in (
                    "RICH_TEXT_NODE_TYPE_TEXT", "RICH_TEXT_NODE_TYPE_TOPIC",
                    "RICH_TEXT_NODE_TYPE_AT", "RICH_TEXT_NODE_TYPE_EMOJI",
                    "RICH_TEXT_NODE_TYPE_LOTTERY"
                )
            )
            text = normalize_text(text)
            if text:
                return text

        major = dyn.get("major") or {}
        t = major.get("type", "")
        if t == "MAJOR_TYPE_ARCHIVE":
            a = major.get("archive") or {}
            title = normalize_text(a.get("title", ""))
            desc_text = normalize_text(a.get("desc", ""))
            return f"【视频】{title}\n{desc_text}".strip()
        if t == "MAJOR_TYPE_ARTICLE":
            a = major.get("article") or {}
            title = normalize_text(a.get("title", ""))
            desc_text = normalize_text(a.get("desc", ""))
            return f"【专栏】{title}\n{desc_text}".strip()
        if t == "MAJOR_TYPE_OPUS":
            opus = major.get("opus") or {}
            title = normalize_text(opus.get("title", ""))
            summary = opus.get("summary") or {}
            nodes = summary.get("rich_text_nodes") or []
            text = normalize_text("".join(n.get("text", "") for n in nodes if isinstance(n, dict)))
            return f"【图文】{title}\n{text}".strip()
        if t == "MAJOR_TYPE_DRAW":
            return normalize_text(desc.get("text", "")) or "【图片动态】"
        if t == "MAJOR_TYPE_COMMON":
            common = major.get("common") or {}
            title = normalize_text(common.get("title", ""))
            desc_text = normalize_text(common.get("desc", ""))
            return f"【卡片】{title}\n{desc_text}".strip()
        if t == "MAJOR_TYPE_LIVE":
            live = major.get("live") or {}
            title = normalize_text(live.get("title", ""))
            desc_text = normalize_text(live.get("desc_second", ""))
            return f"【直播】{title}\n{desc_text}".strip()
        return normalize_text(desc.get("text", ""))
    except Exception:
        return ""


def collect_image_urls(obj, out=None, depth=0):
    """兼容 draw/opus/archive/article/forward 等结构，收集全部图片 URL。"""
    if out is None:
        out = []
    if obj is None or depth > 6:
        return out
    if isinstance(obj, str):
        low = obj.lower()
        if low.startswith(("http://", "https://")) and any(x in low for x in ("hdslb.com", "bfs/", ".jpg", ".png", ".jpeg", ".webp")):
            if obj not in out:
                out.append(obj)
        return out
    if isinstance(obj, dict):
        priority_keys = (
            "src", "url", "image", "image_url", "cover", "thumbnail", "pic", "picture",
            "origin_url", "source_url", "jump_url"
        )
        for k in priority_keys:
            v = obj.get(k)
            if isinstance(v, str):
                low = v.lower()
                if low.startswith(("http://", "https://")) and ("hdslb.com" in low or "bfs/" in low):
                    if v not in out:
                        out.append(v)
            elif isinstance(v, (dict, list)):
                collect_image_urls(v, out, depth + 1)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                collect_image_urls(v, out, depth + 1)
        return out
    if isinstance(obj, list):
        for v in obj:
            collect_image_urls(v, out, depth + 1)
    return out


def extract_images(item):
    urls = []
    try:
        modules = item.get("modules") or {}
        dyn = modules.get("module_dynamic") or {}
        major = dyn.get("major") or {}
        # 先按明确字段取，避免递归误把作者头像等当图片
        draw = major.get("draw") or {}
        for x in draw.get("items") or []:
            if isinstance(x, dict):
                u = x.get("src") or x.get("url")
                if u:
                    urls.append(u)
        opus = major.get("opus") or {}
        for x in opus.get("pics") or []:
            if isinstance(x, dict):
                u = x.get("url") or x.get("src")
                if u:
                    urls.append(u)
        for key in ("archive", "article"):
            section = major.get(key) or {}
            for k in ("cover", "covers"):
                v = section.get(k)
                if isinstance(v, str):
                    urls.append(v)
                elif isinstance(v, list):
                    for x in v:
                        if isinstance(x, str):
                            urls.append(x)
        # 兜底递归，但限制图片域名和常见字段
        collect_image_urls(major, urls)

        if item.get("type") == "DYNAMIC_TYPE_FORWARD":
            orig = item.get("orig")
            if isinstance(orig, dict):
                collect_image_urls(orig, urls)
    except Exception:
        pass

    clean = []
    for u in urls:
        if not isinstance(u, str):
            continue
        if u.startswith("//"):
            u = "https:" + u
        if u.startswith(("http://", "https://")) and u not in clean:
            clean.append(u)
    return clean[:12]


def format_dynamic_message(item):
    dyn_id = str(item.get("id_str") or "")
    author = item.get("modules", {}).get("module_author", {}) or {}
    name = author.get("name", "未知UP")
    uid = str(author.get("mid", ""))
    pub_ts = int(author.get("pub_ts", 0) or 0)
    text = cut_text(extract_dynamic_text(item), 900)

    if item.get("type") == "DYNAMIC_TYPE_FORWARD":
        orig = item.get("orig")
        if isinstance(orig, dict):
            orig_text = cut_text(extract_dynamic_text(orig), 350)
            if orig_text:
                text = f"{text}\n\n【转发原文】\n{orig_text}" if text else f"【转发原文】\n{orig_text}"
            orig_id = orig.get("id_str")
            if orig_id:
                text += f"\n\n原动态：https://t.bilibili.com/{orig_id}"

    if not text:
        text = "（该动态无可提取正文）"

    images = extract_images(item)
    return {
        "user": name,
        "uid": uid,
        "message": text,
        "time": ts_to_str(pub_ts),
        "link": f"https://t.bilibili.com/{dyn_id}",
        "cover": images[0] if images else "",
        "covers": images,
        "images": images,
        "image_count": len(images),
        "kind": "dynamic",
    }


# =========================================================
# UID 统计（仅统计整体关注流命中情况，不做单UID请求）
# =========================================================
def get_uid_stat(state, uid, name=""):
    uid = str(uid)
    root = state.setdefault("uid_stats", {})
    info = root.setdefault(uid, {})
    info.setdefault("name", name or uid)
    info.setdefault("daily_new", 0)
    info.setdefault("daily_delayed", 0)
    info.setdefault("daily_verify_recovered", 0)
    info.setdefault("daily_deep_recovered", 0)
    info.setdefault("total_seen", 0)
    info.setdefault("last_pub_ts", 0)
    info.setdefault("last_first_seen", 0)
    info.setdefault("last_global_seen", 0)
    info.setdefault("max_delay_today", 0)
    info.setdefault("seen_ids", [])
    return info


def remember_uid_id(uid_stat, dyn_id):
    ids = list(uid_stat.get("seen_ids", []) or [])
    if dyn_id in ids:
        ids.remove(dyn_id)
    ids.insert(0, dyn_id)
    uid_stat["seen_ids"] = ids[:500]


def uid_seen(uid_stat, dyn_id):
    return str(dyn_id) in set(uid_stat.get("seen_ids", []) or [])


# =========================================================
# Outbox / Webhook
# =========================================================
def add_recent_pushed(state, dyn_id):
    feed = state.setdefault("feed", {})
    ids = list(feed.get("recent_pushed_ids", []) or [])
    if dyn_id in ids:
        ids.remove(dyn_id)
    ids.insert(0, dyn_id)
    feed["recent_pushed_ids"] = ids[:RECENT_PUSHED_IDS_LIMIT]


def is_recent_pushed(state, dyn_id):
    return str(dyn_id) in set(state.setdefault("feed", {}).get("recent_pushed_ids", []) or [])


def safe_enqueue_notify(title, items, notify_type="dynamic", dyn_id="", uid="", pub_ts=0, first_seen=0, discovery_mode="primary"):
    dyn_id = str(dyn_id or "")
    if notify_type == "dynamic" and dyn_id:
        with STATE_LOCK:
            if dyn_id in PENDING_PUSH_IDS or is_recent_pushed(ACTIVE_STATE, dyn_id):
                return False
            outbox = ACTIVE_STATE.setdefault("feed", {}).setdefault("outbox", {})
            if dyn_id in outbox:
                return False
            if len(outbox) >= OUTBOX_MAX:
                logging.error("❌ Outbox 已满，拒绝继续丢进程内队列")
                notify_system_once("❌ Webhook队列已满", f"当前待发送超过 {OUTBOX_MAX} 条，暂停新增入队。")
                return False
            task = {
                "title": title,
                "items": items,
                "notify_type": notify_type,
                "dyn_id": dyn_id,
                "uid": str(uid or ""),
                "pub_ts": int(pub_ts or 0),
                "first_seen": int(first_seen or time.time()),
                "discovery_mode": discovery_mode,
                "created_at": time.time(),
                "attempt": 0,
                "next_attempt": 0,
            }
            outbox[dyn_id] = task
            PENDING_PUSH_IDS.add(dyn_id)
            mark_state_dirty(ACTIVE_STATE)
            # 关键可靠性：先落盘，再进内存队列。进程此刻崩溃也能从 outbox 恢复。
            save_dynamic_state(ACTIVE_STATE)
        try:
            notify_queue.put_nowait(task)
            return True
        except queue.Full:
            with STATE_LOCK:
                ACTIVE_STATE.get("feed", {}).get("outbox", {}).pop(dyn_id, None)
                PENDING_PUSH_IDS.discard(dyn_id)
                mark_state_dirty(ACTIVE_STATE)
                save_dynamic_state(ACTIVE_STATE)
            return False

    task = {
        "title": title,
        "items": items,
        "notify_type": notify_type,
        "dyn_id": dyn_id,
        "uid": str(uid or ""),
        "pub_ts": int(pub_ts or 0),
        "first_seen": int(first_seen or time.time()),
        "discovery_mode": discovery_mode,
        "created_at": time.time(),
        "attempt": 0,
        "next_attempt": 0,
    }
    try:
        notify_queue.put_nowait(task)
        return True
    except queue.Full:
        return False


def restore_outbox_to_queue(state):
    outbox = state.setdefault("feed", {}).setdefault("outbox", {})
    now = time.time()
    for dyn_id, task in list(outbox.items()):
        if not isinstance(task, dict):
            outbox.pop(dyn_id, None)
            continue
        next_attempt = float(task.get("next_attempt", 0) or 0)
        if next_attempt <= now:
            PENDING_PUSH_IDS.add(str(dyn_id))
            try:
                notify_queue.put_nowait(task)
            except queue.Full:
                break


def mark_sent(state, task):
    dyn_id = str(task.get("dyn_id") or "")
    uid = str(task.get("uid") or "")
    pub_ts = int(task.get("pub_ts") or 0)
    if not dyn_id:
        return
    feed = state.setdefault("feed", {})
    outbox = feed.setdefault("outbox", {})
    outbox.pop(dyn_id, None)
    add_recent_pushed(state, dyn_id)
    feed.setdefault("push_retry_after", {}).pop(dyn_id, None)
    PENDING_PUSH_IDS.discard(dyn_id)

    stat = get_uid_stat(state, uid)
    remember_uid_id(stat, dyn_id)
    stat["total_seen"] += 1
    stat["last_pub_ts"] = max(int(stat.get("last_pub_ts", 0)), pub_ts)
    first_seen = int(task.get("first_seen") or 0)
    if first_seen and pub_ts:
        delay = max(0, first_seen - pub_ts)
        stat["max_delay_today"] = max(int(stat.get("max_delay_today", 0)), delay)

    daily = state.setdefault("daily", {})
    daily["webhook_success"] = int(daily.get("webhook_success", 0)) + 1
    mark_state_dirty(state)


def schedule_retry(state, task):
    dyn_id = str(task.get("dyn_id") or "")
    if not dyn_id:
        return
    task["attempt"] = int(task.get("attempt", 0) or 0) + 1
    attempt = task["attempt"]
    delay = min(NOTIFY_RETRY_MAX, NOTIFY_RETRY_BASE * (2 ** max(0, attempt - 1)))
    delay += random.uniform(0, min(30, delay * 0.15))
    task["next_attempt"] = time.time() + delay
    state.setdefault("feed", {}).setdefault("outbox", {})[dyn_id] = task
    state.setdefault("feed", {}).setdefault("push_retry_after", {})[dyn_id] = task["next_attempt"]
    state.setdefault("daily", {})["webhook_fail"] = int(state.setdefault("daily", {}).get("webhook_fail", 0)) + 1
    PENDING_PUSH_IDS.discard(dyn_id)
    mark_state_dirty(state)
    logging.error(f"[Webhook重试] dyn_id={dyn_id} 第{attempt}次失败，下次约 {delay:.0f}s 后重试")


def notify_worker():
    while IS_RUNNING or not notify_queue.empty():
        try:
            task = notify_queue.get(timeout=1)
        except queue.Empty:
            continue
        try:
            dyn_id = str(task.get("dyn_id") or "")
            ntype = task.get("notify_type")
            if dyn_id and float(task.get("next_attempt", 0) or 0) > time.time():
                # 暂未到重试时间，放回去稍后处理
                try:
                    notify_queue.put_nowait(task)
                except queue.Full:
                    pass
                time.sleep(2)
                continue

            ok = bool(notifier.send_webhook_notification(
                task.get("title", ""), task.get("items", []), notify_type=ntype
            ))
            if ok:
                with STATE_LOCK:
                    if ntype == "dynamic" and ACTIVE_STATE is not None:
                        mark_sent(ACTIVE_STATE, task)
                        save_dynamic_state(ACTIVE_STATE)
                logging.info(f"[发送成功] type={ntype} dyn_id={dyn_id or '-'}")
            else:
                with STATE_LOCK:
                    if ntype == "dynamic" and ACTIVE_STATE is not None:
                        schedule_retry(ACTIVE_STATE, task)
                        save_dynamic_state(ACTIVE_STATE)
                logging.warning(f"[发送失败] dyn_id={dyn_id or '-'}")
        except Exception as e:
            logging.error(f"推送线程异常: {repr(e)}")
            if task.get("dyn_id") and ACTIVE_STATE is not None:
                with STATE_LOCK:
                    schedule_retry(ACTIVE_STATE, task)
                    save_dynamic_state(ACTIVE_STATE)
        finally:
            notify_queue.task_done()
        time.sleep(NOTIFY_SEND_DELAY)


def requeue_due_outbox(state):
    outbox = state.setdefault("feed", {}).setdefault("outbox", {})
    now = time.time()
    for dyn_id, task in list(outbox.items()):
        if not isinstance(task, dict):
            outbox.pop(dyn_id, None)
            continue
        if float(task.get("next_attempt", 0) or 0) > now:
            continue
        if dyn_id in PENDING_PUSH_IDS:
            continue
        try:
            notify_queue.put_nowait(task)
            PENDING_PUSH_IDS.add(str(dyn_id))
        except queue.Full:
            break


# =========================================================
# 关注流 API
# =========================================================
FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/nav"
NAV_MODE = True
NAV_FAIL_COUNT = 0
NAV_FAIL_LIMIT = 2


def fetch_following_feed(offset="", update_baseline=""):
    """App导航栏动态流主接口实验版"""
    params = {
        "web_location": "333.1365",
        "timezone_offset": "-480",
    }
    if offset:
        params["offset"] = offset
    if update_baseline:
        params["update_baseline"] = update_baseline
    return wbi_request(FEED_URL, params)


def fetch_feed_page(offset=""):
    global NAV_FAIL_COUNT
    data = fetch_following_feed(offset)
    if data.get("code") != 0:
        NAV_FAIL_COUNT += 1
        logging.warning(f"❌ feed/nav失败 count={NAV_FAIL_COUNT} code={data.get('code')}")
        return None
    NAV_FAIL_COUNT = 0
    return data.get("data") or {}


def process_feed_items(items, target_uids, state, refresh_seq, discovery_mode):
    """只比较动态 ID / 首次发现时间；不再用全局 pub_ts 游标吃掉迟到动态。"""
    now_ts = int(time.time())
    candidates = []
    page_ids = []
    daily = state.setdefault("daily", {})

    for item in items:
        if not isinstance(item, dict):
            continue
        dyn_id = str(item.get("id_str") or "")
        if not dyn_id:
            continue
        page_ids.append(dyn_id)

        author = item.get("modules", {}).get("module_author", {}) or {}
        uid = str(author.get("mid", ""))
        name = author.get("name", "未知UP")
        pub_ts = int(author.get("pub_ts", 0) or 0)
        if uid not in target_uids:
            continue

        stat = get_uid_stat(state, uid, name)
        stat["last_global_seen"] = now_ts
        if pub_ts:
            stat["last_pub_ts"] = max(int(stat.get("last_pub_ts", 0)), pub_ts)
        mark_state_dirty(state)

        if not is_allowed_dynamic(item):
            continue

        # 任何已发送 / 已持久化 pending 动态都跳过
        if is_recent_pushed(state, dyn_id) or dyn_id in PENDING_PUSH_IDS:
            remember_uid_id(stat, dyn_id)
            continue
        if dyn_id in state.setdefault("feed", {}).setdefault("outbox", {}):
            remember_uid_id(stat, dyn_id)
            continue

        # 这里是核心：不再依赖“全局 last_ts”来决定新旧。
        # 只要这个动态 ID 没见过，并且发布时间在保护窗口内，就允许进入候选。
        first_seen_map = state.setdefault("feed", {}).setdefault("discovered", {})
        entry = first_seen_map.get(dyn_id)
        if entry:
            first_seen = int(entry.get("first_seen", now_ts))
            # 已经发现过但没发成功，交给 outbox/retry；不重复入队
            continue

        if pub_ts <= 0:
            pub_ts = now_ts
        age = now_ts - pub_ts
        if age > DYNAMIC_NEW_WINDOW:
            logging.debug(f"[历史过滤] dyn_id={dyn_id} age={age}s > {DYNAMIC_NEW_WINDOW}s")
            continue

        # 先进入候选；只有成功写入 Outbox 后才登记 discovered，避免入队失败导致永久跳过。
        candidates.append((pub_ts, dyn_id, uid, item, now_ts))

    daily["items_seen"] = int(daily.get("items_seen", 0)) + len(page_ids)
    return candidates, page_ids


def trim_discovered(state):
    discovered = state.setdefault("feed", {}).setdefault("discovered", {})
    if len(discovered) <= SEEN_DYNAMIC_LIMIT:
        return
    items = sorted(
        discovered.items(),
        key=lambda kv: int((kv[1] or {}).get("first_seen", 0) or 0),
        reverse=True,
    )[:SEEN_DYNAMIC_LIMIT]
    state["feed"]["discovered"] = dict(items)


def update_uid_stats_after_enqueue(state, task, discovery_mode):
    uid = str(task.get("uid") or "")
    dyn_id = str(task.get("dyn_id") or "")
    stat = get_uid_stat(state, uid)
    stat["daily_new"] = int(stat.get("daily_new", 0)) + 1
    first_seen = int(task.get("first_seen", 0) or 0)
    pub_ts = int(task.get("pub_ts", 0) or 0)
    delay = max(0, first_seen - pub_ts) if first_seen and pub_ts else 0
    stat["max_delay_today"] = max(int(stat.get("max_delay_today", 0)), delay)

    daily = state.setdefault("daily", {})
    daily["new_found"] = int(daily.get("new_found", 0)) + 1
    if discovery_mode == "primary":
        daily["primary_found"] = int(daily.get("primary_found", 0)) + 1
    if delay >= 30:
        daily["delayed_found"] = int(daily.get("delayed_found", 0)) + 1
        stat["daily_delayed"] = int(stat.get("daily_delayed", 0)) + 1
    if discovery_mode == "verify":
        daily["verify_recovered"] = int(daily.get("verify_recovered", 0)) + 1
        stat["daily_verify_recovered"] = int(stat.get("daily_verify_recovered", 0)) + 1
    elif discovery_mode == "deep":
        daily["deep_recovered"] = int(daily.get("deep_recovered", 0)) + 1
        stat["daily_deep_recovered"] = int(stat.get("daily_deep_recovered", 0)) + 1
    mark_state_dirty(state)


def enqueue_candidates(candidates, state, discovery_mode):
    candidates.sort(key=lambda x: (x[0], x[1]))
    has_new = False
    for pub_ts, dyn_id, uid, item, first_seen in candidates:
        try:
            push_data = format_dynamic_message(item)
            task_title = f"{push_data.get('user', '未知UP')} 发布了新动态"
            ok = safe_enqueue_notify(
                task_title,
                [push_data],
                "dynamic",
                dyn_id=dyn_id,
                uid=uid,
                pub_ts=pub_ts,
                first_seen=first_seen,
                discovery_mode=discovery_mode,
            )
            if ok:
                state.setdefault("feed", {}).setdefault("discovered", {})[dyn_id] = {
                    "first_seen": first_seen,
                    "pub_ts": pub_ts,
                    "uid": uid,
                    "refresh_seq": STATE.refresh_seq,
                    "discovery_mode": discovery_mode,
                }
                update_uid_stats_after_enqueue(state, {
                    "uid": uid,
                    "dyn_id": dyn_id,
                    "pub_ts": pub_ts,
                    "first_seen": first_seen,
                }, discovery_mode)
                logging.info(
                    f"🚨 [{discovery_mode}] 发现动态 seq={STATE.refresh_seq} uid={uid} "
                    f"dyn_id={dyn_id} pub={ts_to_str(pub_ts)} delay={max(0, first_seen-pub_ts)}s "
                    f"images={push_data.get('image_count', 0)}"
                )
                if discovery_mode != "primary":
                    logging.warning(
                        f"🚨 漏报诊断：{discovery_mode} 才发现 uid={uid} dyn_id={dyn_id}"
                    )
                has_new = True
            else:
                logging.warning(f"[入队失败] dyn_id={dyn_id}")
        except Exception as e:
            logging.error(f"动态处理异常 dyn_id={dyn_id}: {repr(e)}")
    trim_discovered(state)
    return has_new


def full_refresh(target_uids, state, mode="primary", max_pages=1, stop_at_snapshot=True):
    """一次完整的关注流刷新。成功返回后，才允许更新 baseline / snapshot。"""
    STATE.refresh_seq += 1
    refresh_seq = STATE.refresh_seq
    state.setdefault("daily", {})["refreshes"] = int(state.setdefault("daily", {}).get("refreshes", 0)) + 1

    all_new = []
    all_ids = []
    offset = ""
    completed = True
    stable_pages = 0
    old_snapshot = set(state.setdefault("feed", {}).get("last_snapshot_ids", []) or [])
    reached_old = False
    first_baseline = ""
    pages_done = 0

    logging.info(f"🔄 [{mode}] 关注流整体刷新开始 seq={refresh_seq}")

    for page_idx in range(max_pages):
        if not IS_RUNNING:
            completed = False
            break
        data = fetch_feed_page(offset)
        if data is None:
            completed = False
            state.setdefault("daily", {})["api_fail"] = int(state.setdefault("daily", {}).get("api_fail", 0)) + 1
            logging.warning(f"❌ [{mode}] feed/nav 第{page_idx + 1}页失败，保留旧边界")
            break

        pages_done += 1
        items = data.get("items") or []
        if not items:
            break
        if page_idx == 0:
            first_baseline = str(data.get("update_baseline") or items[0].get("id_str") or "")

        candidates, page_ids = process_feed_items(
            items, target_uids, state, refresh_seq, mode
        )
        all_new.extend(candidates)
        all_ids.extend(page_ids)

        old_count = sum(1 for x in page_ids if x in old_snapshot)
        if old_count >= max(1, min(3, len(page_ids))):
            stable_pages += 1
        else:
            stable_pages = 0

        if old_snapshot and any(x in old_snapshot for x in page_ids):
            reached_old = True

        next_offset = str(data.get("offset") or "")
        has_more = bool(data.get("has_more"))
        if not next_offset or not has_more:
            break
        if stop_at_snapshot and (reached_old or stable_pages >= DEEP_SCAN_STOP_STABLE_PAGES):
            break
        offset = next_offset
        if page_idx + 1 < max_pages:
            time.sleep(random.uniform(0.4, 0.8))

    # 去重，先入队再更新 snapshot
    unique = {}
    for c in all_new:
        unique[c[1]] = c
    has_new = enqueue_candidates(list(unique.values()), state, mode)

    # snapshot 只在“至少一页成功”后更新；失败不覆盖旧快照
    if completed and pages_done > 0:
        feed = state.setdefault("feed", {})
        feed["last_refresh_time"] = time.time()
        feed["last_success_refresh"] = time.time()
        if first_baseline:
            feed["baseline"] = first_baseline
        # snapshot 保留最近一批完整本轮顶部动态，防止只用 pub_ts 判断
        new_snapshot = list(dict.fromkeys(all_ids))[:RECENT_SNAPSHOT_LIMIT]
        feed["last_snapshot_ids"] = new_snapshot
        history = feed.setdefault("recent_snapshot_history", [])
        history.append({
            "seq": refresh_seq,
            "time": int(time.time()),
            "mode": mode,
            "count": len(all_ids),
            "ids": new_snapshot[:100],
        })
        history[:] = history[-20:]
        mark_state_dirty(state)

    logging.info(
        f"🔄 [{mode}] 刷新结束 seq={refresh_seq} pages={pages_done} "
        f"items={len(all_ids)} new={len(unique)} reached_old={reached_old}"
    )
    return has_new, len(unique), pages_done


# =========================================================
# 15:30 健康报告
# =========================================================
def format_health_report(state, target_uids, china_dt):
    daily = state.setdefault("daily", {})
    feed = state.setdefault("feed", {})
    outbox = feed.setdefault("outbox", {})
    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        f"B站关注动态监控报告 {china_dt.strftime('%Y-%m-%d %H:%M')}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"监控 UID：{len(target_uids)}",
        f"整体刷新：{daily.get('refreshes', 0)} 次",
        f"深度刷新：{daily.get('deep_scans', 0)} 次",
        f"读取动态：{daily.get('items_seen', 0)} 条",
        f"新动态：{daily.get('new_found', 0)} 条",
        f"首次刷新发现：{daily.get('primary_found', 0)} 条",
        f"二次确认追回：{daily.get('verify_recovered', 0)} 条",
        f"深扫追回：{daily.get('deep_recovered', 0)} 条",
        f"延迟动态：{daily.get('delayed_found', 0)} 条",
        f"Webhook 成功：{daily.get('webhook_success', 0)}",
        f"Webhook 失败：{daily.get('webhook_fail', 0)}",
        f"Outbox 待发送：{len(outbox)}",
        f"API失败：{daily.get('api_fail', 0)}",
        f"当前连续失败：{STATE.consecutive_failures}",
        "",
        "【UID 健康度】",
    ]
    stats = state.get("uid_stats", {})
    for uid in sorted(target_uids):
        s = stats.get(str(uid), {}) or {}
        last_global = int(s.get("last_global_seen", 0) or 0)
        age = int(time.time() - last_global) if last_global else -1
        delayed = int(s.get("daily_delayed", 0) or 0)
        if last_global == 0:
            icon = "⚪"
        elif age > 1800:
            icon = "⚠️"
        elif delayed >= 2:
            icon = "⚠️"
        else:
            icon = "✅"
        name = s.get("name", uid)
        lines.append(
            f"{icon} {name}({uid}) | 新{s.get('daily_new', 0)} "
            f"延迟{delayed} 最大延迟{s.get('max_delay_today', 0)}s "
            f"最后命中={ts_to_str(last_global) if last_global else '从未'}"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def maybe_send_health_report(state, target_uids, china_dt):
    if china_dt.weekday() not in RUN_WEEKDAYS:
        return
    if china_dt.hour != HEALTH_REPORT_HOUR or china_dt.minute != HEALTH_REPORT_MINUTE:
        return
    date_str = china_dt.strftime("%Y-%m-%d")
    if state.get("_meta", {}).get("last_report_date") == date_str or STATE.last_report_date == date_str:
        return
    report = format_health_report(state, target_uids, china_dt)
    safe_enqueue_notify("📊 B站动态监控 15:30 健康报告", [{"user": "系统雷达", "message": report}], "system")
    STATE.last_report_date = date_str
    state.setdefault("_meta", {})["last_report_date"] = date_str
    mark_state_dirty(state)


# =========================================================
# 启动 / 主循环
# =========================================================
def initialize_state(target_uids):
    state = load_dynamic_state()
    reset_daily_stats(state, now_cn().strftime("%Y-%m-%d"))
    feed = state.setdefault("feed", {})

    # 兼容旧版本：如果旧状态只有 recent_pushed_ids / last_ts，不把它继续当作唯一新旧判断依据。
    feed.setdefault("last_snapshot_ids", [])
    feed.setdefault("recent_snapshot_history", [])
    feed.setdefault("discovered", {})

    # 启动时做一次“基线建立”，只采集，不把现有历史全部推送
    logging.info("🧭 正在建立 App 关注流启动基线...")
    data = fetch_feed_page("")
    if data is not None:
        items = data.get("items") or []
        ids = [str(x.get("id_str")) for x in items if isinstance(x, dict) and x.get("id_str")]
        feed["baseline"] = str(data.get("update_baseline") or (ids[0] if ids else feed.get("baseline", "")))
        feed["last_snapshot_ids"] = ids[:RECENT_SNAPSHOT_LIMIT]
        # 启动时只建立已知 ID 索引，不推送旧动态
        for item in items:
            if not isinstance(item, dict):
                continue
            author = item.get("modules", {}).get("module_author", {}) or {}
            uid = str(author.get("mid", ""))
            if uid not in target_uids:
                continue
            dyn_id = str(item.get("id_str") or "")
            if not dyn_id:
                continue
            stat = get_uid_stat(state, uid, author.get("name", uid))
            remember_uid_id(stat, dyn_id)
            stat["last_global_seen"] = int(time.time())
            pub_ts = int(author.get("pub_ts", 0) or 0)
            stat["last_pub_ts"] = max(int(stat.get("last_pub_ts", 0)), pub_ts)
        mark_state_dirty(state)
        save_dynamic_state(state)
        logging.info(f"✅ 启动基线建立完成，首页动态={len(items)}")
    else:
        logging.warning("⚠️ 启动基线获取失败，将依赖缓存继续启动")
    return state


def refresh_following_if_due(state, following_list, last_refresh_time):
    now = time.time()
    if now - last_refresh_time < FOLLOWING_REFRESH_INTERVAL:
        return following_list, last_refresh_time
    new_list = get_following_list(SOURCE_UID)
    if new_list is None:
        logging.warning("⚠️ 关注列表本轮刷新失败，保留旧列表")
        return following_list, now
    new_list = [str(x) for x in new_list]
    if str(SOURCE_UID) not in new_list:
        new_list.append(str(SOURCE_UID))
    new_list = list(dict.fromkeys(new_list))
    old_set = set(following_list)
    new_set = set(new_list)
    following_list = new_list
    save_following_cache(following_list)
    for uid in new_set:
        get_uid_stat(state, uid)
    logging.info(
        f"🔄 关注列表每小时刷新完成 {len(old_set)} → {len(new_set)} UID"
        if old_set != new_set else
        f"🔄 关注列表每小时检查完成，UID={len(new_set)} 无变化"
    )
    mark_state_dirty(state)
    return following_list, now


def initial_following_list():
    live = get_following_list(SOURCE_UID)
    source = "实时"
    if live is None:
        live = load_following_cache()
        source = "缓存"
    if not live:
        live = FALLBACK_DYNAMIC_UIDS[:]
        source = "fallback"
    live = [str(x) for x in live]
    if str(SOURCE_UID) not in live:
        live.append(str(SOURCE_UID))
    live = list(dict.fromkeys(live))
    if len(live) > MAX_MONITOR_UIDS:
        logging.warning(f"关注列表 {len(live)} > {MAX_MONITOR_UIDS}，截断保护到前{MAX_MONITOR_UIDS}个")
        live = live[:MAX_MONITOR_UIDS]
    save_following_cache(live)
    logging.info(f"关注列表加载完成：{len(live)} UID，来源={source}")
    return live


def start_monitoring():
    global IS_RUNNING, ACTIVE_STATE
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    activate_session_cookies()
    if not load_cookies_into_session():
        logging.critical("❌ Cookie 不可用，程序退出")
        return
    update_wbi_keys()
    if not IS_RUNNING:
        return

    following_list = initial_following_list()
    target_uids = set(following_list)
    state = initialize_state(target_uids)
    ACTIVE_STATE = state
    reset_daily_stats(state, now_cn().strftime("%Y-%m-%d"))

    threading.Thread(target=notify_worker, daemon=True, name="notify-worker").start()
    restore_outbox_to_queue(state)

    last_scan = 0.0
    last_following_refresh = time.time()
    last_heartbeat = 0.0
    STATE.last_deep_scan = 0.0
    STATE.last_new_dynamic_time = time.time()

    logging.info(
        f"✅ 启动完成：工作日 {RUN_START_HOUR}:{RUN_START_MINUTE:02d}-{RUN_END_HOUR}:00；"
        f"整体关注流刷新(feed/nav) {NORMAL_INTERVAL_MIN:g}~{NORMAL_INTERVAL_MAX:g}s；"
        f"二次确认 {VERIFY_DELAY_MIN:g}~{VERIFY_DELAY_MAX:g}s；"
        f"深扫每{DEEP_SCAN_INTERVAL}s；关注列表/心跳均1小时"
    )

    while IS_RUNNING:
        try:
            now = time.time()
            cn = now_cn()
            reset_daily_stats(state, cn.strftime("%Y-%m-%d"))

            # 每小时心跳，不受工作窗口影响
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                feed = state.setdefault("feed", {})
                outbox = feed.setdefault("outbox", {})
                logging.info(
                    f"💓 心跳 | UID={len(target_uids)} | 主刷新={NORMAL_INTERVAL_MIN:g}~{NORMAL_INTERVAL_MAX:g}s "
                    f"| 最近成功刷新={ts_to_str(feed.get('last_success_refresh', 0))} "
                    f"| Outbox={len(outbox)} | 连续失败={STATE.consecutive_failures} "
                    f"| CookieFail={STATE.consecutive_cookie_failures}"
                )
                last_heartbeat = now

            # 每小时刷新关注列表
            following_list, last_following_refresh = refresh_following_if_due(
                state, following_list, last_following_refresh
            )
            target_uids = set(following_list)

            # 恢复/重试 webhook outbox
            requeue_due_outbox(state)

            # 15:30 报告
            maybe_send_health_report(state, target_uids, cn)

            # 工作时间外不刷动态，但维持心跳/列表/outbox
            if not is_in_monitor_window(cn):
                if now - STATE.last_state_save >= STATE_SAVE_INTERVAL:
                    save_dynamic_state(state)
                    STATE.last_state_save = now
                time.sleep(2.0)
                continue

            # 每日上班打卡
            today = cn.strftime("%Y-%m-%d")
            if STATE.last_checkin_date != today:
                STATE.last_checkin_date = today
                safe_enqueue_notify(
                    "☀️ B站动态监控系统打卡上班",
                    [{"user": "系统雷达", "message": f"{today} 工作日监控开始，当前监控 {len(target_uids)} 个 UID。"}],
                    "system"
                )

            interval = random_main_interval()
            if now - last_scan >= interval:
                try:
                    STATE.consecutive_failures = 0
                    has_new, _, _ = full_refresh(
                        target_uids, state, mode="primary", max_pages=1, stop_at_snapshot=True
                    )

                    # 发现新动态后做一次整体二次确认，不拆 UID
                    if has_new and IS_RUNNING:
                        state.setdefault("daily", {})["verify_rounds"] = int(state.setdefault("daily", {}).get("verify_rounds", 0)) + 1
                        time.sleep(random.uniform(VERIFY_DELAY_MIN, VERIFY_DELAY_MAX))
                        full_refresh(
                            target_uids, state, mode="verify", max_pages=VERIFY_MAX_PAGES, stop_at_snapshot=True
                        )

                    # 每5分钟整体深扫一次，直到已知 snapshot 边界或最大页数
                    if now - STATE.last_deep_scan >= DEEP_SCAN_INTERVAL and IS_RUNNING:
                        state.setdefault("daily", {})["deep_scans"] = int(state.setdefault("daily", {}).get("deep_scans", 0)) + 1
                        STATE.last_deep_scan = now
                        logging.info("🔎 开始5分钟整体关注流深扫")
                        full_refresh(
                            target_uids, state, mode="deep",
                            max_pages=DEEP_SCAN_MAX_PAGES,
                            stop_at_snapshot=True,
                        )
                    last_scan = now
                except Exception as e:
                    STATE.consecutive_failures += 1
                    logging.error(f"❌ 关注流扫描异常: {repr(e)}", exc_info=True)

            if now - STATE.last_state_save >= STATE_SAVE_INTERVAL or state.get("_meta", {}).get("dirty"):
                save_dynamic_state(state)
                STATE.last_state_save = now

            time.sleep(0.5)

        except Exception as e:
            if IS_RUNNING:
                logging.error(f"❌ 主循环异常: {repr(e)}", exc_info=True)
                time.sleep(8)
            else:
                break

    save_dynamic_state(state)
    logging.info("💾 状态已安全保存，程序退出。")


if __name__ == "__main__":
    init_logging()
    start_monitoring()
