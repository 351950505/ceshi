#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站扫码登录：二维码以「公网图片链接」推到钉钉（关键词：动态）
不依赖在服务器上打开本地图片。
用法: python3 login_bilibili.py
必须用【哔哩哔哩 App → 扫一扫】扫钉钉里的图。
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
        print("⚠️ 无 webhook")
        return False
    title, text = ensure_keyword(title, text)
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
    try:
        data = requests.post(url, json=payload, timeout=10).json()
        print("钉钉 markdown 返回:", data)
        return data.get("errcode") == 0
    except Exception as e:
        print("钉钉 markdown 失败:", e)
        return False


def push_image_picurl(pic_url):
    """钉钉 image 类型：使用 picURL（不是 base64）"""
    url = get_webhook_url()
    if not url:
        return False
    payload = {"msgtype": "image", "image": {"picURL": pic_url}}
    try:
        data = requests.post(url, json=payload, timeout=10).json()
        print("钉钉 picURL 图片返回:", data)
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
            print("下载失败:", e)
    try:
        import qrcode
        qrcode.make(login_url).save(save_path)
        print("✅ qrcode 库生成", save_path)
        return True
    except Exception as e:
        print("本地生成失败:", e)
    return False


def upload_public_image(png_path):
    """把本地 png 传到临时图床，得到钉钉可访问的 https 链接"""
    uploaders = []

    # 1) litterbox 1 小时
    def _litterbox():
        with open(png_path, "rb") as f:
            r = requests.post(
                "https://litterbox.catbox.moe/resources/internals/api.php",
                data={"reqtype": "fileupload", "time": "1h"},
                files={"fileToUpload": ("qrcode.png", f, "image/png")},
                timeout=30,
            )
        t = (r.text or "").strip()
        if r.status_code == 200 and t.startswith("http"):
            return t
        return None

    # 2) catbox
    def _catbox():
        with open(png_path, "rb") as f:
            r = requests.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": ("qrcode.png", f, "image/png")},
                timeout=30,
            )
        t = (r.text or "").strip()
        if r.status_code == 200 and t.startswith("http"):
            return t
        return None

    for name, fn in (("litterbox", _litterbox), ("catbox", _catbox)):
        try:
            u = fn()
            if u:
                print("✅ 图床(%s):" % name, u)
                return u
            print("图床(%s)失败:" % name, "无有效URL")
        except Exception as e:
            print("图床(%s)异常:" % name, e)
    return None


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
    print("等待扫码（哔哩哔哩 App → 扫一扫）…")
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


def push_qr_to_dingtalk(login_url, png_path):
    """优先：图床公网链接；其次：在线 QR 服务链接。用 markdown + picURL 双推。"""
    public_url = upload_public_image(png_path)
    if not public_url:
        public_url = build_qr_urls(login_url)[0]
        print("使用在线 QR 链接:", public_url)

    # 方式1：markdown 内嵌完整图片（含关键词「动态」）
    md = (
        "### 动态项目登录\n\n"
        "请在 **3 分钟内** 用 **哔哩哔哩 App → 扫一扫** 扫描下方二维码，并点确认。\n\n"
        "![动态登录二维码](%s)\n\n"
        "不要用微信/浏览器打开链接。\n"
        "若图不显示，点此打开再扫：[%s](%s)"
    ) % (public_url, public_url, public_url)

    ok1 = push_markdown("动态项目登录", md)

    # 方式2：image.picURL（兼容你遇到的 400802 接口）
    ok2 = push_image_picurl(public_url)

    if ok1 or ok2:
        print("✅ 已向钉钉推送二维码图片链接")
        return True

    print("❌ 钉钉推送失败")
    return False


def main():
    print("=" * 50)
    print(" B站扫码登录（公网图片 → 钉钉）")
    print(" 关键词：动态 | 请用哔哩哔哩 App 扫一扫")
    print("=" * 50)

    login_url, qrcode_key = generate_qrcode()
    if not qrcode_key:
        sys.exit(1)

    if not download_qr_png(login_url, QR_IMAGE_FILE):
        print("❌ 无法生成二维码，退出")
        sys.exit(1)

    push_qr_to_dingtalk(login_url, QR_IMAGE_FILE)

    session = poll_for_login_status(qrcode_key)
    if not session or not save_cookie_from_session(session):
        push_markdown(
            "动态项目登录失败",
            "### 动态项目登录未完成\n\n请重新执行：`python3 login_bilibili.py`",
        )
        sys.exit(1)

    push_markdown(
        "动态项目登录成功",
        "### 动态项目登录成功\n\nCookie 已写入。\n\n请执行：`/opt/deploy.sh` 重启监控",
    )
    print("完成。请 /opt/deploy.sh 重启监控")


if __name__ == "__main__":
    main()
