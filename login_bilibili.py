#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站扫码登录
- 钉钉推送二维码（关键词：动态）
- 只写入: DedeUserID; DedeUserID__ckMd5; SESSDATA
用法: python3 login_bilibili.py
"""

import os
import sys
import time
import urllib.parse
import requests

QR_GENERATE_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

COOKIE_FILE = "bili_cookie.txt"
WEBHOOK_CONFIG_FILE = "webhook_config.txt"
QR_IMAGE_FILE = "qrcode.png"
POLL_INTERVAL = 2
QR_TIMEOUT = 180

NEEDED_COOKIE_KEYS = ("DedeUserID", "DedeUserID__ckMd5", "SESSDATA")

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


def ensure_keyword(title, text):
    if "动态" not in title:
        title = "动态" + title
    if "动态" not in text:
        text = "动态\n\n" + text
    return title, text


def push_markdown(title, text):
    url = get_webhook_url()
    if not url:
        print("⚠️ 无 webhook，跳过钉钉")
        return False
    title, text = ensure_keyword(title, text)
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
    try:
        data = requests.post(url, json=payload, timeout=10).json()
        print("钉钉 markdown 返回:", data)
        return data.get("errcode") == 0
    except Exception as e:
        print("钉钉失败:", e)
        return False


def push_image_picurl(pic_url):
    url = get_webhook_url()
    if not url:
        return False
    payload = {"msgtype": "image", "image": {"picURL": pic_url}}
    try:
        data = requests.post(url, json=payload, timeout=10).json()
        print("钉钉 picURL 返回:", data)
        return data.get("errcode") == 0
    except Exception as e:
        print("钉钉 picURL 失败:", e)
        return False


def build_qr_urls(login_url):
    encoded = urllib.parse.quote(login_url, safe="")
    return [
        "https://quickchart.io/qr?size=300&margin=2&text=" + encoded,
        "https://api.qrserver.com/v1/create-qr-code/?size=300x300&margin=10&data=" + encoded,
    ]


def download_qr_png(login_url, save_path):
    for api in build_qr_urls(login_url):
        try:
            r = requests.get(api, timeout=15)
            if r.status_code == 200 and len(r.content) > 100:
                with open(save_path, "wb") as f:
                    f.write(r.content)
                print("✅ 已保存", save_path, "大小", len(r.content))
                return True
        except Exception as e:
            print("下载二维码失败:", e)
    try:
        import qrcode
        qrcode.make(login_url).save(save_path)
        print("✅ qrcode 库生成", save_path)
        return True
    except Exception as e:
        print("本地生成失败:", e)
    return False


def upload_public_image(png_path):
    def _litterbox():
        with open(png_path, "rb") as f:
            r = requests.post(
                "https://litterbox.catbox.moe/resources/internals/api.php",
                data={"reqtype": "fileupload", "time": "1h"},
                files={"fileToUpload": ("qrcode.png", f, "image/png")},
                timeout=30,
            )
        t = (r.text or "").strip()
        return t if r.status_code == 200 and t.startswith("http") else None

    def _catbox():
        with open(png_path, "rb") as f:
            r = requests.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": ("qrcode.png", f, "image/png")},
                timeout=30,
            )
        t = (r.text or "").strip()
        return t if r.status_code == 200 and t.startswith("http") else None

    for name, fn in (("litterbox", _litterbox), ("catbox", _catbox)):
        try:
            u = fn()
            if u:
                print("✅ 图床(%s):" % name, u)
                return u
        except Exception as e:
            print("图床(%s)异常:" % name, e)
    return None


def push_qr_to_dingtalk(login_url, png_path):
    public_url = upload_public_image(png_path) or build_qr_urls(login_url)[0]
    print("二维码图链:", public_url)
    md = (
        "### 动态项目登录\n\n"
        "请用 **哔哩哔哩 App → 扫一扫** 扫下方二维码（3 分钟内）。\n\n"
        "![动态登录二维码](%s)\n\n"
        "不要用微信/浏览器打开链接。\n"
        "[点此打开图片](%s)"
    ) % (public_url, public_url)
    ok1 = push_markdown("动态项目登录", md)
    ok2 = push_image_picurl(public_url)
    return ok1 or ok2


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


def collect_cookies_after_login(session, poll_resp):
    cookie_map = {}
    try:
        cookie_map.update(session.cookies.get_dict())
    except Exception:
        pass
    try:
        for c in poll_resp.cookies:
            cookie_map[c.name] = c.value
    except Exception:
        pass

    try:
        data = poll_resp.json()
        jump = (data.get("data") or {}).get("url") or ""
        if jump.startswith("http"):
            print("跟随登录跳转:", jump[:100], "...")
            session.get(jump, timeout=12, allow_redirects=True)
            cookie_map.update(session.cookies.get_dict())
        session.get("https://www.bilibili.com/", timeout=10)
        cookie_map.update(session.cookies.get_dict())
        session.get("https://api.bilibili.com/x/web-interface/nav", timeout=10)
        cookie_map.update(session.cookies.get_dict())
    except Exception as e:
        print("补全 Cookie 过程异常:", e)

    return cookie_map


def save_cookie_map(cookie_map, filename=COOKIE_FILE):
    """只写 DedeUserID; DedeUserID__ckMd5; SESSDATA"""
    picked = {}
    for k in NEEDED_COOKIE_KEYS:
        v = cookie_map.get(k)
        if v is not None and str(v).strip() != "":
            picked[k] = str(v).strip()

    missing = [k for k in NEEDED_COOKIE_KEYS if k not in picked]
    if missing:
        print("❌ 缺少字段:", ", ".join(missing))
        print("当前拿到的键:", list(cookie_map.keys()))
        return False

    cookie_str = "; ".join("%s=%s" % (k, picked[k]) for k in NEEDED_COOKIE_KEYS)
    with open(filename, "w", encoding="utf-8") as f:
        f.write(cookie_str)

    print("✅ 已写入", filename)
    print(cookie_str[:80] + ("..." if len(cookie_str) > 80 else ""))
    return True


def poll_and_save(qrcode_key):
    session = requests.Session()
    session.headers.update(HEADERS)
    scanned = False
    print("等待扫码…")
    start = time.time()

    while time.time() - start < QR_TIMEOUT:
        try:
            resp = session.get(
                QR_POLL_API, params={"qrcode_key": qrcode_key}, timeout=10
            )
            data = resp.json()
        except Exception as e:
            print("轮询错误:", e)
            time.sleep(POLL_INTERVAL)
            continue

        d = data.get("data") or {}
        code = d.get("code")

        if code == 0:
            print("\n✅ 登录成功，提取 3 个 Cookie 字段…")
            cookie_map = collect_cookies_after_login(session, resp)
            return save_cookie_map(cookie_map)

        if code == 86090:
            if not scanned:
                print("\n已扫码，请在手机确认…")
                scanned = True
        elif code == 86101:
            print(".", end="", flush=True)
        elif code == 86038:
            print("\n二维码过期")
            return False
        else:
            print("\n状态:", code, d.get("message"))
            return False
        time.sleep(POLL_INTERVAL)

    print("\n超时")
    return False


def main():
    print("=" * 50)
    print(" B站扫码登录（仅 DedeUserID / DedeUserID__ckMd5 / SESSDATA）")
    print("=" * 50)

    login_url, qrcode_key = generate_qrcode()
    if not qrcode_key:
        sys.exit(1)

    if not download_qr_png(login_url, QR_IMAGE_FILE):
        print("❌ 无法生成二维码")
        sys.exit(1)

    push_qr_to_dingtalk(login_url, QR_IMAGE_FILE)

    if not poll_and_save(qrcode_key):
        push_markdown(
            "动态项目登录失败",
            "### 动态项目登录未完成\n\n请重新执行：`python3 login_bilibili.py`",
        )
        sys.exit(1)

    push_markdown(
        "动态项目登录成功",
        "### 动态项目登录成功\n\n已写入 DedeUserID / DedeUserID__ckMd5 / SESSDATA\n\n"
        "请执行：`/opt/deploy.sh`",
    )
    print("完成。请 /opt/deploy.sh 重启监控")


if __name__ == "__main__":
    main()
