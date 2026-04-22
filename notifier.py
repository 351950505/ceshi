import os
import time
import logging
import requests

WEBHOOK_CONFIG_FILE = "webhook_config.txt"
REQUEST_TIMEOUT = 10

# 钉钉 markdown 最好别太长，保守一些
MAX_MARKDOWN_LENGTH = 3500
MAX_TEXT_LENGTH = 1800

_session = requests.Session()

# 确保日志配置（若主程序已配置则不会重复）
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )


def check_webhook_configured():
    """检查 webhook_config.txt 是否存在且非空"""
    try:
        if not os.path.exists(WEBHOOK_CONFIG_FILE):
            return False
        with open(WEBHOOK_CONFIG_FILE, "r", encoding="utf-8") as f:
            url = f.read().strip()
        return bool(url)
    except Exception as e:
        logging.error(f"检查 webhook 配置失败: {e}")
        return False


def get_webhook():
    """读取 webhook URL"""
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
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def clean_markdown_text(text: str) -> str:
    """
    简单清洗文本，避免钉钉 markdown 渲染混乱
    """
    if text is None:
        return ""
    text = str(text).replace("\r", "").strip()
    return text


def post_dingtalk(payload, retries=2):
    """
    统一发送钉钉 webhook 请求
    """
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

            errcode = data.get("errcode", -1)
            errmsg = data.get("errmsg", "unknown")

            if errcode == 0:
                return True

            logging.error(f"钉钉 webhook 发送失败: errcode={errcode}, errmsg={errmsg}")

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
    """
    构造动态通知 markdown
    items: [
        {
            "user": "...",
            "message": "...",
            "time": "...",   # 可选
            "link": "..."    # 可选
        }
    ]
    """
    lines = []
    lines.append("### B站动态更新")
    lines.append("")

    for idx, item in enumerate(items, 1):
        user = clean_markdown_text(item.get("user", "未知UP"))
        message = clean_markdown_text(item.get("message", ""))
        pub_time = clean_markdown_text(item.get("time", ""))
        link = clean_markdown_text(item.get("link", ""))

        lines.append(f"**UP主**：{user}  ")

        if pub_time:
            lines.append(f"**时间**：{pub_time}  ")

        lines.append("")

        if message:
            lines.append(truncate_text(message, 1200))
            lines.append("")

        if link:
            lines.append(f"[查看原动态]({link})")
            lines.append("")

        if idx != len(items):
            lines.append("---")
            lines.append("")

    markdown_text = "\n".join(lines).strip()
    return truncate_text(markdown_text, MAX_MARKDOWN_LENGTH)


def build_comment_markdown(video_title, comments):
    """
    构造评论通知 markdown
    comments: [
        {"user": "...", "message": "..."}
    ]
    """
    lines = []
    lines.append("### B站新评论")
    lines.append("")
    lines.append(f"**视频**：{clean_markdown_text(video_title)}")
    lines.append("")

    for idx, c in enumerate(comments, 1):
        user = clean_markdown_text(c.get("user", "未知用户"))
        message = clean_markdown_text(c.get("message", ""))

        lines.append(f"**{user}**  ")
        if message:
            lines.append(truncate_text(message, 500))
        else:
            lines.append("（无内容）")
        lines.append("")

        # 多条评论之间空一行，避免过于拥挤
        if idx != len(comments):
            lines.append("")

    markdown_text = "\n".join(lines).strip()
    return truncate_text(markdown_text, MAX_MARKDOWN_LENGTH)


def send_webhook_notification(title, comments, retries=2):
    """
    发送钉钉通知，兼容旧接口：
    - title: 视频标题或动态提示标题
    - comments: list[dict]，元素包含 user / message，可额外有 time / link
    - retries: 重试次数（不含首次）

    返回 True / False
    """
    if not isinstance(comments, list):
        comments = []

    # 动态通知
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

    # 评论通知
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
    """
    发送纯文本消息（调试或兜底用）
    """
    payload = {
        "msgtype": "text",
        "text": {
            "content": truncate_text(text, MAX_TEXT_LENGTH)
        }
    }
    return post_dingtalk(payload, retries=retries)


def send_markdown_message(title, markdown_text, retries=2):
    """
    直接发送 markdown，自定义内容时可用
    """
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": truncate_text(markdown_text, MAX_MARKDOWN_LENGTH)
        }
    }
    return post_dingtalk(payload, retries=retries)
