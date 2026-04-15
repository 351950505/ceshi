#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
BiliBili 24h 全量监控（增量版）
核心改动：
- 使用 /feed/all/update 检测是否有新动态
- 统一、容错的 HTTP 请求层 (`http_request` 装饰器)
- 统一的动态解析 (`parse_dynamic_item`)
- 抽奖/转发/图片动态专门处理
- 增量基线保存到 SQLite，跨重启不丢失
- 统一的 webhook 报警入口 `notify()`
"""

import sys
import os
import time
import json
import random
import logging
import traceback
from typing import List, Dict, Any, Tuple, Optional

import requests

# ----------------- 项目内部模块 -----------------
import database as db
import notifier

# ====================== 配置 ======================
# 这里的常量可以直接被环境变量覆盖（例如 `export TARGET_UID=12345`），
# 这样在 Docker / CI 中无需改代码。
TARGET_UID = int(os.getenv("TARGET_UID", "1671203508"))
VIDEO_CHECK_INTERVAL = int(os.getenv("VIDEO_CHECK_INTERVAL", "21600"))   # 6h
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "600"))        # 10min

# 需要额外监控的 UP 主（转发、抽奖等）
EXTRA_DYNAMIC_UIDS = [
    int(u) for u in os.getenv(
        "EXTRA_DYNAMIC_UIDS",
        "3546905852250875,3546961271589219,3546610447419885,285340365,3706948578969654"
    ).split(",")
]

# 动态轮询策略
DYNAMIC_CHECK_INTERVAL = int(os.getenv("DYNAMIC_CHECK_INTERVAL", "30"))        # 普通轮询
DYNAMIC_BURST_INTERVAL = int(os.getenv("DYNAMIC_BURST_INTERVAL", "10"))        # 高频轮询
DYNAMIC_BURST_DURATION = int(os.getenv("DYNAMIC_BURST_DURATION", "300"))       # 高频窗口（秒）
DYNAMIC_MAX_AGE = int(os.getenv("DYNAMIC_MAX_AGE", "300"))                     # 丢弃超过该秒数的历史动态

# 日志
LOG_FILE = os.getenv("LOG_FILE", "bili_monitor.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# ==================== 日志初始化 ====================
def init_logging() -> None:
    # 只在第一次启动时清空日志
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.truncate()
    except Exception:
        pass

    logging.basicConfig(
        filename=LOG_FILE,
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        encoding="utf-8",
        filemode="w"
    )
    console = logging.StreamHandler()
    console.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(console)

    logging.info("=" * 60)
    logging.info("B站监控系统启动（24h 全量+增量混合模式）")
    logging.info("=" * 60)


# ==================== HTTP 请求层 ====================
def _retry(max_retries: int = 3, backoff: float = 2.0):
    """装饰器：对函数进行指数退避重试并记录日志"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            last_exc = None
            for i in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    wait = backoff * (2 ** i) + random.random()
                    logging.warning(
                        f"[网络] {func.__name__} 第 {i+1}/{max_retries} 次请求失败: {e}，{wait:.1f}s 后重试"
                    )
                    time.sleep(wait)
            # 所有重试都失败，抛出异常让上层捕获
            logging.error(
                f"[网络] {func.__name__} 重试全部失败: {repr(last_exc)}\n{traceback.format_exc()}"
            )
            raise last_exc
        return wrapper
    return decorator


@_retry(max_retries=3, backoff=2)
def http_get(url: str, params: Optional[Dict] = None,
             headers: Optional[Dict] = None,
             cookies: Optional[Dict] = None,
             timeout: int = 10) -> Dict:
    """安全的 GET 请求 → JSON（异常自动抛出）"""
    resp = requests.get(
        url,
        params=params,
        headers=headers,
        cookies=cookies,
        timeout=timeout
    )
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        # 某些 endpoint 可能返回非 json（如 404 页面），统一记录后抛异常
        logging.error(f"[解析] {url} 返回非 JSON：{resp.text[:200]}")
        raise


# ==================== 认证 & Header ====================
def get_header() -> Dict[str, str]:
    """
    读取本地 `bili_cookie.txt`，若不存在则尝试执行 `login_bilibili.py` 登录。
    返回可直接用于 `requests` 的 Header（含 Cookie、User-Agent、Referer）。
    """
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    except Exception:
        # 自动执行登录脚本（内部已处理验证码/二维码）
        logging.info("[认证] 未找到 Cookie，尝试运行 login_bilibili.py")
        subprocess.run([sys.executable, "login_bilibili.py"])
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()

    return {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
    }


# ==================== WBI 签名（保持原实现） ====================

WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}
mixinKeyEncTab = [
    46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,
    27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,
    37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,
    22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52
]

def getMixinKey(orig: str) -> str:
    return "".join([orig[i] for i in mixinKeyEncTab])[:32]

def encWbi(params: Dict, img_key: str, sub_key: str) -> Dict:
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

def update_wbi_keys(header: Dict) -> None:
    try:
        data = http_get(
            "https://api.bilibili.com/x/web-interface/nav",
            params=None,
            headers=header
        )
        if data.get("code") == 0:
            img = data["data"]["wbi_img"]
            WBI_KEYS["img_key"] = img["img_url"].rsplit("/", 1)[1].split(".")[0]
            WBI_KEYS["sub_key"] = img["sub_url"].rsplit("/", 1)[1].split(".")[0]
            WBI_KEYS["last_update"] = time.time()
            logging.info("WBI 密钥已更新")
    except Exception:
        logging.warning("WBI 更新失败（网络或登录失效），后续请求会使用旧 key")

def wbi_request(url: str, params: Dict, header: Dict) -> Dict:
    if not WBI_KEYS["img_key"] or time.time() - WBI_KEYS["last_update"] > 21600:
        update_wbi_keys(header)
    signed = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    return http_get(url, params=signed, headers=header)


# ==================== 数据库：基线持久化 ====================
def init_baseline_table() -> None:
    """
    在已有 `database.py` 基础上创建（if not exists）一个专门保存
    - 全局 update_baseline
    - 每个额外 UID 的最新 offset
    """
    sql = """
    CREATE TABLE IF NOT EXISTS dyn_baseline (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """
    db.exec_sql(sql)


def set_baseline(key: str, value: str) -> None:
    db.exec_sql("REPLACE INTO dyn_baseline (key, value) VALUES (?, ?);", (key, value))


def get_baseline(key: str, default: Optional[str] = None) -> Optional[str]:
    rows = db.query_sql("SELECT value FROM dyn_baseline WHERE key = ?;", (key,))
    return rows[0][0] if rows else default


# ==================== 辅助函数 ====================
def limit_text(txt: str, limit: int = 1500) -> str:
    """安全截断，防止 webhook payload 过大。"""
    if len(txt) <= limit:
        return txt
    return txt[:limit] + "\n\n...（已截断，全文请前往 B 站查看）"


def notify(title: str, items: List[Dict[str, Any]]) -> None:
    """统一调用 notifier 并捕获异常。"""
    try:
        notifier.send_webhook_notification(title, items)
        logging.info(f"✅ 通知已发送（{len(items)} 条）")
    except Exception as e:
        logging.error(f"❌ webhook 发送失败：{e}\n{traceback.format_exc()}")


# ==================== 视频监控（保持原实现） ====================
def get_latest_video(header: Dict) -> Optional[str]:
    data = http_get(
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
        params={"host_mid": TARGET_UID},
        headers=header
    )
    if data.get("code") != 0:
        return None
    for item in (data.get("data") or {}).get("items", []):
        if item.get("type") == "DYNAMIC_TYPE_AV":
            return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
    return None
