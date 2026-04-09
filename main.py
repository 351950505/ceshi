import sys
import time
import datetime
import subprocess
import random
import logging
import traceback
import requests
import hashlib
import urllib.parse
import database as db
import notifier

TARGET_UID = 1671203508
VIDEO_CHECK_INTERVAL = 21600
HEARTBEAT_INTERVAL = 600

logging.basicConfig(
    filename='bili_monitor.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    encoding='utf-8',
    filemode='a'
)

# ------------------------
# Wbi 签名算法加密模块 (防风控核心)
# ------------------------
WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}

mixinKeyEncTab =[
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52
]

def getMixinKey(orig: str):
    """对 imgKey 和 subKey 进行字符顺序打乱编码"""
    return "".join([orig[i] for i in mixinKeyEncTab])[:32]

def encWbi(params: dict, img_key: str, sub_key: str):
    """为请求参数进行 wbi 签名"""
    mixin_key = getMixinKey(img_key + sub_key)
    curr_time = round(time.time())
    params['wts'] = curr_time
    # 按照 key 升序字典排序
    params = dict(sorted(params.items()))
    # 过滤掉特殊字符
    filtered_params = {}
    for k, v in params.items():
        v_str = str(v)
        for char in "!'()*":
            v_str = v_str.replace(char, '')
        filtered_params[k] = v_str
    # 序列化参数并计算 md5
    query = urllib.parse.urlencode(filtered_params)
    wbi_sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
    filtered_params['w_rid'] = wbi_sign
    return filtered_params

def update_wbi_keys(header):
    """从导航接口获取最新的 Wbi 密钥"""
    url = "https://api.bilibili.com/x/web-interface/nav"
    try:
        r = requests.get(url, headers=header, timeout=10)
        data = r.json()
        wbi_img = data["data"]["wbi_img"]
        WBI_KEYS["img_key"] = wbi_img["img_url"].rsplit('/', 1)[1].split('.')[0]
        WBI_KEYS["sub_key"] = wbi_img["sub_url"].rsplit('/', 1)[1].split('.')[0]
        WBI_KEYS["last_update"] = time.time()
        logging.info("Wbi 密钥已自动更新")
    except Exception as e:
        logging.error("获取 Wbi 密钥失败: %s", e)

def wbi_request(url, params, header):
    """封装带 Wbi 签名的安全请求"""
    # 密钥有效期为数小时，我们这里设定每 6 小时强制刷新一次密钥
    if time.time() - WBI_KEYS["last_update"] > 21600 or not WBI_KEYS["img_key"]:
        update_wbi_keys(header)
        time.sleep(1) # 避免请求过频
        
    signed_params = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    r = requests.get(url, headers=header, params=signed_params, timeout=10)
    data = r.json()
    
    # 容错：如果遇到 -400，说明密钥可能过期失效，立刻刷新重试一次
    if data.get("code") == -400:
        logging.warning("触发 -400 风控，正在重新计算 Wbi 签名重试...")
        update_wbi_keys(header)
        signed_params = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
        r = requests.get(url, headers=header, params=signed_params, timeout=10)
        data = r.json()
        
    return data

# ------------------------
# 基础功能模块
# ------------------------
def get_header():
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    except:
        logging.warning("未找到Cookie，启动扫码登录")
        subprocess.run([sys.executable, "login_bilibili.py"])
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    return {"Cookie": cookie, "User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com"}

def is_work_time():
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
    return now.weekday() < 5 and 9 <= now.hour < 19

def get_video_info(bv, header):
    url = f"https://api.bilibili.com/x/web-interface/view?bvid={bv}"
    try:
        r = requests.get(url, headers=header, timeout=10)
        data = r.json()
        if data["code"] == 0:
            return str(data["data"]["aid"]), data["data"]["title"]
    except:
        pass
    return None, None

def get_latest_video(header):
    url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    params = {"host_mid": TARGET_UID}
    try:
        r = requests.get(url, headers=header, params=params, timeout=10)
        data = r.json()
        if data["code"] != 0: return None
        for item in data.get("data", {}).get("items",[]):
            try:
                if item.get("type") == "DYNAMIC_TYPE_AV":
                    return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
            except:
                continue
    except:
        return None

def sync_latest_video(header):
    for i in range(5): 
        bv = get_latest_video(header)
        if bv:
            videos = db.get_monitored_videos()
            if videos and videos[0][1] == bv:
                return videos[0][0], videos[0][2]
            oid, title = get_video_info(bv, header)
            if oid:
                db.clear_videos()
                db.add_video_to_db(oid, bv, title)
                logging.info("开始监控视频: %s", title)
                return oid, title
        logging.warning(f"获取最新视频失败，第 {i+1} 次重试...")
        time.sleep(10)
    logging.error("连续5次获取视频失败")
    return None, None

def fetch_sub_replies(oid, root_rpid, header):
    all_replies =[]
    pn = 1
    while pn <= 5:
        params = {"oid": oid, "type": 1, "root": root_rpid, "pn": pn, "ps": 20}
        try:
            # 【使用 Wbi 封装请求】
            data = wbi_request("https://api.bilibili.com/x/v2/reply/reply", params, header)
            if data.get("code") != 0 or not data.get("data", {}).get("replies"):
                break
            all_replies.extend(data["data"]["replies"])
            pn += 1
            time.sleep(random.uniform(1.2, 2.0))
        except:
            break
    return all_replies

def send_exception_notification(msg):
    try:
        notifier.send_webhook_notification("程序异常",[{"user": "系统", "message": msg}])
    except:
        pass

# ------------------------
# 核心改动：Wbi 签名 + 5分钟时间回溯缓冲池 (防漏消息彻底解决)
# ------------------------
def scan_new_comments(oid, header, last_read_time, seen, seen_rcounts):
    new_list =[]
    max_ctime_in_this_round = last_read_time
    # 设定 300 秒（5分钟）的回溯缓冲时间，完美解决 B站 CDN 延迟导致的消息遗漏
    safe_read_time = last_read_time - 300 
    
    pn = 1
    while pn <= 10:  
        params = {"oid": oid, "type": 1, "sort": 0, "pn": pn, "ps": 20}
        
        try:
            # 【使用 Wbi 封装请求】
            data = wbi_request("https://api.bilibili.com/x/v2/reply", params, header)
            replies = data.get("data", {}).get("replies") or[]
            
            if not replies:
                break
                
            page_all_older = True  
            
            for r_obj in replies:
                rpid = r_obj["rpid_str"]
                r_ctime = r_obj["ctime"]
                max_ctime_in_this_round = max(max_ctime_in_this_round, r_ctime)
                
                # 【防漏优化】：不使用绝对的 last_read_time，而是留有 5 分钟余量！
                if r_ctime > safe_read_time:
                    page_all_older = False  # 只要还有 5 分钟内的数据，就可能还有遗漏，继续翻页
                    
                    if rpid not in seen:
                        seen.add(rpid)
                        new_list.append({
                            "user": r_obj["member"]["uname"], 
                            "message": r_obj["content"]["message"], 
                            "is_reply": False,
                            "ctime": r_ctime
                        })
                
                current_rcount = r_obj.get("rcount", 0)
                if current_rcount > seen_rcounts.get(rpid, 0):
                    page_all_older = False  # 有子回复变动，强制翻页检查
                    sub_replies = fetch_sub_replies(oid, rpid, header)
                    
                    for sub in sub_replies:
                        srpid = sub["rpid_str"]
                        s_ctime = sub["ctime"]
                        max_ctime_in_this_round = max(max_ctime_in_this_round, s_ctime)
                        
                        # 同样使用缓冲时间去捕获子回复的延迟
                        if s_ctime > safe_read_time and srpid not in seen:
                            seen.add(srpid)
                            new_list.append({
                                "user": sub["member"]["uname"], 
                                "message": sub["content"]["message"], 
                                "is_reply": True, 
                                "reply_to": r_obj["member"]["uname"],
                                "ctime": s_ctime
                            })
                            
                    seen_rcounts[rpid] = current_rcount
                            
            if page_all_older:
                break
                
            pn += 1
            time.sleep(random.uniform(1.0, 1.5))
            
        except Exception as e:
            logging.error("分页获取评论异常: %s", e)
            break
            
    return new_list, max_ctime_in_this_round

# ------------------------
# 主循环守护
# ------------------------
def start_monitoring(header):
    last_check = time.time()
    last_heartbeat = time.time()
    oid, title = sync_latest_video(header)

    if not oid:
        send_exception_notification("初始视频获取失败（已重试5次），请检查 Cookie 或 UP 主动态")
        logging.error("初始视频获取失败（已重试5次）")
        oid, title = None, "待获取视频"

    last_read_time = int(time.time())
    seen = set()
    seen_rcounts = {}
    
    logging.info("程序启动成功，开始时间基准线监控: %s", title or "待获取视频")

    while True:
        try:
            current = time.time()

            if is_work_time() and current - last_heartbeat >= HEARTBEAT_INTERVAL:
                now_str = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
                notifier.send_webhook_notification(
                    "监控心跳",[{"user": "系统", "message": f"程序运行正常\n时间: {now_str.strftime('%Y-%m-%d %H:%M:%S')}\n监控视频: {title or '待获取'}"}]
                )
                last_heartbeat = current
                logging.info("已发送10分钟心跳")

            if is_work_time() and oid:
                new_list, new_last_read_time = scan_new_comments(oid, header, last_read_time, seen, seen_rcounts)
                
                if new_last_read_time > last_read_time:
                    last_read_time = new_last_read_time

                if new_list:
                    new_list.sort(key=lambda x: x["ctime"])
                    logging.info("发现 %d 条新评论/回复", len(new_list))
                    for item in new_list:
                        prefix = f"回复@{item.get('reply_to','')} " if item.get("is_reply") else ""
                        logging.info("%s%s : %s", prefix, item["user"], item["message"])
                    try:
                        notifier.send_webhook_notification(title, new_list)
                    except:
                        pass
                
                time.sleep(random.uniform(10, 20))
            else:
                time.sleep(30)

            if time.time() - last_check > VIDEO_CHECK_INTERVAL:
                new_oid, new_title = sync_latest_video(header)
                if new_oid and new_oid != oid:
                    oid, title = new_oid, new_title
                    last_read_time = int(time.time())
                    seen.clear()
                    seen_rcounts.clear()
                    logging.info("切换新视频，重置时间线监控")
                last_check = time.time()

        except Exception as e:
            err = traceback.format_exc()
            logging.error("程序异常: %s", err)
            send_exception_notification(f"监控程序异常: {err[:300]}")
            time.sleep(60)

if __name__ == "__main__":
    db.init_db()
    header = get_header()
    # 启动前初始化并获取一次 Wbi 密钥
    update_wbi_keys(header)
    logging.info("B站监控程序启动（集成 Wbi 签名 + 时间回溯防漏补捞机制）")
    start_monitoring(header)
