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
VIDEO_CHECK_INTERVAL = 21600      
HEARTBEAT_INTERVAL = 600          

# 动态监控名单
EXTRA_DYNAMIC_UIDS = [
    3546905852250875, 
    3546961271589219, 
    3546610447419885, 
    285340365, 
    3706948578969654
]

DYNAMIC_CHECK_INTERVAL = 30
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
    logging.info("B站监控启动：旧日志已清空，300s动态时效已激活")
    logging.info("="*50)

# ------------------------
# Wbi 签名模块 (保持原样)
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
        r = requests.get(url, headers=header, timeout=10)
        data = r.json()
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
    try:
        r = requests.get(url, headers=header, params=signed_params, timeout=10)
        return r.json()
    except Exception: return {"code": -1}

# ------------------------
# 基础辅助模块 (保持原样)
# ------------------------
def get_header():
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    except:
        subprocess.run([sys.executable, "login_bilibili.py"])
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    return {"Cookie": cookie, "User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com"}

def is_work_time():
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
    return now.weekday() < 5 and 9 <= now.hour < 19

def get_video_info(bv, header):
    url = f"https://api.bilibili.com/x/web-interface/view?bvid={bv}"
    try:
        r = requests.get(url, headers=header, timeout=10)
        data = r.json()
        if data["code"] == 0:
            return str(data["data"]["aid"]), data["data"]["title"]
    except: pass
    return None, None

def sync_latest_video(header):
    try:
        r = requests.get(f"https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?host_mid={TARGET_UID}", headers=header, timeout=10)
        data = r.json()
        items = data.get("data", {}).get("items", [])
        for item in items:
            if item.get("type") == "DYNAMIC_TYPE_AV":
                arc = item.get("modules", {}).get("module_dynamic", {}).get("major", {}).get("archive", {})
                aid, bv, title = str(arc.get("aid")), arc.get("bvid"), arc.get("title")
                v = db.get_monitored_videos()
                if not v or v[0][0] != aid:
                    db.clear_videos(); db.add_video_to_db(aid, bv, title)
                    logging.info(f"监控视频切换: {title}")
                return aid, title
    except: pass
    return None, None

# ------------------------
# 深度优化：动态正文抓取逻辑
# ------------------------
def init_extra_dynamics(header):
    return {uid: set() for uid in EXTRA_DYNAMIC_UIDS}

def check_new_dynamics(header, seen_dynamics):
    new_alerts = []
    now_ts = time.time()
    for uid in EXTRA_DYNAMIC_UIDS:
        url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
        params = {"host_mid": uid}
        try:
            r = requests.get(url, headers=header, params=params, timeout=10)
            data = r.json()
            if data.get("code") != 0: continue
            items = data.get("data", {}).get("items", [])
            if not items: continue

            item = items[0]
            id_str = item.get("id_str")
            if not id_str or id_str in seen_dynamics[uid]: continue

            # 时效性校验
            author_mod = item.get("modules", {}).get("module_author", {})
            try: pub_ts = float(author_mod.get("pub_ts", 0))
            except: pub_ts = 0
            if now_ts - pub_ts > DYNAMIC_MAX_AGE:
                seen_dynamics[uid].add(id_str)
                continue

            seen_dynamics[uid].add(id_str)

            # --- 深度提取内容逻辑 ---
            dyn_text = ""
            attach_str = ""
            module_dyn = item.get("modules", {}).get("module_dynamic", {})
            
            # 1. 提取发布者的描述文本 (通用文字/图文)
            if module_dyn.get("desc") and module_dyn["desc"].get("text"):
                dyn_text = module_dyn["desc"]["text"]
            
            # 2. 如果是新版图文 (Opus格式) 且 dyn_text 为空
            major = module_dyn.get("major", {})
            if major.get("opus"):
                opus = major["opus"]
                if not dyn_text: dyn_text = opus.get("summary", {}).get("text", "")
            
            # 3. 提取关联卡片信息
            dyn_type = item.get("type")
            if dyn_type == "DYNAMIC_TYPE_AV":
                arc = major.get("archive", {})
                attach_str = f"🎥 视频：《{arc.get('title')}》\n摘要：{arc.get('desc', '')[:50]}"
            elif dyn_type == "DYNAMIC_TYPE_ARTICLE":
                art = major.get("article", {})
                attach_str = f"📄 专栏：《{art.get('title')}》\n摘要：{art.get('desc', '')[:50]}"
            elif dyn_type == "DYNAMIC_TYPE_FORWARD":
                # 处理转发：尝试抓取原作者名
                fwd_item = item.get("orig")
                orig_author = "未知用户"
                try: orig_author = fwd_item["modules"]["module_author"]["name"]
                except: pass
                attach_str = f"🔄 转发了 {orig_author} 的内容"
            elif dyn_type == "DYNAMIC_TYPE_LIVE_RCMD":
                live = major.get("live_rcmd", {}).get("content", {}).get("live_play_info", {})
                attach_str = f"🔴 正在直播：{live.get('title')}"

            # 组装正文
            final_desc = ""
            if dyn_text: final_desc += f"【正文】:\n{dyn_text}\n"
            if attach_str: final_desc += f"【附带】: {attach_str}"
            if not final_desc: final_desc = "发布了新内容"

            name = author_mod.get("name", str(uid))
            new_alerts.append({"user": name, "message": final_desc})
            
            # 日志输出：完整正文压缩为单行
            log_content = final_desc.replace('\n', ' ')
            logging.info(f"成功抓取动态 - [{name}] 内容: {log_content}")

        except Exception: pass

    if new_alerts:
        try: notifier.send_webhook_notification("💡 特别关注UP主发布新内容", new_alerts)
        except: pass

# ------------------------
# 核心扫描：主视频评论 (零修改)
# ------------------------
def scan_new_comments(oid, header, last_read_time, seen):
    new_list = []
    max_ctime = last_read_time
    safe_time = last_read_time - 300
    pn = 1
    while pn <= 10:
        params = {"oid": oid, "type": 1, "sort": 0, "pn": pn, "ps": 20}
        try:
            data = wbi_request("https://api.bilibili.com/x/v2/reply", params, header)
            if data.get("code") != 0: break
            replies = data.get("data", {}).get("replies") or []
            if not replies: break
            page_all_older = True
            for r_obj in replies:
                rpid, r_ctime = r_obj["rpid_str"], r_obj["ctime"]
                max_ctime = max(max_ctime, r_ctime)
                if r_ctime > safe_time:
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
        except: break
    return new_list, max_ctime

def start_monitoring(header):
    last_v_check = 0; last_hb = time.time(); last_d_check = 0
    oid, title = sync_latest_video(header)
    last_read_time = int(time.time()); seen_comments = set()
    seen_dynamics = init_extra_dynamics(header)

    logging.info("监控服务已启动...")

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
                        for item in new_c:
                            logging.info(f"抓取评论 - [{item['user']}]: {item['message'][:30]}...")
                        try: notifier.send_webhook_notification(title, new_c)
                        except: pass

                # 2. 动态监控
                if now - last_d_check >= DYNAMIC_CHECK_INTERVAL:
                    check_new_dynamics(header, seen_dynamics)
                    last_d_check = now

                # 3. 心跳
                if now - last_hb >= HEARTBEAT_INTERVAL:
                    notifier.send_webhook_notification("心跳", [{"user": "系统", "message": f"运行中\n监控: {title or '无'}"}])
                    last_hb = now

                time.sleep(random.uniform(10, 20))
            else:
                time.sleep(30)

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
