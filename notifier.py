import os
import time
import random
import logging
import requests

WEBHOOK_CONFIG_FILE = "webhook_config.txt"
REQUEST_TIMEOUT = 10

MAX_MARKDOWN_LENGTH = 3500
MAX_TEXT_LENGTH = 1800

_session = requests.Session()
_session.headers.update({
    "User-Agent": "BilibiliNotifier/1.0"
})

_cached_webhook = None

if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )


def check_webhook_configured():
    try:
        return bool(get_webhook())
    except Exception as e:
        logging.error(f"检查 webhook 配置失败: {e}")
        return False


def get_webhook(force_reload=False):
    global _cached_webhook

    if _cached_webhook is not None and not force_reload:
        return _cached_webhook

    try:
        if not os.path.exists(WEBHOOK_CONFIG_FILE):
            _cached_webhook = ""
            return ""

        with open(WEBHOOK_CONFIG_FILE, "r", encoding="utf-8") as f:
            _cached_webhook = f.read().strip()
            return _cached_webhook
    except Exception as e:
        logging.error(f"读取 webhook 配置失败: {e}")
        return ""


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


def post_dingtalk(payload, retries=2):
    url = get_webhook()
    if not url:
        logging.error("Webhook URL 未配置，请在 webhook_config.txt 中填写")
        return False

    msgtype = payload.get("msgtype", "unknown")
    msgtitle = payload.get("markdown", {}).get("title") or payload.get("text", {}).get("content", "")[:30]

    for attempt in range(retries + 1):
        try:
            resp = _session.post(url, json=payload, timeout=REQUEST_TIMEOUT)

            if resp.status_code != 200:
                logging.error(
                    f"钉钉 webhook HTTP 异常: status={resp.status_code}, title={msgtitle}, body={resp.text[:300]}"
                )
                if 500 <= resp.status_code < 600 and attempt < retries:
                    time.sleep(1 + attempt + random.random())
                    continue
                return False

            try:
                data = resp.json()
            except Exception:
                logging.error(f"钉钉 webhook 返回非 JSON: title={msgtitle}, body={resp.text[:300]}")
                if attempt < retries:
                    time.sleep(1 + attempt + random.random())
                    continue
                return False

            if data.get("errcode") == 0:
                logging.info(f"钉钉消息发送成功: type={msgtype}, title={msgtitle}")
                return True

            errcode = data.get("errcode")
            errmsg = data.get("errmsg")
            logging.error(
                f"钉钉 webhook 发送失败: type={msgtype}, title={msgtitle}, errcode={errcode}, errmsg={errmsg}"
            )

            if attempt < retries:
                time.sleep(1.5 + attempt + random.random())
                continue
            return False

        except requests.RequestException as e:
            logging.error(
                f"钉钉 webhook 请求异常 ({attempt+1}/{retries+1}): type={msgtype}, title={msgtitle}, error={e}"
            )
            if attempt < retries:
                time.sleep(1 + attempt + random.random())
            else:
                return False

        except Exception as e:
            logging.error(
                f"钉钉 webhook 未知异常 ({attempt+1}/{retries+1}): type={msgtype}, title={msgtitle}, error={e}"
            )
            if attempt < retries:
                time.sleep(1 + attempt + random.random())
            else:
                return False

    return False


def build_dynamic_markdown(title, items):
    lines = ["## B站动态更新", ""]

    for idx, item in enumerate(items, 1):
        user = clean_text(item.get("user", "未知UP")) or "未知UP"
        message = item.get("message", "")
        pub_time = clean_text(item.get("time", ""))
        link = normalize_link(item.get("link", ""))

        lines.append(f"### {user}")
        if pub_time:
            lines.append(pub_time)
        lines.append("")
        lines.append(format_quote_block(message, max_len=1200, max_lines=12))
        lines.append("")

        if link:
            lines.append(f"[查看原动态]({link})")
            lines.append("")

        if idx != len(items):
            lines.append("---")
            lines.append("")

    return truncate_text("\n".join(lines).strip(), MAX_MARKDOWN_LENGTH)


def build_comment_markdown(video_title, comments):
    lines = [
        "## B站新评论",
        "",
        f"### {clean_text(video_title) or '未知视频'}",
        ""
    ]

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


def send_webhook_notification(title, items, retries=2, notify_type="comment"):
    if not isinstance(items, list):
        items = []

    if not items:
        logging.info(f"没有可发送内容，跳过通知: type={notify_type}, title={title[:50]}")
        return False

    if notify_type == "dynamic":
        markdown_text = build_dynamic_markdown(title, items)
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": "B站动态更新",
                "text": markdown_text
            }
        }
        return post_dingtalk(payload, retries=retries)

    markdown_text = build_comment_markdown(title, items)
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": "B站新评论",
            "text": markdown_text
        }
    }
    return post_dingtalk(payload, retries=retries)


def send_text_message(text, retries=2):
    text = truncate_text(clean_text(text), MAX_TEXT_LENGTH)
    if not text:
        logging.info("文本消息为空，跳过发送")
        return False

    payload = {
        "msgtype": "text",
        "text": {
            "content": text
        }
    }
    return post_dingtalk(payload, retries=retries)


def send_markdown_message(title, markdown_text, retries=2):
    title = clean_text(title) or "通知"
    markdown_text = truncate_text(clean_text(markdown_text), MAX_MARKDOWN_LENGTH)

    if not markdown_text:
        logging.info(f"Markdown 消息为空，跳过发送: title={title}")
        return False

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": markdown_text
        }
    }
    return post_dingtalk(payload, retries=retries)
