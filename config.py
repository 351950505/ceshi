# config.py
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# 读取 .env（若不存在则回落到 .env.example）
env_path = BASE_DIR / ".env"
if not env_path.is_file():
    env_path = BASE_DIR / ".env.example"
load_dotenv(dotenv_path=env_path)

# 基础配置
TARGET_UID = int(os.getenv("TARGET_UID", "1671203508"))
EXTRA_DYNAMIC_UIDS = [int(uid) for uid in os.getenv(
    "EXTRA_DYNAMIC_UIDS",
    "3546905852250875,3546961271589219,3546610447419885,285340365,3706948578969654"
).split(",")]

# 时间间隔
VIDEO_CHECK_INTERVAL   = int(os.getenv("VIDEO_CHECK_INTERVAL", "21600"))
HEARTBEAT_INTERVAL     = int(os.getenv("HEARTBEAT_INTERVAL", "600"))
DYNAMIC_CHECK_INTERVAL = int(os.getenv("DYNAMIC_CHECK_INTERVAL", "30"))
DYNAMIC_BURST_INTERVAL = int(os.getenv("DYNAMIC_BURST_INTERVAL", "10"))
DYNAMIC_BURST_DURATION = int(os.getenv("DYNAMIC_BURST_DURATION", "300"))
DYNAMIC_MAX_AGE        = int(os.getenv("DYNAMIC_MAX_AGE", "300"))

# 其它
COOKIE_FILE = "bili_cookie.txt"
DB_FILE     = "bili_monitor.db"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
