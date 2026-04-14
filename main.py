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
import database as db
import notifier

# ================= 核心配置区 =================
TARGET_UID = 1671203508
VIDEO_CHECK_INTERVAL = 21600
HEARTBEAT_INTERVAL = 600
EXTRA_DYNAMIC_UIDS = [3546905852250875, 3546961271589219, 3546610447419885]
DYNAMIC_CHECK_INTERVAL = 60
DYNAMIC_BURST_INTERVAL = 10
DYNAMIC_BURST_DURATION = 300
# ==============================================

logging.basicConfig(
    filename='bili_monitor.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    encoding='utf-8',
    filemode='a'
)

# Wbi 签名模块
WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}
mixinKeyEncTab = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]

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
        for char in "!'()*": v_str = v_str.replace(char, '')
        filtered_params[k] = v_str
    query = urllib.parse.urlencode(filtered_params)
    wbi_sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
    filtered_params['w_rid'] = wbi_sign
    return filtered_params

def update_wbi_keys(header):
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
    if time.time() - WBI_KEYS["last_update"] > 21600 or not WBI_KEYS["img_key"]:
        update_wbi_keys(header)
        time.sleep(1)
    signed_params = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    try:
        r = requests.get(url, headers=header, params=signed_params, timeout=10)
        data = r.json()
    except Exception:
        return {"code": -1}
    if data.get("code") == -400:
        update_wbi_keys(header)
        signed_params = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
        try:
            r = requests.get(url, headers=header, params=signed_params, timeout=10)
            data = r.json()
        except Exception:
            return {"code": -1}
    return data

def get_header():
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    except:
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
    except: pass
    return None, None

def get_latest_video(header, target_uid):
    url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    params = {"host_mid": target_uid}
    try:
        r = requests.get(url, headers=header, params=params, timeout=10)
        data = r.json()
        if data.get("code") != 0: return None
        for item in data.get("data", {}).get("items",[]):
            if item.get("type") == "DYNAMIC_TYPE_AV":
                return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
    except: pass
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
        time.sleep(10)
    return None, None

def send_exception_notification(msg):
    try:
        notifier.send_webhook_notification("程序异常", [{"user": "系统", "message": msg}])
    except: pass

# 动态雷达 - 修复版（显示具体动态内容）
def init_extra_dynamics(header):
    seen_dynamics = {}
    active_dynamics = {}
    for uid in EXTRA_DYNAMIC_UIDS:
        seen_dynamics[uid] = set()
        active_dynamics[uid] = {}
        url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
        params = {"host_mid": uid}
        try:
            r = requests.get(url, headers=header, params=params, timeout=10)
            data = r.json()
            if data.get("code") == 0:
                for item in data.get("data", {}).get("items",[]):
                    id_str = item.get("id_str")
                    if id_str:
                        seen_dynamics[uid].add(id_str)
                        basic = item.get("basic", {})
                        c_oid = basic.get("comment_id_str")
                        c_type = basic.get("comment_type")
                        if c_oid and c_type:
                            active_dynamics[uid][id_str] = {"oid": c_oid, "type": c_type, "ctime": time.time()}
        except: pass
    return seen_dynamics, active_dynamics

def check_new_dynamics(header, seen_dynamics, active_dynamics):
    new_alerts = []
    has_new_dynamic = False
    for uid in EXTRA_DYNAMIC_UIDS:
        url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
        params = {"host_mid": uid}
        try:
            r = requests.get(url, headers=header, params=params, timeout=10)
            data = r.json()
            if data.get("code") != 0: continue
            for item in data.get("data", {}).get("items",[]):
                id_str = item.get("id_str")
                if not id_str or id_str in seen_dynamics[uid]: continue
                seen_dynamics[uid].add(id_str)
                has_new_dynamic = True

                # 提取动态具体内容
                dyn_text = ""
                try:
                    dyn_text = item["modules"]["module_dynamic"]["desc"]["text"]
                except:
                    pass

                dyn_type = item.get("type")
                attach_str = ""
                try:
                    if dyn_type == "DYNAMIC_TYPE_AV":
                        title = item["modules"]["module_dynamic"]["major"]["archive"]["title"]
                        attach_str = f"🎥 视频：《{title}》"
                    elif dyn_type == "DYNAMIC_TYPE_ARTICLE":
                        title = item["modules"]["module_dynamic"]["major"]["article"]["title"]
                        attach_str = f"📄 专栏：《{title}》"
                    elif dyn_type == "DYNAMIC_TYPE_FORWARD":
                        attach_str = "🔄 转发了动态"
                    elif dyn_type == "DYNAMIC_TYPE_LIVE_RCMD":
                        attach_str = "🔴 开启了直播"
                except:
                    pass

                final_desc = dyn_text if dyn_text else "发布了新动态"
                if attach_str:
                    final_desc += f"\n{attach_str}"

                name = str(uid)
                try:
                    name = item["modules"]["module_author"]["name"]
                except:
                    pass

                new_alerts.append({
                    "user": name,
                    "message": final_desc
                })

                basic = item.get("basic", {})
                c_oid = basic.get("comment_id_str")
                c_type = basic.get("comment_type")
                if c_oid and c_type:
                    active_dynamics[uid][id_str] = {"oid": c_oid, "type": c_type, "ctime": time.time()}
        except:
            pass
    if new_alerts:
        logging.info(f"发现 {len(new_alerts)} 条特别关注UP主新动态！")
        try:
            notifier.send_webhook_notification("💡 特别关注UP主发布新内容", new_alerts)
        except:
            pass
    return has_new_dynamic

def check_dynamic_up_replies(header, active_dynamics, seen_dynamic_replies):
    new_alerts = []
    current_time = time.time()
    for uid, dynamics in list(active_dynamics.items()):
        for dyn_id, dyn_info in list(dynamics.items()):
            if current_time - dyn_info["ctime"] > 86400:
                del dynamics[dyn_id]
                continue
            params = {"oid": dyn_info["oid"], "type": dyn_info["type"], "sort": 0, "pn": 1, "ps": 20}
            try:
                data = wbi_request("https://api.bilibili.com/x/v2/reply", params, header)
                if data.get("code") != 0: continue
                replies = data.get("data", {}).get("replies") or []
                top_reply = data.get("data", {}).get("upper", {}).get("top", None)
                all_to_check = replies.copy()
                if top_reply:
                    all_to_check.append(top_reply)
                for r_obj in all_to_check:
                    if not r_obj: continue
                    rpid = str(r_obj["rpid_str"])
                    r_mid = str(r_obj["member"]["mid"])
                    if r_mid == str(uid) and rpid not in seen_dynamic_replies:
                        seen_dynamic_replies.add(rpid)
                        msg = r_obj["content"]["message"]
                        name = r_obj["member"]["uname"]
                        new_alerts.append({"user": name, "message": f"💬 UP主补充评论：\n{msg}"})
                time.sleep(random.uniform(0.5, 1.0))
            except:
                pass
    if new_alerts:
        try:
            notifier.send_webhook_notification("🔔 UP主本尊动态评论区出没", new_alerts)
        except:
            pass

# 主视频评论监控
def scan_new_comments(oid, header, last_read_time, seen):
    new_list = []
    max_ctime_in_this_round = last_read_time
    safe_read_time = last_read_time - 300
    pn = 1
    while pn <= 10:
        params = {"oid": oid, "type": 1, "sort": 0, "pn": pn, "ps": 20}
        try:
            data = wbi_request("https://api.bilibili.com/x/v2/reply", params, header)
            if data.get("code") != 0: break
            replies = data.get("data", {}).get("replies") or []
            if not replies: break
            page_all_older = True
            for r_obj in replies:
                rpid = r_obj["rpid_str"]
                r_ctime = r_obj["ctime"]
                max_ctime_in_this_round = max(max_ctime_in_this_round, r_ctime)
                if r_ctime > safe_read_time:
                    page_all_older = False
                    if rpid not in seen:
                        seen.add(rpid)
                        new_list.append({"user": r_obj["member"]["uname"], "message": r_obj["content"]["message"], "ctime": r_ctime})
            if page_all_older: break
            pn += 1
            time.sleep(random.uniform(0.5, 1.0))
        except:
            break
    return new_list, max_ctime_in_this_round

def start_monitoring(header):
    last_check = time.time()
    last_heartbeat = time.time()
    oid, title = sync_latest_video(header)
    if not oid:
        send_exception_notification("初始视频获取失败（已重试5次）")
        oid, title = None, "待获取视频"
    last_read_time = int(time.time())
    seen = set()
    seen_dynamics, active_dynamics = init_extra_dynamics(header)
    seen_dynamic_replies = set()
    last_dynamic_check = time.time()
    dynamic_burst_end_time = 0
    logging.info("程序启动成功，开始监控: %s", title or "待获取视频")
    while True:
        try:
            current = time.time()
            if is_work_time() and current - last_heartbeat >= HEARTBEAT_INTERVAL:
                notifier.send_webhook_notification("监控心跳", [{"user": "系统", "message": f"程序运行正常\n监控视频: {title or '待获取'}"}])
                last_heartbeat = current
            if is_work_time():
                if oid:
                    new_list, new_last_read_time = scan_new_comments(oid, header, last_read_time, seen)
                    if new_last_read_time > last_read_time:
                        last_read_time = new_last_read_time
                    if new_list:
                        new_list.sort(key=lambda x: x["ctime"])
                        logging.info("发现 %d 条新主评论", len(new_list))
                        try:
                            notifier.send_webhook_notification(title, new_list)
                        except:
                            pass
                current_dyn_interval = DYNAMIC_BURST_INTERVAL if current < dynamic_burst_end_time else DYNAMIC_CHECK_INTERVAL
                if current - last_dynamic_check >= current_dyn_interval:
                    has_new_dyn = check_new_dynamics(header, seen_dynamics, active_dynamics)
                    if has_new_dyn:
                        dynamic_burst_end_time = current + DYNAMIC_BURST_DURATION
                    check_dynamic_up_replies(header, active_dynamics, seen_dynamic_replies)
                    last_dynamic_check = time.time()
                time.sleep(random.uniform(5, 10))
            else:
                time.sleep(30)
            if time.time() - last_check > VIDEO_CHECK_INTERVAL:
                new_oid, new_title = sync_latest_video(header)
                if new_oid and new_oid != oid:
                    oid, title = new_oid, new_title
                    last_read_time = int(time.time())
                    seen.clear()
                last_check = time.time()
        except Exception as e:
            err = traceback.format_exc()
            logging.error("主循环异常: %s", err)
            time.sleep(30)

if __name__ == "__main__":
    db.init_db()
    header = get_header()
    update_wbi_keys(header)
    logging.info("B站监控程序启动（动态内容修复版）")
    start_monitoring(header)
