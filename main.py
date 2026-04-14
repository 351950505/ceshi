import sys
import os
import time
import subprocess
import random
import logging
import traceback
import json
import requests

import database as db
import notifier


# ================= 配置 =================
EXTRA_DYNAMIC_UIDS = [
    3546905852250875,
    3546961271589219,
    3546610447419885,
    285340365,
    3706948578969654
]

LOG_FILE = "bili_monitor.log"


# ================= 日志 =================
def init_logging():
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        filemode="w"
    )

    logging.info("=" * 80)
    logging.info("🚀 B站监控 DEBUG最终稳定版启动")
    logging.info("=" * 80)


# ================= 网络（强可观测版） =================
def safe_request(url, params, headers):
    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)

        logging.info(f"🌐 请求 URL={r.url}")
        logging.info(f"🌐 HTTP状态={r.status_code}")

        if not r.text:
            logging.warning("⚠️ 空响应")
            return {"code": -1, "error": "empty response"}

        try:
            return r.json()
        except Exception:
            logging.error("❌ JSON解析失败，返回原文")
            logging.info(r.text[:1000])
            return {"code": -2, "raw": r.text}

    except Exception:
        logging.error(f"❌ 请求失败\n{traceback.format_exc()}")
        return {"code": -500}


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


# ================= 🔥 核心：不再“猜结构”，只做安全提取 =================
def extract_dynamic_text(item):
    try:
        logging.info("📦 开始解析单条动态")

        # 永远先打印结构（关键）
        try:
            logging.info("📦 RAW ITEM:")
            logging.info(json.dumps(item, ensure_ascii=False, indent=2)[:3000])
        except:
            logging.info(str(item)[:2000])

        modules = item.get("modules")

        if not modules:
            return "【无modules字段】"

        dyn = modules.get("module_dynamic")

        if not isinstance(dyn, dict):
            return "【无module_dynamic】"

        result = []

        # ================= desc =================
        desc = dyn.get("desc")
        if isinstance(desc, dict):
            t = desc.get("text")
            if t:
                result.append(t)

        # ================= major =================
        major = dyn.get("major") or {}

        if isinstance(major, dict):
            for k, v in major.items():

                if not isinstance(v, dict):
                    continue

                # draw
                if k == "draw":
                    items = v.get("items") or []
                    for it in items:
                        if isinstance(it, dict):
                            txt = it.get("text")
                            if txt:
                                result.append(txt)

                # opus / article / archive
                for field in ["title", "desc", "content"]:
                    if field in v and isinstance(v[field], str):
                        result.append(v[field])

        # ================= 最终兜底 =================
        if not result:
            logging.warning("⚠️ 解析失败，返回raw结构")
            return f"【RAW动态】{str(dyn)[:1000]}"

        return "\n".join(result)

    except Exception:
        logging.error(traceback.format_exc())
        return "【解析异常】"


# ================= 动态扫描（保证永远有输出） =================
def check_new_dynamics(header, seen):
    logging.info("🔁 开始扫描动态")

    total_new = 0

    for uid in EXTRA_DYNAMIC_UIDS:
        logging.info(f"➡️ 请求 UID={uid}")

        data = safe_request(
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
            {"host_mid": uid},
            header
        )

        logging.info(f"UID={uid} code={data.get('code')}")

        items = (data.get("data") or {}).get("items") or []

        logging.info(f"UID={uid} items={len(items)}")

        for item in items:
            id_str = item.get("id_str")

            if not id_str:
                continue

            if id_str in seen[uid]:
                continue

            seen[uid].add(id_str)
            total_new += 1

            logging.info("====================================")
            logging.info(f"🆕 NEW UID={uid} ID={id_str}")

            text = extract_dynamic_text(item)

            logging.info("📢 FINAL OUTPUT:")
            logging.info(text)
            logging.info("====================================")

    logging.info(f"✅ 本轮扫描结束 new={total_new}")

    return total_new


# ================= 主循环 =================
def start_monitoring(header):
    seen = {uid: set() for uid in EXTRA_DYNAMIC_UIDS}

    logging.info("🟢 系统进入主循环")

    loop = 0

    while True:
        try:
            loop += 1

            logging.info(f"💓 LOOP {loop} alive")

            check_new_dynamics(header, seen)

            time.sleep(8)

        except Exception:
            logging.error(traceback.format_exc())
            time.sleep(5)


# ================= main =================
if __name__ == "__main__":
    init_logging()
    db.init_db()

    h = get_header()

    logging.info("🚀 启动完成")

    start_monitoring(h)
