import sys
import os
import time
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
TARGET_UID = 1671203508
VIDEO_CHECK_INTERVAL = 21600
HEARTBEAT_INTERVAL = 600
EXTRA_DYNAMIC_UIDS = [
    3546905852250875,
    3546961271589219,
    3546610447419885,
    285340365,
    3706948578969654
]
DYNAMIC_CHECK_INTERVAL = 30
DYNAMIC_MAX_AGE = 600   # 超过10分钟直接忽略
LOG_FILE = "bili_monitor.log"

def init_logging():
    try:
        if os.path.exists(LOG_FILE):
            open(LOG_FILE, "w").close()
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
    logging.info("B站监控系统启动")
    logging.info("=" * 60)

def safe_request(url, params, header, retries=3):
    h = header.copy()
    h["Connection"] = "close"
    for i in range(retries):
        try:
            r = requests.get(url, headers=h, params=params, timeout=10)
            if r.text.strip():
                return r.json()
        except:
            time.sleep(2 + i)
    return {"code": -500}

# WBI 部分保持不变（省略中间不变代码，只保留关键）
WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}
mixinKeyEncTab = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]

def getMixinKey(orig):
    return "".join([orig[i] for i in mixinKeyEncTab])[:32]

def encWbi(params, img_key, sub_key):
    mixin_key = getMixinKey(img_key + sub_key)
    params["wts"] = round(time.time())
    params = dict(sorted(params.items()))
    filtered = {k: str(v).translate({ord(c): None for c in "!'()*"}) for k, v in params.items()}
    query = urllib.parse.urlencode(filtered)
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
    except:
        pass

def wbi_request(url, params, header):
    if not WBI_KEYS["img_key"] or time.time() - WBI_KEYS["last_update"] > 21600:
        update_wbi_keys(header)
    signed = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    return safe_request(url, signed, header)

def get_header():
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    except:
        subprocess.run([sys.executable, "login_bilibili.py"])
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    return {"Cookie": cookie, "User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}

# ---------------- 极简动态解析 ----------------
def deep_find_text(obj):
    result = []
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ["text", "content", "desc", "title"] and isinstance(v, str) and v.strip():
                    result.append(v.strip())
                walk(v)
        elif isinstance(x, list):
            for i in x:
                walk(i)
    walk(obj)
    return " ".join(dict.fromkeys(result)).strip()

def extract_dynamic_text(item):
    try:
        dyn = (item.get("modules") or {}).get("module_dynamic") or {}
        desc = dyn.get("desc") or {}
        text = desc.get("text") or ""
        if text:
            return str(text).strip()
        rich = desc.get("rich_text_nodes") or []
        if rich:
            texts = [str(n.get("orig_text") or n.get("text") or "").strip() for n in rich if isinstance(n, dict)]
            text = "\n".join(t for t in texts if t)
            if text:
                return text
        return deep_find_text(dyn) or "发布了新动态"
    except:
        return "发布了新动态"

# ---------------- 动态检查（只取最新，超10分钟忽略） ----------------
def init_extra_dynamics(header):
    seen = {uid: set() for uid in EXTRA_DYNAMIC_UIDS}
    return seen

def check_new_dynamics(header, seen_dynamics):
    alerts = []
    now_ts = time.time()
    for uid in EXTRA_DYNAMIC_UIDS:
        try:
            data = safe_request(
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
                {"host_mid": uid},
                header
            )
            if data.get("code") != 0:
                continue
            items = (data.get("data") or {}).get("items", [])
            if not items:
                continue
            item = items[0]                     # 只取最新一条
            id_str = item.get("id_str")
            if not id_str or id_str in seen_dynamics[uid]:
                continue
            modules = item.get("modules") or {}
            author = modules.get("module_author") or {}
            pub_ts = float(author.get("pub_ts", 0))
            if now_ts - pub_ts > DYNAMIC_MAX_AGE:   # 超过10分钟忽略
                continue
            seen_dynamics[uid].add(id_str)
            name = author.get("name", str(uid))
            text = extract_dynamic_text(item)
            final_msg = f"{text}\n\n🔗 https://t.bilibili.com/{id_str}"
            alerts.append({"user": name, "message": final_msg})
            logging.info(f"抓取新动态 [{name}]")
        except:
            pass
    if alerts:
        try:
            notifier.send_webhook_notification("💡 特别关注UP主发布新内容", alerts)
            logging.info(f"发送 {len(alerts)} 条动态通知")
        except Exception as e:
            logging.error(f"Webhook失败: {e}")
    return bool(alerts)

# ---------------- 视频和评论部分保持不变（省略，实际使用时保留你原来的） ----------------
# get_latest_video / get_video_info / sync_latest_video / scan_new_comments / start_monitoring 等保持你之前稳定版本

# 为保证完整，这里给出简化版主循环（只保留核心）
def start_monitoring(header):
    last_d_check = 0
    seen_dynamics = init_extra_dynamics(header)
    while True:
        try:
            now = time.time()
            if now - last_d_check >= DYNAMIC_CHECK_INTERVAL:
                check_new_dynamics(header, seen_dynamics)
                last_d_check = now
            time.sleep(random.uniform(10, 15))
        except:
            time.sleep(60)

if __name__ == "__main__":
    init_logging()
    db.init_db()
    h = get_header()
    update_wbi_keys(h)
    start_monitoring(h)
