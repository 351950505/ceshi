import requests
import time
import hashlib
import urllib.parse

# 您的完整 Cookie
COOKIE = "SESSDATA=621ec661%2C1788928717%2C00151%2A32CjAn3ZLnY6usYGfEdMfECxEzxRBqqRl99bTM_lFQxmihBVKeF0ffJcqa0LfT_uozBXYSVnZyY043cUtZZzZOelNGTUlDTUlqa2lUR1gxN1RaZU5TbUxRblpYU09KeG52al9YODhPN0VKZllPaFlSczVDT2t5VGs1ZFUzakVSLUZHcWpXRUxsTkRRIIEC; bili_jct=1d175473fb5959439dd8085a64c0e019; DedeUserID=3706948578969654; sid=gv9f3e9m"

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.bilibili.com/",
    "Cookie": COOKIE
}

# 测试导航接口
nav = requests.get("https://api.bilibili.com/x/web-interface/nav", headers=headers)
print("导航接口:", nav.json())

# 测试动态接口（不带签名，预期返回 -400 或 -352）
url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all"
params = {"host_mid": "3546905852250875", "type": "all"}
resp = requests.get(url, headers=headers, params=params)
print("无签名响应:", resp.json())
