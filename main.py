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
from datetime import datetime, timedelta

import database as db
import notifier

# ================= 核心配置 =================
TARGET_UID = 1671203508
VIDEO_CHECK_INTERVAL = 21600
HEARTBEAT_INTERVAL = 10

SOURCE_UID = 3706948578969654
FOLLOWING_REFRESH_INTERVAL = 3600

FALLBACK_DYNAMIC_UIDS = [
    3546905852250875,
    3546961271589219,
    3546610447419885,
    285340365,
    3706948578969654
]

DYNAMIC_CHECK_INTERVAL = 15
DYNAMIC_BURST_INTERVAL = 8
DYNAMIC_BURST_DURATION = 300
DYNAMIC_MAX_AGE = 300

COMMENT_SCAN_INTERVAL = 5
COMMENT_MAX_PAGES = 3
COMMENT_SAFE_WINDOW = 60

LOG_FILE = "bili_monitor.log"
DYNAMIC_STATE_FILE = "dynamic_state.json"
FOLLOWING_CACHE_FILE = "following_cache.json"

# 日志去重与失败通知配置
LOG_DEDUP_INTERVAL = 600      # 10秒内相同日志不重复
FAILURE_NOTIFY_INTERVAL = 600 # 10分钟内相同失败不重复通知
# =============================================

# 全局去重记录
_last_log_time = defaultdict(float)
_last_notify_time = defaultdict(float)

def should_log(message_key, interval=LOG_DEDUP_INTERVAL):
    """检查是否应该记录这条日志（10分钟内相同key不重复）"""
    now = time.time()
    if now - _last_log_time[message_key] >= interval:
        _last_log_time[message_key] = now
        return True
    return False

def should_notify(message_key, interval=FAILURE_NOTIFY_INTERVAL):
    """检查是否应该发送失败通知（10分钟内相同key不重复）"""
    now = time.time()
    if now - _last_notify_time[message_key] >= interval:
        _last_notify_time[message_key] = now
        return True
    return False

def send_failure_notification(title, message):
    """发送失败通知（去重）"""
    key = f"{title}:{message[:100]}"
    if should_notify(key):
        try:
            notifier.send_webhook_notification(title, [{"user": "系统", "message": message}])
            logging.info(f"已发送失败通知: {title}")
        except Exception as e:
            logging.error(f"发送失败通知异常: {e}")

def cleanup_log_file():
    """检查日志文件，如果超过24小时则清空重建"""
    if not os.path.exists(LOG_FILE):
        return
    try:
        mtime = os.path.getmtime(LOG_FILE)
        age = time.time() - mtime
        if age > 86400:  # 24小时
            # 关闭现有 logging handler
            for handler in logging.root.handlers[:]:
                logging.root.removeHandler(handler)
                handler.close()
            # 重新初始化日志（清空文件）
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.truncate()
            init_logging()  # 重新配置日志
            logging.info("日志文件已自动清理（超过24小时）")
    except Exception as e:
        print(f"清理日志文件失败: {e}")

def init_logging():
    """初始化日志配置（清空模式）"""
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.truncate()
    except:
        pass
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        encoding="utf-8",
        filemode="w"
    )
    logging.info("=" * 60)
    logging.info("B站监控系统启动 (动态关注列表模式 - 时间戳去重)")
    logging.info("=" * 60)

def refresh_cookie():
    logging.warning("Cookie 失效，尝试重新登录...")
    try:
        subprocess.run([sys.executable, "login_bilibili.py"], check=True)
        logging.info("重新登录成功")
        return True
    except Exception as e:
        msg = f"重新登录失败: {e}"
        logging.error(msg)
        send_failure_notification("Cookie 刷新失败", msg)
        return False

def safe_request(url, params, header, retries=3):
    h = header.copy()
    h["Connection"] = "close"
    base_delay = 2

    for i in range(retries):
        try:
            r = requests.get(url, headers=h, params=params, timeout=10)
            txt = r.text.strip()
            if not txt:
                time.sleep(base_delay * (2 ** i))
                continue
            data = r.json()
            code = data.get("code")

            if code == -101:
                msg = f"API 返回 -101，Cookie 失效: {url}"
                logging.error(msg)
                send_failure_notification("Cookie 失效", msg)
                return {"code": -101, "need_refresh": True}
            if code == -400:
                msg = f"API 返回 -400 请求错误: url={url}, params={params}"
                log_key = f"req_400_{url}"
                if should_log(log_key):
                    logging.error(msg)
                send_failure_notification("请求参数错误", msg)
                return data
            if code in (-799, -352, -509):
                wait = base_delay * (2 ** i) + random.uniform(0, 2)
                log_key = f"ratelimit_{code}_{url}"
                if should_log(log_key):
                    logging.warning(f"触发风控/限流 ({code})，等待 {wait:.1f} 秒后重试")
                time.sleep(wait)
                continue
            if code != 0:
                msg = f"API 返回错误 {code}: {data.get('message')}"
                log_key = f"api_error_{code}_{url}"
                if should_log(log_key):
                    logging.warning(msg)
                if i < retries - 1:
                    time.sleep(base_delay * (2 ** i))
                    continue
            return data
        except Exception as e:
            msg = f"请求异常: {e}"
            log_key = f"req_exception_{url}"
            if should_log(log_key):
                logging.error(msg)
            time.sleep(base_delay * (2 ** i))
    final_msg = "所有重试均失败"
    logging.error(final_msg)
    send_failure_notification("API 请求最终失败", final_msg)
    return {"code": -500, "message": final_msg}


# ---------------- WBI 签名 ----------------
WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}
mixinKeyEncTab = [
    46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,
    37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52
]

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
            img = data["data"]["wbi_img"]
            WBI_KEYS["img_key"] = img["img_url"].rsplit("/", 1)[1].split(".")[0]
            WBI_KEYS["sub_key"] = img["sub_url"].rsplit("/", 1)[1].split(".")[0]
            WBI_KEYS["last_update"] = time.time()
            logging.info("WBI密钥已更新")
        elif data.get("code") == -101:
            logging.error("获取WBI密钥时Cookie失效")
            if refresh_cookie():
                update_wbi_keys(get_header())
    except Exception as e:
        logging.error(f"更新WBI密钥异常: {e}")

def wbi_request(url, params, header):
    if not WBI_KEYS["img_key"] or time.time() - WBI_KEYS["last_update"] > 21600:
        update_wbi_keys(header)
    signed = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    return safe_request(url, signed, header)


# ---------------- 获取关注列表 ----------------
def get_following_list(uid, header):
    following = []
    pn = 1
    ps = 50
    while True:
        params = {
            "vmid": uid,
            "pn": pn,
            "ps": ps,
            "order": "desc",
            "order_type": "attention"
        }
        data = safe_request("https://api.bilibili.com/x/relation/followings", params, header)
        if data.get("code") != 0:
            logging.warning(f"获取关注列表失败 (pn={pn}): {data.get('message')}")
            break
        info = data.get("data", {})
        items = info.get("list", [])
        if not items:
            break
        for item in items:
            mid = item.get("mid")
            if mid:
                following.append(mid)
        total = info.get("total", 0)
        if total <= pn * ps:
            break
        pn += 1
        time.sleep(random.uniform(0.5, 1))
    logging.info(f"从 UID {uid} 获取关注列表，共 {len(following)} 人")
    return following

def load_following_cache():
    if os.path.exists(FOLLOWING_CACHE_FILE):
        try:
            with open(FOLLOWING_CACHE_FILE, "r") as f:
                return json.load(f)
        except:
            return []
    return []

def save_following_cache(uids):
    with open(FOLLOWING_CACHE_FILE, "w") as f:
        json.dump(uids, f)


# ---------------- 动态监控核心（基于时间戳） ----------------
def load_dynamic_state():
    if os.path.exists(DYNAMIC_STATE_FILE):
        try:
            with open(DYNAMIC_STATE_FILE, "r") as f:
                state = json.load(f)
            cleaned = {}
            for uid_str, value in state.items():
                if isinstance(value, dict):
                    if "last_ts" not in value:
                        value["last_ts"] = 0
                    if "baseline" not in value:
                        value["baseline"] = ""
                    if "offset" not in value:
                        value["offset"] = ""
                    cleaned[uid_str] = value
                else:
                    logging.warning(f"状态文件中的 UID {uid_str} 值类型错误 ({type(value).__name__})，已重置")
                    cleaned[uid_str] = {"last_ts": 0, "baseline": "", "offset": ""}
            return cleaned
        except Exception as e:
            logging.error(f"加载状态文件失败: {e}")
            return {}
    return {}

def save_dynamic_state(state):
    with open(DYNAMIC_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def extract_dynamic_text(item):
    try:
        modules = item.get("modules") or {}
        dyn = modules.get("module_dynamic") or {}
        desc = dyn.get("desc") or {}
        nodes = desc.get("rich_text_nodes") or []
        if nodes and isinstance(nodes, list):
            text_parts = []
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                node_type = node.get("type", "")
                if node_type in ("RICH_TEXT_NODE_TYPE_TEXT", "RICH_TEXT_NODE_TYPE_TOPIC",
                                 "RICH_TEXT_NODE_TYPE_AT", "RICH_TEXT_NODE_TYPE_EMOJI"):
                    text_parts.append(node.get("text", ""))
                elif node_type == "RICH_TEXT_NODE_TYPE_LOTTERY":
                    text_parts.append(node.get("text", ""))
            full_text = "".join(text_parts).strip()
            if full_text:
                return full_text
        major = dyn.get("major") or {}
        if not isinstance(major, dict):
            return ""
        major_type = major.get("type", "")
        if major_type == "MAJOR_TYPE_ARCHIVE":
            archive = major.get("archive") or {}
            title = archive.get("title", "")
            desc_text = archive.get("desc", "")
            return f"【视频】{title}\n{desc_text}".strip()
        elif major_type == "MAJOR_TYPE_ARTICLE":
            article = major.get("article") or {}
            title = article.get("title", "")
            return f"【专栏】{title}".strip()
        elif major_type == "MAJOR_TYPE_OPUS":
            opus = major.get("opus") or {}
            summary = opus.get("summary") or {}
            nodes = summary.get("rich_text_nodes") or []
            if nodes and isinstance(nodes, list):
                return "".join([n.get("text", "") for n in nodes if isinstance(n, dict)]).strip()
        return ""
    except Exception as e:
        logging.error(f"提取动态文本异常: {e}")
        return ""

def fetch_dynamics_page(uid, offset, header):
    params = {
        "host_mid": uid,
        "type": "all",
        "timezone_offset": "-480",
        "platform": "web",
        "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,decorationCard,onlyfansAssetsV2,forwardListHidden,ugcDelete",
        "web_location": "333.1365"
    }
    if offset:
        params["offset"] = offset
    return wbi_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all", params, header)

def init_dynamic_states_for_uids(uids, header):
    seen = {}
    state = load_dynamic_state()
    for uid in uids:
        uid_str = str(uid)
        seen[uid] = set()
        if uid_str not in state or not isinstance(state[uid_str], dict):
            state[uid_str] = {"last_ts": 0, "baseline": "", "offset": ""}
        try:
            data = fetch_dynamics_page(uid, "", header)
            if data.get("code") == 0:
                feed_data = data.get("data") or {}
                items = feed_data.get("items")
                if not isinstance(items, list):
                    items = []
                offset = feed_data.get("offset", "")
                baseline = items[0].get("id_str", "") if items else ""
                max_ts = 0
                for item in items:
                    if isinstance(item, dict):
                        dyn_id = item.get("id_str")
                        if dyn_id:
                            seen[uid].add(dyn_id)
                        modules = item.get("modules") or {}
                        author = modules.get("module_author") or {}
                        pub_ts = author.get("pub_ts", 0)
                        if pub_ts > max_ts:
                            max_ts = pub_ts
                if max_ts > 0:
                    state[uid_str]["last_ts"] = max_ts
                if baseline:
                    state[uid_str]["baseline"] = baseline
                if offset:
                    state[uid_str]["offset"] = offset
                logging.info(f"初始化 UID {uid}: last_ts={max_ts}, baseline={baseline}, offset={offset}, 已收录 {len(seen[uid])} 条动态")
            elif data.get("code") == -101:
                if refresh_cookie():
                    return init_dynamic_states_for_uids(uids, get_header())
                else:
                    logging.warning(f"初始化 UID {uid} 失败: Cookie 无效")
            else:
                logging.warning(f"初始化 UID {uid} 失败: {data.get('message')}")
        except Exception as e:
            logging.error(f"初始化 UID {uid} 异常: {e}")
            if uid_str not in state or not isinstance(state[uid_str], dict):
                state[uid_str] = {"last_ts": 0, "baseline": "", "offset": ""}
        time.sleep(random.uniform(0.5, 1))
    save_dynamic_state(state)
    return seen

def check_new_dynamics_for_uid(uid, header, seen_dynamics, state, now_ts):
    alerts = []
    uid_str = str(uid)
    current_state = state.get(uid_str)
    if not isinstance(current_state, dict):
        logging.warning(f"UID {uid} 状态无效 (类型: {type(current_state).__name__})，重置")
        state[uid_str] = {"last_ts": 0, "baseline": "", "offset": ""}
        current_state = state[uid_str]
    last_ts = current_state.get("last_ts", 0)
    offset = current_state.get("offset", "")
    data = fetch_dynamics_page(uid, offset, header)
    if data.get("code") != 0:
        logging.warning(f"UID {uid} 拉取动态失败: {data.get('message')}")
        return alerts, False, False

    feed_data = data.get("data") or {}
    items = feed_data.get("items")
    if not isinstance(items, list):
        items = []
    new_offset = feed_data.get("offset", offset)
    new_baseline = items[0].get("id_str", "") if items else ""

    new_items = []
    max_pub_ts = last_ts
    for item in items:
        if not isinstance(item, dict):
            continue
        dyn_id = item.get("id_str")
        if not dyn_id:
            continue
        modules = item.get("modules") or {}
        author = modules.get("module_author") or {}
        pub_ts = author.get("pub_ts", 0)
        if pub_ts > max_pub_ts:
            max_pub_ts = pub_ts
        if pub_ts > last_ts and (now_ts - pub_ts <= DYNAMIC_MAX_AGE):
            new_items.append(item)
            if dyn_id not in seen_dynamics[uid]:
                seen_dynamics[uid].add(dyn_id)
        else:
            if dyn_id not in seen_dynamics[uid]:
                seen_dynamics[uid].add(dyn_id)

    if max_pub_ts > last_ts:
        state[uid_str]["last_ts"] = max_pub_ts
    if new_offset != offset:
        state[uid_str]["offset"] = new_offset
    if new_baseline:
        state[uid_str]["baseline"] = new_baseline

    has_new = False
    for item in new_items:
        dyn_id = item.get("id_str")
        modules = item.get("modules") or {}
        author = modules.get("module_author") or {}
        name = author.get("name", str(uid))
        text = extract_dynamic_text(item)
        if item.get("type") == "DYNAMIC_TYPE_FORWARD":
            orig = item.get("orig")
            if orig and isinstance(orig, dict):
                orig_text = extract_dynamic_text(orig)
                if orig_text:
                    text = f"{text}\n【转发原文】{orig_text}" if text else f"【转发原文】{orig_text}"
                orig_id = orig.get("id_str")
                if orig_id:
                    text = f"{text}\n【原动态链接】https://t.bilibili.com/{orig_id}"
        final_msg = f"{text}\n\n🔗 直达链接: https://t.bilibili.com/{dyn_id}" if text else f"🔗 直达链接: https://t.bilibili.com/{dyn_id}"
        alerts.append({"user": name, "message": final_msg})
        has_new = True
        logging.info(f"✅ 抓取到新动态 [{name}]: {dyn_id} pub_ts={author.get('pub_ts',0)}")
    if has_new:
        logging.info(f"UID {uid} 本次发现 {len(new_items)} 条新动态，更新 last_ts 为 {max_pub_ts}")
    return alerts, True, has_new


# ---------------- 评论监控 ----------------
def scan_new_comments(oid, header, last_read_time, seen):
    new_list = []
    max_ctime = last_read_time
    safe_time = last_read_time - COMMENT_SAFE_WINDOW
    url = "https://api.bilibili.com/x/v2/reply"
    pn = 1
    fetched = 0
    while fetched < COMMENT_MAX_PAGES:
        params = {
            "type": 1,
            "oid": oid,
            "sort": 0,
            "nohot": 1,
            "ps": 20,
            "pn": pn
        }
        data = wbi_request(url, params, header)
        if data.get("code") != 0:
            logging.warning(f"评论接口失败 (pn={pn}): {data.get('message')}")
            break
        reply_data = data.get("data", {})
        replies = reply_data.get("replies", [])
        if not replies:
            break
        all_old = True
        for r in replies:
            ctime = r.get("ctime", 0)
            if ctime > max_ctime:
                max_ctime = ctime
            if ctime > safe_time:
                all_old = False
                rpid = str(r.get("rpid", ""))
                if rpid and rpid not in seen:
                    seen.add(rpid)
                    new_list.append({
                        "user": r["member"]["uname"],
                        "message": r["content"]["message"],
                        "ctime": ctime
                    })
        if all_old:
            break
        if len(replies) < params["ps"]:
            break
        pn += 1
        fetched += 1
        time.sleep(random.uniform(0.3, 0.6))
    return new_list, max_ctime


# ---------------- 视频监控 ----------------
def get_latest_video(header):
    data = safe_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space", {"host_mid": TARGET_UID}, header)
    if data.get("code") == -101:
        if refresh_cookie():
            return get_latest_video(get_header())
        return None
    if data.get("code") != 0:
        return None
    items = (data.get("data") or {}).get("items", [])
    for item in items:
        try:
            if item.get("type") == "DYNAMIC_TYPE_AV":
                return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
        except:
            pass
    return None

def get_video_info(bv, header):
    data = safe_request(f"https://api.bilibili.com/x/web-interface/view?bvid={bv}", None, header)
    if data.get("code") == -101:
        if refresh_cookie():
            return get_video_info(bv, get_header())
        return None, None
    if data.get("code") == 0:
        return str(data["data"]["aid"]), data["data"]["title"]
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


# ---------------- 主循环 ----------------
def get_header():
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    except:
        subprocess.run([sys.executable, "login_bilibili.py"])
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    return {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/"
    }

def start_monitoring(header):
    last_v_check = 0
    last_hb = time.time()
    last_d_check = 0
    last_comment_check = 0
    last_following_refresh = 0
    last_cleanup_check = time.time()
    burst_end = 0

    oid, title = sync_latest_video(header)

    last_read_time = int(time.time())
    seen_comments = set()

    following_list = load_following_cache()
    if not following_list:
        following_list = get_following_list(SOURCE_UID, header)
        if not following_list:
            logging.warning("获取关注列表失败，使用备用静态列表")
            following_list = FALLBACK_DYNAMIC_UIDS
        save_following_cache(following_list)
    if SOURCE_UID not in following_list:
        following_list.append(SOURCE_UID)
    logging.info(f"初始监控 UID 列表 ({len(following_list)} 个): {following_list}")

    seen_dynamics = init_dynamic_states_for_uids(following_list, header)
    state = load_dynamic_state()

    logging.info("监控服务已启动，正在扫描新数据...")

    while True:
        try:
            now = time.time()

            # 日志清理（每小时检查一次）
            if now - last_cleanup_check >= 3600:
                cleanup_log_file()
                last_cleanup_check = now

            # 定时刷新关注列表
            if now - last_following_refresh >= FOLLOWING_REFRESH_INTERVAL:
                logging.info("开始刷新关注列表...")
                new_list = get_following_list(SOURCE_UID, header)
                if new_list:
                    if SOURCE_UID not in new_list:
                        new_list.append(SOURCE_UID)
                    old_set = set(following_list)
                    new_set = set(new_list)
                    added = new_set - old_set
                    removed = old_set - new_set
                    if added or removed:
                        logging.info(f"关注列表变化: 新增 {len(added)} 个, 移除 {len(removed)} 个")
                        for uid in added:
                            if str(uid) not in state or not isinstance(state.get(str(uid)), dict):
                                state[str(uid)] = {"last_ts": 0, "baseline": "", "offset": ""}
                            seen_dynamics[uid] = set()
                        for uid in removed:
                            uid_str = str(uid)
                            if uid_str in state:
                                del state[uid_str]
                            if uid in seen_dynamics:
                                del seen_dynamics[uid]
                        following_list = new_list
                        save_following_cache(following_list)
                        save_dynamic_state(state)
                        logging.info(f"监控列表已更新，当前共 {len(following_list)} 个 UID")
                    else:
                        logging.info("关注列表无变化")
                else:
                    logging.warning("刷新关注列表失败，保持原有列表")
                last_following_refresh = now

            # 评论监控
            if oid and (now - last_comment_check >= COMMENT_SCAN_INTERVAL):
                new_c, new_t = scan_new_comments(oid, header, last_read_time, seen_comments)
                last_comment_check = now
                if new_t > last_read_time:
                    last_read_time = new_t
                if new_c:
                    new_c.sort(key=lambda x: x["ctime"])
                    try:
                        notifier.send_webhook_notification(title, new_c)
                        logging.info(f"💬 成功发送 {len(new_c)} 条新评论通知")
                    except Exception as e:
                        logging.error(f"评论通知发送失败: {e}")

            # 动态监控
            interval = DYNAMIC_BURST_INTERVAL if now < burst_end else DYNAMIC_CHECK_INTERVAL
            if now - last_d_check >= interval:
                logging.info(f"开始动态扫描，当前监控 {len(following_list)} 个UID")
                all_alerts = []
                state_updated = False
                for idx, uid in enumerate(following_list):
                    alerts, updated, has_new = check_new_dynamics_for_uid(uid, header, seen_dynamics, state, now)
                    if alerts:
                        all_alerts.extend(alerts)
                    if updated:
                        state_updated = True
                    if has_new and not burst_end:
                        burst_end = now + DYNAMIC_BURST_DURATION
                    time.sleep(random.uniform(0.5, 1))
                if state_updated:
                    save_dynamic_state(state)
                if all_alerts:
                    try:
                        notifier.send_webhook_notification("💡 特别关注UP主发布新内容", all_alerts)
                        logging.info(f"🚀 成功发送 {len(all_alerts)} 条 Webhook 动态通知！")
                    except Exception as e:
                        logging.error(f"❌ Webhook 发送失败: {e}")
                last_d_check = now

            # 心跳
            if now - last_hb >= HEARTBEAT_INTERVAL:
                logging.info("💓 心跳: 监控系统正常运行中")
                last_hb = now

            time.sleep(random.uniform(2, 4))

            # 视频检查
            if now - last_v_check > VIDEO_CHECK_INTERVAL:
                res = sync_latest_video(header)
                if res:
                    oid, title = res
                last_v_check = now

        except Exception:
            logging.error(traceback.format_exc())
            time.sleep(60)


if __name__ == "__main__":
    init_logging()
    db.init_db()
    h = get_header()
    update_wbi_keys(h)
    start_monitoring(h)
