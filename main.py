import sys
import time
import datetime
import subprocess
import random
import logging
import traceback
import hashlib
import urllib.parse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import database as db
import notifier

# ================= 核心配置区 =================
TARGET_UID = 1671203508           # 主监控UP主(监控他的最新视频评论)
VIDEO_CHECK_INTERVAL = 21600      # 6小时刷新一次目标UP的最新视频
HEARTBEAT_INTERVAL = 600          # 10分钟发一次运行心跳

# [新增] 监听其他UP主动态的UID列表
EXTRA_DYNAMIC_UIDS =[3546905852250875, 3546961271589219,3546610447419885]
DYNAMIC_CHECK_INTERVAL = 60       # 动态检查间隔(秒)，60秒最安全
# ==============================================

logging.basicConfig(
    filename='bili_monitor.log',
    level=logging.INFO,
    format='%(asctime)s[%(levelname)s] %(message)s',
    encoding='utf-8',
    filemode='a'
)

# ------------------------
# 核心网络优化：全局会话与自动重试机制
# ------------------------
retry_strategy = Retry(
    total=5,  
    backoff_factor=1,  
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["HEAD", "GET", "OPTIONS"]
)
adapter = HTTPAdapter(max_retries=retry_strategy)
session = requests.Session()
session.mount("http://", adapter)
session.mount("https://", adapter)

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
    return "".join([orig[i] for i in mixinKeyEncTab])[:32]

def encWbi(params: dict, img_key: str, sub_key: str):
    mixin_key = getMixinKey(img_key + sub_key)
    curr_time = round(time.time())
    params['wts'] = curr_time
    params = dict(sorted(params.items()))
    filtered_params = {}
    for k, v in params.items():
        v_str = str(v)
        for char in "!'()*":
            v_str = v_str.replace(char, '')
        filtered_params[k] = v_str
    query = urllib.parse.urlencode(filtered_params)
    wbi_sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
    filtered_params['w_rid'] = wbi_sign
    return filtered_params

def update_wbi_keys(header):
    url = "https://api.bilibili.com/x/web-interface/nav"
    try:
        r = session.get(url, headers=header, timeout=10)
        data = r.json()
        wbi_img = data["data"]["wbi_img"]
        WBI_KEYS["img_key"] = wbi_img["img_url"].rsplit('/', 1)[1].split('.')[0]
        WBI_KEYS["sub_key"] = wbi_img["sub_url"].rsplit('/', 1)[1].split('.')[0]
        WBI_KEYS["last_update"] = time.time()
        logging.info("Wbi 密钥已自动更新")
    except Exception as e:
        logging.error("获取 Wbi 密钥失败: %s", e)

def wbi_request(url, params, header):
    if time.time() - WBI_KEYS["last_update"] > 21600 or not WBI_KEYS["img_key"]:
        update_wbi_keys(header)
        time.sleep(1) 
        
    signed_params = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    r = session.get(url, headers=header, params=signed_params, timeout=10)
    data = r.json()
    
    if data.get("code") == -400:
        logging.warning("触发 -400 风控，正在重新计算 Wbi 签名重试...")
        update_wbi_keys(header)
        signed_params = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
        r = session.get(url, headers=header, params=signed_params, timeout=10)
        data = r.json()
        
    return data

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
        r = session.get(url, headers=header, timeout=10)
        data = r.json()
        if data["code"] == 0:
            return str(data["data"]["aid"]), data["data"]["title"]
    except:
        pass
    return None, None

def get_latest_video(header, target_uid):
    url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    params = {"host_mid": target_uid}
    try:
        r = session.get(url, headers=header, params=params, timeout=10)
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
        bv = get_latest_video(header, TARGET_UID)
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

def send_exception_notification(msg):
    try:
        notifier.send_webhook_notification("程序异常",[{"user": "系统", "message": msg}])
    except:
        pass

# ------------------------
#[新增] 监听特别关注UP主动态的功能
# ------------------------
def init_extra_dynamics(header):
    """启动时初始化，记录历史动态，防止刚开机产生大量旧消息提醒"""
    seen_dynamics = {}
    for uid in EXTRA_DYNAMIC_UIDS:
        seen_dynamics[uid] = set()
        url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
        params = {"host_mid": uid}
        try:
            r = session.get(url, headers=header, params=params, timeout=10)
            data = r.json()
            if data.get("code") == 0:
                for item in data.get("data", {}).get("items",[]):
                    id_str = item.get("id_str")
                    if id_str:
                        seen_dynamics[uid].add(id_str)
        except Exception as e:
            logging.error(f"初始化动态 UID:{uid} 失败: {e}")
    return seen_dynamics

def check_new_dynamics(header, seen_dynamics):
    """检查特别关注的UP主是否有发新动态/视频"""
    new_alerts =[]
    for uid in EXTRA_DYNAMIC_UIDS:
        url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
        params = {"host_mid": uid}
        try:
            r = session.get(url, headers=header, params=params, timeout=10)
            data = r.json()
            if data.get("code") != 0:
                continue
            
            items = data.get("data", {}).get("items",[])
            for item in items:
                id_str = item.get("id_str")
                if not id_str:
                    continue
                    
                if id_str not in seen_dynamics[uid]:
                    seen_dynamics[uid].add(id_str) # 记录下来防止重复通知
                    
                    # 解析动态类型和内容
                    name = str(uid)
                    desc = "发布了新动态"
                    try:
                        name = item["modules"]["module_author"]["name"]
                    except: pass
                        
                    try:
                        dyn_type = item.get("type")
                        if dyn_type == "DYNAMIC_TYPE_AV":
                            title = item["modules"]["module_dynamic"]["major"]["archive"]["title"]
                            desc = f"🎥 发布了新视频：\n《{title}》"
                        elif dyn_type in ["DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_WORD"]:
                            text = item["modules"]["module_dynamic"]["desc"]["text"]
                            if len(text) > 80: text = text[:80] + "..."
                            desc = f"📝 发布了新图文/文字：\n{text}"
                        elif dyn_type == "DYNAMIC_TYPE_FORWARD":
                            desc = "🔄 转发了一条动态"
                        elif dyn_type == "DYNAMIC_TYPE_LIVE_RCMD":
                            desc = "🔴 开启了直播"
                    except: pass
                        
                    link = f"https://t.bilibili.com/{id_str}"
                    new_alerts.append({
                        "user": name,
                        "message": f"{desc}\n\n传送门直达: {link}"
                    })
        except Exception as e:
            logging.error(f"检查动态 UID:{uid} 网络异常: {e}")
            
    if new_alerts:
        logging.info(f"发现 {len(new_alerts)} 条特别关注UP主新动态！")
        try:
            # 使用已有的 webhook 接口发送专属动态通知
            notifier.send_webhook_notification("💡 特别关注UP主更新", new_alerts)
        except: pass


# ------------------------
# 极致精简版：仅扫描主界面新评论
# ------------------------
def scan_new_comments(oid, header, last_read_time, seen):
    new_list =[]
    max_ctime_in_this_round = last_read_time
    safe_read_time = last_read_time - 300 
    
    pn = 1
    while pn <= 10:  
        params = {"oid": oid, "type": 1, "sort": 0, "pn": pn, "ps": 20}
        
        try:
            data = wbi_request("https://api.bilibili.com/x/v2/reply", params, header)
            replies = data.get("data", {}).get("replies") or[]
            
            if not replies:
                break
                
            page_all_older = True  
            
            for r_obj in replies:
                rpid = r_obj["rpid_str"]
                r_ctime = r_obj["ctime"]
                max_ctime_in_this_round = max(max_ctime_in_this_round, r_ctime)
                
                if r_ctime > safe_read_time:
                    page_all_older = False
                    if rpid not in seen:
                        seen.add(rpid)
                        new_list.append({
                            "user": r_obj["member"]["uname"], 
                            "message": r_obj["content"]["message"], 
                            "ctime": r_ctime
                        })
                            
            if page_all_older:
                break
                
            pn += 1
            time.sleep(random.uniform(0.5, 1.0))
            
        except Exception as e:
            logging.error("分页获取评论网络异常(已多次重试): %s", e)
            break
            
    return new_list, max_ctime_in_this_round


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
    
    #[新增] 初始化特别关注UP主的动态缓存池
    seen_dynamics = init_extra_dynamics(header)
    last_dynamic_check = time.time()
    
    logging.info("程序启动成功，开始时间基准线监控: %s", title or "待获取视频")

    while True:
        try:
            current = time.time()

            # 10分钟心跳
            if is_work_time() and current - last_heartbeat >= HEARTBEAT_INTERVAL:
                now_str = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
                notifier.send_webhook_notification(
                    "监控心跳",[{"user": "系统", "message": f"程序运行正常\n时间: {now_str.strftime('%Y-%m-%d %H:%M:%S')}\n监控视频: {title or '待获取'}"}]
                )
                last_heartbeat = current
                logging.info("已发送10分钟心跳")

            # 主监控：工作时间循环获取新评论、及监听新动态
            if is_work_time():
                # 1. 评论监控
                if oid:
                    new_list, new_last_read_time = scan_new_comments(oid, header, last_read_time, seen)
                    
                    if new_last_read_time > last_read_time:
                        last_read_time = new_last_read_time

                    if new_list:
                        new_list.sort(key=lambda x: x["ctime"])
                        logging.info("发现 %d 条新主评论", len(new_list))
                        for item in new_list:
                            logging.info("%s : %s", item["user"], item["message"])
                        try:
                            notifier.send_webhook_notification(title, new_list)
                        except:
                            pass
                
                # 2. [新增] 动态监听 (每隔 60 秒触发一次)
                if current - last_dynamic_check >= DYNAMIC_CHECK_INTERVAL:
                    check_new_dynamics(header, seen_dynamics)
                    last_dynamic_check = time.time()

                # 极限刷新率：5~10 秒
                time.sleep(random.uniform(5, 10))
            else:
                time.sleep(30)

            # 每6小时尝试刷新目标视频
            if time.time() - last_check > VIDEO_CHECK_INTERVAL:
                new_oid, new_title = sync_latest_video(header)
                if new_oid and new_oid != oid:
                    oid, title = new_oid, new_title
                    last_read_time = int(time.time())
                    seen.clear()
                    logging.info("切换新视频，重置时间线监控")
                last_check = time.time()

        except Exception as e:
            err = traceback.format_exc()
            logging.error("主循环发生异常: %s", err)
            if "NameResolutionError" not in err and "ConnectionError" not in err:
                send_exception_notification(f"监控程序异常: {err[:300]}")
            time.sleep(30)

if __name__ == "__main__":
    db.init_db()
    header = get_header()
    update_wbi_keys(header)
    logging.info("B站监控程序启动（主评论监听 + 专属UP主动态雷达）")
    start_monitoring(header)
