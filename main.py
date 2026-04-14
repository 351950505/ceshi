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
HEARTBEAT_INTERVAL = 15
STALL_WARNING_THRESHOLD = 60

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

    logging.info("=" * 70)
    logging.info("🚀 B站监控系统运行状态增强版启动")
    logging.info("=" * 70)


# ================= 网络层（增强版） =================
def safe_request(url, params, headers, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=10)

            logging.info(f"🌐 请求成功 URL={url} code={r.status_code}")

            txt = r.text.strip()
            if not txt:
                logging.warning("⚠️ 空响应")
                continue

            data = r.json()
            return data

        except requests.exceptions.Timeout:
            logging.error(f"⏰ 超时 retry={i} URL={url}")

        except requests.exceptions.ConnectionError:
            logging.error(f"🔌 连接失败 retry={i} URL={url}")

        except Exception:
            logging.error(f"❌ 请求异常 retry={i} URL={url}\n{traceback.format_exc()}")

        time.sleep(1 + i)

    logging.error(f"💥 请求彻底失败 URL={url}")
    return {"code": -500}


# ================= 动态解析（保守版） =================
def extract_dynamic_text(item):
    try:
        modules = item.get("modules") or {}
        dyn = modules.get("module_dynamic")

        if not isinstance(dyn, dict):
            return "【DEBUG】无module_dynamic"

        content = []

        desc = dyn.get("desc") or {}
        if isinstance(desc, dict):
            if desc.get("text"):
                content.append(desc["text"])

        major = dyn.get("major") or {}
        if isinstance(major, dict):
            for k in ["draw", "opus", "archive", "article"]:
                blk = major.get(k)
                if isinstance(blk, dict):

                    # draw
                    if k == "draw":
                        for it in blk.get("items") or []:
                            if isinstance(it, dict) and it.get("text"):
                                content.append(it["text"])

                    # common fallback
                    for p in ["desc", "title"]:
                        v = blk.get(p)
                        if isinstance(v, str):
                            content.append(v)

        if not content:
            content.append("【DEBUG】无文本内容")

        return "\n".join(content)

    except Exception:
        logging.error(traceback.format_exc())
        return "【DEBUG】解析异常"


# ================= 动态扫描 =================
def check_new_dynamics(header, seen):
    start_time = time.time()

    logging.info("🔁 开始动态扫描")

    new_count = 0

    for uid in EXTRA_DYNAMIC_UIDS:
        try:
            data = safe_request(
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
                {"host_mid": uid},
                header
            )

            if data.get("code") != 0:
                logging.warning(f"UID={uid} 返回异常 code={data.get('code')}")
                continue

            items = (data.get("data") or {}).get("items", [])

            logging.info(f"📦 UID={uid} items={len(items)}")

            for item in items:
                id_str = item.get("id_str")
                if not id_str:
                    continue

                if id_str in seen[uid]:
                    continue

                seen[uid].add(id_str)
                new_count += 1

                text = extract_dynamic_text(item)

                logging.info("=========== NEW DYNAMIC ===========")
                logging.info(f"UID: {uid}")
                logging.info(f"ID: {id_str}")
                logging.info(text)
                logging.info("===================================")

        except Exception:
            logging.error(traceback.format_exc())

    duration = time.time() - start_time
    logging.info(f"✅ 本轮扫描完成 新动态={new_count} 耗时={duration:.2f}s")

    return new_count


# ================= 主循环（增强可观测） =================
def start_monitoring(header):
    seen = {uid: set() for uid in EXTRA_DYNAMIC_UIDS}

    last_heartbeat = time.time()
    last_scan = time.time()
    loop_count = 0
    last_log_time = time.time()

    logging.info("🟢 监控系统进入主循环")

    while True:
        try:
            loop_count += 1
            now = time.time()

            # ================= 心跳 =================
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                logging.info(
                    f"💓 HEARTBEAT | loop={loop_count} "
                    f"uids={len(EXTRA_DYNAMIC_UIDS)} "
                    f"uptime={int(now)}"
                )
                last_heartbeat = now
                last_log_time = now

            # ================= 动态扫描 =================
            if now - last_scan >= 10:
                count = check_new_dynamics(header, seen)
                last_scan = now

                if count == 0:
                    logging.info("📭 本轮无新动态")
                else:
                    logging.info(f"🎯 新动态数量: {count}")

                last_log_time = now

            # ================= 卡死检测 =================
            if now - last_log_time > STALL_WARNING_THRESHOLD:
                logging.error("🚨 WARNING: 系统可能卡死（无日志超过60秒）")
                last_log_time = now

            time.sleep(2)

        except Exception:
            logging.error(traceback.format_exc())
            time.sleep(5)


# ================= 启动 =================
if __name__ == "__main__":
    init_logging()
    db.init_db()

    # 你原来的 header 逻辑保留（这里假设已存在）
    from your_header_module import get_header  # 如果你是单文件请删掉这一行

    h = get_header()

    logging.info("🚀 系统启动完成")
    start_monitoring(h)
