import requests
import time

# 目标UP主UID
TARGET_UID = 1671203508

API_URL = "https://api.bilibili.com/x/space/wbi/arc/search"


def get_all_bvids_from_api(uid=TARGET_UID):
    """
    获取指定UP主的全部BV号
    """

    bvid_list = []
    page = 1

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://space.bilibili.com/"
    }

    while True:
        params = {
            "mid": uid,
            "ps": 50,   # 每页50
            "pn": page
        }

        try:
            resp = requests.get(API_URL, headers=headers, params=params, timeout=10)
            data = resp.json()

            if data["code"] != 0:
                print("获取视频失败:", data)
                return None

            videos = data["data"]["list"]["vlist"]

            if not videos:
                break

            for v in videos:
                bvid_list.append(v["bvid"])

            page += 1
            time.sleep(0.5)

        except Exception as e:
            print("请求失败:", e)
            return None

    return bvid_list