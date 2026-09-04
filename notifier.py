import os
import time
import random
import logging
import requests

WEBHOOK_CONFIG_FILE = "webhook_config.txt"
REQUEST_TIMEOUT = 10
MAX_MARKDOWN_LENGTH = 3500
MAX_ITEM_BLOCK_LENGTH = 1200   # 单条动态的 markdown 块最大长度

_session = requests.Session()
_session.headers.update({
    "User-Agent": "BilibiliNotifier/1.0"
})

_cached_webhooks = None
_webhook_file_mtime = 0
_consecutive_blocked_count = 0   # 连续被 310000 拦截的次数（仅用于日志提醒）

def _mask_url(url):
    """对 Webhook URL 进行脱敏，避免泄露 access_token"""
    if not url or len(url) < 20:
        return "***"
    return f"{url[:12]}...{url[-6:]}"

def check_webhook_configured():
    config = get_webhooks()
    return bool(config.get("dynamic") or config.get("comment") or config.get("system"))

def get_webhooks(force_reload=False):
    """解析键值对格式的 webhook_config.txt，带文件修改时间缓存"""
    global _cached_webhooks, _webhook_file_mtime
    if not force_reload and _cached_webhooks is not None:
        try:
            mtime = os.path.getmtime(WEBHOOK_CONFIG_FILE)
            if mtime == _webhook_file_mtime:
                return _cached_webhooks
        except OSError:
            pass

    config = {}
    try:
        if os.path.exists(WEBHOOK_CONFIG_FILE):
            with open(WEBHOOK_CONFIG_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        config[k.strip()] = v.strip()
            _webhook_file_mtime = os.path.getmtime(WEBHOOK_CONFIG_FILE)
        else:
            _webhook_file_mtime = 0
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

def optimize_cover_url(url, width=480, height=300):
    """对 B 站图片 URL 添加压缩参数，减小推送体积"""
    if not url:
        return ""
    url = clean_text(url)
    if not url.startswith(("http://", "https://")):
        return ""
    # 仅处理常见的 B 站图床域名，避免误伤其他外链
    if any(domain in url for domain in ("hdslb.com", "biliimg.com", "bilibili.com")):
        # 若已包含 @ 参数则不重复添加
        if "@" not in url.split("/")[-1]:
            return f"{url}@{width}w_{height}h_1c.webp"
    return url

def post_dingtalk(webhook_url, payload, retries=2):
    if not webhook_url:
        logging.error("Webhook URL 为空，无法推送")
        return False

    msgtype = payload.get("msgtype", "unknown")
    msgtitle = (
        payload.get("markdown", {}).get("title")
        or payload.get("text", {}).get("content", "")[:30]
    )

    global _consecutive_blocked_count

    for attempt in range(retries + 1):
        try:
            resp = _session.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                logging.error(
                    f"钉钉 webhook HTTP 异常: status={resp.status_code}, "
                    f"title={msgtitle}, url={_mask_url(webhook_url)}"
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
                _consecutive_blocked_count = 0
                logging.info(f"钉钉消息发送成功: type={msgtype}, title={msgtitle}")
                return True

            errcode = data.get("errcode")
            errmsg = data.get("errmsg")

            # 310000：关键字/安全限制，视为已处理，但保留警告日志
            if errcode == 310000:
                _consecutive_blocked_count += 1
                if _consecutive_blocked_count % 5 == 1:  # 每5次提醒一次
                    logging.warning(
                        f"钉钉消息被安全拦截 (310000)，请检查关键词/IP 白名单配置！"
                        f"title={msgtitle}, errmsg={errmsg}, url={_mask_url(webhook_url)}"
                    )
                return True  # 不再重试，避免无限循环

            logging.error(
                f"钉钉 webhook 发送失败: type={msgtype}, title={msgtitle}, "
                f"errcode={errcode}, errmsg={errmsg}, url={_mask_url(webhook_url)}"
            )
            if attempt < retries:
                time.sleep(1.5 + attempt + random.random())
                continue
            return False

        except Exception as e:
            logging.error(
                f"钉钉 webhook 异常 ({attempt + 1}/{retries + 1}): "
                f"type={msgtype}, title={msgtitle}, error={e}, url={_mask_url(webhook_url)}"
            )
            if attempt < retries:
                time.sleep(1 + attempt + random.random())
            else:
                return False
    return False

def build_dynamic_markdown(items):
    """支持动态封面图片显示，并严格控制长度"""
    lines = ["## B站动态更新", ""]
    total_len = len(lines[0]) + len(lines[1])
    added_count = 0

    for idx, item in enumerate(items, 1):
        user = clean_text(item.get("user", "未知UP")) or "未知UP"
        message = item.get("message", "")
        pub_time = clean_text(item.get("time", ""))
        link = normalize_link(item.get("link", ""))
        cover = optimize_cover_url(item.get("cover", ""))

        # 构建单条动态的 markdown 片段
        item_lines = []
        item_lines.append(f"### {user}")
        if pub_time:
            item_lines.append(pub_time)
        item_lines.append("")
        quoted = format_quote_block(message, max_len=800, max_lines=10)
        item_lines.append(quoted)
        item_lines.append("")

        if cover and cover.startswith(("http://", "https://")):
            item_lines.append(f"![动态封面]({cover})")
            item_lines.append("")

        if link:
            item_lines.append(f"[查看原动态]({link})")
            item_lines.append("")

        # 单条动态 markdown 块
        item_block = "\n".join(item_lines).strip()
        if len(item_block) > MAX_ITEM_BLOCK_LENGTH:
            item_block = truncate_text(item_block, MAX_ITEM_BLOCK_LENGTH)

        # 分隔符
        separator = "\n---\n" if idx != len(items) else ""

        # 检查总长度，若超限且已至少包含一条则停止添加
        if added_count > 0 and (total_len + len(item_block) + len(separator)) > MAX_MARKDOWN_LENGTH:
            lines.append("...（更多动态请点击链接查看）")
            break

        lines.extend(item_lines)
        if separator:
            lines.append("---")
            lines.append("")
        total_len += len(item_block) + len(separator)
        added_count += 1

    if added_count == 0:
        lines.append("（没有可显示的动态内容）")

    return truncate_text("\n".join(lines).strip(), MAX_MARKDOWN_LENGTH)

def detect_notify_type(items, notify_type):
    if notify_type in ("dynamic", "system"):
        return notify_type
    if not items:
        return "system"
    first = items[0] if isinstance(items[0], dict) else {}
    # 默认按动态处理（本模块目前仅用于动态通知）
    return "dynamic"

def send_webhook_notification(title, items, retries=2, notify_type=None):
    if not isinstance(items, list):
        items = []

    if not items and notify_type != "system":
        logging.info("没有可发送内容，跳过通知")
        return False

    actual_type = detect_notify_type(items, notify_type)
    config = get_webhooks()

    # 优先用对应类型，其次 dynamic，再其次 system
    webhook_url = (
        config.get(actual_type)
        or config.get("dynamic")
        or config.get("system")
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

    # 动态通知
    markdown_text = build_dynamic_markdown(items)
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": markdown_text
        }
    }
    return post_dingtalk(webhook_url, payload, retries=retries)
