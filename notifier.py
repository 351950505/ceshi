import requests
import logging
import time

# 确保日志配置（若主程序已配置则不会重复）
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

def check_webhook_configured():
    """检查 webhook_config.txt 是否存在且非空"""
    try:
        with open("webhook_config.txt", "r", encoding="utf-8") as f:
            url = f.read().strip()
        return bool(url)
    except:
        return False

def get_webhook():
    """读取 webhook URL"""
    with open("webhook_config.txt", "r", encoding="utf-8") as f:
        return f.read().strip()

def send_webhook_notification(title, comments, retries=2):
    """
    发送 Webhook 通知，支持评论和动态消息。
    - title: 视频标题或动态提示标题
    - comments: list of dict, 每个元素包含 'user' 和 'message' 键
    - retries: 失败重试次数（不含首次）
    返回 True 表示成功，False 表示失败。
    """
    url = get_webhook()
    if not url:
        logging.error("Webhook URL 未配置，请在 webhook_config.txt 中填写")
        return False

    # 根据标题判断消息类型，构建文本
    if "特别关注UP主发布新内容" in title:
        # 动态通知
        text = "【B站新动态】\n"
        for item in comments:
            text += f"👤 {item['user']}\n{item['message']}\n\n"
    else:
        # 评论通知
        text = f"【B站新评论】\n视频: {title}\n\n"
        for c in comments:
            text += f"{c['user']}：{c['message']}\n"

    # 限制消息长度（企业微信最大2048，钉钉最大5000，这里取2000安全值）
    if len(text) > 2000:
        text = text[:1997] + "..."

    data = {
        "msgtype": "text",
        "text": {
            "content": text
        }
    }

    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, json=data, timeout=10)
            if resp.status_code != 200:
                logging.error(f"Webhook 响应状态码异常: {resp.status_code}, 响应内容: {resp.text[:200]}")
                if attempt < retries:
                    time.sleep(1)
                    continue
                return False

            # 解析 JSON 响应（企业微信/钉钉均返回 errcode 字段）
            resp_json = resp.json()
            errcode = resp_json.get("errcode")
            if errcode == 0:
                logging.info(f"Webhook 发送成功: {title[:50]}...")
                return True
            else:
                errmsg = resp_json.get("errmsg", "未知错误")
                logging.error(f"Webhook 发送失败: errcode={errcode}, errmsg={errmsg}")
                # 可重试的错误码（如频率限制、消息重复等）
                if errcode in (45009, 45033):  # 企业微信频率限制或消息重复
                    if attempt < retries:
                        time.sleep(2)
                        continue
                return False
        except Exception as e:
            logging.error(f"Webhook 发送异常 (尝试 {attempt+1}/{retries+1}): {e}")
            if attempt < retries:
                time.sleep(1)
            else:
                return False
    return False
