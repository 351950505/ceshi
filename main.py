import sys
import os
import re
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

RUN_TZ = "Asia/Shanghai"
RUN_WEEKDAYS = {0, 1, 2, 3, 4}
RUN_START_HOUR = 9
RUN_START_MINUTE = 20
RUN_END_HOUR = 16

NORMAL_INTERVAL_MIN = 20.0
NORMAL_INTERVAL_MAX = 35.0
VERIFY_DELAY_MIN = 1.5
VERIFY_DELAY_MAX = 3.0
DEEP_SCAN_INTERVAL = 300
DEEP_SCAN_MAX_PAGES = 20
DEEP_SCAN_STOP_STABLE_PAGES = 2
VERIFY_MAX_PAGES = 6
DETAIL_FETCH_MAX = 8

DYNAMIC_NEW_WINDOW = 6 * 3600

STATE_SAVE_INTERVAL = 60
RECENT_SNAPSHOT_LIMIT = 400
SEEN_DYNAMIC_LIMIT = 12000
RECENT_PUSHED_IDS_LIMIT = 12000
OUTBOX_MAX = 500

NOTIFY_QUEUE_MAXSIZE = 100
NOTIFY_SEND_DELAY = 2.0
NOTIFY_RETRY_BASE = 60
NOTIFY_RETRY_MAX = 1800

REQUEST_TIMEOUT = 12
REQUEST_RETRIES = 3
WBI_REFRESH_INTERVAL = 21600
COOKIE_FAIL_EXIT_THRESHOLD = 3

NAV_FAIL_THRESHOLD = 2
NAV_RECOVER_THRESHOLD = 2

HEALTH_REPORT_HOUR = 15
HEALTH_REPORT_MINUTE = 30

NAV_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/nav"
ALL_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all"
DETAIL_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail"

# feed/nav 的 type 为数字；feed/all 为字符串
NAV_ALLOWED_TYPES = {1, 2, 4, 8, 64, 256, 2048, 4097, 4098, 4099, 4100, 4308, 4310}
ALLOWED_DYNAMIC_TYPES = {
    "", "MAJOR_TYPE_OPUS", "MAJOR_TYPE_ARCHIVE", "MAJOR_TYPE_ARTICLE",
    "MAJOR_TYPE_DRAW", "MAJOR_TYPE_COMMON", "MAJOR_TYPE_LIVE"
}
ALLOWED_TOP_LEVEL_TYPES = {
    "DYNAMIC_TYPE_WORD", "DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_AV",
    "DYNAMIC_TYPE_ARTICLE", "DYNAMIC_TYPE_FORWARD", "DYNAMIC_TYPE_LIVE"
}
ALLOW_FORWARD_DYNAMIC = True

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
COMPRESS_SUFFIX_RE = re.compile(r"@[0-9a-zA-Z_.,%-]+(?:\.(?:webp|jpg|jpeg|png|gif))?$", re.I)


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
    channel: str = "nav"
    nav_fail: int = 0
    nav_ok: int = 0
    all_ok_in_fallback: int = 0


STATE = MonitorState()


def signal_handler(signum, frame):
    global IS_RUNNING
    logging.info("收到停止信号，准备安全退出并保存状态...")
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
        ts = int(ts)
        if ts <= 0:
            return "未知时间"
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "未知时间"


def mark_state_dirty(state):
    state.setdefault("_meta", {})["dirty"] = True


def random_main_interval():
    if STATE.consecutive_failures >= 2:
        return random.uniform(35.0, 60.0)
    return random.uniform(NORMAL_INTERVAL_MIN, NORMAL_INTERVAL_MAX)


def raw_media_url(url):
    """去掉 @480w_300h_1c.webp 这类压缩后缀，使用原始封面。"""
    if not url or not isinstance(url, str):
        return ""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith(("http://", "https://")):
        return ""
    path, sep, query = url.partition("?")
    path = COMPRESS_SUFFIX_RE.sub("", path)
    if "@" in path.rsplit("/", 1)[-1]:
        path = path.split("@", 1)[0]
    return path + (("?" + query) if sep else "")


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
        logging.info("B站首页会话 Cookie 激活完成")
        return True
    except Exception as e:
        logging.warning("首页会话激活失败: %s", e)
        return False


def load_cookies_into_session():
    try:
        if not os.path.exists("bili_cookie.txt"):
            logging.error("未找到 bili_cookie.txt")
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
        logging.info("登录 Cookie 已加载")
        return True
    except Exception as e:
        logging.error("加载 Cookie 异常: %s", e)
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
    logging.info("B站关注动态监控启动（feed/nav 主通道 + feed/all 备用）")
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
        logging.info("WBI 密钥刷新成功")
        return True
    except Exception as e:
        logging.warning("WBI 刷新失败: %s", e)
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
                    "Cookie 验证失败 %s/%s",
                    STATE.consecutive_cookie_failures, COOKIE_FAIL_EXIT_THRESHOLD
                )
                notify_system_once(
                    "B站 Cookie 失效预警",
                    "Cookie 验证失败，请检查 bili_cookie.txt。"
                )
                if STATE.consecutive_cookie_failures >= COOKIE_FAIL_EXIT_THRESHOLD:
                    logging.critical("Cookie 连续失效，停止程序。")
                    globals()["IS_RUNNING"] = False
                return data

            STATE.consecutive_cookie_failures = 0

            if code in (-799, -352, -509, -412) or resp.status_code in (412, 429):
                wait = min(300.0, 15.0 * (2 ** i)) + random.uniform(3, 8)
                logging.warning(
                    "B站风控/限流 code=%s http=%s，退避 %.1fs",
                    code, resp.status_code, wait
                )
                force_update_wbi_keys()
                notify_system_once(
                    "B站风控预警",
                    "code=%s, http=%s，已自动退避 %.1f 秒。" % (code, resp.status_code, wait)
                )
                time.sleep(wait)
                continue

            if code == 0:
                return data

            if i < retries - 1:
                wait = 3.0 * (2 ** i) + random.uniform(1, 3)
                logging.warning("[API重试] code=%s wait=%.1fs url=%s", code, wait, url)
                time.sleep(wait)
            else:
                return data

        except Exception as e:
            last = {"code": -500, "message": repr(e)}
            if i < retries - 1:
                wait = 3.0 * (2 ** i) + random.uniform(1, 3)
                logging.warning("[网络重试] %s wait=%.1fs", repr(e), wait)
                time.sleep(wait)
    logging.error("请求最终失败: %s", url)
    notify_system_once("B站 API 请求失败", "接口连续失败: %s" % url)
    return last


def wbi_request(url, params):
    update_wbi_keys()
    if WBI_KEYS["img_key"] and WBI_KEYS["sub_key"]:
        try:
            return safe_request(url, enc_wbi(params, WBI_KEYS["img_key"], WBI_KEYS["sub_key"]))
        except Exception:
            pass
    return safe_request(url, params)


def default_state():
    return {
        "version": 6,
        "feed": {
            "baseline": "",
            "last_snapshot_ids": [],
            "recent_snapshot_history": [],
            "recent_pushed_ids": [],
            "push_retry_after": {},
            "outbox": {},
            "discovered": {},
            "last_refresh_time": 0,
            "last_success_refresh": 0,
            "active_channel": "nav",
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
            "nav_fail": 0,
            "all_fallback": 0,
            "detail_ok": 0,
            "detail_fail": 0,
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
        feed["active_channel"] = STATE.channel

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
            "nav_fail": 0,
            "all_fallback": 0,
            "detail_ok": 0,
            "detail_fail": 0,
        }


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
        logging.warning("保存关注缓存失败: %s", e)


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
            logging.warning("关注列表第 %s 页失败 code=%s，放弃本次覆盖", pn, data.get("code"))
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
        logging.error("关注列表实际为 %s 个，超过 MAX_MONITOR_UIDS=%s，本次拒绝覆盖", len(result), MAX_MONITOR_UIDS)
        notify_system_once(
            "关注列表异常",
            "检测到 %s 个关注目标，超过安全上限 %s，已保留旧列表。" % (len(result), MAX_MONITOR_UIDS)
        )
        return None
    return result


def is_nav_item(item):
    if not isinstance(item, dict):
        return False
    if item.get("modules"):
        return False
    return "author" in item or "cover" in item or "jump_url" in item


def item_author(item):
    if is_nav_item(item):
        author = item.get("author") or {}
        return author, str(author.get("mid", "")), author.get("name", "未知UP"), 0
    author = (item.get("modules") or {}).get("module_author") or {}
    return author, str(author.get("mid", "")), author.get("name", "未知UP"), int(author.get("pub_ts", 0) or 0)


def is_allowed_dynamic(item):
    try:
        if is_nav_item(item):
            if item.get("visible") is False:
                return False
            t = item.get("type")
            if t is None or t == "":
                return True
            try:
                t = int(t)
            except Exception:
                return True
            if t in NAV_ALLOWED_TYPES:
                return True
            return True
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
        if is_nav_item(item):
            title = normalize_text(item.get("title", ""))
            t = item.get("type")
            prefix = ""
            try:
                t = int(t)
            except Exception:
                t = 0
            if t == 8:
                prefix = "【视频】"
            elif t == 64:
                prefix = "【专栏】"
            elif t in (2, 2048):
                prefix = "【图文】"
            elif t == 1:
                prefix = "【转发】"
            return (prefix + title).strip() or title

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
            return ("【视频】%s\n%s" % (title, desc_text)).strip()
        if t == "MAJOR_TYPE_ARTICLE":
            a = major.get("article") or {}
            title = normalize_text(a.get("title", ""))
            desc_text = normalize_text(a.get("desc", ""))
            return ("【专栏】%s\n%s" % (title, desc_text)).strip()
        if t == "MAJOR_TYPE_OPUS":
            opus = major.get("opus") or {}
            title = normalize_text(opus.get("title", ""))
            summary = opus.get("summary") or {}
            nodes = summary.get("rich_text_nodes") or []
            text = normalize_text("".join(n.get("text", "") for n in nodes if isinstance(n, dict)))
            return ("【图文】%s\n%s" % (title, text)).strip()
        if t == "MAJOR_TYPE_DRAW":
            return normalize_text(desc.get("text", "")) or "【图片动态】"
        if t == "MAJOR_TYPE_COMMON":
            common = major.get("common") or {}
            title = normalize_text(common.get("title", ""))
            desc_text = normalize_text(common.get("desc", ""))
            return ("【卡片】%s\n%s" % (title, desc_text)).strip()
        if t == "MAJOR_TYPE_LIVE":
            live = major.get("live") or {}
            title = normalize_text(live.get("title", ""))
            desc_text = normalize_text(live.get("desc_second", ""))
            return ("【直播】%s\n%s" % (title, desc_text)).strip()
        return normalize_text(desc.get("text", ""))
    except Exception:
        return ""


def collect_image_urls(obj, out=None, depth=0):
    if out is None:
        out = []
    if obj is None or depth > 6:
        return out
    if isinstance(obj, str):
        u = raw_media_url(obj)
        if u and ("hdslb.com" in u or "bfs/" in u) and u not in out:
            out.append(u)
        return out
    if isinstance(obj, dict):
        priority_keys = (
            "src", "url", "image", "image_url", "cover", "thumbnail", "pic", "picture",
            "origin_url", "source_url"
        )
        for k in priority_keys:
            v = obj.get(k)
            if isinstance(v, str):
                collect_image_urls(v, out, depth + 1)
            elif isinstance(v, (dict, list)):
                collect_image_urls(v, out, depth + 1)
        for k, v in obj.items():
            if k in ("face", "avatar", "pendant", "official", "vip"):
                continue
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
        if is_nav_item(item):
            u = raw_media_url(item.get("cover") or "")
            if u:
                urls.append(u)
            return urls[:12]

        modules = item.get("modules") or {}
        dyn = modules.get("module_dynamic") or {}
        major = dyn.get("major") or {}
        draw = major.get("draw") or {}
        for x in draw.get("items") or []:
            if isinstance(x, dict):
                u = raw_media_url(x.get("src") or x.get("url") or "")
                if u:
                    urls.append(u)
        opus = major.get("opus") or {}
        for x in opus.get("pics") or []:
            if isinstance(x, dict):
                u = raw_media_url(x.get("url") or x.get("src") or "")
                if u:
                    urls.append(u)
        for key in ("archive", "article"):
            section = major.get(key) or {}
            for k in ("cover", "covers"):
                v = section.get(k)
                if isinstance(v, str):
                    u = raw_media_url(v)
                    if u:
                        urls.append(u)
                elif isinstance(v, list):
                    for x in v:
                        if isinstance(x, str):
                            u = raw_media_url(x)
                            if u:
                                urls.append(u)
        collect_image_urls(major, urls)
        if item.get("type") == "DYNAMIC_TYPE_FORWARD":
            orig = item.get("orig")
            if isinstance(orig, dict):
                collect_image_urls(orig, urls)
    except Exception:
        pass

    clean = []
    for u in urls:
        u = raw_media_url(u)
        if u and u not in clean:
            clean.append(u)
    return clean[:12]


def item_jump_url(item, dyn_id):
    jump = item.get("jump_url") or ""
    if jump:
        if jump.startswith("//"):
            jump = "https:" + jump
        return jump
    return "https://t.bilibili.com/%s" % dyn_id


def format_dynamic_message(item):
    dyn_id = str(item.get("id_str") or "")
    _author, uid, name, pub_ts = item_author(item)
    text = cut_text(extract_dynamic_text(item), 900)

    if item.get("type") == "DYNAMIC_TYPE_FORWARD":
        orig = item.get("orig")
        if orig and isinstance(orig, dict):
            orig_text = cut_text(extract_dynamic_text(orig), 350)
            if orig_text:
                text = ("%s\n\n【转发原文】\n%s" % (text, orig_text)) if text else ("【转发原文】\n%s" % orig_text)
            orig_id = orig.get("id_str")
            if orig_id:
                text += "\n\n原动态：https://t.bilibili.com/%s" % orig_id

    if not text:
        text = "（该动态无可提取正文）"

    images = extract_images(item)
    time_label = ts_to_str(pub_ts)
    if time_label == "未知时间" and item.get("pub_time"):
        time_label = str(item.get("pub_time"))

    return {
        "user": name,
        "uid": uid,
        "message": text,
        "time": time_label,
        "link": item_jump_url(item, dyn_id),
        "cover": images[0] if images else "",
        "covers": images,
        "images": images,
        "image_count": len(images),
        "kind": "dynamic",
    }


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
    if name and info.get("name") in ("", uid):
        info["name"] = name
    return info


def remember_uid_id(uid_stat, dyn_id):
    ids = list(uid_stat.get("seen_ids", []) or [])
    if dyn_id in ids:
        ids.remove(dyn_id)
    ids.insert(0, dyn_id)
    uid_stat["seen_ids"] = ids[:500]


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
                logging.error("Outbox 已满，拒绝继续入队")
                notify_system_once("Webhook队列已满", "当前待发送超过 %s 条，暂停新增入队。" % OUTBOX_MAX)
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
    stat["total_seen"] = int(stat.get("total_seen", 0)) + 1
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
    logging.error("[Webhook重试] dyn_id=%s 第%s次失败，下次约 %.0fs 后重试", dyn_id, attempt, delay)


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
                logging.info("[发送成功] type=%s dyn_id=%s", ntype, dyn_id or "-")
            else:
                with STATE_LOCK:
                    if ntype == "dynamic" and ACTIVE_STATE is not None:
                        schedule_retry(ACTIVE_STATE, task)
                        save_dynamic_state(ACTIVE_STATE)
                logging.warning("[发送失败] dyn_id=%s", dyn_id or "-")
        except Exception as e:
            logging.error("推送线程异常: %s", repr(e))
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


def fetch_nav_page(offset="", update_baseline=""):
    params = {
        "timezone_offset": "-480",
        "platform": "web",
    }
    if offset:
        params["offset"] = offset
    if update_baseline:
        params["update_baseline"] = update_baseline
    data = wbi_request(NAV_URL, params)
    if data.get("code") != 0:
        return None
    return data.get("data") or {}


def fetch_all_page(offset=""):
    params = {
        "type": "all",
        "timezone_offset": "-480",
        "platform": "web",
        "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,decorationCard,onlyfansAssetsV2,forwardListHidden,ugcDelete",
        "web_location": "333.1365",
    }
    if offset:
        params["offset"] = offset
    data = wbi_request(ALL_URL, params)
    if data.get("code") != 0:
        return None
    return data.get("data") or {}


def fetch_dynamic_detail(dyn_id):
    params = {
        "id": str(dyn_id),
        "timezone_offset": "-480",
        "platform": "web",
        "features": "itemOpusStyle,opusBigCover,forwardListHidden,ugcDelete",
    }
    data = wbi_request(DETAIL_URL, params)
    if data.get("code") != 0:
        return None
    payload = data.get("data") or {}
    item = payload.get("item") or payload.get("items")
    if isinstance(item, list) and item:
        item = item[0]
    if isinstance(item, dict) and item.get("id_str"):
        return item
    return None


def mark_nav_success():
    STATE.nav_fail = 0
    STATE.nav_ok += 1
    if STATE.channel != "nav" and STATE.nav_ok >= NAV_RECOVER_THRESHOLD:
        logging.info("feed/nav 连续成功 %s 次，切回主通道", STATE.nav_ok)
        STATE.channel = "nav"
        STATE.all_ok_in_fallback = 0


def mark_nav_failure(state):
    STATE.nav_fail += 1
    STATE.nav_ok = 0
    daily = state.setdefault("daily", {})
    daily["nav_fail"] = int(daily.get("nav_fail", 0)) + 1
    daily["api_fail"] = int(daily.get("api_fail", 0)) + 1
    if STATE.nav_fail >= NAV_FAIL_THRESHOLD and STATE.channel != "all":
        STATE.channel = "all"
        STATE.all_ok_in_fallback = 0
        daily["all_fallback"] = int(daily.get("all_fallback", 0)) + 1
        logging.warning("feed/nav 连续失败 %s 次，切换 feed/all 备用", STATE.nav_fail)
        notify_system_once(
            "B站动态监控已切备用通道",
            "feed/nav 连续失败，已临时改用 feed/all。恢复后会自动切回。"
        )


def fetch_page_by_channel(offset="", update_baseline=""):
    """每轮只打一个通道，不 nav+all 双打。"""
    if STATE.channel == "nav":
        data = fetch_nav_page(offset=offset, update_baseline=update_baseline if not offset else "")
        if data is None:
            mark_nav_failure(ACTIVE_STATE or {})
            return None, "nav"
        mark_nav_success()
        return data, "nav"

    data = fetch_all_page(offset=offset)
    if data is None:
        if ACTIVE_STATE is not None:
            ACTIVE_STATE.setdefault("daily", {})["api_fail"] = int(
                ACTIVE_STATE.setdefault("daily", {}).get("api_fail", 0)
            ) + 1
        return None, "all"
    STATE.all_ok_in_fallback += 1
    return data, "all"


def maybe_probe_nav_recovery(state, update_baseline=""):
    """备用通道运行中，偶尔探测 nav，成功两次切回。本轮不双打业务数据。"""
    if STATE.channel != "all":
        return
    if STATE.all_ok_in_fallback < NAV_RECOVER_THRESHOLD:
        return
    probe = fetch_nav_page(offset="", update_baseline=update_baseline)
    if probe is None:
        STATE.nav_ok = 0
        STATE.nav_fail += 1
        return
    mark_nav_success()


def process_feed_items(items, target_uids, state, refresh_seq, discovery_mode):
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

        _author, uid, name, pub_ts = item_author(item)
        if uid not in target_uids:
            continue

        stat = get_uid_stat(state, uid, name)
        stat["last_global_seen"] = now_ts
        if pub_ts:
            stat["last_pub_ts"] = max(int(stat.get("last_pub_ts", 0)), pub_ts)
        mark_state_dirty(state)

        if not is_allowed_dynamic(item):
            continue

        if is_recent_pushed(state, dyn_id) or dyn_id in PENDING_PUSH_IDS:
            remember_uid_id(stat, dyn_id)
            continue
        if dyn_id in state.setdefault("feed", {}).setdefault("outbox", {}):
            remember_uid_id(stat, dyn_id)
            continue

        first_seen_map = state.setdefault("feed", {}).setdefault("discovered", {})
        if first_seen_map.get(dyn_id):
            continue

        if pub_ts > 0 and (now_ts - pub_ts) > DYNAMIC_NEW_WINDOW:
            logging.debug("[历史过滤] dyn_id=%s age>%ss", dyn_id, DYNAMIC_NEW_WINDOW)
            continue

        candidates.append((pub_ts or now_ts, dyn_id, uid, item, now_ts))

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


def enrich_new_item(item, state):
    """nav 轻量条目只对已确认新动态拉详情；失败则用 title+cover 推送。"""
    if not is_nav_item(item):
        return item
    dyn_id = str(item.get("id_str") or "")
    if not dyn_id:
        return item
    detail = fetch_dynamic_detail(dyn_id)
    daily = state.setdefault("daily", {})
    if detail:
        daily["detail_ok"] = int(daily.get("detail_ok", 0)) + 1
        return detail
    daily["detail_fail"] = int(daily.get("detail_fail", 0)) + 1
    logging.warning("[详情失败] dyn_id=%s，改用 nav 标题+封面推送", dyn_id)
    return item


def enqueue_candidates(candidates, state, discovery_mode):
    candidates.sort(key=lambda x: (x[0], x[1]))
    has_new = False
    detail_used = 0
    for pub_ts, dyn_id, uid, item, first_seen in candidates:
        try:
            if is_nav_item(item) and detail_used < DETAIL_FETCH_MAX:
                item = enrich_new_item(item, state)
                detail_used += 1
                time.sleep(random.uniform(0.2, 0.5))
            _a, uid2, _n, pub2 = item_author(item)
            if uid2:
                uid = uid2
            if pub2:
                pub_ts = pub2
            push_data = format_dynamic_message(item)
            task_title = "%s 发布了新动态" % push_data.get("user", "未知UP")
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
                    "channel": STATE.channel,
                }
                update_uid_stats_after_enqueue(state, {
                    "uid": uid,
                    "dyn_id": dyn_id,
                    "pub_ts": pub_ts,
                    "first_seen": first_seen,
                }, discovery_mode)
                logging.info(
                    "[%s/%s] 发现动态 seq=%s uid=%s dyn_id=%s pub=%s delay=%ss images=%s",
                    discovery_mode, STATE.channel, STATE.refresh_seq, uid, dyn_id,
                    ts_to_str(pub_ts), max(0, first_seen - pub_ts) if pub_ts else 0,
                    push_data.get("image_count", 0)
                )
                if discovery_mode != "primary":
                    logging.warning("漏报诊断：%s 才发现 uid=%s dyn_id=%s", discovery_mode, uid, dyn_id)
                has_new = True
            else:
                logging.warning("[入队失败] dyn_id=%s", dyn_id)
        except Exception as e:
            logging.error("动态处理异常 dyn_id=%s: %s", dyn_id, repr(e))
    trim_discovered(state)
    return has_new


def full_refresh(target_uids, state, mode="primary", max_pages=1, stop_at_snapshot=True):
    STATE.refresh_seq += 1
    refresh_seq = STATE.refresh_seq
    state.setdefault("daily", {})["refreshes"] = int(state.setdefault("daily", {}).get("refreshes", 0)) + 1
    feed = state.setdefault("feed", {})
    old_baseline = str(feed.get("baseline") or "")

    maybe_probe_nav_recovery(state, old_baseline)

    all_new = []
    all_ids = []
    offset = ""
    completed = True
    stable_pages = 0
    old_snapshot = set(feed.get("last_snapshot_ids", []) or [])
    reached_old = False
    first_baseline = ""
    pages_done = 0
    used_channel = STATE.channel
    update_num = 0

    logging.info("[%s] 整体刷新开始 seq=%s channel=%s", mode, refresh_seq, used_channel)

    for page_idx in range(max_pages):
        if not IS_RUNNING:
            completed = False
            break
        baseline_arg = old_baseline if (page_idx == 0 and used_channel == "nav") else ""
        data, used_channel = fetch_page_by_channel(offset=offset, update_baseline=baseline_arg)
        if data is None:
            completed = False
            logging.warning("[%s] %s 第%s页失败，保留旧边界", mode, used_channel, page_idx + 1)
            break

        pages_done += 1
        items = data.get("items") or []
        if not items:
            break
        if page_idx == 0:
            first_baseline = str(data.get("update_baseline") or items[0].get("id_str") or "")
            try:
                update_num = int(data.get("update_num", 0) or 0)
            except Exception:
                update_num = 0

        candidates, page_ids = process_feed_items(
            items, target_uids, state, refresh_seq, mode
        )
        all_new.extend(candidates)
        all_ids.extend(page_ids)

        old_count = sum(1 for x in page_ids if x in old_snapshot)
        if old_count >= max(1, min(3, len(page_ids) or 1)):
            stable_pages += 1
        else:
            stable_pages = 0

        if old_snapshot and any(x in old_snapshot for x in page_ids):
            reached_old = True

        next_offset = str(data.get("offset") or "")
        if not next_offset and items:
            next_offset = str(items[-1].get("id_str") or "")
        has_more = bool(data.get("has_more"))
        if not next_offset or not has_more:
            break
        if stop_at_snapshot and (reached_old or stable_pages >= DEEP_SCAN_STOP_STABLE_PAGES):
            break
        offset = next_offset
        if page_idx + 1 < max_pages:
            time.sleep(random.uniform(0.4, 0.8))

    unique = {}
    for c in all_new:
        unique[c[1]] = c
    has_new = enqueue_candidates(list(unique.values()), state, mode)

    if completed and pages_done > 0:
        feed["last_refresh_time"] = time.time()
        feed["last_success_refresh"] = time.time()
        if first_baseline:
            feed["baseline"] = first_baseline
        new_snapshot = list(dict.fromkeys(all_ids))[:RECENT_SNAPSHOT_LIMIT]
        feed["last_snapshot_ids"] = new_snapshot
        history = feed.setdefault("recent_snapshot_history", [])
        history.append({
            "seq": refresh_seq,
            "time": int(time.time()),
            "mode": mode,
            "channel": used_channel,
            "count": len(all_ids),
            "update_num": update_num,
            "ids": new_snapshot[:100],
        })
        history[:] = history[-20:]
        STATE.consecutive_failures = 0
        if not has_new:
            STATE.consecutive_no_update_rounds += 1
        else:
            STATE.consecutive_no_update_rounds = 0
            STATE.last_new_dynamic_time = time.time()
        mark_state_dirty(state)
    else:
        STATE.consecutive_failures += 1

    logging.info(
        "[%s] 刷新结束 seq=%s channel=%s pages=%s items=%s new=%s update_num=%s reached_old=%s",
        mode, refresh_seq, used_channel, pages_done, len(all_ids), len(unique), update_num, reached_old
    )
    return has_new, len(unique), pages_done


def format_health_report(state, target_uids, china_dt):
    daily = state.setdefault("daily", {})
    feed = state.setdefault("feed", {})
    outbox = feed.setdefault("outbox", {})
    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        "B站关注动态监控报告 %s" % china_dt.strftime("%Y-%m-%d %H:%M"),
        "━━━━━━━━━━━━━━━━━━━━",
        "当前通道：%s" % STATE.channel,
        "监控 UID：%s" % len(target_uids),
        "整体刷新：%s 次" % daily.get("refreshes", 0),
        "深度刷新：%s 次" % daily.get("deep_scans", 0),
        "读取动态：%s 条" % daily.get("items_seen", 0),
        "新动态：%s 条" % daily.get("new_found", 0),
        "首次刷新发现：%s 条" % daily.get("primary_found", 0),
        "二次确认追回：%s 条" % daily.get("verify_recovered", 0),
        "深扫追回：%s 条" % daily.get("deep_recovered", 0),
        "延迟动态：%s 条" % daily.get("delayed_found", 0),
        "详情成功/失败：%s/%s" % (daily.get("detail_ok", 0), daily.get("detail_fail", 0)),
        "nav失败：%s  切备用：%s" % (daily.get("nav_fail", 0), daily.get("all_fallback", 0)),
        "Webhook 成功：%s" % daily.get("webhook_success", 0),
        "Webhook 失败：%s" % daily.get("webhook_fail", 0),
        "Outbox 待发送：%s" % len(outbox),
        "API失败：%s" % daily.get("api_fail", 0),
        "当前连续失败：%s" % STATE.consecutive_failures,
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
            icon = "[-]"
        elif age > 1800:
            icon = "[!]"
        elif delayed >= 2:
            icon = "[!]"
        else:
            icon = "[OK]"
        name = s.get("name", uid)
        lines.append(
            "%s %s(%s) | 新%s 延迟%s 最大延迟%ss 最后命中=%s" % (
                icon, name, uid, s.get("daily_new", 0), delayed,
                s.get("max_delay_today", 0),
                ts_to_str(last_global) if last_global else "从未"
            )
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
    safe_enqueue_notify("B站动态监控 15:30 健康报告", [{"user": "系统雷达", "message": report}], "system")
    STATE.last_report_date = date_str
    state.setdefault("_meta", {})["last_report_date"] = date_str
    mark_state_dirty(state)


def seed_items_to_baseline(state, target_uids, items, baseline):
    feed = state.setdefault("feed", {})
    ids = [str(x.get("id_str")) for x in items if isinstance(x, dict) and x.get("id_str")]
    feed["baseline"] = str(baseline or (ids[0] if ids else feed.get("baseline", "")))
    feed["last_snapshot_ids"] = ids[:RECENT_SNAPSHOT_LIMIT]
    now_ts = int(time.time())
    for item in items:
        if not isinstance(item, dict):
            continue
        _a, uid, name, pub_ts = item_author(item)
        if uid not in target_uids:
            continue
        dyn_id = str(item.get("id_str") or "")
        if not dyn_id:
            continue
        stat = get_uid_stat(state, uid, name)
        remember_uid_id(stat, dyn_id)
        stat["last_global_seen"] = now_ts
        if pub_ts:
            stat["last_pub_ts"] = max(int(stat.get("last_pub_ts", 0)), pub_ts)
        feed.setdefault("discovered", {})[dyn_id] = {
            "first_seen": now_ts,
            "pub_ts": pub_ts,
            "uid": uid,
            "refresh_seq": 0,
            "discovery_mode": "bootstrap",
        }


def initialize_state(target_uids):
    state = load_dynamic_state()
    reset_daily_stats(state, now_cn().strftime("%Y-%m-%d"))
    feed = state.setdefault("feed", {})
    feed.setdefault("last_snapshot_ids", [])
    feed.setdefault("recent_snapshot_history", [])
    feed.setdefault("discovered", {})
    STATE.channel = str(feed.get("active_channel") or "nav")
    if STATE.channel not in ("nav", "all"):
        STATE.channel = "nav"

    logging.info("正在建立关注流启动基线（优先 feed/nav）...")
    data = fetch_nav_page("", feed.get("baseline") or "")
    if data is not None:
        mark_nav_success()
        items = data.get("items") or []
        seed_items_to_baseline(
            state, target_uids, items,
            data.get("update_baseline")
        )
        mark_state_dirty(state)
        save_dynamic_state(state)
        logging.info("启动基线建立完成 channel=nav 首页动态=%s", len(items))
        return state

    mark_nav_failure(state)
    logging.warning("feed/nav 启动失败，尝试 feed/all 建立基线")
    data = fetch_all_page("")
    if data is not None:
        STATE.channel = "all"
        items = data.get("items") or []
        seed_items_to_baseline(
            state, target_uids, items,
            data.get("update_baseline")
        )
        mark_state_dirty(state)
        save_dynamic_state(state)
        logging.info("启动基线建立完成 channel=all 首页动态=%s", len(items))
        return state

    logging.warning("启动基线获取失败，将依赖缓存继续启动")
    return state


def refresh_following_if_due(state, following_list, last_refresh_time):
    now = time.time()
    if now - last_refresh_time < FOLLOWING_REFRESH_INTERVAL:
        return following_list, last_refresh_time
    new_list = get_following_list(SOURCE_UID)
    if new_list is None:
        logging.warning("关注列表本轮刷新失败，保留旧列表")
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
        "关注列表每小时刷新完成 %s → %s UID" % (len(old_set), len(new_set))
        if old_set != new_set else
        "关注列表每小时检查完成，UID=%s 无变化" % len(new_set)
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
        logging.warning("关注列表 %s > %s，截断保护到前%s个", len(live), MAX_MONITOR_UIDS, MAX_MONITOR_UIDS)
        live = live[:MAX_MONITOR_UIDS]
    save_following_cache(live)
    logging.info("关注列表加载完成：%s UID，来源=%s", len(live), source)
    return live


def start_monitoring():
    global IS_RUNNING, ACTIVE_STATE
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    activate_session_cookies()
    if not load_cookies_into_session():
        logging.critical("Cookie 不可用，程序退出")
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
        "启动完成：工作日 %s:%02d-%s:00；主通道 feed/nav；备用 feed/all(连续失败%s次)；"
        "刷新 %.0f~%.0fs；二次确认 %.1f~%.1fs；深扫每%ss；详情仅针对新动态最多%s条",
        RUN_START_HOUR, RUN_START_MINUTE, RUN_END_HOUR,
        NAV_FAIL_THRESHOLD, NORMAL_INTERVAL_MIN, NORMAL_INTERVAL_MAX,
        VERIFY_DELAY_MIN, VERIFY_DELAY_MAX, DEEP_SCAN_INTERVAL, DETAIL_FETCH_MAX
    )

    while IS_RUNNING:
        try:
            now = time.time()
            cn = now_cn()
            reset_daily_stats(state, cn.strftime("%Y-%m-%d"))

            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                feed = state.setdefault("feed", {})
                outbox = feed.setdefault("outbox", {})
                logging.info(
                    "心跳 | channel=%s UID=%s | 主刷新=%.0f~%.0fs "
                    "| 最近成功刷新=%s | Outbox=%s | 连续失败=%s | CookieFail=%s | nav_fail=%s",
                    STATE.channel, len(target_uids),
                    NORMAL_INTERVAL_MIN, NORMAL_INTERVAL_MAX,
                    ts_to_str(feed.get("last_success_refresh", 0)),
                    len(outbox), STATE.consecutive_failures,
                    STATE.consecutive_cookie_failures, STATE.nav_fail
                )
                last_heartbeat = now

            following_list, last_following_refresh = refresh_following_if_due(
                state, following_list, last_following_refresh
            )
            target_uids = set(following_list)

            requeue_due_outbox(state)
            maybe_send_health_report(state, target_uids, cn)

            if not is_in_monitor_window(cn):
                if now - STATE.last_state_save >= STATE_SAVE_INTERVAL:
                    save_dynamic_state(state)
                    STATE.last_state_save = now
                time.sleep(2.0)
                continue

            today = cn.strftime("%Y-%m-%d")
            if STATE.last_checkin_date != today:
                STATE.last_checkin_date = today
                safe_enqueue_notify(
                    "B站动态监控系统打卡上班（发布了新动态）",
                    [{"user": "系统雷达", "message": "%s 工作日监控开始，当前监控 %s 个 UID，主通道 feed/nav。" % (today, len(target_uids))}],
                    "system"
                )

            interval = random_main_interval()
            if now - last_scan >= interval:
                try:
                    has_new, _, _ = full_refresh(
                        target_uids, state, mode="primary", max_pages=1, stop_at_snapshot=True
                    )

                    if has_new and IS_RUNNING:
                        state.setdefault("daily", {})["verify_rounds"] = int(
                            state.setdefault("daily", {}).get("verify_rounds", 0)
                        ) + 1
                        time.sleep(random.uniform(VERIFY_DELAY_MIN, VERIFY_DELAY_MAX))
                        full_refresh(
                            target_uids, state, mode="verify",
                            max_pages=VERIFY_MAX_PAGES, stop_at_snapshot=True
                        )

                    if now - STATE.last_deep_scan >= DEEP_SCAN_INTERVAL and IS_RUNNING:
                        state.setdefault("daily", {})["deep_scans"] = int(
                            state.setdefault("daily", {}).get("deep_scans", 0)
                        ) + 1
                        STATE.last_deep_scan = now
                        logging.info("开始5分钟整体关注流深扫 channel=%s", STATE.channel)
                        full_refresh(
                            target_uids, state, mode="deep",
                            max_pages=DEEP_SCAN_MAX_PAGES,
                            stop_at_snapshot=True,
                        )
                    last_scan = now
                except Exception as e:
                    STATE.consecutive_failures += 1
                    logging.error("关注流扫描异常: %s", repr(e), exc_info=True)

            if now - STATE.last_state_save >= STATE_SAVE_INTERVAL or state.get("_meta", {}).get("dirty"):
                save_dynamic_state(state)
                STATE.last_state_save = now

            time.sleep(0.5)

        except Exception as e:
            if IS_RUNNING:
                logging.error("主循环异常: %s", repr(e), exc_info=True)
                time.sleep(8)
            else:
                break

    save_dynamic_state(state)
    logging.info("状态已安全保存，程序退出。")


if __name__ == "__main__":
    init_logging()
    start_monitoring()
