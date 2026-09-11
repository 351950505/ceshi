# -*- coding: utf-8 -*-
# =============================================================================
# B站关注动态监控  v3.3.0（feed/all｜低漏报可靠优化版）
# -----------------------------------------------------------------------------
# 本版相对 v3.2.1 的改动（保持架构不变，重点降低漏报/误图/重复）：
#   [FIX-1] is_allowed_dynamic：转发动态 / 纯文字动态的 module_dynamic.major 为显式
#           null 时，原代码 dict.get("major", {}) 返回 None → None.get("type") 抛
#           AttributeError → except 返回 False → 动态在类型过滤层被静默丢弃。
#           实测 dyn_id=1246036657638998040（DYNAMIC_TYPE_FORWARD, major=None,
#           is_allowed=False）。现改为：先判 FORWARD、所有中间层 or {}、异常放行。
#   [FIX-2] format_dynamic_message 增加兜底 payload，解析异常不再导致「既不入队
#           也不写 discovered」的永久漏推。
#   [FIX-3] process_feed_items： 
#           - 类型过滤(not_following/type_filtered)增加 INFO 汇总，不再静默；
#           - 增加 SENT_ACK_IDS 兜底，堵住「ACK 已落盘但 state 保存失败」的重复推送；
#           - item["modules"] 为 null 时不再抛 AttributeError 打断整轮扫描。
#   [FIX-4] full_refresh：[SCAN] done 输出过滤漏斗计数，一眼看出卡在哪一层。
#   [FIX-5] atomic_write_json 增加 fsync + 临时文件清理（掉电时 Outbox/ACK 不丢）。
#   [FIX-6] initialize_state：启动基线不再把「当前不支持的动态类型」登记成
#           discovered/baseline，避免修好类型过滤后仍被 already_discovered 拦掉。
#   [FIX-7] 动态格式最终兜底：未知 top-level / major 类型不再直接过滤；pub_ts 缺失时使用发现时间继续处理，
#           解析失败仍走最小 payload，保证至少推送动态直达链接。
#   未改动（按你的要求）：运行窗口 RUN_WEEKDAYS / 24小时运行问题保持原样。
# =============================================================================
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
SENT_ACK_FILE = "sent_ack.jsonl"  # 发送成功立即落盘，防重启重复推
DEAD_LETTER_FILE = "outbox_dead.jsonl"  # outbox 淘汰/超限死信
SENT_ACK_MAX_BYTES = 256 * 1024       # 运行中超限裁剪，适配 128MB 磁盘
DEAD_LETTER_MAX_BYTES = 256 * 1024

# 日志级别：INFO=只记录关键事件（默认）；排查漏报/验收时临时改为 logging.DEBUG
LOG_LEVEL = logging.INFO

# 运行窗口（Asia/Shanghai）
RUN_TZ = "Asia/Shanghai"
RUN_WEEKDAYS = {0, 1, 2, 3, 4}
RUN_START_HOUR = 0
RUN_START_MINUTE = 0
RUN_END_HOUR = 24

# =========================================================
# 关注流刷新参数（仅 feed/all）
# =========================================================
NORMAL_INTERVAL_MIN = 20.0
NORMAL_INTERVAL_MAX = 35.0

VERIFY_DELAY_MIN = 1.5
VERIFY_DELAY_MAX = 3.0
VERIFY_MAX_PAGES = 6

DEEP_SCAN_INTERVAL = 300
DEEP_SCAN_MAX_PAGES = 20
DEEP_SCAN_STOP_STABLE_PAGES = 2

DYNAMIC_NEW_WINDOW = 6 * 3600        # 历史动态最大年龄
RECENT_FORCE_NEW_WINDOW = 10 * 60    # 近 N 秒强制视为新（防污染漏报）
# 启动时：无 last_success_refresh 时，最多恢复这么久以内的动态，避免冷启动误推全历史。
# 有 last_success_refresh 时：凡 pub_ts > last_ok 均视为停机窗口内新动态（不设年龄上限）。
STARTUP_RECOVER_MAX_AGE = 6 * 3600

STATE_SAVE_INTERVAL = 60
RECENT_SNAPSHOT_LIMIT = 400
SEEN_DYNAMIC_LIMIT = 12000
RECENT_PUSHED_IDS_LIMIT = 12000
OUTBOX_MAX = 500
OUTBOX_EXPIRE_SECONDS = 14 * 24 * 3600
OUTBOX_MAX_ATTEMPTS = 80

NOTIFY_QUEUE_MAXSIZE = 100
NOTIFY_SEND_DELAY = 2.0
NOTIFY_RETRY_BASE = 60
NOTIFY_RETRY_MAX = 1800

REQUEST_TIMEOUT = 12
REQUEST_RETRIES = 3
WBI_REFRESH_INTERVAL = 21600
WBI_
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
ACK_LOCK = threading.Lock()  # sent_ack 追加/裁剪互斥
DEAD_LETTER_LOCK = threading.Lock()
SENT_ACK_IDS = set()  # 运行期 ACK 内存镜像，有上限
SENT_ACK_ORDER = []   # 与 SENT_ACK_IDS 配套，用于淘汰最旧


_last_notify_time = {}
WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}

# 项目基准：不使用 requests.Session()。
# 每次请求都使用 requests.get，并显式传 Cookie/Header，避免长连接/连接池在
# Alpine 长时间运行后出现连接复用异常，同时保留与当前 Cookie 行为一致的能力。
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
        "Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "close",
}
COOKIE_JAR = {}

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
    """原子写。[FIX-5] 增加 fsync：掉电时 Outbox/ACK 等关键状态不会停留在 page cache。"""
    backup_path = path + ".bak"
    tmp_path = path + ".write.tmp"
    if os.path.exists(path):
        try:
            shutil.copy2(path, backup_path)
        except Exception:
            pass
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise


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
        return datetime.datetime.fromtimestamp(ts, tz=ZoneInfo(RUN_TZ)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except Exception:
        try:
            return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return "未知时间"


def _safe_int(v, default=0):
    try:
        if v is None or v is False:
            return default
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return default
            # 兼容 "123.0"
            if "." in v or "e" in v.lower():
                return int(float(v))
            return int(v)
        if isinstance(v, float):
            return int(v)
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(v, default=0.0):
    try:
        if v is None or v is False:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


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
    """生成少量非认证 Cookie，作为普通 requests.get 的显式 Cookie。"""
    try:
        uuid_sec = str(uuid.uuid4())
        time_sec = str(int(time.time() * 1000 % 1e5)).ljust(5, "0")
        COOKIE_JAR["_uuid"] = f"{uuid_sec}{time_sec}infoc"
        COOKIE_JAR["CURRENT_FNVAL"] = "4048"
        COOKIE_JAR["blackside_state"] = "1"
        logging.debug("B站请求 Cookie 环境初始化完成")
        return True
    except Exception as e:
        logging.debug(f"请求 Cookie 环境初始化失败: {e}")
        return False

def load_cookies_into_session():
    """兼容旧函数名；实际把 bili_cookie.txt 解析到 COOKIE_JAR。"""
    try:
        # 保留 activate_session_cookies() 生成的匿名 Cookie，再覆盖认证 Cookie。
        if not os.path.exists("bili_cookie.txt"):
            logging.error("❌ 未找到 bili_cookie.txt")
            return False
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie_str = f.read().strip()
        if not cookie_str:
            logging.error("❌ bili_cookie.txt 为空")
            return False
        count = 0
        for item in cookie_str.split(";"):
            item = item.strip()
            if not item or "=" not in item:
                continue
            k, v = item.split("=", 1)
            k, v = k.strip(), v.strip()
            if not k:
                continue
            COOKIE_JAR[k] = v
            count += 1
        logging.info(f"[AUTH] cookie loaded ({count})")
        return count > 0
    except Exception as e:
        logging.error(f"❌ 加载 Cookie 异常: {e}")
        return False

def init_logging():
    root = logging.getLogger()
    if root.hasHandlers():
        root.handlers.clear()
    formatter = logging.Formatter("[BILI] %(asctime)s [%(levelname)s] %(message)s")
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=2,
        encoding="utf-8", delay=True
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)
    if sys.stdout.isatty():
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        root.addHandler(stream)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    root.setLevel(LOG_LEVEL)
    root.propagate = False
    logging.info("=" * 70)
    logging.info("B站关注动态监控 v3.3.0（feed/all｜低漏报可靠优化版）")
    logging.info("=" * 70)

def force_update_wbi_keys():
    try:
        r = requests.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers=REQUEST_HEADERS,
            cookies=COOKIE_JAR,
            timeout=8,
        )
        try:
            data = r.json()
        finally:
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
        logging.info("[WBI] keys refreshed")
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
    # 服务器时间存在偏差时，沿用当前项目已验证的 -120s 修正。
    params["wts"] = int(time.time()) + int(WBI_TIME_OFFSET)
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
    params = dict(params or {})
    last = {"code": -500, "message": "unknown"}
    for i in range(max(1, retries)):
        try:
            # 每次独立连接，符合当前项目基准；Connection: close 防长连接污染。
            resp = requests.get(
                url,
                params=params,
                headers=REQUEST_HEADERS,
                cookies=COOKIE_JAR,
                timeout=REQUEST_TIMEOUT,
            )
            status_code = int(getattr(resp, "status_code", 0) or 0)
            try:
                data = resp.json()
            except Exception:
                body = (resp.text or "")[:300]
                data = {"code": -500, "message": f"invalid_json http={status_code} body={body}"}
            finally:
                resp.close()

            if not isinstance(data, dict):
                data = {"code": -500, "message": "invalid_response"}
            last = data
            code = data.get("code")

            if code == -101:
                STATE.consecutive_cookie_failures += 1
                logging.error(f"❌ Cookie 验证失败 {STATE.consecutive_cookie_failures}/3")
                notify_system_once(
                    "❌ B站 Cookie 失效预警",
                    "Cookie 验证失败，请检查 bili_cookie.txt。"
                )
                if STATE.consecutive_cookie_failures >= 3:
                    logging.critical("🛑 Cookie 连续失效，停止程序。")
                    globals()["IS_RUNNING"] = False
                return data

            STATE.consecutive_cookie_failures = 0

            rate_limited = code in (-799, -352, -509, -412) or status_code in (412, 429)
            if rate_limited:
                # 风控情况下最后一次也不再无意义等待；下一层扫描会自然重试。
                if i >= retries - 1:
                    logging.warning(
                        f"⚠️ B站风控/限流最终失败 code={code} http={status_code}"
                    )
                    break
                wait = min(300.0, 15.0 * (2 ** i)) + random.uniform(3, 8)
                logging.warning(
                    f"⚠️ B站风控/限流 code={code} http={status_code}，退避 {wait:.1f}s"
                )
                if i == 0:
                    force_update_wbi_keys()
                notify_system_once(
                    "🚨 B站风控预警",
                    f"code={code}, http={status_code}，已自动退避 {wait:.1f} 秒。"
                )
                time.sleep(wait)
                continue

            if code == 0:
                return data

            if i < retries - 1:
                wait = min(30.0, 3.0 * (2 ** i)) + random.uniform(1, 3)
                logging.warning(f"[API重试] code={code} wait={wait:.1f}s url={url}")
                time.sleep(wait)
            else:
                return data

        except requests.RequestException as e:
            last = {"code": -500, "message": repr(e)}
            if i < retries - 1:
                wait = min(30.0, 3.0 * (2 ** i)) + random.uniform(1, 3)
                logging.warning(f"[网络重试] {repr(e)} wait={wait:.1f}s")
                time.sleep(wait)
        except Exception as e:
            last = {"code": -500, "message": repr(e)}
            logging.error(f"[请求异常] {url}: {repr(e)}")
            break

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
        "version": 7,
        "feed": {
            "baseline": "",
            "last_snapshot_ids": [],
            "recent_snapshot_history": [],
            "recent_pushed_ids": [],
            "discovered": {},
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


def _sanitize_id_list(raw, limit):
    """强制去重 + 限长，清理状态污染。"""
    if not isinstance(raw, list):
        return []
    seen = set()
    out = []
    for x in raw:
        s = str(x).strip() if x is not None else ""
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _sanitize_discovered(raw):
    """清理 discovered 字典：只保留合法结构，限长。"""
    if not isinstance(raw, dict):
        return {}
    cleaned = {}
    for k, v in raw.items():
        dyn_id = str(k).strip()
        if not dyn_id:
            continue
        if not isinstance(v, dict):
            # 兼容旧版可能只存时间戳的情况
            cleaned[dyn_id] = {
                "first_seen": _safe_int(v, 0),
                "pub_ts": 0,
                "uid": "",
                "refresh_seq": 0,
                "discovery_mode": "",
                "status": "baseline",
            }
            continue
        cleaned[dyn_id] = {
            "first_seen": _safe_int(v.get("first_seen"), 0),
            "pub_ts": _safe_int(v.get("pub_ts"), 0),
            "uid": str(v.get("uid", "") or ""),
            "refresh_seq": _safe_int(v.get("refresh_seq"), 0),
            "discovery_mode": str(v.get("discovery_mode", "") or ""),
            "time_fallback": bool(v.get("time_fallback", False)),
            "status": str(v.get("status", "baseline") or "baseline"),
        }
        if cleaned[dyn_id]["status"] not in {"baseline", "queued", "retry", "sent"}:
            cleaned[dyn_id]["status"] = "baseline"
    if len(cleaned) > SEEN_DYNAMIC_LIMIT:
        items = sorted(
            cleaned.items(),
            key=lambda kv: _safe_int((kv[1] or {}).get("first_seen"), 0),
            reverse=True,
        )[:SEEN_DYNAMIC_LIMIT]
        cleaned = dict(items)
    return cleaned


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
    # 顶层结构非法时整体回退，避免后续 KeyError/AttributeError
    if not isinstance(state, dict):
        return default_state()
    for k, v in base.items():
        if k not in state or type(state.get(k)) != type(v):
            # feed/daily/uid_stats 类型不对时直接重置该键
            if k in ("feed", "daily", "uid_stats", "_meta") and not isinstance(state.get(k), dict):
                state[k] = v
            elif k not in state:
                state[k] = v
    if not isinstance(state.get("feed"), dict):
        state["feed"] = dict(base["feed"])
    if not isinstance(state.get("daily"), dict):
        state["daily"] = dict(base["daily"])
    if not isinstance(state.get("uid_stats"), dict):
        state["uid_stats"] = {}
    if not isinstance(state.get("_meta"), dict):
        state["_meta"] = {"dirty": False}

    for k, v in base["feed"].items():
        state["feed"].setdefault(k, v)
    for k, v in base["daily"].items():
        state["daily"].setdefault(k, v)
    state.setdefault("uid_stats", {})

    # 启动时自动修复状态污染（去重、限长、清理非法结构）
    feed = state.setdefault("feed", {})
    before_snap = len(feed.get("last_snapshot_ids") or [])
    before_pushed = len(feed.get("recent_pushed_ids") or [])
    before_disc = len(feed.get("discovered") or {})
    feed["last_snapshot_ids"] = _sanitize_id_list(feed.get("last_snapshot_ids"), RECENT_SNAPSHOT_LIMIT)
    feed["recent_pushed_ids"] = _sanitize_id_list(feed.get("recent_pushed_ids"), RECENT_PUSHED_IDS_LIMIT)
    feed["discovered"] = _sanitize_discovered(feed.get("discovered"))
    before_outbox = len(feed.get("outbox") or {})
    feed["outbox"] = _sanitize_outbox(feed.get("outbox"), emit_dead=True)
    after_snap = len(feed["last_snapshot_ids"])
    after_pushed = len(feed["recent_pushed_ids"])
    after_disc = len(feed["discovered"])
    after_outbox = len(feed["outbox"])
    if (
        before_snap != after_snap
        or before_pushed != after_pushed
        or before_disc != after_disc
        or before_outbox != after_outbox
    ):
        logging.warning(
            f"🧹 状态自动修复: snapshot {before_snap}→{after_snap}, "
            f"pushed {before_pushed}→{after_pushed}, discovered {before_disc}→{after_disc}, "
            f"outbox {before_outbox}→{after_outbox}"
        )
        state.setdefault("_meta", {})["dirty"] = True

    # v6: discovered 明确记录状态；旧状态仅补 baseline，不改变历史推送语义。
    state["version"] = 7

    for uid, info in list(state.get("uid_stats", {}).items()):
        if not isinstance(info, dict):
            state["uid_stats"].pop(uid, None)
            continue
        info["seen_ids"] = _sanitize_id_list(info.get("seen_ids"), 500)
        for nk in (
            "daily_new", "daily_delayed", "daily_verify_recovered", "daily_deep_recovered",
            "total_seen", "last_pub_ts", "last_first_seen", "last_global_seen", "max_delay_today",
        ):
            info[nk] = _safe_int(info.get(nk), 0)
        info["name"] = str(info.get("name") or uid)

    return state


def save_dynamic_state(state):
    if not state:
        return
    with STATE_LOCK:
        feed = state.setdefault("feed", {})
        feed["last_snapshot_ids"] = _sanitize_id_list(feed.get("last_snapshot_ids"), RECENT_SNAPSHOT_LIMIT)
        feed["recent_pushed_ids"] = _sanitize_id_list(feed.get("recent_pushed_ids"), RECENT_PUSHED_IDS_LIMIT)
        feed["discovered"] = _sanitize_discovered(feed.get("discovered"))
        history = feed.get("recent_snapshot_history", []) or []
        feed["recent_snapshot_history"] = history[-20:] if isinstance(history, list) else []
        retry = feed.get("push_retry_after", {}) or {}
        now = time.time()
        cleaned_retry = {}
        if isinstance(retry, dict):
            for k, v in retry.items():
                ts = _safe_float(v, 0.0)
                if ts > now:
                    cleaned_retry[str(k)] = ts
        feed["push_retry_after"] = cleaned_retry

        feed["outbox"] = _sanitize_outbox(feed.get("outbox"), emit_dead=False)

        for uid, info in list(state.get("uid_stats", {}).items()):
            if isinstance(info, dict):
                info["seen_ids"] = _sanitize_id_list(info.get("seen_ids"), 500)
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
    # 安全上限20：拿到超过20个目标即可拒绝覆盖，避免无意义翻100页。
    max_pages = max(1, (MAX_MONITOR_UIDS // ps) + 1)
    for pn in range(1, max_pages + 1):
        data = safe_request("https://api.bilibili.com/x/relation/followings", {
            "vmid": uid, "pn": pn, "ps": ps,
            "order": "desc", "order_type": "attention",
        })
        if data.get("code") != 0:
            logging.warning(f"关注列表刷新失败 page={pn} code={data.get('code')}")
            return None
        items = (data.get("data") or {}).get("list") or []
        for item in items:
            if isinstance(item, dict) and item.get("mid") is not None:
                result.append(str(item["mid"]))
        result = list(dict.fromkeys(result))
        if len(result) > MAX_MONITOR_UIDS:
            notify_system_once("⚠️ 关注列表超限", f"检测到超过 {MAX_MONITOR_UIDS} 个关注目标，保留旧列表。")
            return None
        if len(items) < ps:
            break
        if pn < max_pages:
            time.sleep(random.uniform(0.4, 0.8))
    return result or None


# =========================================================
# 动态解析
# =========================================================
def is_allowed_dynamic(item):
    """动态类型采用“保守放行”策略：已知明确支持类型直接通过，未知格式不静默丢弃。

    原因：feed/all 的动态结构会变化，曾出现 major=None 导致类型判断异常而静默漏报。
    当前原则是“解析不了也要推链接”，避免新类型再次形成静默漏报。
    """
    try:
        top_type = str(item.get("type") or "")
        modules = item.get("modules") or {}
        dyn = modules.get("module_dynamic") or {}
        major = dyn.get("major") or {}
        major_type = str(major.get("type") or "")

        if top_type == "DYNAMIC_TYPE_FORWARD":
            return ALLOW_FORWARD_DYNAMIC

        # 已知类型照常放行。未知 top-level / major 类型也放行，交给正文解析兜底。
        if top_type and top_type not in ALLOWED_TOP_LEVEL_TYPES:
            logging.debug(
                "[FORMAT] unknown top_type=%s dyn_id=%s -> allow",
                top_type, item.get("id_str", "-")
            )
            return True

        if major_type and major_type not in ALLOWED_DYNAMIC_TYPES:
            logging.debug(
                "[FORMAT] unknown major_type=%s dyn_id=%s -> allow",
                major_type, item.get("id_str", "-")
            )
            return True

        return True
    except Exception as e:
        logging.debug(
            "[FORMAT] type-check exception dyn_id=%s type=%s -> allow: %s",
            item.get("id_str") if isinstance(item, dict) else "-",
            item.get("type") if isinstance(item, dict) else "-", repr(e),
        )
        return True


def _extract_rich_text(nodes):
    parts = []
    if not isinstance(nodes, list):
        return ""
    allowed = {
        "RICH_TEXT_NODE_TYPE_TEXT",
        "RICH_TEXT_NODE_TYPE_TOPIC",
        "RICH_TEXT_NODE_TYPE_AT",
        "RICH_TEXT_NODE_TYPE_EMOJI",
        "RICH_TEXT_NODE_TYPE_LOTTERY",
        "RICH_TEXT_NODE_TYPE_LINK",
        "RICH_TEXT_NODE_TYPE_BV",
        "RICH_TEXT_NODE_TYPE_AV",
        "RICH_TEXT_NODE_TYPE_GOODS",
    }
    for node in nodes:
        if not isinstance(node, dict):
            continue
        text = node.get("text")
        ntype = str(node.get("type") or "")
        if isinstance(text, str) and (not ntype or ntype in allowed):
            parts.append(text)
    return normalize_text("".join(parts))


def _extract_paragraph_text(paragraphs):
    if not isinstance(paragraphs, list):
        return ""
    parts = []
    for paragraph in paragraphs:
        if not isinstance(paragraph, dict):
            continue
        nodes = paragraph.get("text") or paragraph.get("rich_text_nodes") or paragraph.get("nodes")
        if isinstance(nodes, list):
            text = _extract_rich_text(nodes)
            if text:
                parts.append(text)
        elif isinstance(nodes, str):
            parts.append(nodes)
    return normalize_text("\n".join(parts))


def extract_dynamic_text(item):
    try:
        modules = item.get("modules") or {}
        dyn = modules.get("module_dynamic") or {}
        desc = dyn.get("desc") or {}

        text = _extract_rich_text(desc.get("rich_text_nodes"))
        if text:
            return text

        raw_desc_text = normalize_text(desc.get("text", ""))
        major = dyn.get("major") or {}
        t = str(major.get("type") or "")

        if t == "MAJOR_TYPE_ARCHIVE":
            a = major.get("archive") or {}
            title = normalize_text(a.get("title", ""))
            desc_text = normalize_text(a.get("desc", ""))
            return "\n".join(x for x in (f"【视频】{title}" if title else "", desc_text) if x).strip()

        if t == "MAJOR_TYPE_ARTICLE":
            a = major.get("article") or {}
            title = normalize_text(a.get("title", ""))
            desc_text = normalize_text(a.get("desc", ""))
            return "\n".join(x for x in (f"【专栏】{title}" if title else "", desc_text) if x).strip()

        if t == "MAJOR_TYPE_OPUS":
            opus = major.get("opus") or {}
            title = normalize_text(opus.get("title", ""))
            summary = opus.get("summary") or {}
            text = _extract_rich_text(summary.get("rich_text_nodes"))
            if not text:
                text = _extract_paragraph_text(summary.get("paragraphs"))
            if not text:
                text = _extract_paragraph_text(opus.get("paragraphs"))
            if not text:
                text = normalize_text(opus.get("desc", ""))
            if title and text:
                return f"【图文】{title}\n{text}".strip()
            if title:
                return f"【图文】{title}"
            return text

        if t == "MAJOR_TYPE_DRAW":
            return raw_desc_text or normalize_text(desc.get("text", "")) or "【图片动态】"

        if t == "MAJOR_TYPE_COMMON":
            common = major.get("common") or {}
            title = normalize_text(common.get("title", ""))
            desc_text = normalize_text(common.get("desc", ""))
            return "\n".join(x for x in (f"【卡片】{title}" if title else "", desc_text) if x).strip()

        if t == "MAJOR_TYPE_LIVE":
            live = major.get("live") or {}
            title = normalize_text(live.get("title", ""))
            desc_text = normalize_text(live.get("desc_second", "") or live.get("desc_first", ""))
            return "\n".join(x for x in (f"【直播】{title}" if title else "", desc_text) if x).strip()

        # 未知格式：只从明确正文描述字段兜底，不递归整棵 JSON，避免 UI 文案污染。
        if raw_desc_text:
            return raw_desc_text

        for key in ("title", "desc", "description", "summary"):
            value = major.get(key) if isinstance(major, dict) else None
            if isinstance(value, str):
                value = normalize_text(value)
                if value:
                    return value

        return ""
    except Exception:
        return ""

def _normalize_image_url(url):
    if not isinstance(url, str):
        return ""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith(("http://", "https://")):
        return ""
    return url


def extract_images(item, _forward_depth=0):
    """严格按动态正文字段提图；禁止递归整棵 JSON。"""
    urls = []

    def add(url):
        url = _normalize_image_url(url)
        if url and url not in urls:
            urls.append(url)

    try:
        if not isinstance(item, dict):
            return []

        modules = item.get("modules") or {}
        dyn = modules.get("module_dynamic") or {}
        major = dyn.get("major") or {}
        major_type = str(major.get("type") or "")

        if major_type == "MAJOR_TYPE_DRAW":
            draw = major.get("draw") or {}
            for obj in draw.get("items") or []:
                if isinstance(obj, dict):
                    add(obj.get("src"))

        elif major_type == "MAJOR_TYPE_OPUS":
            opus = major.get("opus") or {}
            for obj in opus.get("pics") or []:
                if isinstance(obj, str):
                    add(obj)
                elif isinstance(obj, dict):
                    # opus.pics 是正文图片白名单字段，只取 url/src。
                    add(obj.get("url") or obj.get("src"))

        # ARCHIVE / ARTICLE / LIVE / COMMON 均不默认提图：
        # cover、thumbnail、jump_url、source_url、头像等都不是正文图片。

        # 转发动态：只递归进入“原动态”这个明确的数据节点，再按原动态自身规则提图。
        if str(item.get("type") or "") == "DYNAMIC_TYPE_FORWARD" and _forward_depth < 1:
            orig = item.get("orig")
            if isinstance(orig, dict):
                for u in extract_images(orig, _forward_depth + 1):
                    add(u)

    except Exception as e:
        logging.debug(f"[IMAGE] 提取异常 dyn_id={item.get('id_str', '-')}: {repr(e)}")

    return urls[:12]

def _minimal_push_payload(item, err="", fallback_ts=0):
    """解析彻底失败时仍生成最小消息，保证动态直达链接不因解析异常而消失。"""
    item = item if isinstance(item, dict) else {}
    modules = item.get("modules") or {}
    author = modules.get("module_author") or {}
    dyn_id = str(item.get("id_str") or "")
    top_type = str(item.get("type") or "")
    pub_ts = _safe_int(author.get("pub_ts"), 0) or _safe_int(fallback_ts, 0) or int(time.time())
    return {
        "user": str(author.get("name") or "未知UP"),
        "uid": str(author.get("mid") or ""),
        "message": f"（正文解析失败，已降级推送｜类型={top_type or '未知'}｜{str(err)[:120]}）",
        "time": ts_to_str(pub_ts),
        "link": f"https://t.bilibili.com/{dyn_id}" if dyn_id else "",
        "cover": "",
        "covers": [],
        "images": [],
        "image_count": 0,
        "kind": "dynamic",
    }

def format_dynamic_message(item):
    """[FIX-2] 全程兜底：任何解析异常都退化为最小载荷，而不是抛出导致漏推。"""
    try:
        dyn_id = str(item.get("id_str") or "")
        modules = item.get("modules") or {}
        author = modules.get("module_author") or {}
        name = author.get("name", "未知UP")
        uid = str(author.get("mid", ""))
        pub_ts = _safe_int(author.get("pub_ts"), 0)
        if pub_ts <= 0:
            pub_ts = int(time.time())

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
    except Exception as e:
        logging.warning(
            f"[FORMAT] 解析异常，降级推送 dyn_id="
            f"{item.get('id_str') if isinstance(item, dict) else '-'}: {repr(e)}"
        )
        return _minimal_push_payload(item if isinstance(item, dict) else {}, repr(e))


# =========================================================
# UID 统计（仅统计整体关注流命中情况，不做单UID请求）
# =========================================================
def get_uid_stat(state, uid, name=""):
    uid = str(uid)
    root = state.setdefault("uid_stats", {})
    info = root.setdefault(uid, {})
    if name:
        info["name"] = str(name)
    else:
        info.setdefault("name", uid)
    for nk, dv in (
        ("daily_new", 0), ("daily_delayed", 0), ("daily_verify_recovered", 0),
        ("daily_deep_recovered", 0), ("total_seen", 0), ("last_pub_ts", 0),
        ("last_first_seen", 0), ("last_global_seen", 0), ("max_delay_today", 0),
    ):
        info[nk] = _safe_int(info.get(nk, dv), dv)
    if not isinstance(info.get("seen_ids"), list):
        info["seen_ids"] = []
    return info


def remember_uid_id(uid_stat, dyn_id):
    ids = list(uid_stat.get("seen_ids", []) or [])
    if dyn_id in ids:
        ids.remove(dyn_id)
    ids.insert(0, dyn_id)
    uid_stat["seen_ids"] = ids[:500]


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


def _remember_ack_id_locked(dyn_id):
    """ACK 落盘成功后调用；保持与 RECENT_PUSHED_IDS_LIMIT 一致。调用方持 ACK_LOCK。"""
    dyn_id = str(dyn_id or "")
    if not dyn_id:
        return
    if dyn_id in SENT_ACK_IDS:
        return
    SENT_ACK_IDS.add(dyn_id)
    SENT_ACK_ORDER.append(dyn_id)
    overflow = len(SENT_ACK_ORDER) - RECENT_PUSHED_IDS_LIMIT
    if overflow > 0:
        for old in SENT_ACK_ORDER[:overflow]:
            SENT_ACK_IDS.discard(old)
        del SENT_ACK_ORDER[:overflow]


def _reset_ack_ids_locked(ids):
    """用最近 N 个 ID 重建内存 ACK。调用方持 ACK_LOCK。"""
    seen = []
    have = set()
    for x in ids:
        x = str(x or "")
        if not x or x in have:
            continue
        have.add(x)
        seen.append(x)
    if len(seen) > RECENT_PUSHED_IDS_LIMIT:
        drop = seen[:-RECENT_PUSHED_IDS_LIMIT]
        for x in drop:
            have.discard(x)
        seen = seen[-RECENT_PUSHED_IDS_LIMIT:]
    SENT_ACK_IDS.clear()
    SENT_ACK_IDS.update(have)
    SENT_ACK_ORDER[:] = seen


def _maybe_trim_ack_file_locked():
    """调用方必须已持有 ACK_LOCK。仅按大小裁剪，不在读路径 replace。"""
    try:
        if not os.path.exists(SENT_ACK_FILE):
            return
        size = os.path.getsize(SENT_ACK_FILE)
        if size <= SENT_ACK_MAX_BYTES:
            return
        keep = RECENT_PUSHED_IDS_LIMIT
        with open(SENT_ACK_FILE, "rb") as f:
            f.seek(-min(size, keep * 64), os.SEEK_END)
            data = f.read()
        nl = data.find(b"\n")
        if nl >= 0:
            data = data[nl + 1:]
        tmp = SENT_ACK_FILE + ".acktrim.tmp"
        with open(tmp, "wb") as wf:
            wf.write(data)
        os.replace(tmp, SENT_ACK_FILE)
        kept = _parse_ack_lines(data.decode("utf-8", errors="ignore").splitlines())
        _reset_ack_ids_locked(kept)
        logging.info(f"[ACK] trim ids={len(SENT_ACK_IDS)}")
    except Exception as e:
        logging.warning(f"运行中裁剪 sent_ack 失败: {e}")


def append_sent_ack(dyn_id):
    """Webhook 成功后立即追加落盘。仅写入成功后才进入 SENT_ACK_IDS。"""
    dyn_id = str(dyn_id or "").strip()
    if not dyn_id:
        return False
    line = f"{int(time.time())}\t{dyn_id}\n"
    with ACK_LOCK:
        persisted = False
        try:
            with open(SENT_ACK_FILE, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            persisted = True
        except Exception as e:
            logging.error(f"sent_ack 写入失败 dyn_id={dyn_id}: {e}")
            try:
                bak = SENT_ACK_FILE + ".pending"
                with open(bak, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
                logging.error(f"sent_ack 已写入备用文件 {bak}")
                persisted = True
            except Exception as e2:
                logging.critical(f"sent_ack 主文件与备用均失败 dyn_id={dyn_id}: {e2}")
                notify_system_once(
                    "🚨 sent_ack 写入全部失败",
                    f"dyn_id={dyn_id} 主文件与 pending 均无法写入，ACK 防线失效。",
                )
                return False
        if persisted:
            _remember_ack_id_locked(dyn_id)
            _maybe_trim_ack_file_locked()
        return persisted


def _parse_ack_lines(lines):
    out = []
    for line in lines:
        parts = line.strip().split("\t")
        if len(parts) >= 2 and parts[-1].strip():
            out.append(parts[-1].strip())
        elif len(parts) == 1 and parts[0].strip():
            out.append(parts[0].strip())
    return out


def _read_sent_ack_tail(limit=RECENT_PUSHED_IDS_LIMIT, do_trim=False):
    """只读尾部；do_trim=True 仅启动时裁剪，运行中读取不 replace。"""
    ids = []

    def _read_one(path, allow_trim=False):
        if not os.path.exists(path):
            return []
        try:
            size = os.path.getsize(path)
            max_bytes = max(65536, limit * 64)
            with open(path, "rb") as f:
                if size > max_bytes:
                    f.seek(-max_bytes, os.SEEK_END)
                    data = f.read()
                    nl = data.find(b"\n")
                    if nl >= 0:
                        data = data[nl + 1:]
                else:
                    data = f.read()
            lines = data.decode("utf-8", errors="ignore").splitlines()
            if allow_trim and size > max_bytes * 2 and path == SENT_ACK_FILE:
                try:
                    tail_lines = lines[-limit:]
                    tmp = path + ".acktrim.tmp"
                    with open(tmp, "w", encoding="utf-8") as wf:
                        for line in tail_lines:
                            wf.write(line.rstrip() + "\n")
                    os.replace(tmp, path)
                    logging.info(f"[ACK] startup trim rows={len(tail_lines)}")
                except Exception as e:
                    logging.warning(f"sent_ack 裁剪失败: {e}")
            return _parse_ack_lines(lines[-limit:])
        except Exception as e:
            logging.warning(f"读取 {path} 失败: {e}")
            return []

    with ACK_LOCK:
        ids.extend(_read_one(SENT_ACK_FILE, allow_trim=do_trim))
        pending_path = SENT_ACK_FILE + ".pending"
        pending_ids = _read_one(pending_path, allow_trim=False)
        if pending_ids:
            ids.extend(pending_ids)
            try:
                with open(SENT_ACK_FILE, "a", encoding="utf-8") as f:
                    for dyn_id in pending_ids:
                        f.write(f"{int(time.time())}\t{dyn_id}\n")
                    f.flush()
                    os.fsync(f.fileno())
            except Exception as e:
                logging.warning(f"合并 pending 写入主文件失败: {e}")
            else:
                try:
                    os.remove(pending_path)
                    logging.info(f"📥 已合并 sent_ack.pending {len(pending_ids)} 条")
                except Exception as e:
                    logging.warning(
                        f"pending 已写入主文件但删除失败（下次可能重复合并，可安全忽略）: {e}"
                    )
        _reset_ack_ids_locked(ids)
    seen = set()
    uniq = []
    for x in ids:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq[-limit:]



def load_sent_acks_into_state(state, limit=RECENT_PUSHED_IDS_LIMIT):
    """启动时把 sent_ack 尾部合并进 recent_pushed_ids。"""
    ids = _read_sent_ack_tail(limit, do_trim=True)
    n = 0
    for dyn_id in ids:
        if not is_recent_pushed(state, dyn_id):
            add_recent_pushed(state, dyn_id)
            n += 1
    return n


def get_recent_pushed_set(state):
    return set(state.setdefault("feed", {}).get("recent_pushed_ids", []) or [])


def is_recent_pushed(state, dyn_id, pushed_set=None):
    if pushed_set is not None:
        return str(dyn_id) in pushed_set
    return str(dyn_id) in get_recent_pushed_set(state)


def safe_enqueue_notify(title, items, notify_type="dynamic", dyn_id="", uid="", pub_ts=0, first_seen=0, discovery_mode="primary"):
    dyn_id = str(dyn_id or "")
    if notify_type == "dynamic" and dyn_id:
        if ACTIVE_STATE is None:
            logging.error(f"safe_enqueue_notify: ACTIVE_STATE 未初始化，拒绝动态入队 dyn_id={dyn_id}")
            return False
        with STATE_LOCK:
            # [FIX-3] SENT_ACK_IDS 兜底：ACK 已落盘但 state 保存失败时不再重复入队
            if dyn_id in PENDING_PUSH_IDS or dyn_id in SENT_ACK_IDS or is_recent_pushed(ACTIVE_STATE, dyn_id):
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
            # 先落盘再入队；落盘失败则回滚，避免永久 PENDING
            try:
                save_dynamic_state(ACTIVE_STATE)
            except Exception as se:
                logging.error(f"入队前状态保存失败 dyn_id={dyn_id}: {repr(se)}")
                outbox.pop(dyn_id, None)
                PENDING_PUSH_IDS.discard(dyn_id)
                mark_state_dirty(ACTIVE_STATE)
                return False
        try:
            notify_queue.put_nowait(task)
            return True
        except queue.Full:
            with STATE_LOCK:
                ACTIVE_STATE.get("feed", {}).get("outbox", {}).pop(dyn_id, None)
                PENDING_PUSH_IDS.discard(dyn_id)
                mark_state_dirty(ACTIVE_STATE)
                try:
                    save_dynamic_state(ACTIVE_STATE)
                except Exception:
                    pass
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



def _maybe_trim_text_file(path, max_bytes):
    """将追加型文本文件裁到约 max_bytes（保留尾部）。"""
    try:
        if not os.path.exists(path):
            return
        size = os.path.getsize(path)
        if size <= max_bytes:
            return
        with open(path, "rb") as f:
            f.seek(-max_bytes, os.SEEK_END)
            data = f.read()
        nl = data.find(b"\n")
        if nl >= 0:
            data = data[nl + 1:]
        tmp = path + ".dltrim.tmp"
        with open(tmp, "wb") as wf:
            wf.write(data)
        os.replace(tmp, path)
        logging.info(f"🧹 裁剪 {path} {size}B → {len(data)}B")
    except Exception as e:
        logging.warning(f"裁剪 {path} 失败: {e}")


def append_dead_letter(reason, tasks):
    """将被淘汰的 outbox 任务写入死信文件并系统告警（节流）。"""
    if not tasks:
        return
    lines = []
    ids = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        dyn_id = str(t.get("dyn_id") or "")
        ids.append(dyn_id)
        try:
            lines.append(json.dumps({
                "reason": reason,
                "ts": int(time.time()),
                "dyn_id": dyn_id,
                "uid": str(t.get("uid") or ""),
                "attempt": _safe_int(t.get("attempt"), 0),
                "created_at": _safe_float(t.get("created_at"), 0),
                "title": str(t.get("title") or "")[:120],
            }, ensure_ascii=False))
        except Exception:
            lines.append(json.dumps({"reason": reason, "dyn_id": dyn_id, "ts": int(time.time())}, ensure_ascii=False))
    try:
        with DEAD_LETTER_LOCK:
            with open(DEAD_LETTER_FILE, "a", encoding="utf-8") as f:
                for line in lines:
                    f.write(line + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            _maybe_trim_text_file(DEAD_LETTER_FILE, DEAD_LETTER_MAX_BYTES)
    except Exception as e:
        logging.error(f"写死信文件失败: {e}")
    sample = ", ".join(x for x in ids[:8] if x) or "-"
    more = f" 等共 {len(ids)} 条" if len(ids) > 8 else f" 共 {len(ids)} 条"
    notify_system_once(
        "⚠️ Outbox 任务进入死信",
        f"原因={reason}；动态ID: {sample}{more}。详见 {DEAD_LETTER_FILE}",
    )
    logging.error(f"[DEAD_LETTER] reason={reason} count={len(ids)} ids={sample}")


def _sanitize_outbox(raw, emit_dead=False):
    """清洗 outbox。emit_dead=True 仅启动/恢复时写死信，普通 save 只剔除。"""
    if not isinstance(raw, dict):
        return {}
    now = time.time()
    candidates = []
    dead = []
    max_age = OUTBOX_EXPIRE_SECONDS
    for k, v in raw.items():
        dyn_id = str(k).strip()
        if not dyn_id or not isinstance(v, dict):
            continue
        created = _safe_float(v.get("created_at"), 0.0)
        attempt = _safe_int(v.get("attempt"), 0)
        next_attempt = _safe_float(v.get("next_attempt"), 0.0)
        pub_ts = _safe_int(v.get("pub_ts"), 0)
        first_seen = _safe_int(v.get("first_seen"), 0)
        task = dict(v)
        task["dyn_id"] = dyn_id
        task["uid"] = str(task.get("uid") or "")
        task["notify_type"] = str(task.get("notify_type") or "dynamic")
        task["title"] = str(task.get("title") or "")
        task["items"] = task.get("items") if isinstance(task.get("items"), list) else []
        task["pub_ts"] = pub_ts
        task["first_seen"] = first_seen or int(created or now)
        task["created_at"] = created or now
        task["attempt"] = attempt
        task["next_attempt"] = next_attempt
        task["discovery_mode"] = str(task.get("discovery_mode") or "")
        task["time_fallback"] = bool(task.get("time_fallback", False))
        if created and (now - created) > max_age:
            dead.append(task)
            continue
        if attempt > OUTBOX_MAX_ATTEMPTS:
            dead.append(task)
            continue
        candidates.append(task)
    candidates.sort(key=lambda t: _safe_float(t.get("created_at"), 0.0))
    if len(candidates) > OUTBOX_MAX:
        overflow = candidates[:-OUTBOX_MAX]
        candidates = candidates[-OUTBOX_MAX:]
        dead.extend(overflow)
        logging.warning(
            f"🧹 outbox 超限，淘汰最旧 {len(overflow)} 条，保留最新 {OUTBOX_MAX} 条"
        )
    if dead and emit_dead:
        by_attempt = [t for t in dead if _safe_int(t.get("attempt"), 0) > OUTBOX_MAX_ATTEMPTS]
        by_age = [t for t in dead if t not in by_attempt and (
            _safe_float(t.get("created_at"), 0) and (now - _safe_float(t.get("created_at"), 0)) > max_age
        )]
        by_overflow = [t for t in dead if t not in by_attempt and t not in by_age]
        if by_attempt:
            append_dead_letter("attempt_exceeded", by_attempt)
        if by_age:
            append_dead_letter("expired", by_age)
        if by_overflow:
            append_dead_letter("outbox_overflow", by_overflow)
    elif dead:
        logging.info(f"outbox 保存时剔除 {len(dead)} 条过期/超次/超限任务（不写死信）")
    return {t["dyn_id"]: t for t in candidates}



def restore_outbox_to_queue(state):
    feed = state.setdefault("feed", {})
    feed["outbox"] = _sanitize_outbox(feed.get("outbox"), emit_dead=False)
    outbox = feed["outbox"]
    now = time.time()
    # 已发送（recent_pushed / sent_ack）的 outbox 任务直接丢弃，防止重启重复推
    ack_ids = set(_read_sent_ack_tail())
    dropped = 0
    for dyn_id, task in list(outbox.items()):
        if is_recent_pushed(state, dyn_id) or dyn_id in ack_ids:
            outbox.pop(dyn_id, None)
            PENDING_PUSH_IDS.discard(str(dyn_id))
            dropped += 1
            continue
        next_attempt = _safe_float(task.get("next_attempt"), 0.0)
        if next_attempt > now:
            continue
        try:
            notify_queue.put_nowait(task)
            PENDING_PUSH_IDS.add(str(dyn_id))
        except queue.Full:
            PENDING_PUSH_IDS.discard(str(dyn_id))
            logging.warning("[OUTBOX] queue full; remaining tasks will retry")
            break
    if dropped:
        logging.info(f"[OUTBOX] dropped_sent={dropped}")
        mark_state_dirty(state)


def mark_sent(state, task):
    dyn_id = str(task.get("dyn_id") or "")
    uid = str(task.get("uid") or "")
    pub_ts = _safe_int(task.get("pub_ts"), 0)
    if not dyn_id:
        return
    # ack 由 notify_worker 在发送成功时写一次；这里只更新内存状态
    feed = state.setdefault("feed", {})
    outbox = feed.setdefault("outbox", {})
    outbox.pop(dyn_id, None)
    add_recent_pushed(state, dyn_id)
    feed.setdefault("push_retry_after", {}).pop(dyn_id, None)
    PENDING_PUSH_IDS.discard(dyn_id)

    # 补写 discovered，保证与 recent_pushed 一致
    discovered = feed.setdefault("discovered", {})
    entry = discovered.setdefault(dyn_id, {
        "first_seen": int(task.get("first_seen") or time.time()),
        "pub_ts": pub_ts,
        "uid": uid,
        "refresh_seq": STATE.refresh_seq,
        "discovery_mode": str(task.get("discovery_mode") or "sent"),
    })
    entry["status"] = "sent"
    entry["pub_ts"] = pub_ts or _safe_int(entry.get("pub_ts"), 0)
    entry["uid"] = uid or str(entry.get("uid") or "")

    stat = get_uid_stat(state, uid)
    remember_uid_id(stat, dyn_id)
    stat["total_seen"] += 1
    if pub_ts > 0:
        stat["last_pub_ts"] = max(int(stat.get("last_pub_ts", 0)), pub_ts)
    try:
        first_seen = int(task.get("first_seen") or 0)
    except (TypeError, ValueError):
        first_seen = 0
    if first_seen and pub_ts > 0:
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
    entry = state.setdefault("feed", {}).setdefault("discovered", {}).get(dyn_id)
    if isinstance(entry, dict):
        entry["status"] = "retry"
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
        sent_ok = False
        try:
            dyn_id = str(task.get("dyn_id") or "")
            ntype = task.get("notify_type")
            next_attempt = _safe_float(task.get("next_attempt"), 0.0)
            # 未到重试时间：不塞回队列（避免空转），留给 requeue_due_outbox
            if dyn_id and next_attempt > time.time():
                PENDING_PUSH_IDS.discard(dyn_id)
                time.sleep(0.5)
                continue

            ok = bool(notifier.send_webhook_notification(
                task.get("title", ""), task.get("items", []), notify_type=ntype
            ))
            if ok:
                sent_ok = True
                ack_ok = True
                if dyn_id and ntype == "dynamic":
                    ack_ok = append_sent_ack(dyn_id)
                state_ok = True
                if ntype == "dynamic" and ACTIVE_STATE is not None:
                    try:
                        with STATE_LOCK:
                            mark_sent(ACTIVE_STATE, task)
                            save_dynamic_state(ACTIVE_STATE)
                    except Exception as se:
                        state_ok = False
                        logging.error(
                            f"[发送成功但状态更新失败] dyn_id={dyn_id or '-'} err={repr(se)} "
                            f"ack_ok={ack_ok}"
                        )
                        try:
                            with STATE_LOCK:
                                ACTIVE_STATE.setdefault("feed", {}).setdefault("outbox", {}).pop(dyn_id, None)
                                add_recent_pushed(ACTIVE_STATE, dyn_id)
                                PENDING_PUSH_IDS.discard(dyn_id)
                                mark_state_dirty(ACTIVE_STATE)
                        except Exception as e2:
                            logging.error(f"发送成功后内存补记失败 dyn_id={dyn_id}: {repr(e2)}")
                            PENDING_PUSH_IDS.discard(dyn_id)
                        if not ack_ok:
                            logging.critical(
                                f"[发送成功但 ack+state 均失败] dyn_id={dyn_id}，保留 outbox 待确认，可能重复推送"
                            )
                            notify_system_once(
                                "🚨 去重保证失效预警",
                                f"动态 {dyn_id} webhook 已成功，但 sent_ack 与状态均未落盘，重启可能重复推送。",
                            )
                            try:
                                with STATE_LOCK:
                                    ob = ACTIVE_STATE.setdefault("feed", {}).setdefault("outbox", {})
                                    t = dict(task)
                                    t["next_attempt"] = time.time() + 30
                                    t["attempt"] = _safe_int(t.get("attempt"), 0)
                                    ob[dyn_id] = t
                                    PENDING_PUSH_IDS.discard(dyn_id)
                                    mark_state_dirty(ACTIVE_STATE)
                                    save_dynamic_state(ACTIVE_STATE)
                            except Exception as se2:
                                logging.critical(
                                    f"ack+state 失败后 outbox 落盘仍失败 dyn_id={dyn_id}: {repr(se2)}"
                                )
                logging.info(f"[PUSH] sent dyn_id={dyn_id or '-'}")
            else:
                # 发送失败：先 schedule_retry（只改内存），再单独 save，避免 save 异常触发外层二次 schedule_retry
                retried = False
                if ntype == "dynamic" and ACTIVE_STATE is not None:
                    try:
                        with STATE_LOCK:
                            schedule_retry(ACTIVE_STATE, task)
                            retried = True
                    except Exception as se:
                        logging.error(f"[OUTBOX] retry_schedule_failed: {repr(se)}")
                        PENDING_PUSH_IDS.discard(dyn_id)
                    if retried:
                        try:
                            with STATE_LOCK:
                                save_dynamic_state(ACTIVE_STATE)
                        except Exception as se:
                            logging.error(f"[OUTBOX] save_after_fail dyn_id={dyn_id}: {repr(se)}")
                logging.warning(f"[PUSH] failed dyn_id={dyn_id or '-'}")
        except Exception as e:
            logging.error(f"推送线程异常: {repr(e)}")
            # 只有「未确认发送成功」且尚未 schedule_retry 时才重试
            if not sent_ok and task.get("dyn_id") and ACTIVE_STATE is not None:
                dyn = str(task.get("dyn_id") or "")
                try:
                    with STATE_LOCK:
                        # 若已在 outbox 且 next_attempt 已设置，说明 else 分支已 schedule，勿重复
                        ob = ACTIVE_STATE.setdefault("feed", {}).setdefault("outbox", {})
                        existing = ob.get(dyn)
                        if existing and _safe_float(existing.get("next_attempt"), 0) > time.time():
                            pass  # 已调度
                        else:
                            schedule_retry(ACTIVE_STATE, task)
                    try:
                        with STATE_LOCK:
                            save_dynamic_state(ACTIVE_STATE)
                    except Exception as se:
                        logging.error(f"[OUTBOX] exception_save_failed: {repr(se)}")
                except Exception as se:
                    logging.error(f"[OUTBOX] retry_schedule_failed: {repr(se)}")
                    PENDING_PUSH_IDS.discard(dyn)
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
        # 已发送的不再重推（recent_pushed + 运行期 ACK 镜像）
        if is_recent_pushed(state, dyn_id) or dyn_id in SENT_ACK_IDS:
            outbox.pop(dyn_id, None)
            PENDING_PUSH_IDS.discard(str(dyn_id))
            mark_state_dirty(state)
            continue
        next_attempt = _safe_float(task.get("next_attempt"), 0.0)
        if next_attempt > now:
            continue
        if dyn_id in PENDING_PUSH_IDS:
            continue
        try:
            notify_queue.put_nowait(task)
            PENDING_PUSH_IDS.add(str(dyn_id))
        except queue.Full:
            PENDING_PUSH_IDS.discard(str(dyn_id))
            break


# =========================================================
# 关注流 API（仅 feed/all，对应 App 关注页）
# =========================================================
FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all"
FEED_FEATURES = (
    "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,decorationCard,"
    "onlyfansAssetsV2,forwardListHidden,ugcDelete,onlyfansQaCard,commentsNewVersion,htmlNewStyle"
)


def fetch_following_feed(offset="", update_baseline=""):
    """关注动态完整流，接近 App 关注页下拉刷新。"""
    params = {
        "type": "all",
        "timezone_offset": "-480",
        "platform": "web",
        "web_location": "333.1365",
        "features": FEED_FEATURES,
    }
    if offset:
        params["offset"] = offset
    if update_baseline:
        params["update_baseline"] = update_baseline
    return wbi_request(FEED_URL, params)


def fetch_feed_page(offset="", update_baseline=""):
    data = fetch_following_feed(offset, update_baseline=update_baseline)
    if data.get("code") != 0:
        logging.warning(
            f"❌ feed/all 失败 code={data.get('code')} msg={data.get('message')}"
        )
        return None
    return data.get("data") or {}


def process_feed_items(items, target_uids, state, refresh_seq, discovery_mode,
                       pushed_set=None, stats=None):
    """过滤整体关注流：发送成功与否由 ACK/outbox 决定；时间用于防状态污染和长停机漏报。
    [FIX-3] stats 输出过滤漏斗计数，让「被哪一层丢掉」在 INFO 下可见。"""
    now_ts = int(time.time())
    feed = state.setdefault("feed", {})
    discovered = feed.setdefault("discovered", {})
    outbox = feed.setdefault("outbox", {})
    last_ok = _safe_float(feed.get("last_success_refresh"), 0.0)
    pushed_set = pushed_set if pushed_set is not None else get_recent_pushed_set(state)
    # [FIX-3] ACK 兜底：发送成功但 state 落盘失败时，recent_pushed 可能缺这条，SENT_ACK_IDS 仍在
    with ACK_LOCK:
        ack_ids = set(SENT_ACK_IDS)
    candidates, page_ids = [], []
    daily = state.setdefault("daily", {})

    if stats is None:
        stats = {}
    st = {"seen": 0, "target": 0, "type_block": 0, "no_pub_ts": 0, "ack": 0,
          "already_pushed": 0, "pending_or_outbox": 0, "already_discovered": 0,
          "too_old": 0, "new": 0}
    for k in st:
        stats.setdefault(k, 0)
    type_block_ids = []

    def dbg_skip(dyn_id, uid, pub_ts, entry, reason):
        """逐动态跳过原因诊断。仅 DEBUG 可见，INFO 运行零输出。"""
        info = entry if isinstance(entry, dict) else {}
        age = (now_ts - pub_ts) if pub_ts > 0 else -1
        logging.debug(
            "[FILTER SKIP] dyn_id=%s uid=%s pub_ts=%s age=%ss first_seen=%s "
            "discovered=%s/%s reason=%s mode=%s last_ok=%s",
            dyn_id, uid or "-", pub_ts or "-", age,
            _safe_int(info.get("first_seen"), 0),
            "yes" if entry else "no", str(info.get("status", "-")),
            reason, discovery_mode, int(last_ok),
        )

    for item in items:
        if not isinstance(item, dict):
            continue
        dyn_id = str(item.get("id_str") or "")
        if not dyn_id:
            continue
        st["seen"] += 1
        page_ids.append(dyn_id)
        # 注意：modules 可能显式为 null，必须 or {}，否则整轮扫描会被 AttributeError 打断
        author = (item.get("modules") or {}).get("module_author") or {}
        uid = str(author.get("mid", ""))
        if uid not in target_uids:
            continue
        st["target"] += 1
        name = author.get("name", "未知UP")
        pub_ts = _safe_int(author.get("pub_ts"), 0)
        stat = get_uid_stat(state, uid, name)
        stat["last_global_seen"] = now_ts
        if pub_ts:
            stat["last_pub_ts"] = max(_safe_int(stat.get("last_pub_ts"), 0), pub_ts)

        if not is_allowed_dynamic(item):
            st["type_block"] += 1
            type_block_ids.append(f"{dyn_id}:{item.get('type') or '-'}")
            dbg_skip(dyn_id, uid, pub_ts, discovered.get(dyn_id), "type_filtered")
            continue
        # [FIX-7] 没有 pub_ts 也不能静默丢弃：使用本次发现时间作为临时事件时间。
        # 这样动态仍会进入 outbox，并在消息里至少带上动态直达链接。
        time_fallback = False
        raw_pub_ts = pub_ts
        if not pub_ts:
            st["no_pub_ts"] += 1
            time_fallback = True
            pub_ts = now_ts
            logging.debug(
                f"[TIME_FALLBACK] dyn_id={dyn_id} uid={uid} pub_ts缺失，使用发现时间继续判断"
            )
        if dyn_id in ack_ids:
            st["ack"] += 1
            dbg_skip(dyn_id, uid, pub_ts, discovered.get(dyn_id), "already_sent_ack")
            continue
        if is_recent_pushed(state, dyn_id, pushed_set):
            st["already_pushed"] += 1
            dbg_skip(dyn_id, uid, pub_ts, discovered.get(dyn_id), "already_pushed")
            continue
        if dyn_id in PENDING_PUSH_IDS or dyn_id in outbox:
            st["pending_or_outbox"] += 1
            dbg_skip(dyn_id, uid, pub_ts, discovered.get(dyn_id), "pending_or_outbox")
            continue

        age = now_ts - pub_ts
        recent = age <= RECENT_FORCE_NEW_WINDOW
        downtime_new = last_ok > 0 and pub_ts > last_ok
        entry = discovered.get(dyn_id)
        entry_pub = _safe_int((entry or {}).get("pub_ts"), 0) if isinstance(entry, dict) else 0
        entry_time_fallback = bool(isinstance(entry, dict) and entry.get("time_fallback"))
        pub_changed = bool(entry_pub and raw_pub_ts and pub_ts > entry_pub)

        # 长停机恢复：只要发布时间晚于最后一次完整成功刷新，不受6小时历史窗限制。
        # 对“启动基线阶段无 pub_ts”的历史动态，保持 baseline 边界，不能因每次
        # fallback 到当前时间而被误判成新动态。
        if time_fallback and entry_time_fallback and entry.get("status") == "baseline":
            force_recover = False
        else:
            force_recover = recent or downtime_new or pub_changed

        if entry and not force_recover:
            st["already_discovered"] += 1
            dbg_skip(dyn_id, uid, pub_ts, entry, "already_discovered")
            continue
        if not force_recover and age > DYNAMIC_NEW_WINDOW:
            st["too_old"] += 1
            dbg_skip(dyn_id, uid, pub_ts, entry, "too_old")
            continue

        reason = (
            "time_fallback" if time_fallback else
            "recent_force" if recent else
            "downtime_recovery" if downtime_new else
            "pub_time_advanced" if pub_changed else
            "new_id"
        )
        st["new"] += 1
        logging.debug(
            "[FEED DEBUG] found dyn_id=%s uid=%s pub_ts=%s first_seen=%s discovered=%s/%s last_ok=%s",
            dyn_id, uid, pub_ts,
            _safe_int((entry or {}).get("first_seen"), 0) if isinstance(entry, dict) else 0,
            "yes" if entry else "no",
            str((entry or {}).get("status", "-")) if isinstance(entry, dict) else "-",
            int(last_ok),
        )
        logging.info(f"[FILTER] new dyn_id={dyn_id} uid={uid} reason={reason} age={max(0, age)}s mode={discovery_mode}")
        candidates.append((pub_ts, dyn_id, uid, item, now_ts, reason))

    # [FIX-3] 类型过滤是历史上最难查的静默丢动态路径，这里必须有 INFO 痕迹
    if type_block_ids:
        logging.info(
            f"[FILTER SKIP] type_filtered count={len(type_block_ids)} "
            f"sample={','.join(type_block_ids[:5])} mode={discovery_mode}"
        )
    for k, v in st.items():
        stats[k] = stats.get(k, 0) + v

    daily["items_seen"] = int(daily.get("items_seen", 0)) + st["seen"]
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
        stat["daily_verify_recovered"] = int(daily.get("daily_verify_recovered", 0)) + 1
    elif discovery_mode == "deep":
        daily["deep_recovered"] = int(daily.get("deep_recovered", 0)) + 1
        stat["daily_deep_recovered"] = int(daily.get("daily_deep_recovered", 0)) + 1
    mark_state_dirty(state)


def enqueue_candidates(candidates, state, discovery_mode):
    candidates.sort(key=lambda x: (x[0], x[1]))
    has_new = False
    discovered = state.setdefault("feed", {}).setdefault("discovered", {})
    for pub_ts, dyn_id, uid, item, first_seen, reason in candidates:
        try:
            push_data = format_dynamic_message(item)
            task_title = f"{push_data.get('user', '未知UP')} 发布了新动态"
            if not safe_enqueue_notify(task_title, [push_data], "dynamic", dyn_id, uid, pub_ts, first_seen, discovery_mode):
                logging.warning(f"[PUSH] enqueue_failed dyn_id={dyn_id}")
                continue
            discovered[dyn_id] = {
                "first_seen": first_seen, "pub_ts": pub_ts, "uid": uid,
                "refresh_seq": STATE.refresh_seq, "discovery_mode": discovery_mode,
                "time_fallback": reason == "time_fallback",
                "status": "queued",
            }
            update_uid_stats_after_enqueue(state, {"uid": uid, "dyn_id": dyn_id, "pub_ts": pub_ts, "first_seen": first_seen}, discovery_mode)
            delay = max(0, first_seen - pub_ts)
            tag = "追回" if discovery_mode != "primary" else "发现"
            logging.info(f"[PUSH] {tag} uid={uid} dyn_id={dyn_id} delay={delay}s reason={reason}")
            has_new = True
        except Exception as e:
            logging.error(f"[PUSH] 处理失败 dyn_id={dyn_id}: {e}")
    trim_discovered(state)
    if has_new:
        mark_state_dirty(state)
    return has_new


def full_refresh(target_uids, state, mode="primary", max_pages=5, stop_at_snapshot=True):
    """一次完整的关注流刷新。成功返回后，才允许更新 baseline / snapshot。"""
    STATE.refresh_seq += 1
    refresh_seq = STATE.refresh_seq
    state.setdefault("daily", {})["refreshes"] = int(state.setdefault("daily", {}).get("refreshes", 0)) + 1

    all_new = []
    all_ids = []
    stats = {}
    offset = ""
    completed = True
    stable_pages = 0
    old_snapshot = set(dict.fromkeys(state.setdefault("feed", {}).get("last_snapshot_ids", []) or []))
    reached_old = False
    first_baseline = ""
    pages_done = 0
    # primary 至少 3 页；deep 至少 5 页，避免过早 stop 漏补
    min_pages_before_stop = 5 if mode == "deep" else 3
    # 首页带上已知 baseline，便于 update_num 反映增量
    known_baseline = str(state.setdefault("feed", {}).get("baseline") or "")
    partial_fail = False
    next_offset = ""
    has_more = False
    stopped_at_snapshot = False
    hit_page_limit = False

    logging.debug(f"[SCAN] start mode={mode} seq={refresh_seq}")

    for page_idx in range(max_pages):
        if not IS_RUNNING:
            completed = False
            break
        page_baseline = known_baseline if page_idx == 0 and known_baseline else ""
        data = fetch_feed_page(offset, update_baseline=page_baseline)
        if data is None:
            if pages_done == 0:
                completed = False
            else:
                # 已有成功页后的后续失败：记为部分失败，仍保留已处理候选
                partial_fail = True
            state.setdefault("daily", {})["api_fail"] = int(state.setdefault("daily", {}).get("api_fail", 0)) + 1
            logging.warning(
                f"❌ [{mode}] feed 第{page_idx + 1}页失败"
                f"{'（部分失败，保留已扫页）' if pages_done else '，保留旧边界'}"
            )
            break

        pages_done += 1
        items = data.get("items") or []
        first_id = str(items[0].get("id_str") if items else "-")
        last_id = str(items[-1].get("id_str") if items else "-")
        logging.debug(f"[FEED DEBUG] page={page_idx + 1} count={len(items)} first={first_id} last={last_id}")
        if not items:
            has_more = False
            next_offset = ""
            break
        if page_idx == 0:
            first_baseline = str(data.get("update_baseline") or items[0].get("id_str") or "")

        # 每页刷新 pushed_set，避免同轮多页扫描用旧快照漏判已发送
        pushed_set = get_recent_pushed_set(state)
        candidates, page_ids = process_feed_items(
            items, target_uids, state, refresh_seq, mode, pushed_set=pushed_set, stats=stats
        )
        all_new.extend(candidates)
        all_ids.extend(page_ids)

        # 稳定页：本页绝大多数 ID 已在 snapshot
        old_count = sum(1 for x in page_ids if x in old_snapshot)
        if page_ids and old_count >= max(1, int(len(page_ids) * 0.6)):
            stable_pages += 1
        else:
            stable_pages = 0

        if old_snapshot and any(x in old_snapshot for x in page_ids):
            reached_old = True

        next_offset = str(data.get("offset") or "")
        has_more = bool(data.get("has_more"))
        if not next_offset or not has_more:
            break
        if (
            stop_at_snapshot
            and pages_done >= min_pages_before_stop
            and stable_pages >= DEEP_SCAN_STOP_STABLE_PAGES
        ):
            stopped_at_snapshot = True
            break
        offset = next_offset
        if page_idx + 1 < max_pages:
            time.sleep(random.uniform(0.4, 0.8))

    unique = {}
    for c in all_new:
        unique[c[1]] = c
    has_new = enqueue_candidates(list(unique.values()), state, mode)

    # 跑满 max_pages 且流仍有后续：分页未完成。若已碰到旧 snapshot 边界则视为够用。
    hit_page_limit = bool(
        pages_done >= max_pages
        and next_offset
        and has_more
        and not stopped_at_snapshot
    )
    truncated = bool(hit_page_limit and not reached_old)

    if pages_done > 0:
        feed = state.setdefault("feed", {})
        # 仅完整成功时更新 last_refresh_time / success / snapshot
        if not partial_fail and completed and not truncated:
            now_ok = time.time()
            feed["last_refresh_time"] = now_ok
            feed["last_success_refresh"] = now_ok
            if first_baseline:
                feed["baseline"] = first_baseline
            new_ids = list(dict.fromkeys(all_ids))
            old_ids = feed.get("last_snapshot_ids") or []
            merged = new_ids + [x for x in old_ids if x not in set(new_ids)]
            feed["last_snapshot_ids"] = merged[:RECENT_SNAPSHOT_LIMIT]
            history = feed.setdefault("recent_snapshot_history", [])
            history.append({
                "seq": refresh_seq,
                "time": int(now_ok),
                "mode": mode,
                "count": len(all_ids),
                "reached_old": reached_old,
                "partial_fail": False,
                "hit_page_limit": hit_page_limit,
                "ids": list(feed["last_snapshot_ids"])[:100],
            })
            history[:] = history[-20:]
        else:
            logging.warning(f"[SCAN] boundary_kept mode={mode} pages={pages_done}/{max_pages} partial={partial_fail} truncated={truncated}")
        mark_state_dirty(state)

    # API 本轮可用：不因「页数上限」触发退避（否则 primary=5 且 has_more 会永远失败）
    refresh_ok = bool(pages_done > 0 and not partial_fail and completed)
    if truncated:
        logging.warning(f"[SCAN] truncated mode={mode} pages={pages_done}/{max_pages}")
    # [FIX-4] done 行输出过滤漏斗，一眼看出动态卡在哪一层
    funnel = (
        f"seen={stats.get('seen', 0)} target={stats.get('target', 0)} "
        f"type_block={stats.get('type_block', 0)} no_pub_ts={stats.get('no_pub_ts', 0)} "
        f"ack={stats.get('ack', 0)} pushed={stats.get('already_pushed', 0)} "
        f"pending={stats.get('pending_or_outbox', 0)} disc={stats.get('already_discovered', 0)} "
        f"too_old={stats.get('too_old', 0)} time_fallback={stats.get('no_pub_ts', 0)}"
    )
    if unique or not refresh_ok:
        logging.info(f"[SCAN] done mode={mode} pages={pages_done} {funnel} new={len(unique)} ok={refresh_ok}")
    else:
        logging.debug(f"[SCAN] done mode={mode} pages={pages_done} {funnel} new=0 ok=True")
    return has_new, len(unique), pages_done, refresh_ok


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

    feed.setdefault("last_snapshot_ids", [])
    feed.setdefault("recent_snapshot_history", [])
    feed.setdefault("discovered", {})
    feed.setdefault("recent_pushed_ids", [])

    # 合并 sent_ack，保证「发送成功但 state 未落盘」的动态不会重启后重推
    n_ack = load_sent_acks_into_state(state)
    if n_ack:
        logging.info(f"📥 已从 sent_ack 恢复 {n_ack} 条已发送记录")
        mark_state_dirty(state)

    # 启动基线：
    # - 更新 snapshot / baseline
    # - 仅把「超出强制新窗口」的动态写入 discovered（避免静默吃掉停机窗口内的新动态）
    # - 强制窗口内的动态不写 discovered，交给正常扫描推送
    logging.info("[STATE] building startup baseline")
    all_ids = []
    offset = ""
    baseline_pages = 3
    now_ts = int(time.time())
    discovered = feed.setdefault("discovered", {})
    pages_ok = 0
    skipped_recent = 0
    registered = 0
    skipped_type = 0
    pending_baseline = ""
    baseline_has_more = False

    for page_idx in range(baseline_pages):
        data = fetch_feed_page(offset)
        if data is None:
            break
        items = data.get("items") or []
        if not items:
            baseline_has_more = False
            break
        pages_ok += 1
        if page_idx == 0:
            pending_baseline = str(
                data.get("update_baseline") or items[0].get("id_str") or ""
            )
        for item in items:
            if not isinstance(item, dict):
                continue
            dyn_id = str(item.get("id_str") or "")
            if not dyn_id:
                continue
            all_ids.append(dyn_id)
            author = (item.get("modules") or {}).get("module_author") or {}
            uid = str(author.get("mid", ""))
            pub_ts = _safe_int(author.get("pub_ts"), 0)
            if uid not in target_uids:
                continue
            stat = get_uid_stat(state, uid, author.get("name", uid))
            remember_uid_id(stat, dyn_id)
            stat["last_global_seen"] = now_ts
            if pub_ts > 0:
                stat["last_pub_ts"] = max(_safe_int(stat.get("last_pub_ts"), 0), pub_ts)

            # 已真正推送过的跳过
            if is_recent_pushed(state, dyn_id):
                continue
            # [FIX-6] 当前不允许的动态类型不要登记 baseline：
            # 否则该动态会被永久标记为「已处理」，将来放宽类型或修好解析后仍被
            # already_discovered 拦掉（1246036657638998040 就踩过这个坑）。
            if not is_allowed_dynamic(item):
                skipped_type += 1
                continue
            # 启动基线策略：
            # - 无 pub_ts：登记为 baseline + time_fallback，防止历史无时间动态在后续
            #   每轮都被“发现时间=现在”误判为新动态。
            # - 近期动态/停机窗口动态：不登记，交给正式扫描恢复。
            # - 其余历史动态：登记 baseline，避免冷启动历史洪水。
            age = (now_ts - pub_ts) if pub_ts > 0 else 0
            last_ok = _safe_float(feed.get("last_success_refresh"), 0.0)
            is_recent = pub_ts > 0 and age <= RECENT_FORCE_NEW_WINDOW
            if last_ok > 0:
                is_downtime_new = pub_ts > 0 and pub_ts > last_ok
            else:
                is_downtime_new = pub_ts > 0 and age <= STARTUP_RECOVER_MAX_AGE

            if pub_ts <= 0:
                if dyn_id not in discovered:
                    discovered[dyn_id] = {
                        "first_seen": now_ts,
                        "pub_ts": 0,
                        "uid": uid,
                        "refresh_seq": 0,
                        "discovery_mode": "baseline",
                        "time_fallback": True,
                        "status": "baseline",
                    }
                    registered += 1
                else:
                    skipped_recent += 1
                continue

            if is_recent or is_downtime_new:
                skipped_recent += 1
                continue

            if dyn_id not in discovered:
                discovered[dyn_id] = {
                    "first_seen": now_ts,
                    "pub_ts": pub_ts,
                    "uid": uid,
                    "refresh_seq": 0,
                    "discovery_mode": "baseline",
                    "time_fallback": False,
                    "status": "baseline",
                }
                registered += 1

        next_offset = str(data.get("offset") or "")
        baseline_has_more = bool(data.get("has_more") and next_offset)
        if not baseline_has_more:
            break
        offset = next_offset
        time.sleep(random.uniform(0.3, 0.6))

    if pages_ok > 0:
        # snapshot 合并，不整表覆盖
        old_ids = feed.get("last_snapshot_ids") or []
        merged = list(dict.fromkeys(all_ids + [x for x in old_ids if x not in set(all_ids)]))
        feed["last_snapshot_ids"] = merged[:RECENT_SNAPSHOT_LIMIT]
        # 仅启动基线扫完（无更多页）时才改 baseline，避免不完整页覆盖运行边界
        if pending_baseline and not baseline_has_more:
            feed["baseline"] = pending_baseline
        elif baseline_has_more:
            logging.info("启动基线未扫完流（仍 has_more），保留原 baseline")
        trim_discovered(state)
        mark_state_dirty(state)
        save_dynamic_state(state)
        logging.info(
            f"[STATE] baseline pages={pages_ok} items={len(all_ids)} "
            f"registered={registered} skip_recent={skipped_recent} skip_type={skipped_type}"
        )
    else:
        logging.warning("⚠️ 启动基线获取失败，将依赖已有状态继续启动")
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
    if len(new_list) > MAX_MONITOR_UIDS:
        logging.warning(f"关注列表刷新 {len(new_list)} > {MAX_MONITOR_UIDS}，截断并保留 SOURCE_UID")
        new_list = [u for u in new_list if u != str(SOURCE_UID)][: MAX_MONITOR_UIDS - 1]
        new_list.append(str(SOURCE_UID))
        new_list = list(dict.fromkeys(new_list))
    old_set = set(following_list)
    new_set = set(new_list)
    following_list = new_list
    save_following_cache(following_list)
    for uid in new_set:
        get_uid_stat(state, uid)
    if old_set != new_set:
        logging.info(f"[FOLLOWING] changed {len(old_set)}→{len(new_set)}")
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
        # 保证 SOURCE_UID 始终保留
        live = [u for u in live if u != str(SOURCE_UID)][: MAX_MONITOR_UIDS - 1]
        live.append(str(SOURCE_UID))
        live = list(dict.fromkeys(live))
    save_following_cache(live)
    logging.info(f"[FOLLOWING] loaded uid={len(live)} source={source}")
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

    # 先过滤/恢复 outbox，再启动推送线程，避免与 ACK 读写并发
    restore_outbox_to_queue(state)
    threading.Thread(target=notify_worker, daemon=True, name="notify-worker").start()

    last_scan = 0.0
    last_following_refresh = time.time()
    last_heartbeat = 0.0
    next_interval = random_main_interval()  # 固定本轮间隔，不在循环里反复重抽
    STATE.last_deep_scan = 0.0
    STATE.last_new_dynamic_time = time.time()

    logging.info(
        f"✅ 启动完成：工作日 {RUN_START_HOUR}:{RUN_START_MINUTE:02d}-{RUN_END_HOUR}:00；"
        f"整体关注流刷新 {NORMAL_INTERVAL_MIN:g}~{NORMAL_INTERVAL_MAX:g}s；"
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
                daily = state.setdefault("daily", {})
                last_ok = _safe_int(feed.get("last_success_refresh"), 0)
                ok_age = f"{int(now - last_ok)}s前" if last_ok else "从未"
                logging.info(
                    f"[HEARTBEAT] uid={len(target_uids)} outbox={len(outbox)} "
                    f"failures={STATE.consecutive_failures} refreshes={daily.get('refreshes', 0)} "
                    f"deep={daily.get('deep_scans', 0)} new={daily.get('new_found', 0)} last_ok={ok_age}"
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

            if now - last_scan >= next_interval:
                try:
                    has_new, _, pages_done, refresh_ok = full_refresh(
                        target_uids, state, mode="primary", max_pages=5, stop_at_snapshot=True
                    )

                    # 发现新动态后做一次整体二次确认，不拆 UID
                    if has_new and IS_RUNNING and refresh_ok:
                        state.setdefault("daily", {})["verify_rounds"] = int(
                            state.setdefault("daily", {}).get("verify_rounds", 0)
                        ) + 1
                        time.sleep(random.uniform(VERIFY_DELAY_MIN, VERIFY_DELAY_MAX))
                        full_refresh(
                            target_uids, state, mode="verify",
                            max_pages=VERIFY_MAX_PAGES, stop_at_snapshot=True,
                        )

                    # 每5分钟整体深扫一次（primary 未成功/部分失败时跳过，降低压力）
                    if (
                        refresh_ok
                        and now - STATE.last_deep_scan >= DEEP_SCAN_INTERVAL
                        and IS_RUNNING
                    ):
                        state.setdefault("daily", {})["deep_scans"] = int(
                            state.setdefault("daily", {}).get("deep_scans", 0)
                        ) + 1
                        STATE.last_deep_scan = now
                        logging.debug("🔎 开始5分钟整体关注流深扫")
                        full_refresh(
                            target_uids, state, mode="deep",
                            max_pages=DEEP_SCAN_MAX_PAGES,
                            stop_at_snapshot=True,
                        )
                    elif not refresh_ok and now - STATE.last_deep_scan >= DEEP_SCAN_INTERVAL:
                        logging.warning("⚠️ primary 未完整成功，本轮跳过深扫以降低压力")

                    last_scan = time.time()
                    if refresh_ok:
                        STATE.consecutive_failures = 0
                    else:
                        STATE.consecutive_failures += 1
                        logging.warning(f"[SCAN] primary_incomplete pages={pages_done} failures={STATE.consecutive_failures}")
                    next_interval = random_main_interval()
                except Exception as e:
                    STATE.consecutive_failures += 1
                    last_scan = time.time()
                    next_interval = random_main_interval()
                    logging.error(f"[SCAN] exception={repr(e)} failures={STATE.consecutive_failures}")

            if now - STATE.last_state_save >= STATE_SAVE_INTERVAL or state.get("_meta", {}).get("dirty"):
                save_dynamic_state(state)
                STATE.last_state_save = now

            time.sleep(0.5)

        except Exception as e:
            if IS_RUNNING:
                logging.error(f"[LOOP] exception={repr(e)}")
                time.sleep(8)
            else:
                break

    save_dynamic_state(state)
    logging.info("[EXIT] state saved")


if __name__ == "__main__":
    init_logging()
    start_monitoring()
