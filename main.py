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

import database as db
import notifier

# ================= 核心配置 =================
TARGET_UID = 1671203508
VIDEO_CHECK_INTERVAL = 21600
HEARTBEAT_INTERVAL = 30
FOLLOWING_REFRESH_INTERVAL = 3600
SOURCE_UID = 3706948578969654

FALLBACK_DYNAMIC_UIDS = ["3546905852250875", "3546961271589219", "3546610447419885", "285340365", "3706948578969654"]

COMMENT_SCAN_INTERVAL = 5
COMMENT_MAX_PAGES = 3
COMMENT_SAFE_WINDOW = 60

LOG_FILE = "bili_monitor.log"
DYNAMIC_STATE_FILE = "dynamic_state.json"
FOLLOWING_CACHE_FILE = "following_cache.json"

# 最稳24小时参数（已调松防漏+防风控）
INIT_SLEEP_MIN, INIT_SLEEP_MAX = 3.5, 7.0
STATE_SAVE_INTERVAL = 30
BURST_MODE_DURATION = 18
BURST_INTERVAL = 1.6
NORMAL_INTERVAL = 2.0

MAX_SEEN_DYNAMIC_IDS = 3000
DYNAMIC_NEW_WINDOW = 300          # 仅推送最近5分钟内的动态
FEED_FETCH_MAX_PAGES = 3          # 检测到更新后最多补抓页数
# =============================================

push_queue = queue.Queue()
burst_end_time = 0
last_state_save = time.time()
last_seen_clean = time.time()
_last_notify_time = {}

def push_worker():
    while True:
        try:
            item = push_queue.get(timeout=1)
            if item:
                notifier.send_webhook_notification("💡 特别关注UP主发布新内容", [item])
        except queue.Empty:
            continue
        except Exception as e:
            logging.error(f"推送失败: {e}")

def get_scan_interval():
    global burst_end_time
    return BURST_INTERVAL if time.time() < burst_end_time else NORMAL_INTERVAL

def init_logging():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.truncate()
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        encoding="utf-8",
        filemode="w"
    )
    logging.info("=" * 60)
    logging.info("B站监控系统启动 (关注流 feed/all 版 - 防漏动态)")
    logging.info("=" * 60)

def safe_request(url, params, header, retries=5):
    h = header.copy()
    h["Connection"] = "close"
    base_delay = 3
    for i in range(retries):
        try:
            r = requests.get(url, headers=h, params=params, timeout=12)
            data = r.json()
            code = data.get("code")
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
                time.sleep(base_delay * (2 ** i) + random.uniform(0.8, 2.5))
                continue
            return data
        except Exception:
            time.sleep(base_delay * (2 ** i) + random.uniform(0.8, 2.5))
    logging.error("请求最终失败")
    send_failure_notification("API 请求最终失败", "所有重试均失败")
    return {"code": -500}

WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}
mixinKeyEncTab = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]

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
    except Exception as e:
        logging.error(f"更新WBI异常: {e}")

def wbi_request(url, params, header):
    if not WBI_KEYS["img_key"] or time.time() - WBI_KEYS["last_update"] > 21600:
        update_wbi_keys(header)
    signed = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    return safe_request(url, signed, header)

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

def load_following_cache():
    if os.path.exists(FOLLOWING_CACHE_FILE):
        try:
            with open(FOLLOWING_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []
    return []

def save_following_cache(uids):
    with open(FOLLOWING_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(uids, f)

def load_dynamic_state():
    default_state = {
        "feed": {
            "last_ts": 0,
            "baseline": "",
            "offset": ""
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
                    "last_ts": feed.get("last_ts", 0),
                    "baseline": feed.get("baseline", ""),
                    "offset": feed.get("offset", "")
                }
            }
        except:
            return default_state
    return default_state

def save_dynamic_state(state):
    with open(DYNAMIC_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def clean_old_seen(seen_dynamic_ids):
    if len(seen_dynamic_ids) <= MAX_SEEN_DYNAMIC_IDS:
        return
    items = sorted(seen_dynamic_ids.items(), key=lambda x: x[1], reverse=True)
    kept = dict(items[:MAX_SEEN_DYNAMIC_IDS])
    seen_dynamic_ids.clear()
    seen_dynamic_ids.update(kept)

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
            if text:
                return text

        major = dyn.get("major") or {}
        t = major.get("type", "")

        if t == "MAJOR_TYPE_ARCHIVE":
            a = major.get("archive") or {}
            return f"【视频】{a.get('title', '')}\n{a.get('desc', '')}".strip()

        if t == "MAJOR_TYPE_ARTICLE":
            a = major.get("article", {}) or {}
            return f"【专栏】{a.get('title', '')}\n{a.get('desc', '')}".strip()

        if t == "MAJOR_TYPE_OPUS":
            opus = major.get("opus", {}) or {}
            summary = opus.get("summary", {}) or {}
            nodes = summary.get("rich_text_nodes") or []
            text = "".join(n.get("text", "") for n in nodes if isinstance(n, dict)).strip()
            title = opus.get("title") or ""
            if title and text:
                return f"【图文】{title}\n{text}".strip()
            return text or f"【图文】{title}".strip()

        if t == "MAJOR_TYPE_DRAW":
            desc_text = desc.get("text", "").strip()
            if desc_text:
                return desc_text
            return "【图片动态】"

        if t == "MAJOR_TYPE_COMMON":
            common = major.get("common", {}) or {}
            return f"【卡片】{common.get('title', '')}\n{common.get('desc', '')}".strip()

        if t == "MAJOR_TYPE_LIVE":
            live = major.get("live", {}) or {}
            return f"【直播】{live.get('title', '')}\n{live.get('desc_second', '')}".strip()

        if t == "MAJOR_TYPE_PGC":
            pgc = major.get("pgc", {}) or {}
            return f"【PGC】{pgc.get('title', '')}".strip()

        if t == "MAJOR_TYPE_COURSES":
            c = major.get("courses", {}) or {}
            return f"【课程】{c.get('title', '')}\n{c.get('desc', '')}".strip()

        if t == "MAJOR_TYPE_MUSIC":
            m = major.get("music", {}) or {}
            return f"【音频】{m.get('title', '')}".strip()

        return desc.get("text", "").strip()
    except:
        return ""

def format_dynamic_message(item):
    dyn_id = item.get("id_str", "")
    author = item.get("modules", {}).get("module_author", {}) or {}
    name = author.get("name", "未知UP")
    pub_ts = author.get("pub_ts", 0)

    text = extract_dynamic_text(item)

    if item.get("type") == "DYNAMIC_TYPE_FORWARD":
        orig = item.get("orig")
        if orig:
            orig_text = extract_dynamic_text(orig)
            if orig_text:
                text = f"{text}\n【转发原文】{orig_text}" if text else f"【转发原文】{orig_text}"
            orig_id = orig.get("id_str")
            if orig_id:
                text = f"{text}\n【原动态链接】https://t.bilibili.com/{orig_id}"

    time_str = datetime.datetime.fromtimestamp(pub_ts).strftime("%Y-%m-%d %H:%M:%S") if pub_ts > 0 else "未知时间"
    final_msg = (
        f"{text}\n\n📅 发布于: {time_str}\n🔗 直达链接: https://t.bilibili.com/{dyn_id}"
        if text else
        f"📅 发布于: {time_str}\n🔗 直达链接: https://t.bilibili.com/{dyn_id}"
    )
    return name, final_msg

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

def check_feed_update(header, update_baseline):
    params = {
        "type": "all",
        "update_baseline": update_baseline or "0",
        "web_location": "333.1365"
    }
    return safe_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all/update", params, header)

def init_feed_state(header, target_uids):
    state = load_dynamic_state()
    seen_dynamic_ids = {}

    try:
        data = fetch_following_feed(header, offset="")
        if data.get("code") == 0:
            feed = data.get("data", {}) or {}
            items = feed.get("items") or []

            baseline = feed.get("update_baseline", "")
            offset = feed.get("offset", "")
            max_ts = 0

            for item in items:
                if not isinstance(item, dict):
                    continue
                dyn_id = item.get("id_str")
                if dyn_id:
                    seen_dynamic_ids[dyn_id] = time.time()

                author = item.get("modules", {}).get("module_author", {}) or {}
                author_mid = str(author.get("mid", ""))
                pub_ts = author.get("pub_ts", 0)
                if author_mid in target_uids and pub_ts > max_ts:
                    max_ts = pub_ts

            state["feed"]["baseline"] = baseline
            state["feed"]["offset"] = offset
            state["feed"]["last_ts"] = max_ts
            save_dynamic_state(state)

            logging.info(f"关注流初始化完成 baseline={baseline} offset={offset} last_ts={max_ts}")
        else:
            logging.warning(f"关注流初始化失败，code={data.get('code')}")
    except Exception as e:
        logging.error(f"关注流初始化异常: {e}")

    return seen_dynamic_ids, state

def process_feed_items(items, target_uids, seen_dynamic_ids, state, now_ts):
    global burst_end_time
    has_new = False
    feed_state = state.setdefault("feed", {"last_ts": 0, "baseline": "", "offset": ""})
    last_ts = feed_state.get("last_ts", 0)
    max_ts = last_ts

    new_items = []

    for item in items:
        if not isinstance(item, dict):
            continue

        dyn_id = item.get("id_str")
        if not dyn_id:
            continue

        seen_dynamic_ids[dyn_id] = time.time()

        author = item.get("modules", {}).get("module_author", {}) or {}
        author_mid = str(author.get("mid", ""))
        pub_ts = author.get("pub_ts", 0)

        if author_mid not in target_uids:
            continue

        if pub_ts > max_ts:
            max_ts = pub_ts

        if dyn_id in new_items:
            continue

        if pub_ts > last_ts and (now_ts - pub_ts <= DYNAMIC_NEW_WINDOW):
            new_items.append(dyn_id)

    if max_ts > last_ts:
        feed_state["last_ts"] = max_ts

    pushed_ids = set()
    for item in items:
        if not isinstance(item, dict):
            continue

        dyn_id = item.get("id_str")
        if not dyn_id or dyn_id not in new_items or dyn_id in pushed_ids:
            continue

        name, final_msg = format_dynamic_message(item)
        push_queue.put({"user": name, "message": final_msg})
        pushed_ids.add(dyn_id)
        has_new = True
        logging.info(f"✅ 新动态 [{name}] {dyn_id}")

    if has_new:
        burst_end_time = time.time() + BURST_MODE_DURATION
        logging.info(f"🚀 爆发模式启动 {BURST_MODE_DURATION}s")

    return has_new

def scan_following_feed(header, target_uids, seen_dynamic_ids, state, now_ts):
    feed_state = state.setdefault("feed", {"last_ts": 0, "baseline": "", "offset": ""})
    baseline = feed_state.get("baseline", "")
    old_baseline = baseline

    update_data = check_feed_update(header, baseline)
    if update_data.get("code") != 0:
        return False

    update_num = update_data.get("data", {}).get("update_num", 0)
    if update_num <= 0:
        return False

    logging.info(f"📡 检测到关注流更新 {update_num} 条，开始拉取")

    has_new = False
    offset = ""
    page_count = 0
    first_page_baseline = ""

    while page_count < FEED_FETCH_MAX_PAGES:
        data = fetch_following_feed(header, offset=offset)
        if data.get("code") != 0:
            break

        feed = data.get("data", {}) or {}
        items = feed.get("items") or []
        if not items:
            break

        if page_count == 0:
            first_page_baseline = feed.get("update_baseline", "") or (items[0].get("id_str", "") if items else "")
            if first_page_baseline:
                feed_state["baseline"] = first_page_baseline

        page_has_new = process_feed_items(items, target_uids, seen_dynamic_ids, state, now_ts)
        if page_has_new:
            has_new = True

        reached_old = False
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("id_str") == old_baseline:
                reached_old = True
                break

        offset = feed.get("offset", "")
        feed_state["offset"] = offset

        page_count += 1

        if reached_old:
            break
        if not offset:
            break

        time.sleep(random.uniform(0.4, 0.8))

    return has_new

def scan_new_comments(oid, header, last_read_time, seen):
    new_list = []
    max_ctime = last_read_time
    safe_time = last_read_time - COMMENT_SAFE_WINDOW
    url = "https://api.bilibili.com/x/v2/reply"
    pn = 1
    fetched = 0
    while fetched < COMMENT_MAX_PAGES:
        params = {"type": 1, "oid": oid, "sort": 0, "nohot": 1, "ps": 20, "pn": pn}
        data = wbi_request(url, params, header)
        if data.get("code") != 0:
            break
        replies = data.get("data", {}).get("replies", [])
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
                    comment_time = datetime.datetime.fromtimestamp(ctime).strftime('%H:%M:%S')
                    new_list.append({
                        "user": f"[{comment_time}] {r.get('member', {}).get('uname', '')}",
                        "message": r.get("content", {}).get("message", ""),
                        "ctime": ctime
                    })
        if all_old or len(replies) < 20:
            break
        pn += 1
        fetched += 1
        time.sleep(random.uniform(0.4, 0.8))
    return new_list, max_ctime

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
            except:
                pass
    return None

def get_video_info(bv, header):
    data = safe_request(f"https://api.bilibili.com/x/web-interface/view?bvid={bv}", None, header)
    if data.get("code") == -101 and refresh_cookie():
        return get_video_info(bv, get_header())
    if data.get("code") == 0:
        d = data["data"]
        return str(d["aid"]), d["title"]
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

def send_failure_notification(title, message):
    key = f"{title}:{message[:100]}"
    if time.time() - _last_notify_time.get(key, 0) >= 600:
        _last_notify_time[key] = time.time()
        try:
            notifier.send_webhook_notification(title, [{"user": "系统", "message": message}])
        except:
            pass

def start_monitoring(header):
    global last_state_save, last_seen_clean

    last_v_check = 0
    last_hb = 0
    last_comment_check = 0
    last_following_refresh = 0
    last_d_check = 0

    oid, title = sync_latest_video(header)
    last_read_time = int(time.time())
    seen_comments = set()

    following_list = load_following_cache() or get_following_list(SOURCE_UID, header) or FALLBACK_DYNAMIC_UIDS[:]
    following_list = [str(uid) for uid in following_list]
    if str(SOURCE_UID) not in following_list:
        following_list.append(str(SOURCE_UID))
    save_following_cache(following_list)

    target_uids = set(following_list)
    logging.info(f"监控 {len(target_uids)} 个 UID（关注流模式）")

    seen_dynamic_ids, state = init_feed_state(header, target_uids)

    threading.Thread(target=push_worker, daemon=True).start()
    logging.info("✅ 关注流版启动（feed/all/update + feed/all + 补抓防漏）")

    while True:
        try:
            now = time.time()

            # 动态扫描：先update，再feed/all
            if now - last_d_check >= get_scan_interval():
                try:
                    state_updated = scan_following_feed(header, target_uids, seen_dynamic_ids, state, now)
                    if state_updated or now - last_state_save > STATE_SAVE_INTERVAL:
                        save_dynamic_state(state)
                        last_state_save = now
                except Exception as e:
                    logging.error(f"关注流扫描异常: {e}")
                last_d_check = now

            # 刷新关注列表
            if now - last_following_refresh >= FOLLOWING_REFRESH_INTERVAL:
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

                last_following_refresh = now

            # 评论扫描保持原逻辑
            if oid and now - last_comment_check >= COMMENT_SCAN_INTERVAL:
                new_c, new_t = scan_new_comments(oid, header, last_read_time, seen_comments)
                last_comment_check = now
                if new_t > last_read_time:
                    last_read_time = new_t
                if new_c:
                    new_c.sort(key=lambda x: x["ctime"])
                    notifier.send_webhook_notification(title, new_c)
                    logging.info(f"💬 发送 {len(new_c)} 条评论")

            if now - last_hb >= HEARTBEAT_INTERVAL:
                logging.info("💓 心跳正常")
                last_hb = now

            if now - last_v_check > VIDEO_CHECK_INTERVAL:
                res = sync_latest_video(header)
                if res:
                    oid, title = res
                last_v_check = now

            if now - last_seen_clean > 3600:
                clean_old_seen(seen_dynamic_ids)
                last_seen_clean = now

            time.sleep(0.25)

        except Exception:
            logging.error(traceback.format_exc())
            time.sleep(8)

if __name__ == "__main__":
    init_logging()
    db.init_db()
    h = get_header()
    update_wbi_keys(h)
    start_monitoring(h)
