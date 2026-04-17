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
import datetime

import database as db
import notifier

# ================= 核心配置 =================
TARGET_UID = 1671203508
VIDEO_CHECK_INTERVAL = 21600
HEARTBEAT_INTERVAL = 600

SOURCE_UID = 3706948578969654
FOLLOWING_REFRESH_INTERVAL = 3600

# 备用静态列表（当 API 获取失败时使用）
FALLBACK_DYNAMIC_UIDS =[
    "3546905852250875",
    "3546961271589219",
    "3546610447419885",
    "285340365",
    "3706948578969654"
]

DYNAMIC_CHECK_INTERVAL = 15
DYNAMIC_BURST_INTERVAL = 8
DYNAMIC_BURST_DURATION = 300
DYNAMIC_MAX_AGE = 86400  # 宽容度24小时，防CDN延迟漏报

COMMENT_SCAN_INTERVAL = 5
COMMENT_MAX_PAGES = 3
COMMENT_SAFE_WINDOW = 60

LOG_FILE = "bili_monitor.log"
DYNAMIC_STATE_FILE = "dynamic_state.json"
FOLLOWING_CACHE_FILE = "following_cache.json"

LOG_DEDUP_INTERVAL = 600      
FAILURE_NOTIFY_INTERVAL = 600 

_last_log_time = defaultdict(float)
_last_notify_time = defaultdict(float)

# ---------- 辅助函数 ----------
def should_log(message_key, interval=LOG_DEDUP_INTERVAL):
    now = time.time()
    if now - _last_log_time[message_key] >= interval:
        _last_log_time[message_key] = now
        return True
    return False

def should_notify(message_key, interval=FAILURE_NOTIFY_INTERVAL):
    now = time.time()
    if now - _last_notify_time[message_key] >= interval:
        _last_notify_time[message_key] = now
        return True
    return False

def send_failure_notification(title, message):
    key = f"{title}:{message[:100]}"
    if should_notify(key):
        try:
            notifier.send_webhook_notification(title, [{"user": "系统", "message": message}])
            logging.info(f"已发送失败通知: {title}")
        except Exception as e:
            logging.error(f"发送失败通知异常: {e}")

def cleanup_log_file():
    if not os.path.exists(LOG_FILE): return
    try:
        if time.time() - os.path.getmtime(LOG_FILE) > 86400:
            for handler in logging.root.handlers[:]:
                logging.root.removeHandler(handler)
                handler.close()
            with open(LOG_FILE, "w", encoding="utf-8") as f: f.truncate()
            init_logging()
            logging.info("日志文件已自动清理（超过24小时）")
    except Exception as e:
        print(f"清理日志失败: {e}")

def init_logging():
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "w", encoding="utf-8") as f: f.truncate()
    except: pass
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s[%(levelname)s] %(message)s",
        encoding="utf-8",
        filemode="w"
    )
    logging.info("=" * 60)
    logging.info("B站监控系统启动 (解卡顿熔断 + 精确时间 + 完美动态解析)")
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

# 🌟 快速熔断防卡死
def safe_request(url, params, header, retries=3, fast_fail=False):
    h = header.copy()
    h["Connection"] = "close"
    base_delay = 2

    for i in range(retries):
        try:
            r = requests.get(url, headers=h, params=params, timeout=10)
            txt = r.text.strip()
            if not txt:
                if fast_fail: return {"code": -500}
                time.sleep(base_delay * (2 ** i))
                continue
            data = r.json()
            code = data.get("code")

            if code == -101:
                if should_notify("cookie_invalid"): send_failure_notification("Cookie 失效", f"URL: {url}")
                return {"code": -101, "need_refresh": True}
            
            if code in (-799, -352, -509):
                if fast_fail: return {"code": code, "message": "限流快熔断"}
                wait = base_delay * (2 ** i) + random.uniform(0, 2)
                if should_log(f"ratelimit_{code}"): logging.warning(f"风控 ({code})，等待 {wait:.1f} 秒")
                time.sleep(wait)
                continue
                
            if code != 0 and code != -400:
                if should_log(f"api_error_{code}"): logging.warning(f"API 错误 {code}: {data.get('message')}")
                if i < retries - 1 and not fast_fail:
                    time.sleep(base_delay * (2 ** i))
                    continue
            return data
            
        except Exception as e:
            if fast_fail: return {"code": -500}
            if should_log(f"req_exc_{url}"): logging.error(f"请求异常: {e}")
            time.sleep(base_delay * (2 ** i))
            
    return {"code": -500, "message": "重试均失败"}

# ---------------- WBI 签名 ----------------
WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}
mixinKeyEncTab =[46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]

def getMixinKey(orig): return ''.join([orig[i] for i in mixinKeyEncTab])[:32]

def encWbi(params, img_key, sub_key):
    mixin_key = getMixinKey(img_key + sub_key)
    params["wts"] = int(time.time())
    params = dict(sorted(params.items()))
    filtered = {k: str(v).translate({ord(c): None for c in "!'()*"}) for k, v in params.items()}
    query = urllib.parse.urlencode(filtered, quote_via=urllib.parse.quote)
    filtered["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
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
    except: pass

def wbi_request(url, params, header, fast_fail=False):
    if not WBI_KEYS["img_key"] or time.time() - WBI_KEYS["last_update"] > 21600:
        update_wbi_keys(header)
    signed = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    return safe_request(url, signed, header, fast_fail=fast_fail)

# ---------------- 关注列表获取 ----------------
def get_following_list(uid, header):
    following =[]
    pn, ps = 1, 50
    while True:
        params = {"vmid": uid, "pn": pn, "ps": ps, "order": "desc", "order_type": "attention"}
        data = safe_request("https://api.bilibili.com/x/relation/followings", params, header)
        if data.get("code") != 0: break
        info = data.get("data", {})
        items = info.get("list", [])
        if not items: break
        following.extend([str(item["mid"]) for item in items if item.get("mid")])
        if info.get("total", 0) <= pn * ps: break
        pn += 1
        time.sleep(random.uniform(0.3, 0.6))
    if following: logging.info(f"成功获取 UID {uid} 的关注列表，共 {len(following)} 人")
    return following

def load_following_cache():
    if os.path.exists(FOLLOWING_CACHE_FILE):
        try:
            with open(FOLLOWING_CACHE_FILE, "r") as f: return json.load(f)
        except: pass
    return[]

def save_following_cache(uids):
    with open(FOLLOWING_CACHE_FILE, "w") as f: json.dump(uids, f)

# ---------------- 动态解析引擎 ----------------
def parse_rich_nodes(nodes):
    if not nodes or not isinstance(nodes, list): return ""
    return "".join([str(n.get("text", "")) for n in nodes if isinstance(n, dict)])

def parse_opus_paragraphs(paragraphs):
    if not paragraphs or not isinstance(paragraphs, list): return ""
    p_texts =[]
    for p in paragraphs:
        if isinstance(p, dict) and "children" in p:
            p_texts.append("".join([str(c.get("text", "")) for c in p["children"] if isinstance(c, dict)]))
    return "\n".join(p_texts)

def extract_dynamic_text(item):
    try:
        res =[]
        dyn_type = item.get("type", "")
        if dyn_type == "DYNAMIC_TYPE_FORWARD": res.append("【🔄 转发动态】")
        elif dyn_type == "DYNAMIC_TYPE_LIVE_RCMD": res.append("【🔴 直播推送】")

        def parse_module(dyn_obj):
            out =[]
            if not dyn_obj: return out
            desc = dyn_obj.get("desc") or {}
            desc_text = parse_rich_nodes(desc.get("rich_text_nodes")) or str(desc.get("text") or "")
            if desc_text.strip(): out.append(desc_text.strip())

            major = dyn_obj.get("major") or {}
            m_type = major.get("type", "")
            
            if m_type == "MAJOR_TYPE_OPUS":
                opus = major.get("opus") or {}
                if opus.get("title"): out.append(f"📰 图文: 《{opus.get('title')}》")
                content_str = parse_opus_paragraphs(opus.get("content", {}).get("paragraphs",[]))
                if not content_str:
                    summary = opus.get("summary") or {}
                    content_str = parse_rich_nodes(summary.get("rich_text_nodes")) or str(summary.get("text") or "")
                if content_str.strip(): out.append(f"📝 正文: {content_str.strip()}")
                if opus.get("pics"): out.append(f"🖼️ [附图 {len(opus.get('pics'))} 张]")
                
            elif m_type == "MAJOR_TYPE_ARCHIVE":
                arc = major.get("archive") or {}
                if arc.get("title"): out.append(f"▶️ 视频: 《{arc.get('title')}》")
                if arc.get("desc"): out.append(f"📝 简介: {arc.get('desc')}")
                
            elif m_type == "MAJOR_TYPE_DRAW":
                items = major.get("draw", {}).get("items") or[]
                if items: out.append(f"🖼️[共 {len(items)} 张图片]")
                
            elif m_type == "MAJOR_TYPE_ARTICLE":
                art = major.get("article") or {}
                if art.get("title"): out.append(f"📚 专栏: 《{art.get('title')}》")
            return out

        modules = item.get("modules") or {}
        res.extend(parse_module(modules.get("module_dynamic")))

        orig = item.get("orig")
        if orig:
            res.append("\n------ 被转发内容 ------")
            orig_author = orig.get("modules", {}).get("module_author", {}).get("name", "某用户")
            res.append(f"@{orig_author}:")
            res.extend(parse_module(orig.get("modules", {}).get("module_dynamic")))

        final_text = "\n".join(res).strip()
        if len(final_text) > 1500: final_text = final_text[:1500] + "\n...(内容已截断)"
        return final_text
    except Exception as e:
        return "发布了新动态 (内容解析兜底)"

# ---------------- 动态监控核心 ----------------
def load_dynamic_state():
    if os.path.exists(DYNAMIC_STATE_FILE):
        try:
            with open(DYNAMIC_STATE_FILE, "r") as f: return json.load(f).get("seen_ids",[])
        except: pass
    return[]

def save_dynamic_state(seen_list):
    with open(DYNAMIC_STATE_FILE, "w") as f: json.dump({"seen_ids": list(seen_list)}, f)

def fetch_dynamics_page(uid, header):
    params = {"host_mid": uid}
    return safe_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space", params, header, fast_fail=True)

def init_dynamic_states_for_uids(uids, header):
    seen = set(load_dynamic_state())
    new_adds = 0
    for uid in uids:
        try:
            data = fetch_dynamics_page(str(uid), header)
            if data.get("code") == 0:
                for item in data.get("data", {}).get("items",[]):
                    dyn_id = item.get("id_str")
                    if dyn_id and dyn_id not in seen:
                        seen.add(dyn_id)
                        new_adds += 1
            time.sleep(random.uniform(0.1, 0.3))
        except: pass
    save_dynamic_state(seen)
    logging.info(f"动态初始化完成，内存库共 {len(seen)} 条数据")
    return seen

def check_new_dynamics_for_uid(uid, header, seen_dynamics, now_ts):
    alerts =[]
    uid_str = str(uid)
    data = fetch_dynamics_page(uid_str, header)
    
    if data.get("code") != 0: return alerts, False

    items = data.get("data", {}).get("items",[])
    has_new = False

    for item in items:
        if not isinstance(item, dict): continue
        dyn_id = item.get("id_str")
        if not dyn_id or dyn_id in seen_dynamics: continue

        seen_dynamics.add(dyn_id)

        modules = item.get("modules") or {}
        author = modules.get("module_author") or {}
        pub_ts = author.get("pub_ts", 0)
        
        # 防止过旧置顶
        if now_ts - pub_ts > DYNAMIC_MAX_AGE: continue

        # 🌟 获取并格式化发布时间
        pub_time_str = datetime.datetime.fromtimestamp(pub_ts).strftime('%Y-%m-%d %H:%M:%S') if pub_ts else "刚刚"
        name = author.get("name", uid_str)
        text = extract_dynamic_text(item)
        
        final_msg = f"【发布时间】{pub_time_str}\n{text}\n\n🔗 直达链接: https://t.bilibili.com/{dyn_id}"
        alerts.append({"user": name, "message": final_msg})
        has_new = True
        logging.info(f"✅ 抓取到新动态[{name}]: {dyn_id} 发布于 {pub_time_str}")

    return alerts, has_new

# ---------------- 评论监控 ----------------
def scan_new_comments(oid, header, last_read_time, seen):
    new_list =[]
    max_ctime = last_read_time
    safe_time = last_read_time - COMMENT_SAFE_WINDOW
    url = "https://api.bilibili.com/x/v2/reply"
    pn = 1
    while pn <= COMMENT_MAX_PAGES:
        params = {"type": 1, "oid": oid, "sort": 0, "nohot": 1, "ps": 20, "pn": pn}
        data = wbi_request(url, params, header)
        if data.get("code") != 0: break
        replies = data.get("data", {}).get("replies",[])
        if not replies: break
        
        all_old = True
        for r in replies:
            ctime = r.get("ctime", 0)
            if ctime > max_ctime: max_ctime = ctime
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
        if all_old or len(replies) < params["ps"]: break
        pn += 1
        time.sleep(random.uniform(0.3, 0.6))
    return new_list, max_ctime

# ---------------- 视频监控 ----------------
def get_latest_video(header):
    data = safe_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space", {"host_mid": TARGET_UID}, header)
    if data.get("code") == -101 and refresh_cookie(): return get_latest_video(get_header())
    for item in (data.get("data") or {}).get("items",[]):
        try:
            if item.get("type") == "DYNAMIC_TYPE_AV":
                return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
        except: pass
    return None

def get_video_info(bv, header):
    data = safe_request(f"https://api.bilibili.com/x/web-interface/view?bvid={bv}", None, header)
    if data.get("code") == -101 and refresh_cookie(): return get_video_info(bv, get_header())
    if data.get("code") == 0: return str(data["data"]["aid"]), data["data"]["title"]
    return None, None

def sync_latest_video(header):
    bv = get_latest_video(header)
    if not bv: return None, None
    videos = db.get_monitored_videos()
    if videos and videos[0][1] == bv: return videos[0][0], videos[0][2]
    oid, title = get_video_info(bv, header)
    if oid:
        db.clear_videos()
        db.add_video_to_db(oid, bv, title)
        return oid, title
    return None, None

# ---------------- 主循环 ----------------
def is_work_time(now=None):
    if now is None: now = datetime.datetime.now()
    if now.weekday() >= 5: return False
    return datetime.time(8, 30) <= now.time() <= datetime.time(17, 0)

def get_sleep_until_work_time(now=None):
    if now is None: now = datetime.datetime.now()
    target = datetime.datetime(now.year, now.month, now.day, 8, 30)
    if now > target: target += datetime.timedelta(days=1)
    while target.weekday() >= 5: target += datetime.timedelta(days=1)
    return max(1, (target - now).total_seconds())

def get_header():
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f: cookie = f.read().strip()
    except:
        subprocess.run([sys.executable, "login_bilibili.py"])
        with open("bili_cookie.txt", "r", encoding="utf-8") as f: cookie = f.read().strip()
    return {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://space.bilibili.com/"
    }

def start_monitoring(header):
    last_v_check = last_hb = last_d_check = last_comment_check = last_following_refresh = last_cleanup_check = time.time()
    burst_end = 0

    oid, title = sync_latest_video(header)
    last_read_time = int(time.time())
    seen_comments = set()

    following_list = load_following_cache()
    if not following_list:
        following_list = get_following_list(SOURCE_UID, header) or FALLBACK_DYNAMIC_UIDS.copy()
        if str(SOURCE_UID) not in following_list: following_list.append(str(SOURCE_UID))
        save_following_cache(following_list)

    seen_dynamics = init_dynamic_states_for_uids(following_list, header)

    while True:
        try:
            now_dt = datetime.datetime.now()
            if not is_work_time(now_dt):
                sleep_sec = get_sleep_until_work_time(now_dt)
                logging.info(f"非工作时间，休眠至 {datetime.datetime.now() + datetime.timedelta(seconds=sleep_sec)}")
                time.sleep(sleep_sec)
                header = get_header()
                update_wbi_keys(header)
                continue

            now = time.time()
            if now - last_cleanup_check >= 3600:
                cleanup_log_file()
                last_cleanup_check = now

            if now - last_following_refresh >= FOLLOWING_REFRESH_INTERVAL:
                new_list = get_following_list(SOURCE_UID, header)
                if new_list:
                    new_list =[str(uid) for uid in new_list]
                    if str(SOURCE_UID) not in new_list: new_list.append(str(SOURCE_UID))
                    added = set(new_list) - set(following_list)
                    if added: init_dynamic_states_for_uids(added, header)
                    following_list = new_list
                    save_following_cache(following_list)
                last_following_refresh = now

            if oid and (now - last_comment_check >= COMMENT_SCAN_INTERVAL):
                new_c, new_t = scan_new_comments(oid, header, last_read_time, seen_comments)
                last_comment_check = now
                if new_t > last_read_time: last_read_time = new_t
                if new_c:
                    new_c.sort(key=lambda x: x["ctime"])
                    try: notifier.send_webhook_notification(title, new_c)
                    except: pass

            interval = DYNAMIC_BURST_INTERVAL if now < burst_end else DYNAMIC_CHECK_INTERVAL
            if now - last_d_check >= interval:
                all_alerts =
