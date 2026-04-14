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


# ================= 日志 =================
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
    logging.info("B站监控系统 DEBUG启动（仅打印模式）")
    logging.info("=" * 60)


# ================= 网络 =================
def safe_request(url, params, header, retries=3):
    h = header.copy()
    h["Connection"] = "close"

    for i in range(retries):
        try:
            r = requests.get(url, headers=h, params=params, timeout=10)
            txt = r.text.strip()

            if not txt:
                time.sleep(1)
                continue

            return r.json()

        except Exception as e:
            logging.warning(f"请求失败 retry={i}: {e}")
            time.sleep(1 + i)

    return {"code": -500}


# ================= WBI（原样保留） =================
WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}

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

            logging.info("WBI更新成功")
    except Exception as e:
        logging.error(f"WBI更新失败: {e}")


def wbi_request(url, params, header):
    if not WBI_KEYS["img_key"] or time.time() - WBI_KEYS["last_update"] > 21600:
        update_wbi_keys(header)

    signed = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    return safe_request(url, signed, header)


# ================= header =================
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
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.bilibili.com/"
    }


def is_work_time():
    return True


# ================= 视频（保留） =================
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
        except:
            pass

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


# ================= 动态 DEBUG核心 =================
def deep_find_text(obj):
    result = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if isinstance(v, str) and k in ["text", "content", "desc", "title", "words"]:
                    result.append(v)
                walk(v)
        elif isinstance(x, list):
            for i in x:
                walk(i)

    walk(obj)

    return " ".join(list(dict.fromkeys(result)))


def extract_dynamic_text(item):
    try:
        id_str = item.get("id_str", "unknown")

        logging.info("=" * 80)
        logging.info(f"🔍 FINAL解析 ID: {id_str}")

        modules = item.get("modules") or {}
        dyn = modules.get("module_dynamic") if isinstance(modules, dict) else None

        if not isinstance(dyn, dict):
            return "【DEBUG】module_dynamic缺失"

        content = []

        # =========================
        # ① 最关键：desc.text（你漏掉的核心）
        # =========================
        try:
            desc = dyn.get("desc")
            logging.info(f"desc: {desc}")

            if isinstance(desc, dict):
                text = desc.get("text")
                if text:
                    content.append(text)
                    logging.info("✅ desc.text 命中")

                # 有些版本是 orig_text
                orig = desc.get("orig_text")
                if orig:
                    content.append(orig)
                    logging.info("✅ desc.orig_text 命中")

                # 富文本
                rich = desc.get("rich_text_nodes")
                if isinstance(rich, list):
                    for n in rich:
                        if isinstance(n, dict):
                            t = n.get("text") or n.get("orig_text")
                            if t:
                                content.append(t)

        except Exception as e:
            logging.error(f"desc解析失败: {e}")

        # =========================
        # ② major（仅补充，不主依赖）
        # =========================
        try:
            major = dyn.get("major")
            logging.info(f"major.type = {major.get('type') if isinstance(major, dict) else None}")

            if isinstance(major, dict):
                for k in ["opus", "draw", "archive", "article"]:
                    if k in major:
                        blk = major.get(k)
                        logging.info(f"hit major.{k}")

                        if isinstance(blk, dict):
                            # draw特别处理
                            if k == "draw":
                                items = blk.get("items") or []
                                for it in items:
                                    if isinstance(it, dict):
                                        t = it.get("text")
                                        if t:
                                            content.append(t)

                            # 通用兜底
                            for path in [("desc", "text"), ("title",)]:
                                cur = blk
                                for p in path:
                                    if isinstance(cur, dict):
                                        cur = cur.get(p)
                                    else:
                                        cur = None

                                if isinstance(cur, str):
                                    content.append(cur)

        except Exception as e:
            logging.error(f"major解析失败: {e}")

        # =========================
        # ③ 最强兜底：全树扫描
        # =========================
        if not content:
            logging.warning("⚠️ 进入 deep_find_text 全兜底")
            try:
                content.append(deep_find_text(dyn))
            except:
                pass

        # =========================
        # ④ 最终兜底
        # =========================
        if not content:
            return "【DEBUG】完全解析失败"

        final_text = "\n".join(list(dict.fromkeys(content)))

        logging.info(f"✅ FINAL结果: {final_text}")

        return final_text

    except Exception as e:
        logging.error(traceback.format_exc())
        return "【DEBUG】异常"

# ================= 动态扫描（不推送版） =================
def check_new_dynamics(header, seen):
    logging.info("🔁 动态扫描（DEBUG无推送）")

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
                if not id_str:
                    continue

                if id_str in seen[uid]:
                    continue

                seen[uid].add(id_str)

                logging.info(f"🆕 新动态 UID={uid} ID={id_str}")

                text = extract_dynamic_text(item)

                logging.info("======== 动态 BEGIN ========")
                logging.info(text)
                logging.info("======== 动态 END ========")

        except Exception as e:
            logging.error(traceback.format_exc())

        time.sleep(1)

    return False


# ================= 主循环 =================
def start_monitoring(header):
    seen = {uid: set() for uid in EXTRA_DYNAMIC_UIDS}

    logging.info("DEBUG监控启动")

    while True:
        try:
            if is_work_time():
                check_new_dynamics(header, seen)
                time.sleep(10)
            else:
                time.sleep(30)

        except Exception:
            logging.error(traceback.format_exc())
            time.sleep(60)


# ================= main =================
if __name__ == "__main__":
    init_logging()
    db.init_db()

    h = get_header()
    update_wbi_keys(h)

    start_monitoring(h)
