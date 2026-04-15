#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import os
import time
import random
import logging
import traceback
import hashlib
import urllib.parse
import json
import subprocess
import requests

import database as db
import notifier

# ====================== 配置区 ======================
TARGET_UID = 1671203508               # 主监控 UP 主（如果只想监控自己可置为 0）
VIDEO_CHECK_INTERVAL = 21600         # 6 h 检查一次新视频
HEARTBEAT_INTERVAL = 600             # 10 min 心跳

# 需要额外关注的 UP 主（动态）
EXTRA_DYNAMIC_UIDS = [
    3546905852250875,
    3546961271589219,
    3546610447419885,
    285340365,
    3706948578969654
]

# 动态轮询参数
DYNAMIC_CHECK_INTERVAL = 30          # 常规轮询间隔（秒）
DYNAMIC_BURST_INTERVAL = 10          # 触发突发（检测到新动态后）轮询间隔
DYNAMIC_BURST_DURATION = 300         # 突发模式持续时间（秒）
DYNAMIC_MAX_AGE = 300                 # 动态最大保留时间（秒），超过则忽略
LOG_FILE = "bili_monitor.log"

# ----------------- 新增功能 -----------------
# 1️⃣ 启动延迟（秒）——防止脚本刚启动时大量并发请求
STARTUP_DELAY_SECONDS = 120          # 2 分钟

# 2️⃣ 时间平移（秒）——让内部感知的“现在时间”比真实时间慢 2 分钟
#    仅在评论轮询中使用（防止因网络时延错过刚发布的评论），动态检测不受影响
TIME_SHIFT_SECONDS = 120
# -------------------------------------------------

def init_logging():
    """初始化日志（每次启动会覆盖旧日志）"""
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.truncate()
    except Exception:
        pass

    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        encoding="utf-8",
        filemode="w"
    )
    logging.info("=" * 60)
    logging.info("B站监控系统启动 (24 h 全天候监控)")
    logging.info(f"⚡ 项目时间已平移 {TIME_SHIFT_SECONDS}s（实际时间 = 记录时间 + {TIME_SHIFT_SECONDS}s）")
    logging.info("=" * 60)


# ----------------- 基础请求封装 -----------------
def safe_request(url, params, header, retries=3):
    """GET 请求 + 重试，返回 JSON（出错返回 {'code': -500}）"""
    h = header.copy()
    h["Connection"] = "close"

    for i in range(retries):
        try:
            r = requests.get(url, headers=h, params=params, timeout=10)
            r.raise_for_status()
            txt = r.text.strip()
            if not txt:
                time.sleep(2)
                continue
            return r.json()
        except requests.RequestException as e:
            logging.error(f"safe_request error [{i+1}/{retries}] to {url}: {e}")
            time.sleep(2 + i)

    return {"code": -500, "message": "request failed after retries"}


# ----------------- WBI（防 403） -----------------
WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}
mixinKeyEncTab = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52
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
            logging.info("WBI 密钥已更新")
    except Exception:
        pass

def wbi_request(url, params, header):
    if (not WBI_KEYS["img_key"]) or (time.time() - WBI_KEYS["last_update"] > 21600):
        update_wbi_keys(header)
    signed = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    return safe_request(url, signed, header)


# ----------------- 认证 & 时间校准 -----------------
def get_header():
    """读取本地 cookie；若不存在自动跑一次扫码登录"""
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    except Exception:
        subprocess.run([sys.executable, "login_bilibili.py"], check=True)
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    return {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/128.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/"
    }


def is_work_time():
    """已解除时间封印，强制 24 h 全天候运行"""
    return True


# ----------------- 视频监控（保留原有逻辑） -----------------
def get_latest_video(header):
    data = safe_request(
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
        {"host_mid": TARGET_UID},
        header
    )
    if data.get("code") != 0:
        return None
    for item in (data.get("data") or {}).get("items", []):
        try:
            if item.get("type") == "DYNAMIC_TYPE_AV":
                return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
        except Exception:
            continue
    return None


def get_video_info(bv, header):
    data = safe_request(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bv}",
        None,
        header
    )
    if data.get("code") == 0:
        return str(data["data"]["aid"]), data["data"]["title"]
    return None, None


def sync_latest_video(header):
    """如果有新视频则更新 DB 并返回 (oid, title)；否则返回 (None, None)"""
    bv = get_latest_video(header)
    if not bv:
        return None, None
    videos = db.get_monitored_videos()
    if videos and videos[0][1] == bv:
        return videos[0][0], videos[0][2]   # 已是最新视频
    oid, title = get_video_info(bv, header)
    if oid:
        db.clear_videos()
        db.add_video_to_db(oid, bv, title)
        return oid, title
    return None, None


# ----------------- 动态监控（核心） -----------------
def init_extra_dynamics(header):
    """初始化已读动态集合（首次运行时全部标记为已读）"""
    seen = {}
    for uid in EXTRA_DYNAMIC_UIDS:
        seen[uid] = set()
        data = safe_request(
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
            {"host_mid": uid},
            header
        )
        if data.get("code") == 0:
            for item in (data.get("data") or {}).get("items", []):
                if item.get("id_str"):
                    seen[uid].add(item["id_str"])
    return seen


def deep_find_text(obj):
    """兜底深度搜索文本（保留原实现）"""
    result = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ["text", "content", "desc", "title", "words"]:
                    if isinstance(v, str) and v.strip():
                        result.append(v.strip())
                walk(v)
        elif isinstance(x, list):
            for i in x:
                walk(i)

    walk(obj)
    uniq = []
    for x in result:
        if x not in uniq:
            uniq.append(x)
    return " ".join(uniq).strip()


def extract_dynamic_text(item):
    """
    完整提取动态文字（包括富文本 + 回退），并做安全截断
    """
    try:
        # 1️⃣ 优先读取 rich_text_nodes（如果有的话）
        rich = (item.get("modules", {})
                   .get("module_dynamic", {})
                   .get("desc", {})
                   .get("rich_text_nodes", []))
        if rich:
            txt = "".join([node.get("text", "") for node in rich if isinstance(node, dict)])
            txt = txt.strip()
        else:
            txt = ""

        # 2️⃣ 若没有富文本，使用深度遍历兜底
        if not txt:
            txt = deep_find_text(item.get("modules", {}))

        # 3️⃣ 仍然为空则返回一个标识（防止出现 None）
        if not txt:
            txt = "[未检测到文字内容]"

        # 4️⃣ 安全截断（防止 webhook 过长）
        MAX_LEN = 1500
        if len(txt) > MAX_LEN:
            txt = txt[:MAX_LEN] + "\n\n...(内容过长，已安全截断)"
        return txt
    except Exception as e:
        logging.error(f"动态文字提取异常: {e}\n{traceback.format_exc()}")
        return "[动态文字提取失败]"


def get_dynamic_update_number(baseline, header):
    """
    使用官方 “动态更新基线” 接口查询是否有新动态
    返回 update_num（>0 表示有新动态）
    """
    url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all/update"
    params = {
        "update_baseline": baseline,
        "type": "all",
        "web_location": "333.1365"
    }
    data = safe_request(url, params, header)
    if data.get("code") == 0:
        return data.get("data", {}).get("update_num", 0)
    return 0


def fetch_all_dynamics(header, offset=""):
    """
    拉取全部动态列表（type=all），返回 items（列表）和新的 offset
    """
    url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all"
    params = {
        "type": "all",
        "timezone_offset": "-480",
        "web_location": "333.1365",
        "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,decorationCard,onlyfansAssetsV2,ugcDelete"
    }
    if offset:
        params["offset"] = offset
    data = safe_request(url, params, header)
    if data.get("code") != 0:
        return [], ""
    items = (data.get("data") or {}).get("items", [])
    new_offset = (data.get("data") or {}).get("offset", "")
    return items, new_offset


def check_new_dynamics(header, seen_dynamics):
    """
    检查所有 EXTRA_DYNAMIC_UIDS 是否有新动态。
    若有新动态立即推送 webhook，并返回 True（触发突发模式）。
    """
    alerts = []
    now_ts = time.time()                     # **真实时间**（不做平移）

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
            for item in items:
                id_str = item.get("id_str")
                if not id_str or id_str in seen_dynamics[uid]:
                    continue

                # 标记为已读
                seen_dynamics[uid].add(id_str)

                # 动态发布时间（秒级时间戳）
                pub_ts = float(item.get("modules", {})
                                   .get("module_author", {})
                                   .get("pub_ts", 0))

                # **时间阈值**：只保留最近 5 分钟内的动态（不受 TIME_SHIFT 影响）
                if now_ts - pub_ts > DYNAMIC_MAX_AGE:
                    logging.info(
                        f"⏭️ 忽略过旧动态 [{item['modules']['module_author']['name']}] "
                        f"ID:{id_str} (距今 {int(now_ts - pub_ts)}s > {DYNAMIC_MAX_AGE}s)"
                    )
                    continue

                # 提取文字
                text = extract_dynamic_text(item)

                # 拼装推送信息
                final_msg = f"{text}\n\n🔗 直达链接: https://t.bilibili.com/{id_str}"
                alerts.append({"user": item["modules"]["module_author"]["name"],
                               "message": final_msg})

                logging.info(f"✅ 检测到新动态 [{item['modules']['module_author']['name']}] "
                             f"ID:{id_str}\n{final_msg}")

                # 每个 UID 本轮只取第一条新动态，防止一次轮询一次性推送太多
                break

        except Exception as e:
            logging.error(f"❌ 动态获取异常 UID={uid}: {e}\n{traceback.format_exc()}")

        # 随机间隔，降低风控概率
        time.sleep
