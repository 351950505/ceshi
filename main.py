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

import database as db
import notifier

# ================= 核心配置 =================
TARGET_UID = 1671203508
VIDEO_CHECK_INTERVAL = 21600
HEARTBEAT_INTERVAL = 10

SOURCE_UID = 3706948578969654
FOLLOWING_REFRESH_INTERVAL = 3600
FALLBACK_DYNAMIC_UIDS =[
    3546905852250875,
    3546961271589219,
    3546610447419885,
    285340365,
    3706948578969654
]

DYNAMIC_CHECK_INTERVAL = 15       # 动态扫描间隔（秒）
COMMENT_SCAN_INTERVAL = 5
COMMENT_MAX_PAGES = 3
COMMENT_SAFE_WINDOW = 60

# 服务器时间补偿（快2分钟）
TIME_OFFSET = -120

LOG_FILE = "bili_monitor.log"
DYNAMIC_STATE_FILE = "dynamic_state.json"
FOLLOWING_CACHE_FILE = "following_cache.json"

LAST_ERROR_ALERT = {}
ERROR_ALERT_INTERVAL = 300
# =============================================

def init_logging():
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
    logging.info("B站监控系统启动 (修复5分钟监听漏报 + 优化风控版)")
    logging.info(f"服务器时间补偿: {TIME_OFFSET} 秒")
    logging.info("=" * 60)

def refresh_cookie():
    logging.warning("Cookie 失效，尝试重新登录...")
    try:
        subprocess.run([sys.executable, "login_bilibili.py"], check=True)
        logging.info("重新登录成功")
        return True
    except Exception as e:
        logging.error(f"重新登录失败: {e}")
        return False

def send_error_alert(error_code, message):
    global LAST_ERROR_ALERT
    now = time.time()
    last_time = LAST_ERROR_ALERT.get(error_code, 0)
    if now - last_time >= ERROR_ALERT_INTERVAL:
        LAST_ERROR_ALERT[error_code] = now
        try:
            notifier.send_webhook_notification(
                f"⚠️ B站监控 API 错误",[{"user": "系统", "message": f"错误码 {error_code}: {message}"}]
            )
            logging.info(f"已发送错误告警: {error_code}")
        except Exception as e:
            logging.error(f"发送错误告警失败: {e}")

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
                logging.error(f"API 返回 -101，Cookie 失效: {url}")
                return {"code": -101, "need_refresh": True}
            if code in (-799, -352, -509):
                wait = base_delay * (2 ** i) + random.uniform(0, 2)
                logging.warning(f"触发风控/限流 ({code})，等待 {wait:.1f} 秒后重试")
                time.sleep(wait)
                continue
            if code != 0:
                logging.warning(f"API 返回错误 {code}: {data.get('message')} | URL: {url}")
                send_error_alert(str(code), data.get('message', '未知错误'))
                if i < retries - 1:
                    time.sleep(base_delay * (2 ** i))
                    continue
            return data
        except Exception as e:
            logging.error(f"请求异常: {e}")
            if i < retries - 1:
                time.sleep(base_delay * (2 ** i))
                continue
    return {"code": -500, "message": "所有重试均失败"}

# ---------------- WBI 签名 ----------------
WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}
mixinKeyEncTab =[
    46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,
    37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52
]

def getMixinKey(orig):
    return ''.join([orig[i] for i in mixinKeyEncTab])[:32]

def encWbi(params, img_key, sub_key):
    mixin_key = getMixinKey(img_key + sub_key)
    adjusted_ts = int(time.time() + TIME_OFFSET)
    params["wts"] = adjusted_ts
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
    following =[]
    pn = 1
    ps = 50
    while True:
        params = {"vmid": uid, "pn": pn, "ps": ps, "order": "desc", "order_type": "attention"}
        data = safe_request("https://api.bilibili.com/x/relation/followings", params, header)
        if data.get("code") != 0:
            logging.warning(f"获取关注列表失败 (pn={pn}): {data.get('message')}")
            break
        info = data.get("data", {})
        items = info.get("list",[])
        if not items:
            break
        for item in items:
            mid = item.get("mid")
            if mid:
                following.append(mid)
        if info.get("total", 0) <= pn * ps:
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
            return[]
    return[]

def save_following_cache(uids):
    with open(FOLLOWING_CACHE_FILE, "w") as f:
        json.dump(uids, f)

# ---------------- 动态监控核心 ----------------
def load_dynamic_state():
    if os.path.exists(DYNAMIC_STATE_FILE):
        try:
            with open(DYNAMIC_STATE_FILE, "r") as f:
                return json.load(f)
        except:
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
        if nodes:
            text_parts =[]
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
            if nodes:
                return "".join([n.get("text", "") for n in nodes if isinstance(n, dict)]).strip()
        return ""
    except Exception as e:
        logging.error(f"提取动态文本异常: {e}")
        return ""

def fetch_latest_dynamics(uid, header):
    """拉取第一页动态（最多20条）"""
    params = {
        "host_mid": uid,
        "type": "all",
        "timezone_offset": "-480",
        "platform": "web",
        "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,decorationCard,onlyfansAssetsV2,forwardListHidden,ugcDelete",
        "web_location": "333.1365",
        "offset": ""
    }
    # 🌟 修复关键：改回 safe_request，去除不必要的 WBI 签名，大幅度减少 -352 风控
    return safe_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all", params, header)

def is_fresh_dynamic(item, adjusted_now):
    """
    判断动态是否为最新（5分钟内）
    """
    modules = item.get("modules") or {}
    author = modules.get("module_author") or {}
    
    # 1. 优先使用时间戳绝对判断 (🌟修复关键：放宽到 300 秒/5分钟，应对 B站接口延迟)
    pub_ts = author.get("pub_ts", 0)
    if pub_ts > 0:
        time_diff = adjusted_now - pub_ts
        if time_diff <= 300:  
            return True
        else:
            return False

    # 2. 兜底判断文本
    pub_time = author.get("pub_time", "")
    if isinstance(pub_time, str):
        if "刚刚" in pub_time:
            return True
        if "分钟前" in pub_time:
            try:
                mins = int(pub_time.replace("分钟前", "").strip())
                if mins <= 5:
                    return True
            except:
                pass
    return False

def check_new_dynamics(uid, header, seen_dynamics, adjusted_now):
    alerts =[]
    data = fetch_latest_dynamics(uid, header)
    if data.get("code") != 0:
        logging.warning(f"UID {uid} 拉取动态失败: {data.get('message')}")
        return alerts

    feed_data = data.get("data") or {}
    items = feed_data.get("items",[])
    if not items:
        return alerts

    for item in items:
        dyn_id = item.get("id_str")
        if not dyn_id:
            continue
        if dyn_id in seen_dynamics[uid]:
            continue

        # 新动态，先判断新鲜度
        if not is_fresh_dynamic(item, adjusted_now):
            # 如果不新鲜（超过5分钟），记录 ID 但不推送
            seen_dynamics[uid].add(dyn_id)
            continue

        # 新鲜动态，触发推送
        modules = item.get("modules") or {}
        author = modules.get("module_author") or {}
        name = author.get("name", str(uid))
        text = extract_dynamic_text(item)

        # 处理转发动态
        if item.get("type") == "DYNAMIC_TYPE_FORWARD":
            orig = item.get("orig")
            if orig:
                orig_text = extract_dynamic_text(orig)
                if orig_text:
                    text = f"{text}\n【转发原文】{orig_text}" if text else f"【转发原文】{orig_text}"
                orig_id = orig.get("id_str")
                if orig_id:
                    text = f"{text}\n【原动态链接】https://t.bilibili.com/{orig_id}"

        final_msg = f"{text}\n\n🔗 直达链接: https://t.bilibili.com/{dyn_id}" if text else f"🔗 直达链接: https://t.bilibili.com/{dyn_id}"
        alerts.append({"user": name, "message": final_msg})
        seen_dynamics[uid].add(dyn_id)
        logging.info(f"✅ 成功监听到 5 分钟内的新动态 [{name}]: {dyn_id} (发布时间: {author.get('pub_time')})")

    return alerts

# ---------------- 评论监控（保持不变） ----------------
def scan_new_comments(oid, header, last_read_time, seen):
    new_list =[]
    max_ctime = last_read_time
    safe_time = last_read_time - COMMENT_SAFE_WINDOW
    for pn in range(1, COMMENT_MAX_PAGES + 1):
        params = {
            "oid": oid,
            "type": 1,
            "sort": 0,
            "pn": pn,
            "ps": 20
        }
        data = wbi_request("https://api.bilibili.com/x/v2/reply", params, header)
        if data.get("code") == -101:
            if refresh_cookie():
                return scan_new_comments(oid, get_header(), last_read_time, seen)
            else:
                break
        if data.get("code") != 0:
            logging.warning(f"旧版评论接口失败: {data.get('message')}")
            break
        replies = (data.get("data") or {}).get("replies") or[]
        if not replies:
            break
        all_old = True
        for r in replies:
            ctime = r.get("ctime", 0)
            if ctime > max_ctime:
                max_ctime = ctime
            if ctime > safe_time:
                all_old = False
                rpid = r.get("rpid_str", "")
                if rpid and rpid not in seen:
                    seen.add(rpid)
                    new_list.append({
                        "user": r["member"]["uname"],
                        "message": r["content"]["message"],
                        "ctime": ctime
                    })
        if all_old:
            break
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
    items = (data.get("data") or {}).get("items",[])
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

# ---------------- 主循环 ----------------
def start_monitoring(header):
    last_v_check = 0
    last_hb = time.time()
    last_d_check = 0
    last_comment_check = 0
    last_following_refresh = 0

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
    logging.info(f"初始监控 UID 列表 ({len(following_list)} 个)")

    # 初始化 seen_dynamics
    seen_dynamics = {}
    for uid in following_list:
        seen_dynamics[uid] = set()
        # 首次拉取，将所有现有动态 ID 加入 seen，避免一启动就误推送旧动态
        try:
            data = fetch_latest_dynamics(uid, header)
            if data.get("code") == 0:
                feed_data = data.get("data") or {}
                items = feed_data.get("items",[])
                for item in items:
                    dyn_id = item.get("id_str")
                    if dyn_id:
                        seen_dynamics[uid].add(dyn_id)
                logging.info(f"初始化 UID {uid}: 已静默收录 {len(seen_dynamics[uid])} 条历史动态")
            else:
                logging.warning(f"初始化 UID {uid} 失败: {data.get('message')}")
        except Exception as e:
            logging.error(f"初始化 UID {uid} 异常: {e}")
        time.sleep(random.uniform(0.5, 1.5))

    logging.info("监控服务已启动，正在实时扫描新动态...")

    while True:
        try:
            now = time.time()
            adjusted_now = now + TIME_OFFSET

            # 刷新关注列表（每小时）
            if now - last_following_refresh >= FOLLOWING_REFRESH_INTERVAL:
                new_list = get_following_list(SOURCE_UID, header)
                if new_list:
                    if SOURCE_UID not in new_list:
                        new_list.append(SOURCE_UID)
                    old_set = set(following_list)
                    new_set = set(new_list)
                    added = new_set - old_set
                    removed = old_set - new_set
                    if added or removed:
                        logging.info(f"关注列表变化: 新增 {len(added)}, 移除 {len(removed)}")
                        for uid in added:
                            seen_dynamics[uid] = set()
                        for uid in removed:
                            if uid in seen_dynamics:
                                del seen_dynamics[uid]
                        following_list = new_list
                        save_following_cache(following_list)
                        for uid in added:
                            try:
                                data = fetch_latest_dynamics(uid, header)
                                if data.get("code") == 0:
                                    feed_data = data.get("data") or {}
                                    items = feed_data.get("items",[])
                                    for item in items:
                                        dyn_id = item.get("id_str")
                                        if dyn_id:
                                            seen_dynamics[uid].add(dyn_id)
                            except:
                                pass
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
            if now - last_d_check >= DYNAMIC_CHECK_INTERVAL:
                all_alerts =
