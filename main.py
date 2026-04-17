import sys, os, time, subprocess, random, logging, traceback, hashlib, urllib.parse, json, requests
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
FALLBACK_DYNAMIC_UIDS =["3546905852250875", "3546961271589219", "3546610447419885", "285340365", "3706948578969654"]

DYNAMIC_CHECK_INTERVAL = 15
DYNAMIC_BURST_INTERVAL = 8
DYNAMIC_BURST_DURATION = 300
DYNAMIC_MAX_AGE = 86400  # 放宽至24小时防漏报

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

# ================= 辅助模块 =================
def should_log(k, i=LOG_DEDUP_INTERVAL):
    n = time.time()
    if n - _last_log_time[k] >= i: _last_log_time[k] = n; return True
    return False

def should_notify(k, i=FAILURE_NOTIFY_INTERVAL):
    n = time.time()
    if n - _last_notify_time[k] >= i: _last_notify_time[k] = n; return True
    return False

def send_failure_notification(title, message):
    if should_notify(f"{title}:{message[:100]}"):
        try: notifier.send_webhook_notification(title,[{"user": "系统", "message": message}])
        except: pass

def cleanup_log_file():
    if not os.path.exists(LOG_FILE): return
    try:
        if time.time() - os.path.getmtime(LOG_FILE) > 86400:
            for h in logging.root.handlers[:]: logging.root.removeHandler(h); h.close()
            with open(LOG_FILE, "w", encoding="utf-8") as f: f.truncate()
            init_logging()
    except: pass

def init_logging():
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "w", encoding="utf-8") as f: f.truncate()
    except: pass
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", encoding="utf-8")
    logging.info("=" * 60 + "\nB站监控系统启动 (解卡死 + 精确时间 + 纯净解析)\n" + "=" * 60)

def refresh_cookie():
    try:
        subprocess.run([sys.executable, "login_bilibili.py"], check=True)
        return True
    except Exception as e:
        send_failure_notification("Cookie 刷新失败", str(e))
        return False

def safe_request(url, params, header, retries=3, fast_fail=False):
    h = header.copy()
    h["Connection"] = "close"
    b_delay = 2
    for i in range(retries):
        try:
            r = requests.get(url, headers=h, params=params, timeout=10)
            txt = r.text.strip()
            if not txt:
                if fast_fail: return {"code": -500}
                time.sleep(b_delay * (2**i)); continue
            data = r.json()
            c = data.get("code")
            if c == -101: return {"code": -101, "need_refresh": True}
            if c in (-799, -352, -509):
                if fast_fail: return {"code": c, "message": "限流快熔断"}
                w = b_delay * (2**i) + random.uniform(0, 2)
                if should_log(f"ratelimit_{c}_{url}"): logging.warning(f"风控 ({c}) 等待 {w:.1f}s")
                time.sleep(w); continue
            if c != 0 and c != -400:
                if should_log(f"api_error_{c}_{url}"): logging.warning(f"API错误 {c}")
                if i < retries - 1 and not fast_fail: time.sleep(b_delay * (2**i)); continue
            return data
        except Exception as e:
            if fast_fail: return {"code": -500}
            if should_log(f"req_exc_{url}"): logging.error(f"请求异常: {e}")
            time.sleep(b_delay * (2**i))
    return {"code": -500, "message": "重试失败"}

# ================= WBI 签名 =================
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
            WBI_KEYS["img_key"] = data["data"]["wbi_img"]["img_url"].rsplit("/", 1)[1].split(".")[0]
            WBI_KEYS["sub_key"] = data["data"]["wbi_img"]["sub_url"].rsplit("/", 1)[1].split(".")[0]
            WBI_KEYS["last_update"] = time.time()
    except: pass

def wbi_request(url, params, header, fast_fail=False):
    if not WBI_KEYS["img_key"] or time.time() - WBI_KEYS["last_update"] > 21600: update_wbi_keys(header)
    signed = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    return safe_request(url, signed, header, fast_fail=fast_fail)

# ================= 关注与动态解析 =================
def get_following_list(uid, header):
    following =[]
    pn, ps = 1, 50
    while True:
        data = safe_request("https://api.bilibili.com/x/relation/followings", {"vmid": uid, "pn": pn, "ps": ps, "order": "desc", "order_type": "attention"}, header)
        if data.get("code") != 0: break
        items = data.get("data", {}).get("list", [])
        if not items: break
        following.extend([str(i["mid"]) for i in items if i.get("mid")])
        if data.get("data", {}).get("total", 0) <= pn * ps: break
        pn += 1; time.sleep(random.uniform(0.3, 0.6))
    return following

def load_following_cache():
    try:
        if os.path.exists(FOLLOWING_CACHE_FILE):
            with open(FOLLOWING_CACHE_FILE, "r") as f: return json.load(f)
    except: pass
    return[]

def save_following_cache(uids):
    with open(FOLLOWING_CACHE_FILE, "w") as f: json.dump(uids, f)

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
            desc_text = parse_rich_nodes(dyn_obj.get("desc", {}).get("rich_text_nodes")) or str(dyn_obj.get("desc", {}).get("text") or "")
            if desc_text.strip(): out.append(desc_text.strip())

            major = dyn_obj.get("major") or {}
            m_type = major.get("type", "")
            if m_type == "MAJOR_TYPE_OPUS":
                opus = major.get("opus") or {}
                if opus.get("title"): out.append(f"📰 图文: 《{opus.get('title')}》")
                c_str = parse_opus_paragraphs(opus.get("content", {}).get("paragraphs",[]))
                if not c_str: c_str = parse_rich_nodes(opus.get("summary", {}).get("rich_text_nodes")) or str(opus.get("summary", {}).get("text") or "")
                if c_str.strip(): out.append(f"📝 正文: {c_str.strip()}")
                if opus.get("pics"): out.append(f"🖼️[附图 {len(opus.get('pics'))} 张]")
            elif m_type == "MAJOR_TYPE_ARCHIVE":
                arc = major.get("archive") or {}
                if arc.get("title"): out.append(f"▶️ 视频: 《{arc.get('title')}》")
                if arc.get("desc"): out.append(f"📝 简介: {arc.get('desc')}")
            elif m_type == "MAJOR_TYPE_DRAW":
                if major.get("draw", {}).get("items"): out.append(f"🖼️[共 {len(major['draw']['items'])} 张图片]")
            elif m_type == "MAJOR_TYPE_ARTICLE":
                art = major.get("article") or {}
                if art.get("title"): out.append(f"📚 专栏: 《{art.get('title')}》")
            return out

        res.extend(parse_module(item.get("modules", {}).get("module_dynamic")))
        if item.get("orig"):
            res.append("\n------ 被转发内容 ------")
            orig_author = item["orig"].get("modules", {}).get("module_author", {}).get("name", "某用户")
            res.append(f"@{orig_author}:")
            res.extend(parse_module(item["orig"].get("modules", {}).get("module_dynamic")))

        f_txt = "\n".join(res).strip()
        return f_txt[:1500] + "\n...(已截断)" if len(f_txt) > 1500 else f_txt
    except: return "发布了新动态 (解析兜底)"

# ================= 动态核心与状态 =================
def load_dynamic_state():
    try:
        if os.path.exists(DYNAMIC_STATE_FILE):
            with open(DYNAMIC_STATE_FILE, "r") as f: return json.load(f).get("seen_ids",[])
    except: pass
    return[]

def save_dynamic_state(seen_list):
    with open(DYNAMIC_STATE_FILE, "w") as f: json.dump({"seen_ids": list(seen_list)}, f)

def fetch_dynamics_page(uid, header):
    return safe_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space", {"host_mid": uid}, header, fast_fail=True)

def init_dynamic_states_for_uids(uids, header):
    seen = set(load_dynamic_state())
    for uid in uids:
        try:
            d = fetch_dynamics_page(str(uid), header)
            if d.get("code") == 0:
                for item in d.get("data", {}).get("items",[]):
                    i_str = item.get("id_str")
                    if i_str: seen.add(i_str)
        except: pass
    save_dynamic_state(seen)
    return seen

def check_new_dynamics_for_uid(uid, header, seen_dynamics, now_ts):
    alerts, uid_str, has_new =[], str(uid), False
    d = fetch_dynamics_page(uid_str, header)
    if d.get("code") != 0: return alerts, False

    for item in d.get("data", {}).get("items",[]):
        if not isinstance(item, dict): continue
        dyn_id = item.get("id_str")
        if not dyn_id or dyn_id in seen_dynamics: continue

        seen_dynamics.add(dyn_id)
        author = item.get("modules", {}).get("module_author", {})
        pub_ts = author.get("pub_ts", 0)
        if now_ts - pub_ts > DYNAMIC_MAX_AGE: continue

        pt_str = datetime.datetime.fromtimestamp(pub_ts).strftime('%Y-%m-%d %H:%M:%S') if pub_ts else "刚刚"
        name = author.get("name", uid_str)
        txt = extract_dynamic_text(item)
        alerts.append({"user": name, "message": f"【发布时间】{pt_str}\n{txt}\n\n🔗 直达链接: https://t.bilibili.com/{dyn_id}"})
        has_new = True
        logging.info(f"✅ 抓取新动态 [{name}]: {dyn_id} 发布于 {pt_str}")
    return alerts, has_new

# ================= 评论与视频 =================
def scan_new_comments(oid, header, last_read_time, seen):
    new_list, max_ctime =[], last_read_time
    safe_time = last_read_time - COMMENT_SAFE_WINDOW
    for pn in range(1, COMMENT_MAX_PAGES + 1):
        d = wbi_request("https://api.bilibili.com/x/v2/reply", {"type": 1, "oid": oid, "sort": 0, "nohot": 1, "ps": 20, "pn": pn}, header)
        if d.get("code") != 0: break
        replies = d.get("data", {}).get("replies",[])
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
                    new_list.append({"user": r["member"]["uname"], "message": r["content"]["message"], "ctime": ctime})
        if all_old or len(replies) < 20: break
        time.sleep(random.uniform(0.3, 0.6))
    return new_list, max_ctime

def get_latest_video(header):
    d = safe_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space", {"host_mid": TARGET_UID}, header)
    if d.get("code") == -101 and refresh_cookie(): return get_latest_video(get_header())
    for item in (d.get("data") or {}).get("items",[]):
        try:
            if item.get("type") == "DYNAMIC_TYPE_AV": return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
        except: pass
    return None

def get_video_info(bv, header):
    d = safe_request(f"https://api.bilibili.com/x/web-interface/view?bvid={bv}", None, header)
    if d.get("code") == -101 and refresh_cookie(): return get_video_info(bv, get_header())
    if d.get("code") == 0: return str(d["data"]["aid"]), d["data"]["title"]
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

def is_work_time(now=None):
    if now is None: now = datetime.datetime.now()
    if now.weekday() >= 5: return False
    return datetime.time(8, 30) <= now.time() <= datetime.time(17, 0)

def get_sleep_until_work_time(now=None):
    if now is None: now = datetime.datetime.now()
    t = datetime.datetime(now.year, now.month, now.day, 8, 30)
    if now > t: t += datetime.timedelta(days=1)
    while t.weekday() >= 5: t += datetime.timedelta(days=1)
    return max(1, (t - now).total_seconds())

def get_header():
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f: cookie = f.read().strip()
    except:
        subprocess.run([sys.executable, "login_bilibili.py"])
        with open("bili_cookie.txt", "r", encoding="utf-8") as f: cookie = f.read().strip()
    return {"Cookie": cookie, "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36", "Referer": "https://www.bilibili.com/"}

# ================= 主循环 =================
def start_monitoring(header):
    last_v_check = last_hb = last_d_check = last_comment_check = last_following_refresh = last_cleanup_check = time.time()
    burst_end = 0

    oid, title = sync_latest_video(header)
    last_read_time = int(time.time())
    seen_comments = set()

    fl = load_following_cache()
    if not fl:
        fl = get_following_list(SOURCE_UID, header) or FALLBACK_DYNAMIC_UIDS.copy()
        if str(SOURCE_UID) not in fl: fl.append(str(SOURCE_UID))
        save_following_cache(fl)

    seen_dyn = init_dynamic_states_for_uids(fl, header)

    while True:
        try:
            now_dt = datetime.datetime.now()
            if not is_work_time(now_dt):
                s = get_sleep_until_work_time(now_dt)
                time.sleep(s)
                header = get_header(); update_wbi_keys(header)
                continue

            now = time.time()
            if now - last_cleanup_check >= 3600: cleanup_log_file(); last_cleanup_check = now

            if now - last_following_refresh >= FOLLOWING_REFRESH_INTERVAL:
                nl = get_following_list(SOURCE_UID, header)
                if nl:
                    nl = [str(u) for u in nl]
                    if str(SOURCE_UID) not in nl: nl.append(str(SOURCE_UID))
                    added = set(nl) - set(fl)
                    if added: init_dynamic_states_for_uids(added, header)
                    fl = nl
                    save_following_cache(fl)
                last_following_refresh = now

            if oid and (now - last_comment_check >= COMMENT_SCAN_INTERVAL):
                nc, nt = scan_new_comments(oid, header, last_read_time, seen_comments)
                last_comment_check = now
                if nt > last_read_time: last_read_time = nt
                if nc:
                    nc.sort(key=lambda x: x["ctime"])
                    try: notifier.send_webhook_notification(title, nc)
                    except: pass

            interval = DYNAMIC_BURST_INTERVAL if now < burst_end else DYNAMIC_CHECK_INTERVAL
            if now - last_d_check >= interval:
                all_alerts =[]
                has_new_total = False
                
                # 遍历 UID 抓取新动态
                for uid in fl:
                    alerts, has_new = check_new_dynamics_for_uid(uid, header, seen_dyn, now)
                    if alerts: all_alerts.extend(alerts)
                    if has_new: has_new_total = True
                    time.sleep(random.uniform(0.5, 1))

                # 发现新动态处理
                if has_new_total:
                    save_dynamic_state(seen_dyn)
                    burst_end = now + DYNAMIC_BURST_DURATION
                
                # 集中发送通知
                if all_alerts:
                    try: notifier.send_webhook_notification("💡 特别关注UP主发布新内容", all_alerts)
                    except Exception as e: logging.error(f"通知失败: {e}")

                last_d_check = now

            if now - last_hb >= HEARTBEAT_INTERVAL:
                try: notifier.send_webhook_notification("心跳",[{"user": "系统", "message": "正常运行"}])
                except: pass
                last_hb = now

            if now - last_v_check > VIDEO_CHECK_INTERVAL:
                res = sync_latest_video(header)
                if res: oid, title = res
                last_v_check = now

            time.sleep(random.uniform(2, 4))

        except Exception:
            logging.error(traceback.format_exc())
            time.sleep(60)

if __name__ == "__main__":
    init_logging()
    db.init_db()
    h = get_header()
    update_wbi_keys(h)
    start_monitoring(h)
