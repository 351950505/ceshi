import time
import hashlib
import urllib.parse
import requests

# 你的完整 Cookie（从浏览器复制）
COOKIE = "SESSDATA=621ec661%2C1788928717%2C00151%2A32CjAn3ZLnY6usYGfEdMfECxEzxRBqqRl99bTM_lFQxmihBVKeF0ffJcqa0LfT_uozBXYSVnZyY043cUtZZzZOelNGTUlDTUlqa2lUR1gxN1RaZU5TbUxRblpYU09KeG52al9YODhPN0VKZllPaFlSczVDT2t5VGs1ZFUzakVSLUZHcWpXRUxsTkRRIIEC; bili_jct=1d175473fb5959439dd8085a64c0e019; DedeUserID=3706948578969654; sid=gv9f3e9m"

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Cookie": COOKIE
}

# 获取 wbi 密钥
nav = requests.get("https://api.bilibili.com/x/web-interface/nav", headers=headers).json()
img_key = nav['data']['wbi_img']['img_url'].split('/')[-1].split('.')[0]
sub_key = nav['data']['wbi_img']['sub_url'].split('/')[-1].split('.')[0]

mixinKeyEncTab = [
    46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,
    37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52
]

def getMixinKey(orig):
    return ''.join([orig[i] for i in mixinKeyEncTab])[:32]

def enc_wbi(params, img_key, sub_key):
    mixin_key = getMixinKey(img_key + sub_key)
    # 时间补偿：服务器快2分钟，减去120秒
    params['wts'] = int(time.time()) - 120
    params = dict(sorted(params.items()))
    # 编码参数
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
    params['w_rid'] = sign
    return params

# 测试动态接口
params = {
    "host_mid": "3546905852250875",
    "type": "all",
    "web_location": "333.1365"
}
signed_params = enc_wbi(params, img_key, sub_key)
print("请求参数:", signed_params)

resp = requests.get("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all", headers=headers, params=signed_params)
print("响应状态码:", resp.status_code)
print("响应内容:", resp.json())
