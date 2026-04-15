# notifier.py
import requests, logging
from config import WEBHOOK_URL

def send_webhook_notification(title: str, comments: list):
    if not WEBHOOK_URL:
        logging.warning("WEBHOOK_URL 未配置，已跳过推送")
        return
    # …（保持原实现）…
