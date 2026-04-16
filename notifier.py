import requests
import logging

def check_webhook_configured():
    try:
        with open("webhook_config.txt", "r", encoding="utf-8") as f:
            url = f.read().strip()
        return bool(url)
    except:
        return False

def get_webhook():
    with open("webhook_config.txt", "r", encoding="utf-8") as f:
        return f.read().strip()

def send_webhook_notification(title, comments):
    """
    发送 Webhook 通知
    支持两种调用：
    1. 评论通知：comments 为列表，每个元素包含 user, message
    2. 动态通知：comments 为列表，每个元素包含 user, message
    """
    url = get_webhook()
    if not url:
        logging.error("Webhook URL 未配置")
        return False

    # 根据 title 判断通知类型
    if "特别关注UP主发布新内容" in title:
        prefix = "【B站新动态】"
    else:
        prefix = "【B站新评论】"
    
    text = f"{prefix}\n"
    if title and "特别关注" not in title:
        text += f"视频: {title}\n\n"
    
    for c in comments:
        text += f"{c['user']}：{c['message']}\n"
    
    # 限制消息长度（企业微信最大2048，钉钉最大2000）
    if len(text) > 1900:
        text = text[:1900] + "\n...(消息过长已截断)"
    
    data = {
        "msgtype": "text",
        "text": {
            "content": text
        }
    }
    
    try:
        r = requests.post(url, json=data, timeout=10)
        if r.status_code == 200:
            # 进一步检查响应内容，某些 Webhook 返回非 200 但 status_code 可能是 200
            resp = r.json() if r.text else {}
            # 企业微信返回 errcode:0 表示成功
            if resp.get('errcode') == 0 or resp.get('code') == 0:
                logging.info(f"Webhook 发送成功: {title}")
                return True
            else:
                logging.error(f"Webhook 发送失败: {resp}")
                return False
        else:
            logging.error(f"Webhook 请求失败，状态码: {r.status_code}, 响应: {r.text}")
            return False
    except Exception as e:
        logging.error(f"Webhook 发送异常: {e}")
        return False
