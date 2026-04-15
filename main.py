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
import requests

import database as db
import notifier

# ================= 核心配置区 =================
TARGET_UID = 1671203508               # 主监控 UP 主
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

# 动态轮询相关
DYNAMIC_CHECK_INTERVAL = 30          # 常规轮询间隔（秒）
DYNAMIC_BURST_INTERVAL = 10          # 突发模式（检测到新动态后）轮询间隔
DYNAMIC_BURST_DURATION = 300         # 突发模式持续时间（秒）
DYNAMIC_MAX_AGE = 300                 # 动态最大保留时间（秒），超过则忽略

LOG_FILE = "bili_monitor.log"

# ---------- 新增配置 ----------
# 1️⃣ 启动延迟（秒）——防止脚本刚启动时立刻并发大量请求
STARTUP_DELAY_SECONDS = 120          # 2 分钟

# 2️⃣ 时间平移（秒）——让项目内部的“现在时间”比真实时间慢 2 分钟
TIME_SHIFT_SECONDS = 120             # 2 分钟
# =============================================

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
    logging.info("B站监控系统启动 (24小时全天候监控模式)")
    logging.info(f"⚡ 项目时间已平移 {TIME_SHIFT_SECONDS}s（实际时间 = 记录时间 + {TIME_SHIFT_SECONDS}s）")
    logging.info("=" * 60)


def safe_request(url, params, header, retries=3):
    """带重试的 GET 请求，返回 JSON（出错返回 {'code':-500}）"""
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


# ---------------- WBI ----------------
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
            logging.info("WBI密钥已更新")
    except Exception:
        pass

def wbi_request(url, params, header):
    if (not WBI_KEYS["img_key"]) or (time.time() - WBI_KEYS["last_update"] > 21600):
        update_wbi_keys(header)

    signed = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    return safe_request(url, signed, header)


# ---------------- 基础 ----------------
def get_header():
    """读取本地 cookie（若不存在自动跑一次扫码登录）"""
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    except Exception:
        # cookie 不存在或读取异常，走登录脚本
        subprocess.run([sys.executable, "login_bilibili.py"], check=True)

        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()

    return {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.bilibili.com/"
    }

def is_work_time():
    """已解除时间封印，强制 24‑h 全天候运行"""
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
        # 已是最新视频，无需更新
        return videos[0][0], videos[0][2]

    oid, title = get_video_info(bv, header)
    if oid:
        db.clear_videos()
        db.add_video_to_db(oid, bv, title)
        return oid, title
    return None, None


# ---------------- 动态（完整排版） ----------------
def init_extra_dynamics(header):
    """为每个关注的 UID 构造已看到的动态 ID 集合（第一次运行时全部标记为已读）"""
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
    """原版的兜底深度搜索——返回所有 text / content / desc / title / words"""
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
    # 去重
    uniq = []
    for x in result:
        if x not in uniq:
            uniq.append(x)
    return " ".join(uniq).strip()


def extract_dynamic_text(item):
    """
    升级版提取：完整换行排版 + 彻底免疫 NoneType + 安全截断
    """
    try:
        # 1️⃣ 直接取富文本节点（如果有的话）
        rich = (item.get("modules", {})
                   .get("module_dynamic", {})
                   .get("desc", {})
                   .get("rich_text_nodes", []))

        content_list = []

        if rich:
            node_texts = []
            for node in rich:
                if isinstance(node, dict):
                    node_texts.append(str(node.get("text", "")))
            parsed = "".join(node_texts).strip()
            if parsed:
                content_list.append(parsed)

        # 2️⃣ 若无富文本，使用深度搜索作为兜底
        if not content_list:
            text = deep_find_text(item.get("modules", {}))
            if text:
                content_list.append(text)

        # 3️⃣ 仍然为空则给一个标识
        if not content_list:
            raw = json.dumps(item, ensure_ascii=False)
            if len(raw) > 500:
                raw = "【特殊类型动态 / 纯转发 / 纯视频】无正文。"
            content_list.append(raw)

        final_text = "\n".join(content_list).strip()

        # ⚠️ 防止超长导致 webhook 失败（放宽至 1500 字）
        MAX_LEN = 1500
        if len(final_text) > MAX_LEN:
            final_text = final_text[:MAX_LEN] + "\n\n...(内容过长，已安全截断)"
        return final_text
    except Exception as e:
        logging.error(f"提取动态文本异常: {e}\n{traceback.format_exc()}")
        return "发布了新动态 (内容解析失败)"


def check_new_dynamics(header, seen_dynamics):
    """
    检查所有 EXTRA_DYNAMIC_UIDS 是否有新动态。
    若有新动态则立即推送 webhook，并返回 True（触发突发模式）。
    """
    alerts = []
    now_ts = time.time() + TIME_SHIFT_SECONDS  # 平移后的当前时间

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

                modules = item.get("modules") or {}
                author = modules.get("module_author") or {}

                try:
                    pub_ts = float(author.get("pub_ts", 0))
                except Exception:
                    pub_ts = 0

                name = author.get("name", str(uid))

                # ----- 时间阈值检查 -----
                time_diff = now_ts - pub_ts
                if time_diff > DYNAMIC_MAX_AGE:
                    logging.info(
                        f"⏭️ 忽略超时动态 [{name}] ID:{id_str}, "
                        f"距今 {int(time_diff)} 秒 (阈值 {DYNAMIC_MAX_AGE}s)"
                    )
                    continue

                # ----- 提取并构造消息 -----
                text = extract_dynamic_text(item)
                final_msg = f"{text}\n\n🔗 直达链接: https://t.bilibili.com/{id_str}"

                alerts.append({"user": name, "message": final_msg})
                logging.info(f"✅ 抓取到新动态并准备推送 [{name}]:\n{final_msg}")

                # 每个 UID 只取本轮的第一条新动态（防止一次轮询一次性塞满）
                break

        except Exception as e:
            logging.error(f"❌ 动态获取异常 UID={uid}: {e}\n{traceback.format_exc()}")

        # 随机间隔，降低风控概率
        time.sleep(random.uniform(1, 2))

    if alerts:
        try:
            notifier.send_webhook_notification(
                "💡 特别关注UP主发布新内容",
                alerts
            )
            logging.info(f"🚀 成功发送 {len(alerts)} 条 Webhook 动态通知！")
        except Exception as e:
            logging.error(f"❌ Webhook 动态发送失败: {e}\n{traceback.format_exc()}")

    return bool(alerts)


# ---------------- 评论 ----------------
def scan_new_comments(oid, header, last_read_time, seen):
    """
    轮询指定 oid（视频）的最新评论。
    返回 (new_comments_list, newest_ctime)
    """
    new_list = []
    max_ctime = last_read_time

    # 为了保持 “最近 5 分钟” 的范围不变，需要把安全阈值也往后平移
    safe_time = last_read_time - 300 - TIME_SHIFT_SECONDS

    pn = 1
    while pn <= 10:  # 最多翻 10 页
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


# ---------------- 主循环 ----------------
def start_monitoring(header):
    """
    项目入口——负责所有轮询、心跳、日志、异常捕获等。
    """
    # ------------------- 1️⃣ 启动延迟 -------------------
    if STARTUP_DELAY_SECONDS > 0:
        logging.info(f"启动延迟 {STARTUP_DELAY_SECONDS}s，等待后再开始监控...")
        time.sleep(STARTUP_DELAY_SECONDS)
    # ----------------------------------------------------

    # 初始化时间基准（均使用平移后的时间）
    last_v_check = 0
    last_hb = time.time() + TIME_SHIFT
