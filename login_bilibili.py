#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站扫码登录：本地保存 qrcode.png + 钉钉推送（关键词：动态）
用法: python3 login_bilibili.py
必须用【哔哩哔哩 App → 扫一扫】扫图片，不要用浏览器打开登录链接。
"""

import os
import sys
import time
import hashlib
import base64
import urllib.parse
import requests

QR_GENERATE_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

COOKIE_FILE = "bili_cookie.txt"
WEBHOOK_CONFIG_FILE = "webhook_config.txt"
QR_IMAGE_FILE = "qrcode.png"
POLL_INTERVAL = 2
QR_TIMEOUT = 180

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
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


def push_dingtalk_text(title, text):
    url = get_webhook_url()
    if not url:
        print("⚠️ 无 webhook，跳过文字推送")
        return False
    # 钉钉关键词「动态」：标题和正文都带上，避免被拦截
    if "动态" not in title:
        title = "动态" + title
    if "动态" not in text:
        text = "动态\n\n" + text
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
    try:
        data = requests.post(url, json=payload, timeout=10).json()
        print("钉钉文字返回:", data)
        return data.get("errcode") == 0
    except Exception as e:
        print("钉钉文字失败:", e)
        return False


def push_dingtalk_image(png_path):
    """钉钉自定义机器人图片消息：base64 + md5"""
    url = get_webhook_url()
    if not url:
        print("⚠️ 无 webhook，跳过图片推送")
        return False
    with open(png_path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode("ascii")
    md5 = hashlib.md5(raw).hexdigest()
    payload = {"msgtype": "image", "image": {"base64": b64, "md5": md5}}
    try:
        data = requests.post(url, json=payload, timeout=15).json()
        print("钉钉图片返回:", data)
        if data.get("errcode") == 0:
            print("✅ 二维码图片已发到钉钉")
            return True
        print("❌ 图片推送失败，请用本地 qrcode.png 扫码")
        return False
    except Exception as e:
        print("钉钉图片异常:", e)
        return False


def download_qr_png(login_url, save_path):
    """从多个公共接口下载二维码图，不依赖 qrcode/pillow"""
    encoded = urllib.parse.quote(login_url, safe="")
    candidates = [
        "https://api.qrserver.com/v1/create-qr-code/?size=300x300&margin=10&data=" + encoded,
        "https://quickchart.io/qr?size=300&text=" + encoded,
    ]
    for api in candidates:
        try:
            r = requests.get(api, timeout=15)
            ctype = r.headers.get("content-type", "")
            ok = r.status_code == 200 and (
                "image" in ctype or r.content[:8].find(b"PNG") != -1 or r.content[:4] == b"\x89PNG"
            )
            if ok:
                with open(save_path, "wb") as f:
                    f.write(r.content)
                if os.path.getsize(save_path) > 100:
                    print("✅ 已保存", save_path, "大小", os.path.getsize(save_path))
                    return True
        except Exception as e:
            print("下载二维码失败:", api[:50], e)
    try:
        import qrcode
        img = qrcode.make(login_url)
        img.save(save_path)
        print("✅ 用 qrcode 库生成", save_path)
        return True
    except Exception as e:
        print("本地生成失败:", e)
    return False


def generate_qrcode():
    try:
        r = requests.get(QR_GENERATE_API, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            print("获取二维码失败:", data)
            return None, None
        return data["data"]["url"], data["data"]["qrcode_key"]
    except Exception as e:
        print("网络错误:", e)
        return None, None


def poll_for_login_status(qrcode_key):
    session = requests.Session()
    session.headers.update(HEADERS)
    scanned = False
    print("等待扫码（请用【哔哩哔哩App-扫一扫】扫 qrcode.png 或钉钉里的图）…")
    start = time.time()
    while time.time() - start < QR_TIMEOUT:
        try:
            resp = session.get(QR_POLL_API, params={"qrcode_key": qrcode_key}, timeout=10)
            data = resp.json()
        except Exception as e:
            print("轮询错误:", e)
            time.sleep(POLL_INTERVAL)
            continue

        d = data.get("data") or {}
        code = d.get("code")
        if code == 0:
            print("\n✅ 登录成功")
            return session
        if code == 86090:
            if not scanned:
                print("\n已扫码，请在手机上点确认…")
                scanned = True
        elif code == 86101:
            print(".", end="", flush=True)
        elif code == 86038:
            print("\n二维码过期")
            return None
        else:
            print("\n状态:", code, d.get("message"))
            return None
        time.sleep(POLL_INTERVAL)
    print("\n超时")
    return None


def save_cookie_from_session(session, filename=COOKIE_FILE):
    if not session:
        return False
    cookie_dict = session.cookies.get_dict()
    if "SESSDATA" not in cookie_dict:
        print("缺少 SESSDATA:", list(cookie_dict.keys()))
        return False
    cookie_str = "; ".join("%s=%s" % (k, v) for k, v in cookie_dict.items())
    with open(filename, "w", encoding="utf-8") as f:
        f.write(cookie_str)
    print("✅ 已写入", filename)
    return True


def main():
    print("=" * 50)
    print(" B站扫码登录（钉钉关键词：动态）")
    print(" 必须用哔哩哔哩 App 扫一扫，不要用浏览器打开链接")
    print("=" * 50)

    login_url, qrcode_key = generate_qrcode()
    if not qrcode_key:
        sys.exit(1)

    if not download_qr_png(login_url, QR_IMAGE_FILE):
        print("❌ 无法生成二维码图片，退出")
        sys.exit(1)

    # 标题和正文都含「动态」，满足钉钉关键词
    push_dingtalk_text(
        "动态项目登录",
        "### 动态项目登录\n\n"
        "🔑 请扫码更新 Cookie\n\n"
        "1. 看下一条**图片消息**里的二维码\n"
        "2. 打开 **哔哩哔哩 App → 扫一扫**（不要用微信/浏览器）\n"
        "3. 扫码后在手机上点确认\n\n"
        "若没有图片：下载服务器 `qrcode.png` 用 B站扫\n"
        "路径：`/opt/bilibili-comment/ceshi/qrcode.png`",
    )
    push_dingtalk_image(QR_IMAGE_FILE)

    print("本地二维码文件:", os.path.abspath(QR_IMAGE_FILE))
    print("若钉钉没图，请把该文件拷到手机后用 B站 App 扫")

    session = poll_for_login_status(qrcode_key)
    if not session or not save_cookie_from_session(session):
        push_dingtalk_text(
            "动态项目登录失败",
            "### 动态项目登录未完成\n\n请重新执行：`python3 login_bilibili.py`",
        )
        sys.exit(1)

    push_dingtalk_text(
        "动态项目登录成功",
        "### 动态项目登录成功\n\nCookie 已写入服务器。\n\n请执行：`/opt/deploy.sh` 重启监控",
    )
    print("完成。请 /opt/deploy.sh 重启监控")


if __name__ == "__main__":
    main()
