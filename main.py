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
import json
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
DYNAMIC_BURST_INTERVAL = 10
DYNAMIC_BURST_DURATION = 300
DYNAMIC_MAX_AGE = 300

LOG_FILE = "bili_monitor.log"
# ==============================================


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
    logging.info("B站监控系统启动 (24小时全天候监控模式 + 极简反风控)")
    logging.info("=" * 60)


def safe_request(url, params, header, retries=3):
    h = header.copy()
    h["Connection"] = "close"

    for i in range(retries):
        try:
            # 🛡️ 反风控：请求前置随机微延迟，打乱并发特征
            time.sleep(random.uniform(0.5, 1.5))
            
            r = requests.get(
                url,
                headers=h,
                params=params,
                timeout=10
            )

            txt = r.text.strip()

            if not txt:
                time.sleep(2)
                continue

            data = r.json()
            
            # 🛡️ 反风控：-352 专项熔断，一旦触发长休眠，避免Cookie被彻底拉黑
            if data.get("code") == -352:
                logging.warning(f"触发 -352 风控！请求 URL: {url}，进入长休眠避难...")
                time.sleep(random.uniform(10, 20))
                continue

            return data

        except Exception as e:
            logging.error(f"请求异常: {e}")
            time.sleep(2 + i)

    return {"code": -500}


# ---------------- WBI ----------------
WBI_KEYS = {
    "img_key": "",
    "sub_key": "",
    "last_update": 0
}

mixinKeyEncTab = [
    46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,
    27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,
    37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,
    22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52
]


def getMixinKey(orig):
    return "".join([orig[i] for i in mixinKeyEncTab])[:32]


def encWbi(params, img_key, sub_key):
    mixin_key = getMixinKey(img_key + sub_key)

    params["wts"] = round(time.time())
    params = dict(sorted(params.items()))

    filtered = {}

    for k, v in params.items():
        v = str(v)
        for c in "!'()*":
            v = v.replace(c, "")
        filtered[k] = v

    query = urllib.parse.urlencode(filtered)
    sign = hashlib.md5((query + mixin_key).encode()).hexdigest()

    filtered["w_rid"] = sign
    return filtered


def update_wbi_keys(header):
    try:
        data = safe_request(
            "https://api.bilibili.com/x/web-interface/nav",
            None,
            header
        )

        if data.get("code") == 0:
            img = data["data"]["wbi_img"]

            WBI_KEYS["img_key"] = img["img_url"].rsplit("/", 1)[1].split(".")[0]
            WBI_KEYS["sub_key"] = img["sub_url"].rsplit("/", 1)[1].split(".")[0]
            WBI_KEYS["last_update"] = time.time()

            logging.info("WBI密钥已更新")

    except:
        pass


def wbi_request(url, params, header):
    if (
        not WBI_KEYS["img_key"]
        or time.time() - WBI_KEYS["last_update"] > 21600
    ):
        update_wbi_keys(header)

    signed = encWbi(
        params.copy(),
        WBI_KEYS["img_key"],
        WBI_KEYS["sub_key"]
    )

    return safe_request(url, signed, header)


# ---------------- 基础 ----------------
def get_header():
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    except:
        subprocess.run([sys.executable, "login_bilibili.py"])

        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()

    # 🛡️ 反风控：参照 bilibili-API-collect 补全完整浏览器特征请求头
    return {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://www.bilibili.com",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site"
    }


def is_work_time():
    return True


# ---------------- 视频 ----------------
def get_latest_video(header):
    data = safe_request(
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
        {"host_mid": TARGET_UID},
        header
    )

    if data.get("code") != 0:
        return None

    items = (data.get("data") or {}).get("items", [])

    for item in items:
        try:
            if item.get("type") == "DYNAMIC_TYPE_AV":
                return item["modules"]["module_dynamic"]["major"]["archive"]["
