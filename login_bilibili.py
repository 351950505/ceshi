#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站扫码登录（适配 128MB 服务器 + 钉钉）
- 生成登录二维码并推送到钉钉
- 手机 B站 App 扫码确认后写入 bili_cookie.txt
- 用法: python3 login_bilibili.py
"""

import os
import sys
import time
import hashlib
import urllib.parse
import requests

# --- Bilibili API ---
QR_GENERATE_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

COOKIE_FILE = "bili_cookie.txt"
WEBHOOK_CONFIG_FILE = "webhook_config.txt"
QR_IMAGE_FILE = "qrcode.png"
POLL_INTERVAL = 2
QR_TIMEOUT = 180  # 秒，超时退出

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}


def get_webhook_url():
    if not os.path.exists(WEBHOOK_CONFIG_FILE):
        return ""
    try:
        cfg = {}
        with open(WEBHOOK_CONFIG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
        return cfg.get("system") or cfg.get("dynamic") or cfg.get("comment") or ""
    except Exception:
        return ""


def push_dingtalk(title, text):
    url = get_webhook_url()
    if not url:
        print("⚠️ 未配置 webhook_config.txt，跳过钉钉推送")
        return False
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
    try:
        r = requests.post(url, json=payload, timeout=10)
        data = r.json()
        if data.get("errcode") in (0, 310000):
            print("✅ 已推送到钉钉")
            return True
        print("钉钉返回:", data)
        return False
    except Exception as e:
        print("钉钉推送失败:", e)
        return False


def try_save_local_qrcode(login_url):
    """若环境里装了 qrcode，则额外保存 qrcode.png（可选）"""
    try:
        import qrcode
        img = qrcode.make(login_url)
        img.save(QR_IMAGE_FILE)
        print(f"已保存本地二维码: {QR_IMAGE_FILE}")
        return True
    except ImportError:
        return False
    except Exception as e:
        print("保存本地二维码失败:", e)
        return False


def generate_qrcode():
    try:
        r = requests.get(QR_GENERATE_API, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            print("获取二维码失败:", data.get("message", data))
            return None, None
        login_url = data["data"]["url"]
        qrcode_key = data["data"]["qrcode_key"]
        return login_url, qrcode_key
    except requests.RequestException as e:
        print("网络错误，无法获取二维码:", e)
        return None, None


def poll_for_login_status(qrcode_key):
    session = requests.Session()
    session.headers.update(HEADERS)
    scanned_hint = False
    print("\n等待扫码和手机确认…")
    start = time.time()

    try:
        while time.time() - start < QR_TIMEOUT:
            try:
                resp = session.get(
                    QR_POLL_API,
                    params={"qrcode_key": qrcode_key},
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as e:
                print("\n轮询网络错误:", e)
                time.sleep(POLL_INTERVAL)
                continue

            d = data.get("data") or {}
            status_code = d.get("code")
            message = d.get("message", "")

            if status_code == 0:
                print("\n✅ 登录成功！")
                return session

            if status_code == 86090:
                if not scanned_hint:
                    print("\n已扫码，请在手机上点确认登录…")
                    scanned_hint = True
            elif status_code == 86101:
                print(".", end="", flush=True)
            elif status_code == 86038:
                print("\n❌ 二维码已失效，请重新运行脚本")
                return None
            else:
                print(f"\n未知状态: code={status_code}, message={message}")
                return None

            time.sleep(POLL_INTERVAL)

        print("\n❌ 超时未登录（约 %d 秒）" % QR_TIMEOUT)
        return None

    except KeyboardInterrupt:
        print("\n用户中断")
        return None


def save_cookie_from_session(session, filename=COOKIE_FILE):
    if not session:
        return False
    cookie_dict = session.cookies.get_dict()
    if not cookie_dict or "SESSDATA" not in cookie_dict:
        print("❌ Cookie 中缺少 SESSDATA，登录可能不完整")
        print("当前字段:", list(cookie_dict.keys()))
        return False

    cookie_str = "; ".join("%s=%s" % (k, v) for k, v in cookie_dict.items())
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(cookie_str)
        print("✅ Cookie 已保存到", filename)
        return True
    except IOError as e:
        print("写入 Cookie 失败:", e)
        return False


def main():
    print("=" * 50)
    print(" Bilibili 扫码登录（钉钉推送版）")
    print("=" * 50)

    login_url, qrcode_key = generate_qrcode()
    if not qrcode_key:
        sys.exit(1)

    # 本地可选保存（有 qrcode 库才生成）
    try_save_local_qrcode(login_url)

    # 钉钉用在线二维码图（不依赖 pillow/qrcode）
    img_url = (
        "https://api.qrserver.com/v1/create-qr-code/?size=280x280&data=%s"
        % urllib.parse.quote(login_url, safe="")
    )
    md = (
        "### 🔑 B站扫码登录\n\n"
        "请在 **3 分钟内** 用手机 **哔哩哔哩 App → 扫一扫** 扫码，并点确认。\n\n"
        "![登录二维码](%s)\n\n"
        "若图片不显示，在服务器执行：`python3 login_bilibili.py` 重试。\n\n"
        "超时或错过请重新运行同一命令。"
    ) % img_url

    push_dingtalk("B站扫码登录", md)
    print("已请求钉钉推送二维码，请打开钉钉查看并扫码。")
    print("登录 URL:", login_url)

    login_session = poll_for_login_status(qrcode_key)
    if not login_session:
        push_dingtalk(
            "B站登录失败/超时",
            "### ⚠️ 扫码登录未完成\n\n请重新运行：`python3 login_bilibili.py`",
        )
        print("\n登录失败或超时。")
        sys.exit(1)

    if save_cookie_from_session(login_session):
        push_dingtalk(
            "B站登录成功",
            "### ✅ 扫码登录成功\n\nCookie 已写入服务器。\n\n"
            "请执行重启监控：`/opt/deploy.sh`\n"
            "或：\n"
            "`pkill -f 'python3.*main.py'; sleep 2; "
            "cd /opt/bilibili-comment/ceshi && "
            "nohup python3 -u main.py >> bili_monitor.log 2>&1 &`",
        )
        print("\n完成。请重启 main.py 使新 Cookie 生效。")
        sys.exit(0)

    sys.exit(1)


if __name__ == "__main__":
    main()
