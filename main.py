# -*- coding: utf-8 -*-
# =============================================================================
# B站关注动态监控  v3.3.2（feed/all｜长期稳定优化版）
# -----------------------------------------------------------------------------
# 本版相对 v3.3.1 的改动（只做口径修正与稳定性加固，不改动监控架构与投递语义）：
#   [FIX-16] 状态落盘分级：snapshot / baseline / recent_snapshot_history 这类
#           「纯边界」状态不再每轮即时落盘，改为按 STATE_BOUNDARY_SAVE_INTERVAL
#           （300s）批量写盘；关键状态（discovered / outbox / ACK / 重试 / 统计）
#           仍走 mark_state_dirty 立即落盘。稳态下写盘次数约降 90%
#           （原来每 20~35s 一次「全量 JSON 序列化 + fsync」，128MB 小磁盘写放大明显）。
#           代价：崩溃最多丢 5 分钟的 snapshot 边界更新，重启后可能多扫几页；
#           不影响去重与投递（关键状态仍即时落盘）。
#   [FIX-17] 扫描失败退避改为指数退避：原实现仅在连续失败 >=2 时抬到 35~60s，
#           上限过低；现按 60→120→240→480→900s 递增（SCAN_BACKOFF_MAX），
#           成功一次即回到 20~35s 正常节奏。避免 cookie 失效/被限流时持续高频打接口。
#   [FIX-18] primary 连续不完整达 DEEP_SCAN_FORCE_AFTER_FAILURES（3）轮后，即使
#           refresh_ok=False 也强制放行一次深扫——原来 partial_fail 会让深扫长期
#           被跳过，恰好在最需要它追回延迟动态的时候失效。
#   [FIX-19] 转发取图判据由 type==DYNAMIC_TYPE_FORWARD 改为「按 orig 是否存在」：
#           「转发 + 自己补了图」的动态 type 是 DYNAMIC_TYPE_DRAW 且带 orig，
#           旧判据会把原动态配图整段漏取。
#   [FIX-20] 图片张数统一跟随 MAX_PUSH_IMAGES：format_dynamic_message 不再硬截
#           1 张（原 cover=images[0] / one=[cover]），改常量即可全链路生效。
#           默认 MAX_PUSH_IMAGES=1，行为与旧版完全一致。
#   [FIX-21] 健康报告口径修正：
#             - 「整体刷新」只统计 primary（原来把 verify/deep 也算进来，数字偏大），
#               并新增「二次确认」轮次一行，三个口径不再混计；
#             - funnel 中 time_fallback 独立计数（原来与 no_pub_ts 同源，恒相等）；
#             - 「读取动态」改称「扫描条目（含确认/深扫重复计数）」，避免误读。
#   未改动（按用户确认）：RUN_WEEKDAYS 工作日运行策略（周末不监控为有意设计）。
# -----------------------------------------------------------------------------
# 本版相对 v3.3.0 的改动：
#   [FIX-15] 不推送「粉丝专属 / 充电专属」动态（需付费订阅才能看的内容）。
#           背景：账号未开通充电，这类动态推过来也看不到正文，属于纯噪音。
#           判定采用结构化枚举，不依赖正文文案，避免误判：
#             a) major.type == MAJOR_TYPE_UPOWER_COMMON        → 充电相关主体
#             b) major.type == MAJOR_TYPE_NONE 且 none.tips 含
#                「专属 / 充电 / 解锁 / upower」              → 锁定态专属内容
#             c) additional.type 以 ADDITIONAL_TYPE_UPOWER 开头 → 充电专属附件卡
#             d) 防御性：basic / module_dynamic / item 层出现
#                is_only_fans / is_upower_exclusive == true
#           处理方式不是静默 continue：命中后登记 discovered(status=fans_only)
#           并写 INFO 日志 + 每日 fans_only 计数，健康报告单列「专属已过滤」，
#           既不漏报可见动态，也能自证过滤确实生效。
#           注意：**未改动** FEED_FEATURES 请求参数（继续保留 listOnlyfans 等），
#           因为去掉这些 flag 后服务端可能返回不带 UPOWER 标记的内容，
#           反而会让本过滤失效；保留标记做客户端过滤才是确定性方案。
#           转发动态本身是公开内容，照常推送；其原动态若为专属，正文/图片
#           解析本就会得到空值（UPOWER_COMMON / NONE 均无正文可取），不会外泄锁定内容。
# -----------------------------------------------------------------------------
# 本版相对 v3.2.2 的长期稳定优化：
#   [FIX-9] queue/Outbox 拥堵时不删除已持久化任务。
#   [FIX-10] 去除长生命周期 requests.Session，统一普通 requests.get。
#   [FIX-11] 图片严格白名单，只取动态正文图片字段。
#   [FIX-12] pub_ts 缺失按动态 ID/状态去重，启动基线建立 missing-ts 边界。
#   [FIX-13] 死信保存完整推送 items，增强故障恢复。
#   [FIX-14] .bak 降为约10分钟一次，降低长期磁盘写放大。
# 本版相对 v3.2.1 的改动：
#   [FIX-8] 推送只显示一张主图：extract_images / 载荷 covers、images 均限 1 张，
#           避免图文/转发把整组图塞进 Webhook（消息过大、内存、通道刷屏）。
# -----------------------------------------------------------------------------
# 本版相对 v3.2 的改动（全部围绕「动态被静默丢弃」）：
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
# [FIX-18] primary 连续不完整达到该轮数后，即使 refresh_ok=False 也放行一次深扫，
# 避免网络抖动造成的 partial_fail 长期挡住唯一能追回延迟动态的通道。
DEEP_SCAN_FORCE_AFTER_FAILURES = 3

# [FIX-17] 扫描失败指数退避（成功一次即恢复 20~35s 正常节奏）
SCAN_BACKOFF_BASE = 60
SCAN_BACKOFF_MAX = 900

DYNAMIC_NEW_WINDOW = 6 * 3600        # 历史动态最大年龄
RECENT_FORCE_NEW_WINDOW = 10 * 60    # 近 N 秒强制视为新（防污染漏报）
# 启动时：无 last_success_refresh 时，最多恢复这么久以内的动态，避免冷启动误推全历史。
# 有 last_success_refresh 时：凡 pub_ts > last_ok 均视为停机窗口内新动态（不设年龄上限）。
STARTUP_RECOVER_MAX_AGE = 6 * 3600

# [FIX-16] 落盘分三级，取代原来的单一 STATE_SAVE_INTERVAL=60：
#   1) 关键状态（discovered / outbox / ACK / 重试 / 统计）→ mark_state_dirty，立即写；
#   2) 纯边界（last_snapshot_ids / baseline / recent_snapshot_history）→ 标记
#      boundary_dirty，最迟 STATE_BOUNDARY_SAVE_INTERVAL 内落盘；
#   3) 谁都没标记（例如 API 连续失败、状态没变）→ 兜底 STATE_SAVE_MAX_INTERVAL。
# 稳态（无新动态）下写盘次数由「每 20~35s 一次」降到「每 300s 一次」。
STATE_BOUNDARY_SAVE_INTERVAL = 300
STATE_SAVE_MAX_INTERVAL = 600
STATE_BACKUP_INTERVAL = 600
RECENT_SNAPSHOT_LIMIT = 400
SEEN_DYNAMIC_LIMIT = 12000
RECENT_PUSHED_IDS_LIMIT = 12000
OUTBOX_MAX = 500
OUTBOX_REQUEUE_BATCH = 20
NOTIFY_QUEUE_SYSTEM_RESERVE = 5
OUTBOX_MAX_AGE = 7 * 24 * 3600
OUTBOX_MAX_ATTEMPTS = 50

NOTIFY_QUEUE_MAXSIZE = 100
NOTIFY_SEND_DELAY = 2.0
NOTIFY_RETRY_BASE = 60
NOTIFY_RETRY_MAX = 1800

# Webhook 只带一张主图（封面/首图），避免图文动态把整组图全部发出。
MAX_PUSH_IMAGES = 1

REQUEST_TIMEOUT = 12
REQUEST_RETRIES = 3
WBI_REFRESH_INTERVAL = 21600

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
# 粉丝专属 / 充电专属动态过滤（[FIX-15]）
# ---------------------------------------------------------
# 账号未开通充电，专属动态推过来也看不到正文，属纯噪音，直接不推。
# 判定只用结构化枚举 + 锁定文案关键词，不做正文关键词匹配，避免误伤
# UP 主正常讨论「充电」「专属」的普通动态。
# 想恢复推送专属动态：把 SKIP_FANS_ONLY_DYNAMIC 改为 False 即可。
# =========================================================
SKIP_FANS_ONLY_DYNAMIC = True
# major.type 命中即判定为专属（充电相关主体）
FANS_ONLY_MAJOR_TYPES = {
    "MAJOR_TYPE_UPOWER_COMMON",
}
# additional.type 前缀命中即判定为专属（充电专属抽奖等，前缀匹配可覆盖未来新增）
FANS_ONLY_ADDITIONAL_PREFIXES = (
    "ADDITIONAL_TYPE_UPOWER",
)
# major.type == MAJOR_TYPE_NONE 时的锁定文案关键词。
# 注意：MAJOR_TYPE_NONE 也被普通「动态失效」复用，因此必须配合文案才判定，
# 否则会把失效动态一并吞掉，违反「不能静默丢动态」原则。
FANS_ONLY_TIPS_KEYWORDS = ("专属", "充电", "解锁", "upower")
# 防御性布尔字段：文档未确认动态接口一定返回，但一旦出现且为 true 即判定为专属。
FANS_ONLY_FLAG_KEYS = ("is_only_fans", "is_upower_exclusive")

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


HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                  "Chrome/120.0.0.0 Mobile Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "close",
}
BILI_COOKIES = {}


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
    last_backup_save: float = 0.0
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


def atomic_write_json(path, data, make_backup=True):
    """原子写。[FIX-5] 增加 fsync：掉电时 Outbox/ACK 等关键状态不会停留在 page cache。"""
    backup_path = path + ".bak"
    tmp_path = path + ".write.tmp"
    if make_backup and os.path.exists(path):
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


def mark_boundary_dirty(state):
    """[FIX-16] 标记「纯边界」状态变化（last_snapshot_ids / baseline / history）。

    这类状态丢失只影响停流边界的精度（重启后可能多扫几页），不影响去重与投递，
    因此不即时落盘，由主循环按 STATE_BOUNDARY_SAVE_INTERVAL 批量写盘。
    关键状态（discovered / outbox / ACK / 重试 / 统计）必须继续用 mark_state_dirty。
    """
    state.setdefault("_meta", {})["boundary_dirty"] = True


def should_save_state(state, now):
    """[FIX-16] 落盘判定，供主循环调用。

    - 关键状态 dirty             → 立即落盘（新动态/outbox/ACK 不能等）
    - 纯边界 boundary_dirty      → 最迟 STATE_BOUNDARY_SAVE_INTERVAL 内落盘
    - 两者都没有（状态没变化）   → 兜底 STATE_SAVE_MAX_INTERVAL
    正常退出时仍会无条件保存，所以崩溃窗口只影响边界精度，不影响投递。
    """
    meta = state.setdefault("_meta", {})
    since_save = now - STATE.last_state_save
    if meta.get("dirty"):
        return True
    if meta.get("boundary_dirty") and since_save >= STATE_BOUNDARY_SAVE_INTERVAL:
        return True
    return since_save >= STATE_SAVE_MAX_INTERVAL


def random_main_interval():
    """[FIX-17] 扫描失败退避：连续失败按 60→120→240→480→900s 指数递增。

    原来的实现只在连续失败 >=2 时抬到 35~60s，上限过低：cookie 失效或被限流时
    会长期以 60s 以内的频率持续打接口。成功一次即回到 20~35s 正常节奏。
    """
    failures = int(STATE.consecutive_failures or 0)
    if failures <= 0:
        return random.uniform(NORMAL_INTERVAL_MIN, NORMAL_INTERVAL_MAX)
    delay = min(SCAN_BACKOFF_MAX, SCAN_BACKOFF_BASE * (2 ** (failures - 1)))
    delay += random.uniform(0, min(20.0, delay * 0.15))
    return delay


# =========================================================
# Cookie / Logging / WBI
# =========================================================
def activate_session_cookies():
    """一次性首页访问；不保留长期 HTTP Session。"""
    try:
        resp = requests.get(
            "https://www.bilibili.com/", headers=HTTP_HEADERS,
            cookies=BILI_COOKIES, timeout=10
        )
        try:
            for k, v in resp.cookies.items():
                BILI_COOKIES[k] = v
        finally:
            resp.close()
        uuid_sec = str(uuid.uuid4())
        time_sec = str(int(time.time() * 1000 % 1e5)).ljust(5, "0")
        BILI_COOKIES.setdefault("_uuid", f"{uuid_sec}{time_sec}infoc")
        BILI_COOKIES.setdefault("CURRENT_FNVAL", "4048")
        BILI_COOKIES.setdefault("blackside_state", "1")
        logging.debug("B站首页会话 Cookie 激活完成")
        return True
    except Exception as e:
        logging.warning(f"⚠️ 首页会话激活失败: {e}")
        return False


def load_cookies_into_session():
    """兼容旧函数名；实际保存到普通 requests.get 使用的 Cookie 字典。"""
    try:
        if not os.path.exists("bili_cookie.txt"):
            logging.error("❌ 未找到 bili_cookie.txt")
            return False
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie_str = f.read().strip()
        if not cookie_str:
            return False
        BILI_COOKIES.clear()
        for item in cookie_str.split(";"):
            item = item.strip()
            if not item or "=" not in item:
                continue
            k, v = item.split("=", 1)
            k, v = k.strip(), v.strip()
            if k:
                BILI_COOKIES[k] = v
        logging.info(f"[AUTH] cookie loaded count={len(BILI_COOKIES)}")
        return bool(BILI_COOKIES)
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
    # 第三方库日志静音，避免 LOG_LEVEL=DEBUG 时被 urllib3 连接日志刷屏
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    root.setLevel(LOG_LEVEL)
    root.propagate = False
    logging.info("=" * 70)
    logging.info("B站关注动态监控 v3.3.2（feed/all｜长期稳定优化版）")
    logging.info("=" * 70)


def force_update_wbi_keys():
    try:
        r = requests.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers=HTTP_HEADERS, cookies=BILI_COOKIES, timeout=8
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
    params = dict(params or {})
    last = {"code": -500, "message": "unknown"}
    for i in range(max(1, retries)):
        try:
            resp = requests.get(
                url, params=params, headers=HTTP_HEADERS,
                cookies=BILI_COOKIES, timeout=REQUEST_TIMEOUT
            )
            status_code = resp.status_code
            try:
                data = resp.json()
            finally:
                resp.close()
            last = data if isinstance(data, dict) else {"code": -500, "message": "invalid_json"}
            code = last.get("code")
            if code == -101:
                STATE.consecutive_cookie_failures += 1
                logging.error(f"❌ Cookie 验证失败 {STATE.consecutive_cookie_failures}/3")
                notify_system_once("❌ B站 Cookie 失效预警", "Cookie 验证失败，请检查 bili_cookie.txt。")
                if STATE.consecutive_cookie_failures >= 3:
                    logging.critical("🛑 Cookie 连续失效，停止程序。")
                    globals()["IS_RUNNING"] = False
                return last
            STATE.consecutive_cookie_failures = 0
            if code in (-799, -352, -509, -412) or status_code in (412, 429):
                if ACTIVE_STATE is not None:
                    try:
                        daily = ACTIVE_STATE.setdefault("daily", {})
                        daily["rate_limit"] = int(daily.get("rate_limit", 0)) + 1
                        mark_state_dirty(ACTIVE_STATE)
                    except Exception:
                        pass
                hard = code == -412 or status_code == 412
                base = 900.0 if hard else 15.0
                cap = 900.0 if hard else 300.0
                wait = min(cap, base * (2 ** i)) + random.uniform(3, 8)
                logging.warning(f"⚠️ B站风控/限流 code={code} http={status_code}，退避 {wait:.1f}s")
                force_update_wbi_keys()
                notify_system_once("🚨 B站风控预警", f"code={code}, http={status_code}，已自动退避 {wait:.1f} 秒。")
                time.sleep(wait)
                continue
            if code == 0:
                return last
            if i < retries - 1:
                wait = min(30.0, 3.0 * (2 ** i)) + random.uniform(1, 3)
                logging.warning(f"[API重试] code={code} wait={wait:.1f}s url={url}")
                time.sleep(wait)
            else:
                return last
        except Exception as e:
            last = {"code": -500, "message": repr(e)}
            if i < retries - 1:
                wait = min(30.0, 3.0 * (2 ** i)) + random.uniform(1, 3)
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
        "version": 7,
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
            "fans_only": 0,
            "webhook_success": 0,
            "webhook_fail": 0,
            "api_fail": 0,
            "rate_limit": 0,
            "deferred": 0,
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
                "time_missing": False,
                "last_seen": _safe_int(v, 0),
                "skip_reason": "",
            }
            continue
        cleaned[dyn_id] = {
            "first_seen": _safe_int(v.get("first_seen"), 0),
            "pub_ts": _safe_int(v.get("pub_ts"), 0),
            "uid": str(v.get("uid", "") or ""),
            "refresh_seq": _safe_int(v.get("refresh_seq"), 0),
            "discovery_mode": str(v.get("discovery_mode", "") or ""),
            "status": str(v.get("status", "baseline") or "baseline"),
            "time_missing": bool(v.get("time_missing", False)),
            "last_seen": _safe_int(v.get("last_seen"), _safe_int(v.get("first_seen"), 0)),
            # [FIX-15] 过滤原因（目前仅 fans_only 使用），保留下来便于事后复核
            "skip_reason": str(v.get("skip_reason", "") or ""),
        }
        if cleaned[dyn_id]["status"] not in {
            "baseline", "queued", "retry", "sent", "deferred", "fans_only"
        }:
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

    # v7: discovered 明确记录状态，并兼容旧状态结构。
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
        now_save = time.time()
        make_backup = (now_save - STATE.last_backup_save >= STATE_BACKUP_INTERVAL) or not os.path.exists(DYNAMIC_STATE_FILE + ".bak")
        atomic_write_json(DYNAMIC_STATE_FILE, state, make_backup=make_backup)
        if make_backup:
            STATE.last_backup_save = now_save
        meta = state.setdefault("_meta", {})
        meta["dirty"] = False
        meta["boundary_dirty"] = False


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
            "fans_only": 0,
            "webhook_success": 0,
            "webhook_fail": 0,
            "api_fail": 0,
            "rate_limit": 0,
            "deferred": 0,
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


def is_fans_only_dynamic(item):
    """[FIX-15] 判断是否为「粉丝专属 / 充电专属（需付费订阅）」动态。

    返回 (是否专属, 命中原因)。命中原因用于日志，便于事后核对该不该被过滤。

    判定顺序（全部为结构化枚举，不做正文关键词匹配）：
      1. major.type == MAJOR_TYPE_UPOWER_COMMON
      2. major.type == MAJOR_TYPE_NONE 且 major.none.tips 含专属关键词
      3. additional.type 以 ADDITIONAL_TYPE_UPOWER 开头
      4. basic / module_dynamic / item 层 is_only_fans 或 is_upower_exclusive 为 true

    任何异常一律返回 (False, "")，即「放行」——宁可多推，也不因为判定异常
    把正常动态吃掉（与 is_allowed_dynamic 的保守放行原则一致）。
    """
    if not SKIP_FANS_ONLY_DYNAMIC:
        return False, ""
    try:
        if not isinstance(item, dict):
            return False, ""
        modules = item.get("modules") or {}
        dyn = modules.get("module_dynamic") or {}
        major = dyn.get("major") or {}

        major_type = str(major.get("type") or "")
        if major_type in FANS_ONLY_MAJOR_TYPES:
            return True, f"major_type={major_type}"

        if major_type == "MAJOR_TYPE_NONE":
            none_obj = major.get("none") or {}
            tips = str(none_obj.get("tips") or "")
            low = tips.lower()
            for kw in FANS_ONLY_TIPS_KEYWORDS:
                if kw.lower() in low:
                    return True, f"locked_tips={tips[:40]}"

        additional = dyn.get("additional") or {}
        add_type = str(additional.get("type") or "")
        if add_type:
            for prefix in FANS_ONLY_ADDITIONAL_PREFIXES:
                if add_type.startswith(prefix):
                    return True, f"additional_type={add_type}"

        holders = (
            ("basic", item.get("basic") or {}),
            ("module_dynamic", dyn),
            ("modules", modules),
            ("item", item),
        )
        for holder_name, holder in holders:
            if not isinstance(holder, dict):
                continue
            for key in FANS_ONLY_FLAG_KEYS:
                if holder.get(key) is True:
                    return True, f"{holder_name}.{key}=true"

        return False, ""
    except Exception as e:
        logging.debug(
            "[FANS_ONLY] check exception dyn_id=%s -> allow: %s",
            item.get("id_str") if isinstance(item, dict) else "-", repr(e),
        )
        return False, ""


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
            if not text:
                paragraphs = summary.get("paragraphs") or opus.get("paragraphs") or []
                chunks = []
                for para in paragraphs:
                    if isinstance(para, dict):
                        pnodes = para.get("nodes") or para.get("rich_text_nodes") or []
                        chunks.append("".join(n.get("text", "") for n in pnodes if isinstance(n, dict)))
                    elif isinstance(para, str):
                        chunks.append(para)
                text = normalize_text("\n".join(chunks))
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


def _normalize_image_url(url):
    if not isinstance(url, str):
        return ""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    return url if url.startswith(("http://", "https://")) else ""


def extract_images(item, include_forward_orig=True):
    """严格白名单：只取 DRAW/DYNAMIC正文与 OPUS pics；不递归扫描 JSON。"""
    urls = []
    def take(url):
        url = _normalize_image_url(url)
        if url and url not in urls:
            urls.append(url)
        return bool(url)
    try:
        modules = item.get("modules") or {}
        dyn = modules.get("module_dynamic") or {}
        major = dyn.get("major") or {}
        mt = str(major.get("type") or "")
        if mt == "MAJOR_TYPE_DRAW":
            draw = major.get("draw") or {}
            for x in draw.get("items") or []:
                if isinstance(x, dict) and take(x.get("src")) and len(urls) >= MAX_PUSH_IMAGES:
                    break
        elif mt == "MAJOR_TYPE_OPUS":
            opus = major.get("opus") or {}
            for x in opus.get("pics") or []:
                if isinstance(x, dict) and take(x.get("url")) and len(urls) >= MAX_PUSH_IMAGES:
                    break
        # [FIX-19] 取图判据由 type==DYNAMIC_TYPE_FORWARD 改为「按 orig 是否存在」：
        # 「转发 + 自己补了图」的动态 type 是 DYNAMIC_TYPE_DRAW 且带 orig，
        # 旧判据会让原动态配图整段漏取。
        if include_forward_orig and len(urls) < MAX_PUSH_IMAGES:
            orig = item.get("orig")
            if isinstance(orig, dict):
                for u in extract_images(orig, include_forward_orig=False):
                    take(u)
                    if len(urls) >= MAX_PUSH_IMAGES:
                        break
    except Exception as e:
        logging.debug(f"[IMAGE] strict extract failed dyn_id={item.get('id_str') if isinstance(item, dict) else '-'}: {e}")
    return urls[:MAX_PUSH_IMAGES]


def _minimal_push_payload(item, err=""):

    """[FIX-2] 解析彻底失败时的最小可推送载荷：保证「绝不因为解析问题而静默漏推」。"""
    dyn_id = str(item.get("id_str") or "") if isinstance(item, dict) else ""
    author = {}
    top_type = ""
    pub_ts = 0
    if isinstance(item, dict):
        top_type = str(item.get("type") or "")
        author = (item.get("modules") or {}).get("module_author") or {}
        pub_ts = _safe_int(author.get("pub_ts"), 0)
    if pub_ts <= 0:
        pub_ts = int(time.time())
    return {
        "user": str(author.get("name") or "未知UP"),
        "uid": str(author.get("mid") or ""),
        "message": f"（正文解析失败，已降级推送｜类型={top_type or '未知'}）",
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

        # [FIX-20] 图片张数统一跟随 MAX_PUSH_IMAGES：原来这里把结果硬截成 1 张
        # （cover=images[0]、one=[cover]），导致只改常量不生效。
        # 默认 MAX_PUSH_IMAGES=1，行为与旧版完全一致。
        images = extract_images(item)[:MAX_PUSH_IMAGES]
        cover = images[0] if images else ""
        return {
            "user": name,
            "uid": uid,
            "message": text,
            "time": ts_to_str(pub_ts),
            "link": f"https://t.bilibili.com/{dyn_id}",
            "cover": cover,
            "covers": list(images),
            "images": list(images),
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
            if dyn_id in PENDING_PUSH_IDS or dyn_id in SENT_ACK_IDS or is_recent_pushed(ACTIVE_STATE, dyn_id):
                return False
            outbox = ACTIVE_STATE.setdefault("feed", {}).setdefault("outbox", {})
            if dyn_id in outbox:
                return False
            if len(outbox) >= OUTBOX_MAX:
                logging.error(f"❌ Outbox 已达软上限 {OUTBOX_MAX}，暂缓新动态 dyn_id={dyn_id}，不删除既有任务")
                notify_system_once("❌ Webhook队列拥堵", f"Outbox={len(outbox)}，新动态 {dyn_id} 暂缓。")
                return False
            if notify_queue.qsize() >= NOTIFY_QUEUE_MAXSIZE - NOTIFY_QUEUE_SYSTEM_RESERVE:
                logging.warning(f"[QUEUE] dynamic queue near full, keep state dyn_id={dyn_id}")
                return False
            task = {
                "title": title, "items": items if isinstance(items, list) else [],
                "notify_type": notify_type, "dyn_id": dyn_id, "uid": str(uid or ""),
                "pub_ts": int(pub_ts or 0), "first_seen": int(first_seen or time.time()),
                "discovery_mode": discovery_mode, "created_at": time.time(),
                "attempt": 0, "next_attempt": 0,
            }
            outbox[dyn_id] = task
            PENDING_PUSH_IDS.add(dyn_id)
            mark_state_dirty(ACTIVE_STATE)
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
                PENDING_PUSH_IDS.discard(dyn_id)
                mark_state_dirty(ACTIVE_STATE)
            logging.warning(f"[QUEUE] full，Outbox 保留 dyn_id={dyn_id}，等待后续 requeue")
            return True

    task = {
        "title": title, "items": items if isinstance(items, list) else [],
        "notify_type": notify_type, "dyn_id": dyn_id, "uid": str(uid or ""),
        "pub_ts": int(pub_ts or 0), "first_seen": int(first_seen or time.time()),
        "discovery_mode": discovery_mode, "created_at": time.time(),
        "attempt": 0, "next_attempt": 0,
    }
    try:
        notify_queue.put_nowait(task)
        return True
    except queue.Full:
        logging.warning("[QUEUE] system queue full; 系统通知已跳过")
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
            wf.flush()
            try:
                os.fsync(wf.fileno())
            except Exception:
                pass
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
                "next_attempt": _safe_float(t.get("next_attempt"), 0),
                "title": str(t.get("title") or "")[:120],
                "items": t.get("items") if isinstance(t.get("items"), list) else [],
            }, ensure_ascii=False))
        except Exception:
            lines.append(json.dumps({"reason": reason, "dyn_id": dyn_id, "ts": int(time.time())}, ensure_ascii=False))
    try:
        with DEAD_LETTER_LOCK:
            with open(DEAD_LETTER_FILE, "a", encoding="utf-8") as f:
                for line in lines:
                    f.write(line + "\n")
                f.flush()
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
    max_age = OUTBOX_MAX_AGE
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
        if created and (now - created) > max_age:
            dead.append(task)
            continue
        if attempt > OUTBOX_MAX_ATTEMPTS:
            dead.append(task)
            continue
        candidates.append(task)
    candidates.sort(key=lambda t: _safe_float(t.get("created_at"), 0.0))
    if len(candidates) > OUTBOX_MAX:
        logging.error(f"⚠️ outbox={len(candidates)} 超过软上限 {OUTBOX_MAX}，不自动删除未发送任务")
    if dead and emit_dead:
        by_attempt = [t for t in dead if _safe_int(t.get("attempt"), 0) > 50]
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
    ack_ids = set(_read_sent_ack_tail())
    dropped = 0
    queued = 0
    for dyn_id, task in list(outbox.items()):
        if is_recent_pushed(state, dyn_id) or dyn_id in ack_ids:
            outbox.pop(dyn_id, None)
            PENDING_PUSH_IDS.discard(str(dyn_id))
            dropped += 1
            continue
        if _safe_float(task.get("next_attempt"), 0.0) > now:
            continue
        if queued >= NOTIFY_QUEUE_MAXSIZE - NOTIFY_QUEUE_SYSTEM_RESERVE:
            break
        try:
            notify_queue.put_nowait(task)
            PENDING_PUSH_IDS.add(str(dyn_id))
            queued += 1
        except queue.Full:
            break
    if dropped or queued:
        logging.info(f"[OUTBOX] restored={queued} dropped_sent={dropped} remain={len(outbox)}")
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
        "time_missing": pub_ts <= 0,
        "last_seen": int(task.get("first_seen") or time.time()),
    })
    entry["status"] = "sent"
    entry["pub_ts"] = pub_ts or _safe_int(entry.get("pub_ts"), 0)
    entry["uid"] = uid or str(entry.get("uid") or "")
    entry["last_seen"] = int(time.time())
    entry["time_missing"] = bool(entry.get("time_missing")) or pub_ts <= 0

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
                if ntype == "dynamic" and ACTIVE_STATE is not None:
                    try:
                        with STATE_LOCK:
                            if ack_ok:
                                mark_sent(ACTIVE_STATE, task)
                            else:
                                # Webhook 已成功但 ACK 没落盘：保留 Outbox，避免状态断层导致永久丢失。
                                ob = ACTIVE_STATE.setdefault("feed", {}).setdefault("outbox", {})
                                retry_task = dict(task)
                                retry_task["next_attempt"] = time.time() + 30
                                ob[dyn_id] = retry_task
                                PENDING_PUSH_IDS.discard(dyn_id)
                                mark_state_dirty(ACTIVE_STATE)
                                notify_system_once(
                                    "🚨 ACK 写入失败",
                                    f"动态 {dyn_id} webhook 已成功，但 ACK 暂未可靠落盘，已保留 Outbox 防重复/漏状态。",
                                )
                            save_dynamic_state(ACTIVE_STATE)
                    except Exception as se:
                        logging.error(
                            f"[发送成功但状态更新失败] dyn_id={dyn_id or '-'} err={repr(se)} ack_ok={ack_ok}"
                        )
                        if not ack_ok:
                            try:
                                with STATE_LOCK:
                                    ob = ACTIVE_STATE.setdefault("feed", {}).setdefault("outbox", {})
                                    ob[dyn_id] = dict(task)
                                    ob[dyn_id]["next_attempt"] = time.time() + 30
                                    PENDING_PUSH_IDS.discard(dyn_id)
                                    mark_state_dirty(ACTIVE_STATE)
                            except Exception:
                                pass
                        notify_system_once(
                            "🚨 发送后状态异常",
                            f"动态 {dyn_id} webhook 已成功，state 保存异常；ack_ok={ack_ok}。",
                        )
                logging.info(f"[PUSH] sent dyn_id={dyn_id or '-'} ack={ack_ok}")
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
    queued = 0
    changed = False
    with ACK_LOCK:
        ack_ids = set(SENT_ACK_IDS)
    for dyn_id, task in list(outbox.items()):
        if not isinstance(task, dict):
            outbox.pop(dyn_id, None)
            changed = True
            continue
        if is_recent_pushed(state, dyn_id) or dyn_id in ack_ids:
            outbox.pop(dyn_id, None)
            PENDING_PUSH_IDS.discard(str(dyn_id))
            changed = True
            continue
        if _safe_float(task.get("next_attempt"), 0.0) > now:
            continue
        if dyn_id in PENDING_PUSH_IDS:
            continue
        if queued >= OUTBOX_REQUEUE_BATCH:
            break
        if notify_queue.qsize() >= NOTIFY_QUEUE_MAXSIZE - NOTIFY_QUEUE_SYSTEM_RESERVE:
            break
        try:
            notify_queue.put_nowait(task)
            PENDING_PUSH_IDS.add(str(dyn_id))
            queued += 1
        except queue.Full:
            break
    if changed:
        mark_state_dirty(state)
    return queued


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
          "too_old": 0, "new": 0, "deferred": 0, "fans_only": 0,
          # [FIX-21] 与 no_pub_ts 区分：no_pub_ts 是「pub_ts 缺失条数」，
          # time_fallback 是「真正按发现时间入队条数」，两者不再恒等。
          "time_fallback": 0}
    for k in st:
        stats.setdefault(k, 0)
    type_block_ids = []
    fans_only_ids = []

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

        # [FIX-15] 粉丝/充电专属动态：账号未开通充电，推了也看不到正文，直接不推。
        # 注意这不是「静默」丢弃——登记 discovered(status=fans_only) + INFO 日志 +
        # 每日计数，既能避免每轮重复评估，也让过滤量在健康报告里可核对。
        fans_only, fans_reason = is_fans_only_dynamic(item)
        if fans_only:
            prev_entry = discovered.get(dyn_id)
            prev_status = (
                str((prev_entry or {}).get("status") or "")
                if isinstance(prev_entry, dict) else ""
            )
            if prev_status != "fans_only":
                st["fans_only"] += 1
                fans_only_ids.append(f"{dyn_id}:{fans_reason}")
                daily["fans_only"] = int(daily.get("fans_only", 0)) + 1
                logging.info(
                    f"[FILTER] skip fans_only dyn_id={dyn_id} uid={uid} "
                    f"reason={fans_reason} mode={discovery_mode}"
                )
            discovered[dyn_id] = {
                "first_seen": (_safe_int((prev_entry or {}).get("first_seen"), 0)
                               if isinstance(prev_entry, dict) and prev_entry
                               else now_ts),
                "pub_ts": pub_ts,
                "uid": uid,
                "refresh_seq": STATE.refresh_seq,
                "discovery_mode": discovery_mode,
                "status": "fans_only",
                "time_missing": pub_ts <= 0,
                "last_seen": now_ts,
                "skip_reason": fans_reason,
            }
            mark_state_dirty(state)
            continue

        if not is_allowed_dynamic(item):
            st["type_block"] += 1
            type_block_ids.append(f"{dyn_id}:{item.get('type') or '-'}")
            dbg_skip(dyn_id, uid, pub_ts, discovered.get(dyn_id), "type_filtered")
            continue
        # [FIX-7] 没有 pub_ts 也不能静默丢弃：使用本次发现时间作为临时事件时间。
        # 这样动态仍会进入 outbox，并在消息里至少带上动态直达链接。
        entry = discovered.get(dyn_id)
        entry_status = str((entry or {}).get("status") or "") if isinstance(entry, dict) else ""
        time_fallback = pub_ts <= 0
        if time_fallback:
            st["no_pub_ts"] += 1
            if entry and entry_status not in {"deferred", "retry"}:
                st["already_discovered"] += 1
                dbg_skip(dyn_id, uid, pub_ts, entry, "already_discovered_no_pub_ts")
                continue
            pub_ts = _safe_int((entry or {}).get("pub_ts"), 0) or now_ts
            if not entry:
                st["time_fallback"] += 1
                logging.warning(f"[TIME_FALLBACK] dyn_id={dyn_id} uid={uid} pub_ts缺失，首次发现按发现时间入队")
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
        entry_pub = _safe_int((entry or {}).get("pub_ts"), 0) if isinstance(entry, dict) else 0
        pub_changed = bool(entry_pub and pub_ts > entry_pub)

        # 长停机恢复：只要发布时间晚于最后一次完整成功刷新，不受6小时历史窗限制。
        deferred = entry_status == "deferred"
        force_recover = recent or downtime_new or pub_changed or deferred or (time_fallback and not entry)
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
    # [FIX-15] 专属动态过滤同样留痕，便于核对是不是误伤了正常动态
    if fans_only_ids:
        logging.info(
            f"[FILTER SKIP] fans_only count={len(fans_only_ids)} "
            f"sample={','.join(fans_only_ids[:5])} mode={discovery_mode}"
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
                discovered[dyn_id] = {
                    "first_seen": int(first_seen), "pub_ts": int(pub_ts), "uid": uid,
                    "refresh_seq": STATE.refresh_seq, "discovery_mode": discovery_mode,
                    "status": "deferred", "time_missing": False, "last_seen": int(first_seen),
                }
                state.setdefault("daily", {})["deferred"] = int(state.setdefault("daily", {}).get("deferred", 0)) + 1
                mark_state_dirty(state)
                logging.warning(f"[PUSH] enqueue_deferred dyn_id={dyn_id}，保留发现状态等待后续入队")
                continue
            discovered[dyn_id] = {
                "first_seen": first_seen, "pub_ts": pub_ts, "uid": uid,
                "refresh_seq": STATE.refresh_seq, "discovery_mode": discovery_mode,
                "status": "queued", "time_missing": False, "last_seen": int(first_seen),
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
    # [FIX-21] 「整体刷新」只统计 primary 轮次；verify / deep 分别由 verify_rounds
    # 与 deep_scans 计数（主循环里维护），避免三个口径混进同一个数字导致报告偏大。
    if mode == "primary":
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
        # [FIX-16] 边界（snapshot/baseline）变化用 boundary_dirty 批量落盘；
        # 发现新动态等关键状态仍由 enqueue_candidates / update_uid_stats_after_enqueue
        # 走 mark_state_dirty 即时落盘。
        mark_boundary_dirty(state)

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
        f"too_old={stats.get('too_old', 0)} time_fallback={stats.get('time_fallback', 0)} "
        f"fans_only={stats.get('fans_only', 0)}"
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
        f"整体刷新：{daily.get('refreshes', 0)} 次（primary）",
        f"二次确认：{daily.get('verify_rounds', 0)} 轮",
        f"深度刷新：{daily.get('deep_scans', 0)} 次",
        f"扫描条目：{daily.get('items_seen', 0)} 条（含确认/深扫重复计数）",
        f"新动态：{daily.get('new_found', 0)} 条",
        f"首次刷新发现：{daily.get('primary_found', 0)} 条",
        f"二次确认追回：{daily.get('verify_recovered', 0)} 条",
        f"深扫追回：{daily.get('deep_recovered', 0)} 条",
        f"延迟动态：{daily.get('delayed_found', 0)} 条",
        f"专属动态已过滤：{daily.get('fans_only', 0)} 条（未开通充电，不推送）",
        f"Webhook 成功：{daily.get('webhook_success', 0)}",
        f"Webhook 失败：{daily.get('webhook_fail', 0)}",
        f"Outbox 待发送：{len(outbox)}",
        f"队列暂缓：{daily.get('deferred', 0)}",
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
    ok = safe_enqueue_notify(
        "📊 B站动态监控 15:30 健康报告",
        [{"user": "系统雷达", "message": report}],
        "system",
    )
    if ok:
        STATE.last_report_date = date_str
        state.setdefault("_meta", {})["last_report_date"] = date_str
        mark_state_dirty(state)
    else:
        logging.warning("[HEALTH] 报告入队失败，下一轮仍允许重试")


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
    n_ack = load_sent_acks_into_state(state)
    if n_ack:
        logging.info(f"📥 已从 sent_ack 恢复 {n_ack} 条已发送记录")
        mark_state_dirty(state)

    logging.info("[STATE] building startup baseline")
    all_ids, offset = [], ""
    baseline_pages = 3
    now_ts = int(time.time())
    discovered = feed.setdefault("discovered", {})
    pages_ok = registered = skipped_recent = skipped_pushed = 0
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
            pending_baseline = str(data.get("update_baseline") or items[0].get("id_str") or "")
        for item in items:
            if not isinstance(item, dict):
                continue
            dyn_id = str(item.get("id_str") or "")
            if not dyn_id:
                continue
            all_ids.append(dyn_id)
            author = (item.get("modules") or {}).get("module_author") or {}
            uid = str(author.get("mid", ""))
            if uid not in target_uids:
                continue
            name = author.get("name", uid)
            pub_ts = _safe_int(author.get("pub_ts"), 0)
            stat = get_uid_stat(state, uid, name)
            remember_uid_id(stat, dyn_id)
            stat["last_global_seen"] = now_ts
            if pub_ts > 0:
                stat["last_pub_ts"] = max(_safe_int(stat.get("last_pub_ts"), 0), pub_ts)
            if is_recent_pushed(state, dyn_id):
                skipped_pushed += 1
                continue
            age = (now_ts - pub_ts) if pub_ts > 0 else 0
            last_ok = _safe_float(feed.get("last_success_refresh"), 0.0)
            recent = pub_ts > 0 and age <= RECENT_FORCE_NEW_WINDOW
            downtime_new = pub_ts > 0 and (pub_ts > last_ok if last_ok > 0 else age <= STARTUP_RECOVER_MAX_AGE)
            if pub_ts <= 0:
                discovered.setdefault(dyn_id, {
                    "first_seen": now_ts, "pub_ts": 0, "uid": uid,
                    "refresh_seq": 0, "discovery_mode": "baseline",
                    "status": "baseline", "time_missing": True, "last_seen": now_ts,
                })
                skipped_recent += 1
                registered += 1
                continue
            if recent or downtime_new:
                skipped_recent += 1
                continue
            if dyn_id not in discovered:
                discovered[dyn_id] = {
                    "first_seen": now_ts, "pub_ts": pub_ts, "uid": uid,
                    "refresh_seq": 0, "discovery_mode": "baseline",
                    "status": "baseline", "time_missing": False, "last_seen": now_ts,
                }
                registered += 1
        next_offset = str(data.get("offset") or "")
        baseline_has_more = bool(data.get("has_more") and next_offset)
        if not baseline_has_more:
            break
        offset = next_offset
        time.sleep(random.uniform(0.3, 0.6))

    if pages_ok > 0:
        old_ids = feed.get("last_snapshot_ids") or []
        old_set = set(old_ids)
        merged = list(dict.fromkeys(all_ids + [x for x in old_ids if x not in old_set]))
        feed["last_snapshot_ids"] = merged[:RECENT_SNAPSHOT_LIMIT]
        if pending_baseline and not baseline_has_more:
            feed["baseline"] = pending_baseline
        elif baseline_has_more:
            logging.info("启动基线未扫完流（仍 has_more），保留原 baseline")
        trim_discovered(state)
        mark_state_dirty(state)
        save_dynamic_state(state)
        logging.info(f"[STATE] baseline pages={pages_ok} items={len(all_ids)} registered={registered} skip_recent={skipped_recent} skip_pushed={skipped_pushed}")
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
                    f"deep={daily.get('deep_scans', 0)} new={daily.get('new_found', 0)} queue={notify_queue.qsize()} last_ok={ok_age}"
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
                # [FIX-16] 窗口外（下班后/周末）同样走分级落盘判定
                if should_save_state(state, now):
                    save_dynamic_state(state)
                    STATE.last_state_save = now
                time.sleep(2.0)
                continue

            # 每日上班打卡
            today = cn.strftime("%Y-%m-%d")
            if STATE.last_checkin_date != today:
                ok = safe_enqueue_notify(
                    "☀️ B站动态监控系统打卡上班",
                    [{"user": "系统雷达", "message": f"{today} 工作日监控开始，当前监控 {len(target_uids)} 个 UID。"}],
                    "system"
                )
                if ok:
                    STATE.last_checkin_date = today

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
                    # [FIX-18] 但连续不完整达到 DEEP_SCAN_FORCE_AFTER_FAILURES 轮后强制
                    # 放行一次深扫：深扫是追回延迟动态的最后一道网，不能因为网络抖动
                    # 导致的 partial_fail 长期失效。
                    deep_due = (now - STATE.last_deep_scan >= DEEP_SCAN_INTERVAL) and IS_RUNNING
                    force_deep = STATE.consecutive_failures >= DEEP_SCAN_FORCE_AFTER_FAILURES
                    if deep_due and (refresh_ok or force_deep):
                        state.setdefault("daily", {})["deep_scans"] = int(
                            state.setdefault("daily", {}).get("deep_scans", 0)
                        ) + 1
                        STATE.last_deep_scan = now
                        if not refresh_ok:
                            logging.warning(
                                f"⚠️ primary 连续不完整 {STATE.consecutive_failures} 轮，强制执行一次深扫追回"
                            )
                        logging.debug("🔎 开始5分钟整体关注流深扫")
                        full_refresh(
                            target_uids, state, mode="deep",
                            max_pages=DEEP_SCAN_MAX_PAGES,
                            stop_at_snapshot=True,
                        )
                    elif deep_due:
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

            # [FIX-16] 落盘分级判定统一收在 should_save_state() 里（便于验证）
            if should_save_state(state, now):
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
