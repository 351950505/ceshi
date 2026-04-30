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
import datetime
import threading
import queue
from zoneinfo import ZoneInfo
from collections import deque

import database as db
import notifier

# ================= 核心配置 =================
TARGET_UID = 1671203508
VIDEO_CHECK_INTERVAL = 21600
HEARTBEAT_INTERVAL = 30
FOLLOWING_REFRESH_INTERVAL = 3600
SOURCE_UID = 3706948578969654

FALLBACK_DYNAMIC_UIDS = [
    "3546905852250875",
    "3546961271589219",
    "3546610447419885",
    "285340365",
    "3706948578969654"
]

# ===== 评论监控配置 =====
COMMENT_SCAN_INTERVAL = 5
COMMENT_NORMAL_PAGES = 1
COMMENT_RESCAN_INTERVAL = 60
COMMENT_RESCAN_PAGES = 2
COMMENT_STARTUP_LOOKBACK = 300
COMMENT_SAFE_WINDOW = 60
COMMENT_MAX_RETRY_PAGES = 3
MAX_SEEN_COMMENTS = 5000

LOG_FILE = "bili_monitor.log"
DYNAMIC_STATE_FILE = "dynamic_state.json"
FOLLOWING_CACHE_FILE = "following_cache.json"

# ===== 运行时间窗口（中国时间）=====
RUN_TZ = "Asia/Shanghai"
RUN_WEEKDAYS = {0, 1, 2, 3, 4}
RUN_START_HOUR = 9
RUN_START_MINUTE = 20      # 已修改为 9:20 启动
RUN_END_HOUR = 16
OFF_HOURS_SLEEP = 20

# ===== 动态参数 / 智能爆发模式 =====
INIT_SLEEP_MIN, INIT_SLEEP_MAX = 3.5, 7.0
STATE_SAVE_INTERVAL = 30

BURST_MODE_DURATION = 12
BURST_COOLDOWN = 20
BURST_MAX_CHAIN = 3

BURST_INTERVAL_MIN = 1.4
BURST_INTERVAL_MAX = 1.9

NORMAL_INTERVAL_MIN = 1.9
NORMAL_INTERVAL_MAX = 2.6

IDLE_INTERVAL_MIN = 2.4
IDLE_INTERVAL_MAX = 3.2
IDLE_MODE_THRESHOLD = 300

FAILURE_EXIT_BURST = 2
FAILURE_SLOWDOWN_THRESHOLD = 3
FAILURE_SLOW_INTERVAL_MIN = 3.5
FAILURE_SLOW_INTERVAL_MAX = 5.0

# ===== 连续无更新自适应降速 =====
NO_UPDATE_SLOWDOWN_THRESHOLD_1 = 10
NO_UPDATE_SLOWDOWN_THRESHOLD_2 = 30
NO_UPDATE_INTERVAL_1_MIN = 2.6
NO_UPDATE_INTERVAL_1_MAX = 3.4
NO_UPDATE_INTERVAL_2_MIN = 3.2
NO_UPDATE_INTERVAL_2_MAX = 4.5

MAX_SEEN_DYNAMIC_IDS = 3000
DYNAMIC_NEW_WINDOW = 300
FEED_FETCH_MAX_PAGES = 3
FEED_INIT_PAGES = 2
RECENT_PUSHED_IDS_LIMIT = 1000
LAST_TS_IDS_LIMIT = 100

# ===== 动态类型过滤 =====
ALLOWED_DYNAMIC_TYPES = {
    "",
    "MAJOR_TYPE_OPUS",
    "MAJOR_TYPE_ARCHIVE",
    "MAJOR_TYPE_ARTICLE",
    "MAJOR_TYPE_DRAW"
}

ALLOWED_TOP_LEVEL_TYPES = {
    "DYNAMIC_TYPE_WORD",
    "DYNAMIC_TYPE_DRAW",
    "DYNAMIC_TYPE_AV",
    "DYNAMIC_TYPE_ARTICLE",
    "DYNAMIC_TYPE_FORWARD"
}

ALLOW_FORWARD_DYNAMIC = True
# =============================================

push_queue = queue.Queue(maxsize=500)

burst_end_time = 0
last_burst_trigger_time = 0
burst_chain_count = 0
consecutive_failures = 0
last_new_dynamic_time = 0
consecutive_no_update_rounds = 0

last_state_save = time.time()
last_seen_clean = time.time()
_last_notify_time = {}

WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}
mixinKeyEncTab = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52
]


def atomic_write_json(path, data):
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


def normalize_text(text):
    if not text:
        return ""
    text = str(text).replace("\r", "\n")
    lines = [line.strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    return "\n".join(lines).strip()


def cut_text(text, max_len=800):
    text = normalize_text(text)
    if len(text) <= max_len:
        return text
    return text[:max_len - 3].rstrip() + "..."


def is_in_monitor_window(now_dt=None):
    if now_dt is None:
        now_dt = datetime.datetime.now(ZoneInfo(RUN_TZ))

    if now_dt.weekday() not in RUN_WEEKDAYS:
        return False

    current_hm = now_dt.hour * 60 + now_dt.minute
    start_hm = RUN_START_HOUR * 60 + RUN_START_MINUTE
    end_hm = RUN_END_HOUR * 60

    return start_hm <= current_hm < end_hm


def init_logging():
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.truncate()
    except Exception:
        pass

    logger = logging.getLogger()
    logger.handlers.clear()
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    logging.info("=" * 60)
    logging.info("B站监控系统启动（增强排障版）")
    logging.info("=" * 60)


def send_failure_notification(title, message):
    key = f"{title}:{message[:100]}"
    if time.time() - _last_notify_time.get(key, 0) >= 600:
        _last_notify_time[key] = time.time()
        try:
            notifier.send_webhook_notification(title, [{"user": "系统", "message": message}])
        except Exception:
            pass


def safe_request(url, params, header, retries=5):
    h = header.copy()
    h["Connection"] = "close"
    base_delay = 3

    for i in range(retries):
        start_ts = time.time()
        try:
            logging.info(f"[请求开始] url={url} try={i + 1}/{retries} params={params}")
            r = requests.get(url, headers=h, params=params, timeout=12)
            cost = time.time() - start_ts
            logging.info(f"[请求返回] url={url} try={i + 1}/{retries} status={r.status_code} cost={cost:.2f}s")

            try:
                data = r.json()
            except Exception as je:
                logging.warning(f"JSON解析失败: url={url} status={r.status_code} err={je}")
                data = {"code": -500, "message": f"invalid json http={r.status_code}"}

            code = data.get("code")
            logging.info(f"[请求结果] url={url} code={code}")

            if code == -101:
                logging.error("Cookie失效")
                send_failure_notification("Cookie 失效", "需要重新登录")
                return {"code": -101, "need_refresh": True}

            if code in (-799, -352, -509):
                wait = base_delay * (2 ** i) + random.uniform(2.5, 6)
                logging.warning(f"风控 {code}，等待 {wait:.1f}s")
                time.sleep(wait)
                continue

            if code != 0 and i < retries - 1:
                wait = base_delay * (2 ** i) + random.uniform(0.8, 2.5)
                logging.warning(f"[请求重试] url={url} code={code} wait={wait:.1f}s")
                time.sleep(wait)
                continue

            return data

        except requests.RequestException as e:
            cost = time.time() - start_ts
            logging.warning(f"请求异常: url={url} params={params} cost={cost:.2f}s err={repr(e)}")
            time.sleep(base_delay * (2 ** i) + random.uniform(0.8, 2.5))
        except Exception as e:
            cost = time.time() - start_ts
            logging.warning(f"未知请求异常: url={url} params={params} cost={cost:.2f}s err={repr(e)}")
            time.sleep(base_delay * (2 ** i) + random.uniform(0.8, 2.5))

    logging.error(f"请求最终失败: {url}")
    send_failure_notification("API 请求最终失败", f"{url} 所有重试均失败")
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


def update_wbi_keys(header):
    try:
        data = safe_request("https://api.bilibili.com/x/web-interface/nav", None, header)
        if data.get("code") == 0:
            img = data.get("data", {}).get("wbi_img", {})
            img_url = img.get("img_url", "")
            sub_url = img.get("sub_url", "")
            if img_url and sub_url:
                WBI_KEYS["img_key"] = img_url.rsplit("/", 1)[1].split(".")[0]
                WBI_KEYS["sub_key"] = sub_url.rsplit("/", 1)[1].split(".")[0]
                WBI_KEYS["last_update"] = time.time()
                logging.info("WBI密钥已更新")
    except Exception as e:
        logging.error(f"更新WBI异常: {repr(e)}")


def wbi_request(url, params, header):
    if not WBI_KEYS["img_key"] or time.time() - WBI_KEYS["last_update"] > 21600:
        update_wbi_keys(header)

    if not WBI_KEYS["img_key"] or not WBI_KEYS["sub_key"]:
        logging.warning("WBI密钥不可用，退化为普通请求")
        return safe_request(url, params, header)

    try:
        signed = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
        return safe_request(url, signed, header)
    except Exception as e:
        logging.error(f"WBI签名异常: {repr(e)}")
        return safe_request(url, params, header)


def get_scan_interval():
    global burst_end_time, consecutive_failures, last_new_dynamic_time, consecutive_no_update_rounds

    now = time.time()

    if consecutive_failures >= FAILURE_SLOWDOWN_THRESHOLD:
        return random.uniform(FAILURE_SLOW_INTERVAL_MIN, FAILURE_SLOW_INTERVAL_MAX)

    if now < burst_end_time:
        return random.uniform(BURST_INTERVAL_MIN, BURST_INTERVAL_MAX)

    if consecutive_no_update_rounds >= NO_UPDATE_SLOWDOWN_THRESHOLD_2:
        return random.uniform(NO_UPDATE_INTERVAL_2_MIN, NO_UPDATE_INTERVAL_2_MAX)

    if consecutive_no_update_rounds >= NO_UPDATE_SLOWDOWN_THRESHOLD_1:
        return random.uniform(NO_UPDATE_INTERVAL_1_MIN, NO_UPDATE_INTERVAL_1_MAX)

    if last_new_dynamic_time > 0 and now - last_new_dynamic_time >= IDLE_MODE_THRESHOLD:
        return random.uniform(IDLE_INTERVAL_MIN, IDLE_INTERVAL_MAX)

    return random.uniform(NORMAL_INTERVAL_MIN, NORMAL_INTERVAL_MAX)


def trigger_burst_mode():
    global burst_end_time, last_burst_trigger_time, burst_chain_count

    now = time.time()

    if now - last_burst_trigger_time < BURST_COOLDOWN:
        if now < burst_end_time and burst_chain_count < BURST_MAX_CHAIN:
            burst_end_time = max(burst_end_time, now + BURST_MODE_DURATION)
            burst_chain_count += 1
            logging.info(f"🚀 爆发续期，chain={burst_chain_count}, until={int(burst_end_time)}")
        return

    burst_end_time = now + BURST_MODE_DURATION
    last_burst_trigger_time = now
    burst_chain_count = 1
    logging.info(f"🚀 进入智能爆发模式 {BURST_MODE_DURATION}s, chain={burst_chain_count}")


def exit_burst_mode(reason=""):
    global burst_end_time, burst_chain_count
    if time.time() < burst_end_time:
        logging.info(f"🛑 退出爆发模式 reason={reason}")
    burst_end_time = 0
    burst_chain_count = 0


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
    except Exception as e:
        logging.error(f"保存 following_cache 失败: {repr(e)}")


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
                state = json.load(f)

            if not isinstance(state, dict):
                return default_state

            feed = state.get("feed", {})
            if not isinstance(feed, dict):
                feed = {}

            return {
                "feed": {
                    "last_ts": int(feed.get("last_ts", 0) or 0),
                    "last_ts_ids": list(feed.get("last_ts_ids", []) or [])[:LAST_TS_IDS_LIMIT],
                    "baseline": feed.get("baseline", ""),
                    "offset": feed.get("offset", ""),
                    "recent_pushed_ids": list(feed.get("recent_pushed_ids", []) or [])[:RECENT_PUSHED_IDS_LIMIT]
                }
            }
        except Exception:
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


def clean_old_seen(seen_dynamic_ids):
    if len(seen_dynamic_ids) <= MAX_SEEN_DYNAMIC_IDS:
        return
    items = sorted(seen_dynamic_ids.items(), key=lambda x: x[1], reverse=True)
    kept = dict(items[:MAX_SEEN_DYNAMIC_IDS])
    seen_dynamic_ids.clear()
    seen_dynamic_ids.update(kept)


def init_seen_comments():
    return {"set": set(), "queue": deque()}


def add_seen_comment(seen_comments, rpid):
    s = seen_comments["set"]
    q = seen_comments["queue"]

    if rpid in s:
        return False

    s.add(rpid)
    q.append(rpid)

    while len(q) > MAX_SEEN_COMMENTS:
        old = q.popleft()
        s.discard(old)

    return True


def prune_seen_comments(seen_comments):
    s = seen_comments["set"]
    q = seen_comments["queue"]
    while len(q) > MAX_SEEN_COMMENTS:
        old = q.popleft()
        s.discard(old)


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
    last_ts_ids = list(feed_state.get("last_ts_ids", []) or [])

    if pub_ts > last_ts:
        feed_state["last_ts"] = pub_ts
        feed_state["last_ts_ids"] = [dyn_id]
    elif pub_ts == last_ts:
        if dyn_id not in last_ts_ids:
            last_ts_ids.append(dyn_id)
            feed_state["last_ts_ids"] = last_ts_ids[:LAST_TS_IDS_LIMIT]


def is_new_dynamic_candidate(feed_state, dyn_id, pub_ts, now_ts):
    last_ts = int(feed_state.get("last_ts", 0) or 0)
    last_ts_ids = set(feed_state.get("last_ts_ids", []) or [])

    if now_ts - pub_ts > DYNAMIC_NEW_WINDOW:
        return False

    if pub_ts > last_ts:
        return True

    if pub_ts == last_ts and dyn_id not in last_ts_ids:
        return True

    return False


def is_allowed_dynamic(item):
    try:
        if not isinstance(item, dict):
            return False

        top_type = item.get("type", "")
        modules = item.get("modules", {}) or {}
        dyn = modules.get("module_dynamic", {}) or {}
        major = dyn.get("major", {}) or {}
        major_type = major.get("type", "")

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

        if t == "MAJOR_TYPE_PGC":
            pgc = major.get("pgc", {}) or {}
            return normalize_text(f"【PGC】{pgc.get('title', '')}")

        if t == "MAJOR_TYPE_COURSES":
            c = major.get("courses", {}) or {}
            title = normalize_text(c.get("title", ""))
            desc_text = normalize_text(c.get("desc", ""))
            if title and desc_text:
                return f"【课程】{title}\n{desc_text}"
            return f"【课程】{title or desc_text}".strip()

        if t == "MAJOR_TYPE_MUSIC":
            m = major.get("music", {}) or {}
            return normalize_text(f"【音频】{m.get('title', '')}")

        return normalize_text(desc.get("text", ""))
    except Exception:
        return ""


def format_dynamic_message(item):
    dyn_id = item.get("id_str", "")
    author = item.get("modules", {}).get("module_author", {}) or {}
    name = author.get("name", "未知UP")
    pub_ts = int(author.get("pub_ts", 0) or 0)

    text = cut_text(extract_dynamic_text(item), 900)
    dynamic_type = item.get("type", "")

    if dynamic_type == "DYNAMIC_TYPE_FORWARD":
        orig = item.get("orig")
        if orig and isinstance(orig, dict):
            orig_text = cut_text(extract_dynamic_text(orig), 300)
            if orig_text:
                if text:
                    text = f"{text}\n\n【转发原文】\n{orig_text}"
                else:
                    text = f"【转发原文】\n{orig_text}"
            orig_id = orig.get("id_str")
            if orig_id:
                text = f"{text}\n\n原动态： https://t.bilibili.com/{orig_id}"

    if not text:
        text = "（该动态无可提取正文）"

    time_str = datetime.datetime.fromtimestamp(pub_ts).strftime("%Y-%m-%d %H:%M:%S") if pub_ts > 0 else "未知时间"

    # 提取封面图（支持图片显示）
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
            cover = major.get("opus", {}).get("pics", [{}])[0].get("url", "") or \
                    major.get("opus", {}).get("cover", "")
    except Exception:
        cover = ""

    return {
        "user": name,
        "message": text,
        "time": time_str,
        "link": f"https://www.bilibili.com/opus/{dyn_id}",   # 电脑版链接
        "cover": cover,                                      # 新增封面支持
        "kind": "dynamic"
    }


def safe_enqueue_push(item):
    try:
        push_queue.put_nowait(item)
        return True
    except queue.Full:
        logging.warning("push_queue 已满，丢弃一条动态推送")
        return False
    except Exception as e:
        logging.error(f"推送入队失败: {repr(e)}")
        return False


def push_worker():
    while True:
        try:
            item = push_queue.get(timeout=1)
            if not item:
                continue

            logging.info(
                f"[推送队列] 开始发送 user={item.get('user', '未知UP')} "
                f"time={item.get('time', '')} link={item.get('link', '')}"
            )

            title = f"{item.get('user', '未知UP')} 发布了新动态"
            ok = notifier.send_webhook_notification(
               title,
               [item],
               notify_type="dynamic"
             )
            

            if ok:
                logging.info(
                    f"[推送队列] 发送成功 user={item.get('user', '未知UP')} "
                    f"link={item.get('link', '')}"
                )
            else:
                logging.warning(
                    f"[推送队列] 发送失败 user={item.get('user', '未知UP')} "
                    f"link={item.get('link', '')}"
                )

        except queue.Empty:
            continue
        except Exception as e:
            logging.error(f"推送失败: {repr(e)}")
            logging.error(traceback.format_exc())


def fetch_following_feed(header, offset="", update_baseline=""):
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
    return wbi_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all", params, header)


def fetch_following_feed_retry(header, offset="", update_baseline="", retries=2):
    last = None
    for _ in range(retries + 1):
        data = fetch_following_feed(header, offset=offset, update_baseline=update_baseline)
        last = data
        if data.get("code") == 0:
            return data
        time.sleep(random.uniform(0.8, 1.6))
    return last or {"code": -500}


def check_feed_update(header, update_baseline):
    params = {
        "type": "all",
        "update_baseline": update_baseline or "0",
        "web_location": "333.1365"
    }
    return safe_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all/update", params, header)


def get_following_list(uid, header):
    following = []
    pn = 1
    ps = 50

    while True:
        params = {"vmid": uid, "pn": pn, "ps": ps, "order": "desc", "order_type": "attention"}
        data = safe_request("https://api.bilibili.com/x/relation/followings", params, header)
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


def init_feed_state(header, target_uids):
    global last_new_dynamic_time

    state = load_dynamic_state()
    seen_dynamic_ids = {}

    try:
        max_ts = int(state.get("feed", {}).get("last_ts", 0) or 0)
        max_ts_ids = set(state.get("feed", {}).get("last_ts_ids", []) or [])
        offset = ""
        baseline = state.get("feed", {}).get("baseline", "")

        for page_idx in range(FEED_INIT_PAGES):
            data = fetch_following_feed_retry(header, offset=offset)
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
                    seen_dynamic_ids[dyn_id] = time.time()

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
        last_new_dynamic_time = time.time()

        logging.info(f"关注流初始化完成 baseline={baseline} offset={offset} last_ts={max_ts}")
    except Exception as e:
        logging.error(f"关注流初始化异常: {repr(e)}")
        logging.error(traceback.format_exc())

    return seen_dynamic_ids, state


def process_feed_items(items, target_uids, seen_dynamic_ids, state, now_ts):
    global last_new_dynamic_time, consecutive_failures, consecutive_no_update_rounds

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

            seen_dynamic_ids[dyn_id] = time.time()

            author = item.get("modules", {}).get("module_author", {}) or {}
            author_mid = str(author.get("mid", ""))
            pub_ts = int(author.get("pub_ts", 0) or 0)
            top_type = item.get("type", "")

            # 已注释掉每条动态的详细日志，消除重复输出
            # logging.info(f"[动态项] dyn_id={dyn_id} mid={author_mid} pub_ts={pub_ts} type={top_type}")

            if author_mid not in target_uids:
                logging.info(f"[动态过滤] dyn_id={dyn_id} 原因=不在目标UID")
                continue

            if not is_allowed_dynamic(item):
                logging.info(f"[动态过滤] dyn_id={dyn_id} 原因=类型不允许")
                continue

            if is_recent_pushed(state, dyn_id):
                logging.info(f"[动态过滤] dyn_id={dyn_id} 原因=recent_pushed")
                update_last_ts_state(feed_state, dyn_id, pub_ts)
                continue

            if is_new_dynamic_candidate(feed_state, dyn_id, pub_ts, now_ts):
                logging.info(f"[动态命中] dyn_id={dyn_id} 进入候选")
                new_items.add(dyn_id)
                candidate_items[dyn_id] = item
            else:
                logging.info(f"[动态过滤] dyn_id={dyn_id} 原因=不是新动态候选")
        except Exception as e:
            logging.warning(f"处理单条动态异常: {repr(e)}")

    pushed_ids = set()
    for dyn_id in new_items:
        if dyn_id in pushed_ids:
            continue

        item = candidate_items.get(dyn_id)
        if not item:
            continue

        try:
            author = item.get("modules", {}).get("module_author", {}) or {}
            pub_ts = int(author.get("pub_ts", 0) or 0)

            push_data = format_dynamic_message(item)
            ok = safe_enqueue_push(push_data)
            if ok:
                pushed_ids.add(dyn_id)
                add_recent_pushed_id(state, dyn_id)
                update_last_ts_state(feed_state, dyn_id, pub_ts)
                has_new = True
                logging.info(
                    f"✅ 新动态 user={push_data.get('user', '未知UP')} dyn_id={dyn_id} "
                    f"pub_time={push_data.get('time', '')} link={push_data.get('link', '')}"
                )
            else:
                logging.warning(f"动态入队失败，放弃推送 dyn_id={dyn_id}")
        except Exception as e:
            logging.error(f"动态推送处理异常 dyn_id={dyn_id} err={repr(e)}")
            logging.error(traceback.format_exc())

    if has_new:
        last_new_dynamic_time = time.time()
        consecutive_failures = 0
        consecutive_no_update_rounds = 0
        trigger_burst_mode()

    return has_new


def scan_following_feed(header, target_uids, seen_dynamic_ids, state, now_ts):
    global consecutive_failures, consecutive_no_update_rounds

    feed_state = state.setdefault("feed", {
        "last_ts": 0,
        "last_ts_ids": [],
        "baseline": "",
        "offset": "",
        "recent_pushed_ids": []
    })

    baseline = feed_state.get("baseline", "")
    old_baseline = baseline

    logging.info(f"[动态扫描] 开始检查 update, baseline={baseline or 'EMPTY'}")

    update_data = check_feed_update(header, baseline)
    direct_fallback = False

    if update_data.get("code") != 0:
        consecutive_failures += 1
        logging.warning(
            f"[动态扫描] update 接口失败 code={update_data.get('code')}，连续失败={consecutive_failures}，退化到首页兜底"
        )
        direct_fallback = True
        if consecutive_failures >= FAILURE_EXIT_BURST:
            exit_burst_mode("update_failed")
    else:
        update_num = update_data.get("data", {}).get("update_num", 0)
        consecutive_failures = 0

        logging.info(f"[动态扫描] update 接口成功，update_num={update_num}")

        if update_num <= 0:
            consecutive_no_update_rounds += 1
            logging.info(f"[动态扫描] 无更新，no_update_rounds={consecutive_no_update_rounds}")
            return False

        consecutive_no_update_rounds = 0
        logging.info(f"📡 检测到关注流更新 {update_num} 条，开始拉取")

    has_new = False
    offset = ""
    page_count = 0
    candidate_baseline = baseline
    completed = True
    any_success_page = False

    while page_count < FEED_FETCH_MAX_PAGES:
        logging.info(f"[动态扫描] 拉取第 {page_count + 1} 页，offset={offset or 'EMPTY'}")
        data = fetch_following_feed_retry(header, offset=offset)

        if data.get("code") != 0:
            consecutive_failures += 1
            completed = False
            logging.warning(
                f"[动态扫描] 关注流拉取失败 page={page_count + 1} code={data.get('code')} 连续失败={consecutive_failures}"
            )
            if consecutive_failures >= FAILURE_EXIT_BURST:
                exit_burst_mode("feed_page_failed")
            break

        consecutive_failures = 0
        any_success_page = True

        feed = data.get("data", {}) or {}
        items = feed.get("items") or []
        logging.info(f"[动态扫描] 第 {page_count + 1} 页返回 items={len(items)}")

        if not items:
            logging.info(f"[动态扫描] 第 {page_count + 1} 页无 items，结束")
            break

        if page_count == 0:
            first_page_baseline = feed.get("update_baseline", "") or (items[0].get("id_str", "") if items else "")
            if first_page_baseline:
                candidate_baseline = first_page_baseline
            logging.info(f"[动态扫描] 候选 baseline={candidate_baseline or 'EMPTY'}")

        page_has_new = process_feed_items(items, target_uids, seen_dynamic_ids, state, now_ts)
        logging.info(f"[动态扫描] 第 {page_count + 1} 页处理完成，page_has_new={page_has_new}")

        if page_has_new:
            has_new = True

        reached_old = False
        if old_baseline:
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("id_str") == old_baseline:
                    reached_old = True
                    logging.info(f"[动态扫描] 命中旧 baseline={old_baseline}，停止翻页")
                    break

        offset = feed.get("offset", "")
        page_count += 1

        if direct_fallback:
            logging.info("[动态扫描] 当前为 fallback 模式，只拉首页后结束")
            break
        if reached_old:
            break
        if not offset:
            logging.info("[动态扫描] offset 为空，停止翻页")
            break

        time.sleep(random.uniform(0.4, 0.8))

    if not has_new and not direct_fallback:
        try:
            logging.info("[动态扫描] 本轮检测到 update 但未命中新动态，1秒后补拉首页确认")
            time.sleep(1.0)
            retry_data = fetch_following_feed_retry(header, offset="")
            if retry_data.get("code") == 0:
                retry_items = (retry_data.get("data", {}) or {}).get("items") or []
                logging.info(f"[动态扫描] 二次确认首页返回 items={len(retry_items)}")
                if retry_items:
                    retry_has_new = process_feed_items(
                        retry_items, target_uids, seen_dynamic_ids, state, int(time.time())
                    )
                    if retry_has_new:
                        has_new = True
                        logging.info("[动态扫描] 二次确认补拉命中新动态")
        except Exception as e:
            logging.warning(f"[动态扫描] 二次确认补拉异常: {repr(e)}")

    if completed and any_success_page:
        if candidate_baseline:
            feed_state["baseline"] = candidate_baseline
        feed_state["offset"] = offset
        logging.info(f"[动态扫描] baseline 已更新为 {feed_state['baseline']}, offset={offset or 'EMPTY'}")
    else:
        logging.warning("[动态扫描] 本轮关注流未完整成功，baseline 不前移")

    logging.info(f"[动态扫描] 本轮结束 has_new={has_new}")
    return has_new


def scan_comments_pages(oid, header, last_read_time, seen_comments, max_pages=1, startup_mode=False):
    new_list = []
    max_ctime = last_read_time
    safe_time = last_read_time - COMMENT_SAFE_WINDOW
    now_ts = int(time.time())
    url = "https://api.bilibili.com/x/v2/reply"

    max_pages = max(1, min(max_pages, COMMENT_MAX_RETRY_PAGES))
    pn = 1
    fetched = 0

    while fetched < max_pages:
        params = {"type": 1, "oid": oid, "sort": 0, "nohot": 1, "ps": 20, "pn": pn}
        data = wbi_request(url, params, header)
        if data.get("code") != 0:
            logging.warning(f"评论扫描失败 code={data.get('code')} oid={oid} pn={pn}")
            break

        replies = data.get("data", {}).get("replies", [])
        if not replies:
            break

        all_old = True

        for r in replies:
            try:
                ctime = int(r.get("ctime", 0) or 0)
                if ctime > max_ctime:
                    max_ctime = ctime

                if startup_mode:
                    if now_ts - ctime > COMMENT_STARTUP_LOOKBACK:
                        continue
                else:
                    if ctime <= safe_time:
                        continue

                all_old = False

                rpid = str(r.get("rpid", ""))
                if not rpid:
                    continue

                if add_seen_comment(seen_comments, rpid):
                    comment_time = datetime.datetime.fromtimestamp(ctime).strftime('%H:%M:%S')
                    new_list.append({
                        "user": f"[{comment_time}] {r.get('member', {}).get('uname', '')}",
                        "message": r.get("content", {}).get("message", ""),
                        "ctime": ctime,
                        "rpid": rpid
                    })

            except Exception as e:
                logging.warning(f"处理单条评论异常: {repr(e)}")

        if startup_mode:
            if len(replies) < 20:
                break
        else:
            if all_old or len(replies) < 20:
                break

        pn += 1
        fetched += 1
        time.sleep(random.uniform(0.4, 0.8))

    return new_list, max_ctime


def startup_backfill_comments(oid, title, header, seen_comments):
    if not oid:
        return int(time.time())

    logging.info("🧩 启动评论补扫开始")
    try:
        new_c, new_t = scan_comments_pages(
            oid=oid,
            header=header,
            last_read_time=int(time.time()) - COMMENT_STARTUP_LOOKBACK,
            seen_comments=seen_comments,
            max_pages=COMMENT_RESCAN_PAGES,
            startup_mode=True
        )
        if new_c:
            new_c.sort(key=lambda x: x["ctime"])
            payload = [{"user": x["user"], "message": x["message"]} for x in new_c]
            try:
                notifier.send_webhook_notification(title, payload)
            except Exception as e:
                logging.error(f"启动补扫评论推送失败: {repr(e)}")
            logging.info(f"🧩 启动补扫发送 {len(new_c)} 条评论")
        else:
            logging.info("🧩 启动补扫未发现新评论")
        return new_t
    except Exception as e:
        logging.error(f"启动补扫评论异常: {repr(e)}")
        logging.error(traceback.format_exc())
        return int(time.time())


def get_latest_video(header):
    data = safe_request(
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
        {"host_mid": TARGET_UID},
        header
    )
    if data.get("code") == -101:
        if refresh_cookie():
            return get_latest_video(get_header())
        return None
    if data.get("code") != 0:
        return None

    for item in (data.get("data", {}).get("items") or []):
        if item.get("type") == "DYNAMIC_TYPE_AV":
            try:
                return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
            except Exception:
                pass

    return None


def get_video_info(bv, header):
    data = safe_request(f"https://api.bilibili.com/x/web-interface/view?bvid={bv}", None, header)
    if data.get("code") == -101 and refresh_cookie():
        return get_video_info(bv, get_header())
    if data.get("code") == 0:
        d = data.get("data", {}) or {}
        aid = d.get("aid")
        title = d.get("title")
        if aid and title:
            return str(aid), title
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


def get_header():
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    except Exception:
        subprocess.run([sys.executable, "login_bilibili.py"])
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()

    return {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.bilibili.com/"
    }


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


def start_monitoring(header):
    global last_state_save, last_seen_clean, last_new_dynamic_time

    last_v_check = 0
    last_hb = 0
    last_comment_check = 0
    last_comment_rescan = 0
    last_following_refresh = 0
    last_d_check = 0

    oid, title = sync_latest_video(header)

    seen_comments = init_seen_comments()
    last_read_time = int(time.time())

    if oid:
        last_read_time = startup_backfill_comments(oid, title, header, seen_comments)

    following_list = load_following_cache() or get_following_list(SOURCE_UID, header) or FALLBACK_DYNAMIC_UIDS[:]
    following_list = [str(uid) for uid in following_list]
    if str(SOURCE_UID) not in following_list:
        following_list.append(str(SOURCE_UID))
    save_following_cache(following_list)

    target_uids = set(following_list)
    logging.info(f"监控 {len(target_uids)} 个 UID（关注流模式）")

    seen_dynamic_ids, state = init_feed_state(header, target_uids)
    if last_new_dynamic_time == 0:
        last_new_dynamic_time = time.time()

    threading.Thread(target=push_worker, daemon=True).start()

    logging.info("✅ 动态增强版启动（排障日志 + 二次确认补拉 + 清晰推送日志）")
    logging.info("✅ 智能爆发模式启动（冷却 + 续爆上限 + 失败退出 + 失败降速 + idle慢速）")
    logging.info("✅ 连续无更新自适应降速已启用")
    logging.info("✅ 评论去重结构优化已启用（set + deque）")
    logging.info("✅ 动态类型过滤已启用")
    logging.info("✅ 评论增强版启动（常规1页 + 定时补扫2页 + 启动补扫）")
    logging.info("✅ 仅在中国时间工作日 09:00-16:00 运行监听")

    while True:
        try:
            now = time.time()

            china_now = datetime.datetime.now(ZoneInfo(RUN_TZ))
            if not is_in_monitor_window(china_now):
                if now - last_hb >= HEARTBEAT_INTERVAL:
                    logging.info(
                        f"⏸ 当前不在监听时段，中国时间={china_now.strftime('%Y-%m-%d %H:%M:%S')}，"
                        f"仅工作日 09:00-16:00 运行"
                    )
                    last_hb = now
                time.sleep(OFF_HOURS_SLEEP)
                continue

            if now - last_d_check >= get_scan_interval():
                try:
                    state_updated = scan_following_feed(header, target_uids, seen_dynamic_ids, state, int(now))
                    if state_updated or now - last_state_save > STATE_SAVE_INTERVAL:
                        save_dynamic_state(state)
                        last_state_save = now
                except Exception as e:
                    logging.error(f"关注流扫描异常: {repr(e)}")
                    logging.error(traceback.format_exc())
                last_d_check = now

            if now - last_following_refresh >= FOLLOWING_REFRESH_INTERVAL:
                try:
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
                                logging.info(f"新UID {uid} 已加入过滤名单")
                            for uid in removed:
                                logging.info(f"UID {uid} 已移出过滤名单")

                            following_list = new_list
                            target_uids = set(following_list)
                            save_following_cache(following_list)
                            logging.info(f"过滤UID已刷新，当前共 {len(target_uids)} 个")
                except Exception as e:
                    logging.error(f"刷新关注列表异常: {repr(e)}")
                    logging.error(traceback.format_exc())

                last_following_refresh = now

            if oid and now - last_comment_check >= COMMENT_SCAN_INTERVAL:
                try:
                    new_c, new_t = scan_comments_pages(
                        oid=oid,
                        header=header,
                        last_read_time=last_read_time,
                        seen_comments=seen_comments,
                        max_pages=COMMENT_NORMAL_PAGES,
                        startup_mode=False
                    )
                    last_comment_check = now
                    if new_t > last_read_time:
                        last_read_time = new_t
                    if new_c:
                        new_c.sort(key=lambda x: x["ctime"])
                        payload = [{"user": x["user"], "message": x["message"]} for x in new_c]
                        notifier.send_webhook_notification(title, payload)
                        logging.info(f"💬 常规扫描发送 {len(new_c)} 条评论")
                except Exception as e:
                    logging.error(f"常规评论扫描异常: {repr(e)}")
                    logging.error(traceback.format_exc())

            if oid and now - last_comment_rescan >= COMMENT_RESCAN_INTERVAL:
                try:
                    new_c, new_t = scan_comments_pages(
                        oid=oid,
                        header=header,
                        last_read_time=last_read_time,
                        seen_comments=seen_comments,
                        max_pages=COMMENT_RESCAN_PAGES,
                        startup_mode=False
                    )
                    last_comment_rescan = now
                    if new_t > last_read_time:
                        last_read_time = new_t
                    if new_c:
                        new_c.sort(key=lambda x: x["ctime"])
                        payload = [{"user": x["user"], "message": x["message"]} for x in new_c]
                        notifier.send_webhook_notification(title, payload)
                        logging.info(f"🔁 补扫发送 {len(new_c)} 条评论")
                except Exception as e:
                    logging.error(f"评论补扫异常: {repr(e)}")
                    logging.error(traceback.format_exc())

            if now - last_hb >= HEARTBEAT_INTERVAL:
                logging.info(
                    f"💓 心跳正常 interval={get_scan_interval():.2f}s "
                    f"burst={'on' if time.time() < burst_end_time else 'off'} "
                    f"fail={consecutive_failures} no_update={consecutive_no_update_rounds}"
                )
                last_hb = now

            if now - last_v_check > VIDEO_CHECK_INTERVAL:
                try:
                    res = sync_latest_video(header)
                    if res:
                        oid, title = res
                        seen_comments = init_seen_comments()
                        last_read_time = int(time.time())
                        if oid:
                            last_read_time = startup_backfill_comments(oid, title, header, seen_comments)
                except Exception as e:
                    logging.error(f"视频同步异常: {repr(e)}")
                    logging.error(traceback.format_exc())

                last_v_check = now

            if now - last_seen_clean > 3600:
                try:
                    clean_old_seen(seen_dynamic_ids)
                    prune_seen_comments(seen_comments)
                    last_seen_clean = now
                    logging.info("🧹 已清理历史动态/评论去重缓存")
                except Exception as e:
                    logging.error(f"缓存清理异常: {repr(e)}")
                    logging.error(traceback.format_exc())

            time.sleep(0.5)

        except Exception:
            logging.error("主循环异常")
            logging.error(traceback.format_exc())
            time.sleep(8)


if __name__ == "__main__":
    init_logging()
    db.init_db()
    h = get_header()
    update_wbi_keys(h)
    start_monitoring(h)
