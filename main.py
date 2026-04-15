#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
B站监控系统（极简‑仅主评论版）

核心改造：
1️⃣ 废除历史动态初始化（不再请求所有 UP 主的历史动态）。
2️⃣ `check_new_dynamics` 只拉取最近 5 分钟内的最新动态，超时直接丢弃。
3️⃣ 评论抓取仅保留根评论（主评论），彻底抛弃楼中楼（子回复），降低请求频次。
4️⃣ 在遍历 `EXTRA_DYNAMIC_UIDS` 时加入 5~10 秒随机间隔，模拟人类浏览，规避 -352 风控。
"""

import sys
import time
import datetime
import subprocess
import random
import logging
import traceback
import requests
import hashlib
import urllib.parse
import database as db
import notifier

# ------------------- 配置 -------------------
TARGET_UID = 1671203508                # 主UP 主（视频监控）
EXTRA_DYNAMIC_UIDS = [                 # 需要监控动态的额外 UP 主 UID 列表
    12345678,
    87654321,
    # ... 其它 UID
]

VIDEO_CHECK_INTERVAL = 21600          # 6 小时检查一次新视频
HEARTBEAT_INTERVAL    = 600           # 10 分钟一次心跳
DYNAMIC_TIME_WINDOW   = 300           # 5 分钟（秒）

logging.basicConfig(
    filename='bili_monitor.log',
    level=logging.INFO,
    format='%(asctime)s[%(levelname)s] %(message)s',
    encoding='utf-8',
    filemode='a'
)

# ------------------- WBI 签名模块 -------------------
WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}

mixinKeyEncTab = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52
]

def getMixinKey(orig: str) -> str:
    return "".join([orig[i] for i in mixinKeyEncTab])[:32]

def encWbi(params: dict, img_key: str, sub_key: str) -> dict:
    mixin_key = getMixinKey(img_key + sub_key)
    curr_time = round(time.time())
    params['wts'] = curr_time
    params = dict(sorted(params.items()))
    filtered_params = {}
    for k, v in params.items():
        v_str = str(v)
        for ch in "!'()*":
            v_str = v_str.replace(ch, '')
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
        logging.info("WBI 密钥已更新")
    except Exception as e:
        logging.error("获取 WBI 密钥失败: %s", e)

def wbi_request(url, params, header):
    if time.time() - WBI_KEYS["last_update"] > 21600 or not WBI_KEYS["img_key"]:
        update_wbi_keys(header)
        time.sleep(1)

    signed_params = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    r = requests.get(url, headers=header, params=signed_params, timeout=10)
    data = r.json()

    if data.get("code") == -400:
        logging.warning("触发 -400 风控，重新获取 WBI 并重试")
        update_wbi_keys(header)
        signed_params = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
        r = requests.get(url, headers=header, params=signed_params, timeout=10)
        data = r.json()
    return data

def get_header():
    """读取本地 cookie，若不存在则尝试执行扫码登录脚本"""
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    except FileNotFoundError:
        logging.warning("Cookie 文件不存在，启动登录脚本")
        subprocess.run([sys.executable, "login_bilibili.py"])
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    return {"Cookie": cookie, "User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com"}

def is_work_time() -> bool:
    """仅在工作日 9:00~19:00（UTC+8）内开启主动抓取，降低风控概率"""
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
    return now.weekday() < 5 and 9 <= now.hour < 19

def send_exception_notification(msg: str):
    try:
        notifier.send_webhook_notification("程序异常", [{"user": "系统", "message": msg}])
    except Exception:
        pass

# ------------------- 视频监控（原有逻辑） -------------------
def get_video_info(bv, header):
    url = f"https://api.bilibili.com/x/web-interface/view?bvid={bv}"
    try:
        r = requests.get(url, headers=header, timeout=10)
        data = r.json()
        if data["code"] == 0:
            return str(data["data"]["aid"]), data["data"]["title"]
    except Exception:
        pass
    return None, None

def get_latest_video(header):
    url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    params = {"host_mid": TARGET_UID}
    try:
        r = requests.get(url, headers=header, params=params, timeout=10)
        data = r.json()
        if data["code"] != 0:
            return None
        for item in data.get("data", {}).get("items", []):
            if item.get("type") == "DYNAMIC_TYPE_AV":
                return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
    except Exception:
        pass
    return None

def sync_latest_video(header):
    """获取最新上传视频并写入 DB（最多 5 次重试）"""
    for attempt in range(5):
        bv = get_latest_video(header)
        if bv:
            videos = db.get_monitored_videos()
            if videos and videos[0][1] == bv:
                return videos[0][0], videos[0][2]

            oid, title = get_video_info(bv, header)
            if oid:
                db.clear_videos()
                db.add_video_to_db(oid, bv, title)
                logging.info("监控新视频: %s", title)
                return oid, title

        logging.warning("获取最新视频失败（第 %d 次）", attempt + 1)
        time.sleep(10)

    logging.error("连续 5 次获取最新视频均失败")
    return None, None

# ------------------- 主评论抓取（已去掉子回复） -------------------
def scan_new_comments(oid, header, last_read_time, seen):
    """
    只抓取根评论（主评论），不再处理楼中楼。
    - `seen` 为 set，记录本运行期间已推送的 rpid_str，防止重复。
    - 仅保留发布时间在最近 5 分钟内的评论。
    """
    new_comments = []
    max_ctime = last_read_time
    safe_boundary = last_read_time - DYNAMIC_TIME_WINDOW   # 5 分钟阈值

    pn = 1
    while pn <= 10:                     # 10 页已足够覆盖 5 分钟窗口
        params = {"oid": oid, "type": 1, "sort": 0, "pn": pn, "ps": 20}
        try:
            data = wbi_request("https://api.bilibili.com/x/v2/reply", params, header)
            replies = data.get("data", {}).get("replies") or []

            if not replies:
                break

            page_all_older = True

            for r in replies:
                rpid = r["rpid_str"]
                ctime = r["ctime"]
                max_ctime = max(max_ctime, ctime)

                # ------------------- 只保留 5 分钟内的根评论 -------------------
                if ctime > safe_boundary and rpid not in seen:
                    seen.add(rpid)
                    new_comments.append({
                        "user": r["member"]["uname"],
                        "message": r["content"]["message"],
                        "is_reply": False,   # 这里始终是 False
                        "ctime": ctime
                    })
                    page_all_older = False

            if page_all_older:
                # 本页已全部是旧评论，后面的页肯定更旧，直接终止翻页
                break

            pn += 1
            time.sleep(random.uniform(0.5, 1.0))

        except Exception as e:
            logging.error("评论分页异常: %s", e)
            break

    return new_comments, max_ctime

# ------------------- 动态监控（保持原有） -------------------
def fetch_dynamic(uid: int, header: dict):
    """单个 UID 拉取最新动态（只返回第一条最新项目）"""
    url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    params = {"host_mid": uid, "timezone_offset": -480}
    try:
        data = wbi_request(url, params, header)
        if data.get("code") != 0:
            return None
        items = data.get("data", {}).get("items", [])
        return items[0] if items else None
    except Exception as e:
        logging.error("获取 UID %d 动态异常: %s", uid, e)
        return None

def parse_dynamic(item):
    """统一抽取动态关键字段"""
    dyn_id = item.get("desc", {}).get("dynamic_id_str")
    pub_ts = item.get("desc", {}).get("timestamp")
    major = item.get("modules", {}).get("module_dynamic", {}).get("major", {})

    # 文字内容抽取（兼容常见类型）
    content = ""
    if "opus" in major:                         # 图文动态
        for node in major["opus"]["summary"]["rich_text_nodes"]:
            if node.get("type") == "WORD":
                content += node.get("text", "")
    elif "archive" in major:                    # 视频动态
        content = major["archive"]["title"]
    elif "draw" in major:
        content = "[图片动态]"
    else:
        content = "[未知类型动态]"

    return {
        "dyn_id": dyn_id,
        "pub_ts": pub_ts,
        "content": content,
        "type": major.get("type", "UNKNOWN")
    }

def check_new_dynamics(header, seen_dynamics):
    """
    检查 EXTRA_DYNAMIC_UIDS 中的最新动态，只保留最近 5 分钟内的条目。
    - 若动态已在 `seen_dynamics` 中出现则跳过（本运行期间防重）。
    - 每次请求后随机休眠 5~10 秒，模拟真实用户行为。
    """
    new_dyns = []
    now_ts = int(time.time())

    for uid in EXTRA_DYNAMIC_UIDS:
        raw = fetch_dynamic(uid, header)
        if not raw:
            time.sleep(random.uniform(5, 10))
            continue

        parsed = parse_dynamic(raw)
        dyn_id = parsed["dyn_id"]
        pub_ts = parsed["pub_ts"]

        # 时间窗口过滤
        if now_ts - pub_ts > DYNAMIC_TIME_WINDOW:
            logging.debug("动态 %s 超出 5 分钟窗口，已忽略", dyn_id)
        else:
            if dyn_id not in seen_dynamics:
                seen_dynamics.add(dyn_id)
                new_dyns.append({
                    "uid": uid,
                    "dyn_id": dyn_id,
                    "pub_ts": pub_ts,
                    "content": parsed["content"],
                    "type": parsed["type"]
                })

        # 【关键】保持人与人之间的间隔，降低突发请求
        time.sleep(random.uniform(5, 10))

    return new_dyns

# ------------------- 主循环 -------------------
def start_monitoring(header):
    last_video_check = time.time()
    last_heartbeat   = time.time()

    oid, title = sync_latest_video(header)
    if not oid:
        send_exception_notification("启动时获取最新视频失败，请检查 Cookie / UP 主状态")
        logging.error("启动时获取最新视频失败")
        oid, title = None, "待获取视频"

    # 主评论去重集合（仅保留本次运行期间出现的 rpid）
    seen_comments = set()
    # 动态去重集合（仅保留本次运行期间出现的 dyn_id）
    seen_dynamics = set()

    logging.info("程序启动成功，监控视频：%s", title or "待获取")

    while True:
        try:
            now = time.time()

            # ------------------- 心跳 -------------------
            if is_work_time() and now - last_heartbeat >= HEARTBEAT_INTERVAL:
                now_str = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
                notifier.send_webhook_notification(
                    "监控心跳",
                    [{"user": "系统", "message": f"程序运行正常\n时间: {now_str.strftime('%Y-%m-%d %H:%M:%S')}\n监控视频: {title or '待获取'}"}]
                )
                last_heartbeat = now
                logging.info("已发送心跳")

            # ------------------- 评论轮询 -------------------
            if is_work_time() and oid:
                new_comments, latest_ctime = scan_new_comments(oid, header, int(time.time()), seen_comments)

                if new_comments:
                    # 按时间顺序排序后一次性推送
                    new_comments.sort(key=lambda x: x["ctime"])
                    logging.info("发现 %d 条新主评论", len(new_comments))
                    for c in new_comments:
                        logging.info("%s : %s", c["user"], c["message"])
                    try:
                        notifier.send_webhook_notification(title, new_comments)
                    except Exception as e:
                        logging.error("Webhook 推送异常: %s", e)

                # 适度休眠（5~10 秒），模拟正常浏览
                time.sleep(random.uniform(5, 10))
            else:
                # 工作时间外或未获取视频时，延长睡眠以减压
                time.sleep(30)

            # ------------------- 动态检查 -------------------
            # 动态检查与评论轮询是并行的，使用相同的间隔即可
            new_dyns = check_new_dynamics(header, seen_dynamics)
            if new_dyns:
                logging.info("检测到 %d 条新动态", len(new_dyns))
                for d in new_dyns:
                    logging.info(
                        "UID %d 动态 %s（%s）: %s",
                        d["uid"], d["dyn_id"], d["type"], d["content"]
                    )
                try:
                    # 这里使用统一的 “动态推送” 标题
                    notifier.send_webhook_notification("B站动态更新", new_dyns)
                except Exception as e:
                    logging.error("Webhook 推送动态异常: %s", e)

            # ------------------- 视频切换检测 -------------------
            if time.time() - last_video_check > VIDEO_CHECK_INTERVAL:
                new_oid, new_title = sync_latest_video(header)
                if new_oid and
