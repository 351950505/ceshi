# main.py
# ==========================================================
# 2026 B站动态监控 V7.1 终极版（动态 + 视频 + 评论）
# 功能：
# 1. 多UID动态监控，首次启动不回推历史
# 2. 视频最新投稿监控
# 3. 评论区监控
# 4. 自动获取关注列表
# 5. 抗352 / -799 风控
# 6. Cookie失效自动重登
# 7. 低频稳定轮询
# ==========================================================

import sys
import os
import time
import json
import random
import logging
import traceback
import hashlib
import urllib.parse
import subprocess
import requests

import notifier
import database as db

# ==========================================================
# 配置区
# ==========================================================
SOURCE_UID = 3706948578969654  # 用于关注列表抓取
FALLBACK_DYNAMIC_UIDS = [
    3546905852250875,
    3546961271589219,
    3546610447419885,
    285340365,
    3706948578969654
]

# 间隔配置
DYNAMIC_CHECK_INTERVAL = 15
FOLLOWING_REFRESH_INTERVAL = 3600
VIDEO_CHECK_INTERVAL = 21600
COMMENT_SCAN_INTERVAL = 5

TIME_OFFSET = -120
LOG_FILE = "bili_monitor.log"
STATE_FILE = "dynamic_state.json"
FOLLOW_FILE = "following_cache.json"

DYNAMIC_MAX_AGE = 86400  # 最大可推送动态时间差
COMMENT_MAX_PAGES = 3
COMMENT_SAFE_WINDOW = 60

# ==========================================================
# 日志
# ==========================================================
def init_logging():
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        encoding="utf-8",
        filemode="w"
    )
    logging.info("=" * 60)
    logging.info("B站动态 + 视频 + 评论监控 V7.1 启动")
    logging.info("=" * 60)

# ==========================================================
# Cookie管理
# ==========================================================
def refresh_cookie():
    logging.warning("Cookie失效，尝试重新登录")
    try:
        subprocess.run([sys.executable, "login_bilibili.py"], check=True)
        logging.info("Cookie刷新成功")
        return True
    except Exception as e:
        logging.error(f"Cookie刷新失败: {e}")
        return False

def get_header():
    if not os.path.exists("bili_cookie.txt"):
        refresh_cookie()
    with open("bili_cookie.txt", "r", encoding="utf-8") as f:
        cookie = f.read().strip()
    return {
        "Cookie": cookie,
        "Referer": "https://www.bilibili.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Connection": "close"
    }

# ==========================================================
# 请求层
# ==========================================================
def safe_request(url, params=None, header=None, retry=3):
    for i in range(retry):
        try:
            r = requests.get(url, params=params, headers=header, timeout=10)
            if not r.text.strip():
                time.sleep(2)
                continue
            data = r.json()
            code = data.get("code", -999)
            if code == 0:
                return data
            if code == -101:
                if refresh_cookie():
                    return safe_request(url, params, get_header(), retry)
                return data
            if code in (-352, -799, -509):
                wait = (2 ** i) + random.uniform(1, 3)
                logging.warning(f"触发风控 {code}，等待 {wait:.1f}s")
                time.sleep(wait)
                continue
            return data
        except Exception as e:
            logging.error(f"请求异常: {e}")
            time.sleep(2)
    return {"code": -500}

# ==========================================================
# WBI签名
# ==========================================================
WBI_KEYS = {"img": "", "sub": "", "time": 0}
mixinKeyEncTab = [
46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,
27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,
37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,
22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52
]

def mixin(orig):
    return ''.join([orig[i] for i in mixinKeyEncTab])[:32]

def update_wbi(header):
    data = safe_request("https://api.bilibili.com/x/web-interface/nav", header=header)
    if data.get("code") == 0:
        img = data["data"]["wbi_img"]
        WBI_KEYS["img"] = img["img_url"].split("/")[-1].split(".")[0]
        WBI_KEYS["sub"] = img["sub_url"].split("/")[-1].split(".")[0]
        WBI_KEYS["time"] = time.time()

def sign(params):
    if time.time() - WBI_KEYS["time"] > 21600:
        update_wbi(get_header())
    key = mixin(WBI_KEYS["img"] + WBI_KEYS["sub"])
    params["wts"] = int(time.time() + TIME_OFFSET)
    params = dict(sorted(params.items()))
    query = urllib.parse.urlencode(params)
    params["w_rid"] = hashlib.md5((query + key).encode()).hexdigest()
    return params

def wbi_request(url, params, header):
    return safe_request(url, sign(params), header)

# ==========================================================
# 文件缓存
# ==========================================================
def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return default
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ==========================================================
# 关注列表
# ==========================================================
def get_followings(uid, header):
    result = []
    pn = 1
    while True:
        data = safe_request(
            "https://api.bilibili.com/x/relation/followings",
            {"vmid": uid, "pn": pn, "ps": 50},
            header
        )
        if data.get("code") != 0:
            break
        items = data["data"].get("list", [])
        if not items:
            break
        for x in items:
            mid = x.get("mid")
            if mid:
                result.append(mid)
        pn += 1
        time.sleep(random.uniform(0.5, 1))
    return result

# ==========================================================
# 动态接口
# ==========================================================
def fetch_dynamic(uid, header):
    return wbi_request(
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all",
        {"host_mid": uid, "type": "all", "offset": ""},
        header
    )

def extract_text(item):
    try:
        nodes = item["modules"]["module_dynamic"].get("desc", {}).get("rich_text_nodes", [])
        text = "".join(x.get("text", "") for x in nodes if isinstance(x, dict)).strip()
        if text:
            return text
        major = item["modules"]["module_dynamic"].get("major", {})
        if major.get("type") == "MAJOR_TYPE_ARCHIVE":
            return "发布了视频"
        return "发布了新动态"
    except:
        return "发布了新动态"

# ==========================================================
# 视频监控
# ==========================================================
def get_latest_video(header):
    data = safe_request(
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
        {"host_mid": SOURCE_UID},
        header
    )
    if data.get("code") != 0:
        return None
    items = data.get("data", {}).get("items", [])
    for item in items:
        if item.get("type") == "DYNAMIC_TYPE_AV":
            try:
                return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
            except:
                continue
    return None

def get_video_info(bv, header):
    data = safe_request(f"https://api.bilibili.com/x/web-interface/view?bvid={bv}", None, header)
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

# ---------------- 评论 ----------------
def scan_new_comments(oid, header, last_read_time, seen):
    new_list = []
    max_ctime = last_read_time
    safe_time = last_read_time - 300  # 首次启动忽略超过 5 分钟前的评论

    pn = 1
    while pn <= 10:
        data = wbi_request(
            "https://api.bilibili.com/x/v2/reply",
            {
                "oid": oid,
                "type": 1,
                "sort": 0,
                "pn": pn,
                "ps": 20
            },
            header
        )

        replies = (data.get("data") or {}).get("replies") or []
        if not replies:
            break

        page_old = True
        for r in replies:
            rpid = r["rpid_str"]
            ctime = r["ctime"]

            max_ctime = max(max_ctime, ctime)

            if ctime > safe_time:
                page_old = False
                if rpid not in seen:
                    seen.add(rpid)
                    new_list.append({
                        "user": r["member"]["uname"],
                        "message": r["content"]["message"],
                        "ctime": ctime
                    })

        if page_old:
            break

        pn += 1
        time.sleep(random.uniform(0.5, 1))

    return new_list, max_ctime

# ==========================================================
# 主循环
# ==========================================================
def start_monitor():
    header = get_header()
    update_wbi(header)

    last_v_check = 0
    last_comment_check = 0
    last_follow = 0
    last_hb = time.time()

    # 视频和评论
    oid, title = sync_latest_video(header)
    last_read_time = int(time.time())
    seen_comments = set()

    # 关注列表
    follow_list = load_json(FOLLOW_FILE, [])
    if not follow_list:
        follow_list = get_followings(SOURCE_UID, header)
        if not follow_list:
            follow_list = FALLBACK_DYNAMIC_UIDS
        save_json(FOLLOW_FILE, follow_list)
    if SOURCE_UID not in follow_list:
        follow_list.append(SOURCE_UID)
    logging.info(f"监控UID数量: {len(follow_list)}")

    # 动态状态
    state = load_json(STATE_FILE, {})
    seen_dyn = {}
    for uid in follow_list:
        seen_dyn[uid] = set()
        data = fetch_dynamic(uid, header)
        if data.get("code") == 0:
            items = data.get("data", {}).get("items", [])
            # 只记录最新10条，第一次不推送
            for item in items[:10]:
                dyn_id = item.get("id_str")
                if dyn_id:
                    seen_dyn[uid].add(dyn_id)
            if items:
                state[str(uid)] = {"baseline": items[0].get("id_str", ""), "offset": data.get("data", {}).get("offset","")}
    save_json(STATE_FILE, state)

    logging.info("监控服务启动完成")

    while True:
        try:
            now = time.time()
            adjusted_now = now + TIME_OFFSET

            # 刷新关注列表
            if now - last_follow > FOLLOWING_REFRESH_INTERVAL:
                new_follow = get_followings(SOURCE_UID, header)
                if new_follow:
                    if SOURCE_UID not in new_follow:
                        new_follow.append(SOURCE_UID)
                    follow_list = new_follow
                    save_json(FOLLOW_FILE, follow_list)
                last_follow = now

            # 评论监控
            if oid and now - last_comment_check > COMMENT_SCAN_INTERVAL:
                new_c, new_t = scan_new_comments(oid, header, last_read_time, seen_comments)
                if new_t > last_read_time:
                    last_read_time = new_t
                if new_c:
                    new_c.sort(key=lambda x: x["ctime"])
                    notifier.send_webhook_notification(title, new_c)
                last_comment_check = now

            # 动态监控
            for uid in follow_list:
                alerts = []
                data = fetch_dynamic(uid, header)
                if data.get("code") == 0:
                    items = data.get("data", {}).get("items", [])
                    for item in items:
                        dyn_id = item.get("id_str")
                        if dyn_id not in seen_dyn[uid]:
                            ts = item.get("modules", {}).get("module_author", {}).get("pub_ts",0)
                            if adjusted_now - ts > DYNAMIC_MAX_AGE:
                                continue
                            text = extract_text(item)
                            alerts.append({"user": str(uid), "message": text})
                            seen_dyn[uid].add(dyn_id)
                if alerts:
                    notifier.send_webhook_notification("动态更新", alerts)

            # 视频更新检查
            if now - last_v_check > VIDEO_CHECK_INTERVAL:
                res = sync_latest_video(header)
                if res:
                    oid, title = res
                last_v_check = now

            # 心跳
            if now - last_hb > 60:
                logging.info("💓 心跳正常")
                last_hb = now

            time.sleep(random.uniform(2,4))
        except Exception:
            logging.error(traceback.format_exc())
            time.sleep(10)

if __name__ == "__main__":
    init_logging()
    db.init_db()
    start_monitor()
