import sys
import os
import time
import datetime
import subprocess
import random
import logging
import traceback
import hashlib
import urllib.parse
import requests

import database as db
import notifier

# ================= 核心配置区 =================
TARGET_UID = 1671203508           # 主监控视频评论的UP
VIDEO_CHECK_INTERVAL = 21600      # 6小时刷新一次目标UP的最新视频
HEARTBEAT_INTERVAL = 600          # 10分钟发一次运行心跳

# 监听其他UP主动态的UID列表
EXTRA_DYNAMIC_UIDS = [3546905852250875, 3546961271589219, 3546610447419885, 285340365, 3706948578969654]
DYNAMIC_CHECK_INTERVAL = 60       # 动态日常检查间隔(秒)
DYNAMIC_BURST_INTERVAL = 10       # 发现新动态后的狂暴模式刷新间隔(秒)
DYNAMIC_BURST_DURATION = 300      # 狂暴模式持续时间(5分钟)
DYNAMIC_MAX_AGE = 300             # 动态时效性限制：300秒（5分钟）
LOG_FILE = 'bili_monitor.log'
# ==============================================

# ------------------------
# 强制日志初始化：物理清空
# ------------------------
def init_logging():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'w', encoding='utf-8') as f:
            f.truncate()
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        encoding='utf-8',
        filemode='w'
    )
    logging.info("="*50)
    logging.info("B站全能监控启动：评论逻辑保持，动态抓取方法已更新")
    logging.info("="*50)

# ------------------------
# 核心网络优化：安全自愈请求
# ------------------------
def safe_request(url, params, header, retries=3):
    safe_header = header.copy()
    safe_header["Connection"] = "close"
    for i in range(retries):
        try:
            r = requests.get(url, headers=safe_header, params=params, timeout=10)
            text = r.text.strip()
            if not text:
                time.sleep(1.5)
                continue
            return r.json()
        except Exception:
            time.sleep(1.5)
    return {"code": -500}

# ------------------------
# Wbi 签名算法模块
# ------------------------
WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}
mixinKeyEncTab = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52
]

def getMixinKey(orig: str):
    return "".join([orig[i] for i in mixinKeyEncTab])[:32]

def encWbi(params: dict, img_key: str, sub_key: str):
    mixin_key = getMixinKey(img_key + sub_key)
    curr_time = round(time.time())
    params['wts'] = curr_time
    params = dict(sorted(params.items()))
    filtered_params = {}
    for k, v in params.items():
        v_str = str(v)
        for char in "!'()*": v_str = v_str.replace(char, '')
        filtered_params[k] = v_str
    query = urllib.parse.urlencode(filtered_params)
    wbi_sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
    filtered_params['w_rid'] = wbi_sign
    return filtered_params

def update_wbi_keys(header):
    url = "https://api.bilibili.com/x/web-interface/nav"
    try:
        data = safe_request(url, None, header)
        if data.get("code") == 0:
            wbi_img = data["data"]["wbi_img"]
            WBI_KEYS["img_key"] = wbi_img["img_url"].rsplit('/', 1)[1].split('.')[0]
            WBI_KEYS["sub_key"] = wbi_img["sub_url"].rsplit('/', 1)[1].split('.')[0]
            WBI_KEYS["last_update"] = time.time()
            logging.info("Wbi 密钥已自动更新")
    except Exception: pass

def wbi_request(url, params, header):
    if time.time() - WBI_KEYS["last_update"] > 21600 or not WBI_KEYS["img_key"]:
        update_wbi_keys(header)
    signed_params = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    data = safe_request(url, signed_params, header)
    if data.get("code") == -400:
        update_wbi_keys(header)
        signed_params = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
        data = safe_request(url, signed_params, header)
    return data

# ------------------------
# 业务功能块
# ------------------------
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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*"
    }

def is_work_time():
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
    return now.weekday() < 5 and 9 <= now.hour < 19

def get_video_info(bv, header):
    url = f"https://api.bilibili.com/x/web-interface/view?bvid={bv}"
    data = safe_request(url, None, header)
    if data.get("code") == 0:
        return str(data["data"]["aid"]), data["data"]["title"]
    return None, None

def get_latest_video(header, target_uid):
    url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    data = safe_request(url, {"host_mid": target_uid}, header)
    if data.get("code") == 0:
        for item in data.get("data", {}).get("items", []):
            if item.get("type") == "DYNAMIC_TYPE_AV":
                return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
    return None

def sync_latest_video(header):
    bv = get_latest_video(header, TARGET_UID)
    if bv:
        videos = db.get_monitored_videos()
        if videos and videos[0][1] == bv:
            return videos[0][0], videos[0][2]
        oid, title = get_video_info(bv, header)
        if oid:
            db.clear_videos()
            db.add_video_to_db(oid, bv, title)
            logging.info(f"监控视频切换: {title}")
            return oid, title
    return None, None

# ------------------------
# 【核心修改点】动态雷达逻辑：内容穿透抓取
# ------------------------
def init_extra_dynamics(header):
    seen, active = {}, {}
    for uid in EXTRA_DYNAMIC_UIDS:
        seen[uid], active[uid] = set(), {}
        data = safe_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space", {"host_mid": uid}, header)
        if data.get("code") == 0:
            for item in data.get("data", {}).get("items", []):
                if item.get("id_str"): seen[uid].add(item["id_str"])
    return seen, active

def check_new_dynamics(header, seen_dynamics, active_dynamics):
    new_alerts = []
    has_new_dynamic = False
    now_ts = time.time()
    
    for uid in EXTRA_DYNAMIC_UIDS:
        data = safe_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space", {"host_mid": uid}, header)
        if data.get("code") != 0: continue
        
        items = data.get("data", {}).get("items", [])
        for item in items:
            id_str = item.get("id_str")
            if not id_str or id_str in seen_dynamics[uid]: continue
            
            # 1. 时效性校验
            author_mod = item.get("modules", {}).get("module_author", {})
            try: pub_ts = float(author_mod.get("pub_ts", 0))
            except: pub_ts = 0
            if now_ts - pub_ts > DYNAMIC_MAX_AGE:
                seen_dynamics[uid].add(id_str)
                continue
            
            seen_dynamics[uid].add(id_str)
            has_new_dynamic = True
            
            # 2. 深度提取正文 (参考参考代码)
            mod_dyn = item.get("modules", {}).get("module_dynamic", {})
            content_text = mod_dyn.get("desc", {}).get("text", "")
            
            major = mod_dyn.get("major", {})
            if not content_text and major.get("opus"):
                content_text = major["opus"].get("summary", {}).get("text", "")
            
            attach_info = ""
            dyn_type = item.get("type")
            if major.get("archive"):
                v = major["archive"]
                attach_info = f"🎥 视频: {v.get('title')}\n简介: {v.get('desc', '')[:50]}"
            elif major.get("article"):
                a = major["article"]
                attach_info = f"📄 专栏: {a.get('title')}"
            elif dyn_type == "DYNAMIC_TYPE_FORWARD":
                orig_author = item.get("orig", {}).get("modules", {}).get("module_author", {}).get("name", "未知")
                attach_info = f"🔄 转发了 @{orig_author} 的动态"
            
            final_msg = ""
            if content_text: final_msg += f"【正文】: {content_text}\n"
            if attach_info: final_msg += f"【关联】: {attach_info}"
            if not final_msg: final_msg = "发布了新动态"
            
            name = author_mod.get("name", str(uid))
            new_alerts.append({"user": name, "message": final_msg})
            logging.info(f"动态抓取成功: [{name}] {final_msg.replace(chr(10), ' ')[:60]}...")
            
            # 加入评论监控名单
            basic = item.get("basic", {})
            if basic.get("comment_id_str"):
                active_dynamics[uid][id_str] = {
                    "oid": basic["comment_id_str"], "type": basic["comment_type"], "ctime": time.time()
                }
                
    if new_alerts:
        try: notifier.send_webhook_notification("💡 特别关注UP发布新内容", new_alerts)
        except: pass
    return has_new_dynamic

def check_dynamic_up_replies(header, active_dynamics, seen_replies):
    new_alerts = []
    curr = time.time()
    for uid, dyns in list(active_dynamics.items()):
        for did, info in list(dyns.items()):
            if curr - info["ctime"] > 86400:
                del dyns[did]; continue
            
            data = wbi_request("https://api.bilibili.com/x/v2/reply", 
                               {"oid": info["oid"], "type": info["type"], "sort": 0, "pn": 1, "ps": 20}, header)
            if data.get("code") == 0:
                replies = (data.get("data", {}).get("replies") or [])
                top = data.get("data", {}).get("upper", {}).get("top", None)
                if top: replies.append(top)
                
                for r in replies:
                    if r and str(r["member"]["mid"]) == str(uid):
                        rpid = r["rpid_str"]
                        if rpid not in seen_replies:
                            seen_replies.add(rpid)
                            uname = r["member"]["uname"]
                            msg = r["content"]["message"]
                            new_alerts.append({"user": uname, "message": f"💬 UP主在动态下补充：\n{msg}"})
                            logging.info(f"捕捉到UP主动态回复: [{uname}] {msg[:30]}")
    if new_alerts:
        try: notifier.send_webhook_notification("🔔 UP主本尊动态出没", new_alerts)
        except: pass

# ------------------------
# 【零修改】评论监控核心 (保持 Sort 0 及原始结构)
# ------------------------
def scan_new_comments(oid, header, last_read_time, seen):
    new_list = []
    max_ctime_in_this_round = last_read_time
    safe_read_time = last_read_time - 300 
    pn = 1
    while pn <= 10:  
        params = {"oid": oid, "type": 1, "sort": 0, "pn": pn, "ps": 20}
        data = wbi_request("https://api.bilibili.com/x/v2/reply", params, header)
        replies = data.get("data", {}).get("replies") or []
        if not replies: break
        page_all_older = True  
        for r_obj in replies:
            rpid, r_ctime = r_obj["rpid_str"], r_obj["ctime"]
            max_ctime_in_this_round = max(max_ctime_in_this_round, r_ctime)
            if r_ctime > safe_read_time:
                page_all_older = False
                if rpid not in seen:
                    seen.add(rpid)
                    new_list.append({
                        "user": r_obj["member"]["uname"], 
                        "message": r_obj["content"]["message"], 
                        "ctime": r_ctime
                    })
        if page_all_older: break
        pn += 1
        time.sleep(random.uniform(0.5, 1.0))
    return new_list, max_ctime_in_this_round

# ------------------------
# 主循环守护
# ------------------------
def start_monitoring(header):
    last_v_check = 0; last_hb = time.time(); last_d_check = 0; burst_end = 0
    oid, title = sync_latest_video(header)
    last_read_time = int(time.time()); seen_comments = set()
    seen_dyns, active_dyns = init_extra_dynamics(header); seen_dyn_replies = set()
    
    logging.info(f"程序启动。当前监控视频: {title or '获取中'}")

    while True:
        try:
            now = time.time()
            if is_work_time():
                # 1. 评论监控
                if oid:
                    new_c, new_t = scan_new_comments(oid, header, last_read_time, seen_comments)
                    if new_t > last_read_time: last_read_time = new_t
                    if new_c:
                        new_c.sort(key=lambda x: x["ctime"])
                        for item in new_c: logging.info(f"抓取评论成功: [{item['user']}] {item['message'][:30]}...")
                        try: notifier.send_webhook_notification(title, new_c)
                        except: pass

                # 2. 动态监控 (含狂暴模式处理)
                d_interval = DYNAMIC_BURST_INTERVAL if now < burst_end else DYNAMIC_CHECK_INTERVAL
                if now - last_d_check >= d_interval:
                    if check_new_dynamics(header, seen_dyns, active_dyns):
                        burst_end = now + DYNAMIC_BURST_DURATION
                        logging.info("🔥 发现新动态，激活 5 分钟高频扫描模式")
                    check_dynamic_up_replies(header, active_dyns, seen_dyn_replies)
                    last_d_check = now

                # 3. 心跳
                if now - last_hb >= HEARTBEAT_INTERVAL:
                    try: notifier.send_webhook_notification("心跳", [{"user": "系统", "message": f"运行中\n监控视频: {title}"}])
                    except: pass
                    last_hb = now

                time.sleep(random.uniform(10, 15))
            else:
                time.sleep(30)

            # 定时同步视频
            if now - last_v_check > VIDEO_CHECK_INTERVAL:
                res = sync_latest_video(header)
                if res: oid, title = res
                last_v_check = now
        except Exception:
            logging.error(traceback.format_exc()); time.sleep(60)

if __name__ == "__main__":
    init_logging()
    db.init_db()
    h = get_header()
    update_wbi_keys(h)
    start_monitoring(h)
