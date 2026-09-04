#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站扫码登录（优化版）
- 钉钉推送在线二维码
- 只写入必要 Cookie 字段
- 扫码成功后迅速保存，减少不必要请求
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


def build_qr_url(login_url):
    encoded = urllib.parse.quote(login_url, safe="")
    return "https://quickchart.io/qr?size=300&margin=2&text=" + encoded


def push_qr_to_dingtalk(login_url):
    public_url = build_qr_url(login_url)
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
    if ok1 or ok2:
        print("✅ 已向钉钉推送二维码")
        return True
    print("❌ 钉钉推送失败")
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


def merge_cookies(cookie_map, session, resp=None):
    """合并响应和会话中的 Cookie"""
    try:
        cookie_map.update(session.cookies.get_dict())
    except Exception:
        pass
    if resp is not None:
        try:
            for c in resp.cookies:
                cookie_map[c.name] = c.value
        except Exception:
            pass
    return cookie_map


def has_needed_cookies(cookie_map):
    """检查必要字段是否齐全"""
    return all(k in cookie_map and str(cookie_map[k]).strip() for k in NEEDED_COOKIE_KEYS)


def save_cookie_map(cookie_map, filename=COOKIE_FILE):
    """保存 Cookie 到文件"""
    picked = {}
    for k in NEEDED_COOKIE_KEYS:
        v = cookie_map.get(k)
        if v is not None and str(v).strip() != "":
            picked[k] = str(v).strip()

    missing = [k for k in NEEDED_COOKIE_KEYS if k not in picked]
    if missing:
        print("❌ 缺少字段:", ", ".join(missing))
        print("当前可用键:", list(cookie_map.keys()))
        return False

    cookie_str = "; ".join("%s=%s" % (k, picked[k]) for k in NEEDED_COOKIE_KEYS)
    with open(filename, "w", encoding="utf-8") as f:
        f.write(cookie_str)
    print("✅ 已写入", os.path.abspath(filename))
    print(cookie_str[:100] + ("..." if len(cookie_str) > 100 else ""))
    return True


def quick_fetch_jump_cookies(session, poll_resp):
    """从扫码成功响应中提取 Cookie，若必要字段缺失则访问跳转 URL 补充"""
    cookie_map = {}
    merge_cookies(cookie_map, session, poll_resp)
    print("轮询响应后 Cookie 键:", list(cookie_map.keys()))

    # 如果必要字段已齐全，直接返回
    if has_needed_cookies(cookie_map):
        print("✅ 必要 Cookie 已从轮询响应中获取")
        return cookie_map

    # 尝试从 data.url 跳转获取
    try:
        data = poll_resp.json()
        jump = (data.get("data") or {}).get("url") or ""
        if jump.startswith("http"):
            print("缺少必要字段，跟随跳转链接…")
            r = session.get(jump, timeout=5, allow_redirects=True)
            merge_cookies(cookie_map, session, r)
            print("跳转后 Cookie 键:", list(cookie_map.keys()))
    except Exception as e:
        print("跳转获取失败:", e)

    return cookie_map


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
            print("\n✅ 登录成功，快速提取 Cookie…")
            # 优化点：不再进行多余请求，直接从响应和跳转中获取
            cookie_map = quick_fetch_jump_cookies(session, resp)
            if has_needed_cookies(cookie_map):
                return save_cookie_map(cookie_map)
            else:
                print("❌ 仍然缺少必要 Cookie 字段")
                return False

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
    print(" B站扫码登录（优化版，快速保存 Cookie）")
    print("=" * 50)

    if not os.path.exists(COOKIE_FILE):
        print("未找到 bili_cookie.txt，扫码成功后将自动创建")
    else:
        print("将覆盖现有", COOKIE_FILE)

    login_url, qrcode_key = generate_qrcode()
    if not qrcode_key:
        sys.exit(1)

    push_qr_to_dingtalk(login_url)

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
    print("完成。请执行: /opt/deploy.sh")


if __name__ == "__main__":
    main()
