import os
import time
import logging
import requests

WEBHOOK_CONFIG_FILE = "webhook_config.txt"
REQUEST_TIMEOUT = 10

MAX_MARKDOWN_LENGTH = 3500
MAX_TEXT_LENGTH = 1800

_session = requests.Session()

if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )


def check_webhook_configured():
    try:
        if not os.path.exists(WEBHOOK_CONFIG_FILE):
            return False
        with open(WEBHOOK_CONFIG_FILE, "r", encoding="utf-8") as f:
            return bool(f.read().strip())
    except Exception as e:
        logging.error(f"检查 webhook 配置失败: {e}")
        return False


def get_webhook():
    try:
        with open(WEBHOOK_CONFIG_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        logging.error(f"读取 webhook 配置失败: {e}")
        return ""


def truncate_text(text, max_len):
    if not text:
        return ""
    text = str(text)
    return text if len(text) <= max_len else text[:max_len - 3] + "..."


def clean_markdown_text(text):
    if text is None:
        return ""
    return str(text).replace("\r", "").strip()


def format_quote_block(text, max_len):
    text = truncate_text(clean_markdown_text(text), max_len)
    if not text:
        return "> （无内容）"

    result = []
    for line in text.split("\n"):
        line = line.strip()
        result.append(f"> {line}" if line else ">")
    return "\n".join(result)


def post_dingtalk(payload, retries=2):
    url = get_webhook()
    if not url:
        logging.error("Webhook URL 未配置，请在 webhook_config.txt 中填写")
        return False

    for attempt in range(retries + 1):
        try:
            resp = _session.post(url, json=payload, timeout=REQUEST_TIMEOUT)

            if resp.status_code != 200:
                logging.error(
                    f"钉钉 webhook HTTP 异常: {resp.status_code}, body={resp.text[:300]}"
                )
                if attempt < retries:
                    time.sleep(1 + attempt)
                    continue
                return False

            try:
                data = resp.json()
            except Exception:
                logging.error(f"钉钉 webhook 返回非 JSON: {resp.text[:300]}")
                if attempt < retries:
                    time.sleep(1 + attempt)
                    continue
                return False

            if data.get("errcode") == 0:
                return True

            logging.error(
                f"钉钉 webhook 发送失败: errcode={data.get('errcode')}, errmsg={data.get('errmsg')}"
            )
            if attempt < retries:
                time.sleep(1.5 + attempt)
                continue
            return False

        except requests.RequestException as e:
            logging.error(f"钉钉 webhook 请求异常 ({attempt+1}/{retries+1}): {e}")
            if attempt < retries:
                time.sleep(1 + attempt)
            else:
                return False
        except Exception as e:
            logging.error(f"钉钉 webhook 未知异常 ({attempt+1}/{retries+1}): {e}")
            if attempt < retries:
                time.sleep(1 + attempt)
            else:
                return False

    return False


def build_dynamic_markdown(title, items):
    lines = ["## B站动态更新", ""]

    for idx, item in enumerate(items, 1):
        user = clean_markdown_text(item.get("user", "未知UP"))
        message = item.get("message", "")
        pub_time = clean_markdown_text(item.get("time", ""))
        link = clean_markdown_text(item.get("link", ""))

        lines.append(f"### {user}")
        if pub_time:
            lines.append(f"{pub_time}")
        lines.append("")
        lines.append(format_quote_block(message, 1200))
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
        f"### {clean_markdown_text(video_title)}",
        ""
    ]

    for idx, c in enumerate(comments, 1):
        user = clean_markdown_text(c.get("user", "未知用户"))
        message = c.get("message", "")

        lines.append(f"**{user}**")
        lines.append("")
        lines.append(format_quote_block(message, 500))
        lines.append("")

        if idx != len(comments):
            lines.append("---")
            lines.append("")

    return truncate_text("\n".join(lines).strip(), MAX_MARKDOWN_LENGTH)


def send_webhook_notification(title, comments, retries=2):
    if not isinstance(comments, list):
        comments = []

    if "特别关注UP主发布新内容" in title:
        markdown_text = build_dynamic_markdown(title, comments)
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": "B站动态更新",
                "text": markdown_text
            }
        }
        ok = post_dingtalk(payload, retries=retries)
        if ok:
            logging.info(f"动态消息发送成功: {title[:50]}")
        return ok

    markdown_text = build_comment_markdown(title, comments)
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": "B站新评论",
            "text": markdown_text
        }
    }
    ok = post_dingtalk(payload, retries=retries)
    if ok:
        logging.info(f"评论消息发送成功: {title[:50]}")
    return ok


def send_text_message(text, retries=2):
    payload = {
        "msgtype": "text",
        "text": {
            "content": truncate_text(text, MAX_TEXT_LENGTH)
        }
    }
    return post_dingtalk(payload, retries=retries)


def send_markdown_message(title, markdown_text, retries=2):
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": truncate_text(markdown_text, MAX_MARKDOWN_LENGTH)
        }
    }
    return post_dingtalk(payload, retries=retries)
