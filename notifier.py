import requests


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

    url = get_webhook()

    text = f"【B站新评论】\n视频: {title}\n\n"

    for c in comments:

        text += f"{c['user']}：{c['message']}\n"

    data = {
        "msgtype": "text",
        "text": {
            "content": text
        }
    }

    try:

        requests.post(url, json=data, timeout=10)

    except Exception as e:

        print("Webhook发送失败:", e)