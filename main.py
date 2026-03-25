import sys
import time
import datetime
import subprocess
import random
import logging
import traceback
import requests
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
    for i in range(5):  # 重试5次
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
            r = requests.get("https://api.bilibili.com/x/v2/reply/reply", headers=header, params=params, timeout=8)
            data = r.json()
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
# 核心改动：加入 seen_rcounts (子回复数量缓存)
# ------------------------
def scan_new_comments(oid, header, last_read_time, seen, seen_rcounts):
    new_list =[]
    max_ctime_in_this_round = last_read_time
    
    pn = 1
    while pn <= 10:  # 最多往下挖10页
        url = "https://api.bilibili.com/x/v2/reply"
        # sort=0 保证最新发布的永远在最前面
        params = {"oid": oid, "type": 1, "sort": 0, "pn": pn, "ps": 20}
        
        try:
            r = requests.get(url, headers=header, params=params, timeout=10)
            data = r.json()
            replies = data.get("data", {}).get("replies") or[]
            
            if not replies:
                break
                
            page_all_older = True  
            
            for r_obj in replies:
                rpid = r_obj["rpid_str"]
                r_ctime = r_obj["ctime"]
                max_ctime_in_this_round = max(max_ctime_in_this_round, r_ctime)
                
                # 1. 检测主评论
                if r_ctime > last_read_time:
                    page_all_older = False
                    if rpid not in seen:
                        seen.add(rpid)
                        new_list.append({
                            "user": r_obj["member"]["uname"], 
                            "message": r_obj["content"]["message"], 
                            "is_reply": False,
                            "ctime": r_ctime
                        })
                
                # 2. 终极优化：检测子评论（盖楼）
                # 只有当 B站返回的子回复总数(rcount) > 我们记录的数量时，才去请求子接口！
                current_rcount = r_obj.get("rcount", 0)
                
                if current_rcount > seen_rcounts.get(rpid, 0):
                    page_all_older = False  # 即使主评论老了，有新子回复也要继续往后翻页看
                    sub_replies = fetch_sub_replies(oid, rpid, header)
                    
                    for sub in sub_replies:
                        srpid = sub["rpid_str"]
                        s_ctime = sub["ctime"]
                        max_ctime_in_this_round = max(max_ctime_in_this_round, s_ctime)
                        
                        if s_ctime > last_read_time and srpid not in seen:
                            seen.add(srpid)
                            new_list.append({
                                "user": sub["member"]["uname"], 
                                "message": sub["content"]["message"], 
                                "is_reply": True, 
                                "reply_to": r_obj["member"]["uname"],
                                "ctime": s_ctime
                            })
                            
                    # 抓取完后，更新这条评论最新的子回复数量缓存！
                    seen_rcounts[rpid] = current_rcount
                            
            if page_all_older:
                break
                
            pn += 1
            time.sleep(random.uniform(1.0, 1.5))
            
        except Exception as e:
            logging.error("分页获取评论异常: %s", e)
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

    # 初始化时间戳、去重池、回复数缓存池
    last_read_time = int(time.time())
    seen = set()
    seen_rcounts = {}
    
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

            # 正常监控
            if is_work_time() and oid:
                # 传入 seen_rcounts 缓存字典
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
                
                # 休眠 10~20 秒 (极速极静默版)
                time.sleep(random.uniform(10, 20))
            else:
                time.sleep(30)

            # 每6小时尝试刷新视频
            if time.time() - last_check > VIDEO_CHECK_INTERVAL:
                new_oid, new_title = sync_latest_video(header)
                if new_oid and new_oid != oid:
                    oid, title = new_oid, new_title
                    # 切换视频后，清空所有缓存池，重置时间线
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
    logging.info("B站监控程序启动（终极极速防风控版：rcount 缓存 + 时间排序）")
    start_monitoring(header)
