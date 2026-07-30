import os
import time
import random
import logging
import requests

WEBHOOK_CONFIG_FILE = "webhook_config.txt"
REQUEST_TIMEOUT = 10
MAX_MARKDOWN_LENGTH = 3500

_session = requests.Session()
_session.headers.update({
    "User-Agent": "BilibiliNotifier/1.0"
})

_cached_webhooks = None


def check_webhook_configured():
    config = get_webhooks()
    return bool(config.get("dynamic") or config.get("comment") or config.get("system"))


def get_webhooks(force_reload=False):
    """解析键值对格式的 webhook_config.txt"""
    global _cached_webhooks
    if _cached_webhooks is not None and not force_reload:
        return _cached_webhooks

    config = {}
    try:
        if not os.path.exists(WEBHOOK_CONFIG_FILE):
            _cached_webhooks = config
            return config
        with open(WEBHOOK_CONFIG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip()
        _cached_webhooks = config
    except Exception as e:
        logging.error(f"读取 webhook 配置失败: {e}")
    return config


def truncate_text(text, max_len):
    if not text:
        return ""
    text = str(text)
    return text if len(text) <= max_len else text[:max_len - 3] + "..."


def clean_text(text):
    if text is None:
        return ""
    return str(text).replace("\r", "").strip()


def smart_truncate(text, max_len=1200, max_lines=12):
    text = clean_text(text)
    if not text:
        return ""
    lines = text.splitlines()
    if max_lines > 0:
        lines = lines[:max_lines]
    result = "\n".join(line.rstrip() for line in lines).strip()
    if len(result) > max_len:
        result = result[:max_len - 3] + "..."
    return result


def format_quote_block(text, max_len, max_lines=12):
    text = smart_truncate(text, max_len=max_len, max_lines=max_lines)
    if not text:
        return "> （无内容）"
    result = []
    for line in text.split("\n"):
        line = line.strip()
        result.append(f"> {line}" if line else ">")
    return "\n".join(result)


def normalize_link(link):
    link = clean_text(link)
    if not link:
        return ""
    if link.startswith("http://") or link.startswith("https://"):
        return link
    return ""


def post_dingtalk(webhook_url, payload, retries=2):
    if not webhook_url:
        logging.error("Webhook URL 为空，无法推送")
        return False

    msgtype = payload.get("msgtype", "unknown")
    msgtitle = (
        payload.get("markdown", {}).get("title")
        or payload.get("text", {}).get("content", "")[:30]
    )

    for attempt in range(retries + 1):
        try:
            resp = _session.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                logging.error(
                    f"钉钉 webhook HTTP 异常: status={resp.status_code}, title={msgtitle}"
                )
                if 500 <= resp.status_code < 600 and attempt < retries:
                    time.sleep(1 + attempt + random.random())
                    continue
                return False

            try:
                data = resp.json()
            except Exception:
                logging.error(f"钉钉 webhook 返回非 JSON: title={msgtitle}")
                if attempt < retries:
                    time.sleep(1 + attempt + random.random())
                    continue
                return False

            if data.get("errcode") == 0:
                logging.info(f"钉钉消息发送成功: type={msgtype}, title={msgtitle}")
                return True

            errcode = data.get("errcode")
            errmsg = data.get("errmsg")

            # 310000：关键字/安全限制，当作成功，避免主程序反复报警
            if errcode == 310000:
                return True

            logging.error(
                f"钉钉 webhook 发送失败: type={msgtype}, title={msgtitle}, "
                f"errcode={errcode}, errmsg={errmsg}"
            )
            if attempt < retries:
                time.sleep(1.5 + attempt + random.random())
                continue
            return False

        except Exception as e:
            logging.error(
                f"钉钉 webhook 异常 ({attempt + 1}/{retries + 1}): "
                f"type={msgtype}, title={msgtitle}, error={e}"
            )
            if attempt < retries:
                time.sleep(1 + attempt + random.random())
            else:
                return False
    return False


def build_dynamic_markdown(items):
    """支持动态封面图片显示"""
    lines = ["## B站动态更新", ""]
    for idx, item in enumerate(items, 1):
        user = clean_text(item.get("user", "未知UP")) or "未知UP"
        message = item.get("message", "")
        pub_time = clean_text(item.get("time", ""))
        link = normalize_link(item.get("link", ""))
        cover = clean_text(item.get("cover", ""))

        lines.append(f"### {user}")
        if pub_time:
            lines.append(pub_time)
        lines.append("")
        lines.append(format_quote_block(message, max_len=1000, max_lines=10))
        lines.append("")

        if cover and (cover.startswith("http://") or cover.startswith("https://")):
            lines.append(f"![动态封面]({cover})")
            lines.append("")

        if link:
            lines.append(f"[查看原动态]({link})")
            lines.append("")

        if idx != len(items):
            lines.append("---")
            lines.append("")

    return truncate_text("\n".join(lines).strip(), MAX_MARKDOWN_LENGTH)


def build_comment_markdown(comments):
    lines = ["## B站新评论", ""]
    for idx, c in enumerate(comments, 1):
        user = clean_text(c.get("user", "未知用户")) or "未知用户"
        message = c.get("message", "")
        lines.append(f"**{user}**")
        lines.append("")
        lines.append(format_quote_block(message, max_len=500, max_lines=8))
        lines.append("")
        if idx != len(comments):
            lines.append("---")
            lines.append("")
    return truncate_text("\n".join(lines).strip(), MAX_MARKDOWN_LENGTH)


def detect_notify_type(items, notify_type):
    if notify_type in ("dynamic", "comment", "system"):
        return notify_type
    if not items:
        return "system"
    first = items[0] if isinstance(items[0], dict) else {}
    if first.get("kind") == "dynamic":
        return "dynamic"
    if first.get("link") or first.get("time"):
        return "dynamic"
    return "comment"


def send_webhook_notification(title, items, retries=2, notify_type=None):
    if not isinstance(items, list):
        items = []

    if not items and notify_type != "system":
        logging.info("没有可发送内容，跳过通知")
        return False

    actual_type = detect_notify_type(items, notify_type)
    config = get_webhooks()

    # 优先用对应类型，其次 dynamic，再其次任意已配置的
    webhook_url = (
        config.get(actual_type)
        or config.get("dynamic")
        or config.get("system")
        or config.get("comment")
    )

    if not webhook_url:
        logging.error(f"Webhook URL 未配置，无法发送 {actual_type} 通知！")
        return False

    if actual_type == "system":
        text_content = "\n".join(str(i.get("message", "")) for i in items)
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": f"### {title}\n\n> {text_content}"
            }
        }
        return post_dingtalk(webhook_url, payload, retries=retries)

    if actual_type == "dynamic":
        markdown_text = build_dynamic_markdown(items)
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": markdown_text
            }
        }
        return post_dingtalk(webhook_url, payload, retries=retries)

    # comment
    markdown_text = build_comment_markdown(items)
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": markdown_text
        }
    }
    return post_dingtalk(webhook_url, payload, retries=retries)
