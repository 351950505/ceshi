# main.py
import os, sys, time, random, logging, traceback
from config import *
import database as db
import notifier
import utils

def init_logging():
    log_path = os.path.join(os.getcwd(), "bili_monitor.log")
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        encoding="utf-8",
        filemode="a"
    )
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(console)

def get_header():
    """读取永久有效的 Cookie 文件"""
    if not os.path.isfile(COOKIE_FILE):
        raise FileNotFoundError(f"{COOKIE_FILE} not found. 请手动放入有效的 B 站 Cookie")
    with open(COOKIE_FILE, "r", encoding="utf-8") as f:
        cookie = f.read().strip()
    return {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/"
    }

if __name__ == "__main__":
    init_logging()
    db.init_db()
    try:
        header = get_header()
    except Exception as e:
        logging.error(str(e))
        sys.exit(1)

    utils.update_wbi_keys(header)   # 如需 wbi 更新
    utils.start_monitoring(header)  # 业务主循环
