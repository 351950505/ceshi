import sys, os, time, random, logging, traceback, hashlib, urllib.parse, json, subprocess, requests, database as db, notifier

TARGET_UID = 1671203508
VIDEO_CHECK_INTERVAL = 21600
HEARTBEAT_INTERVAL = 600

EXTRA_DYNAMIC_UIDS = [
    3546905852250875,
    3546961271589219,
    3546610447419885,
    285340365,
    3706948578969654
]

DYNAMIC_CHECK_INTERVAL = 30
DYNAMIC_BURST_INTERVAL = 10
DYNAMIC_BURST_DURATION = 300
DYNAMIC_MAX_AGE = 300

LOG_FILE = "bili_monitor.log"

def init_logging():
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.truncate()
    except:
        pass
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        encoding="utf-8",
        filemode="w"
    )
    logging.info("=" * 60)
    logging.info("B站监控系统启动 (24h 全天候模式)")
    logging.info("=" * 60)

def safe_request(url, params, header, retries=3):
    h = header.copy()
    h["Connection"] = "close"
    for i in range(retries):
        try:
            r = requests.get(url, headers=h, params=params, timeout=10)
            txt = r.text.strip()
            if txt:
                return r.json()
            time.sleep(2)
        except:
            time.sleep(2 + i)
    return {"code": -500, "message": "request failed after retries"}

WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}
mixinKeyEncTab = [
    46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,
    27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,
    37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,
    22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52
]

def getMixinKey(orig):
    return "".join([orig[i] for i in mixinKeyEncTab])[:32]

def encWbi(params, img_key, sub_key):
    mixin_key = getMixinKey(img_key + sub_key)
    params["wts"] = round(time.time())
    params = dict(sorted(params.items()))
    filtered = {}
    for k, v in params.items():
        v = str(v)
        for c in "!'()*":
            v = v.replace(c, "")
        filtered[k] = v
    query = urllib.parse.urlencode(filtered)
    sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
    filtered["w_rid"] = sign
    return filtered

def update_wbi_keys(header):
    try:
        data = safe_request(
            "https://api.bilibili.com/x/web-interface/nav",
            None,
            header
        )
        if data.get("code") == 0 and data["data"].get("wbi_img"):
            img = data["data"]["wbi_img"]
            WBI_KEYS["img_key"] = img["img_url"].rsplit("/", 1)[1].split(".")[0]
            WBI_KEYS["sub_key"] = img["sub_url"].rsplit("/", 1)[1].split(".")[0]
            WBI_KEYS["last_update"] = time.time()
            logging.info("WBI密钥已更新")
    except:
        pass

def wbi_request(url, params, header):
    if (not WBI_KEYS["img_key"]) or (time.time() - WBI_KEYS["last_update"] > 21600):
        update_wbi_keys(header)
    signed = encWbi(params.copy(), WBI_KEYS["img_key"], WBI_KEYS["sub_key"])
    return safe_request(url, signed, header)

def get_header():
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    except:
        subprocess.run([sys.executable, "login_bilibili.py"], check=True)
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    return {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.bilibili.com/"
    }

def is_work_time():
    return True

def get_latest_video(header):
    data = safe_request(
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
        {"host_mid": TARGET_UID},
        header
    )
    if data.get("code") != 0:
        return None
    for item in (data.get("data") or {}).get("items", []):
        try:
            if item.get("type") == "DYNAMIC_TYPE_AV":
                return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
        except:
            continue
    return None

def get_video_info(bv, header):
    data = safe_request(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bv}",
        None,
        header
    )
    if data.get("code") == 0:
        return str(data["data"]["aid"]), data["data"]["title"]
    return None, None

def sync_latest_video(header):
    bv = get_latest_video(header)
    if not bv:
        return None, None
    videos = db.get_monitored_videos()
    if videos and videos[0][1] == bv:
        return videos[0][0], videos[0][2]
    oid, title = get_video_info(bv, header)
    if oid:
        db.clear_videos()
        db.add_video_to_db(oid, bv, title)
        return oid, title
    return None, None

def deep_find_text(obj):
    result = []
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ["text", "content", "desc", "title", "words"]:
                    if isinstance(v, str) and v.strip():
                        result.append(v.strip())
                walk(v)
        elif isinstance(x, list):
            for i in x:
                walk(i)
    walk(obj)
    uniq = []
    for x in result:
        if x not in uniq:
            uniq.append(x)
    return " ".join(uniq).strip()

def extract_dynamic_text(item):
    try:
        modules = item.get("modules") or {}
        dyn = modules.get("module_dynamic") or {}
        rich_nodes = dyn.get("desc", {}).get("rich_text_nodes", [])
        if rich_nodes:
            txt = "".join(str(node.get("text", "")) for node in rich_nodes).strip()
            content = txt if txt else None
        else:
            content = None
        if not content:
            txt = deep_find_text(dyn)
            content = txt if txt else None
        if not content:
            content = json.dumps(item, ensure_ascii=False)
        if len(content) > 1500:
            content = content[:1500] + "\n\n...(内容已截断，确保通知成功)"
        return content
    except Exception as e:
        logging.error(f"提取动态文本异常: {e}\n{traceback.format_exc()}")
        return "发布了新动态 (内容解析失败)"

def fetch_latest(uid, header):
    data = safe_request(
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
        {"host_mid": uid},
        header
    )
    if data.get("code") != 0:
        return []
    return (data.get("data") or {}).get("items", [])

def check_new_dynamics(header, seen_dynamics):
    now_ts = time.time()
    newest = None
    newest_pub = 0
    for uid in EXTRA_DYNAMIC_UIDS:
        try:
            items = fetch_latest(uid, header)
            if uid not in seen_dynamics:
                seen_dynamics[uid] = set()
            for item in items:
                id_str = item.get("id_str")
                if not id_str or id_str in seen_dynamics[uid]:
                    continue
                try:
                    pub_ts = float(item.get("modules", {})
                                  .get("module_author", {})
                                  .get("pub_ts", 0))
                except:
                    pub_ts = 0
                if now_ts - pub_ts > DYNAMIC_MAX_AGE:
                    continue
                seen_dynamics[uid].add(id_str)
                if pub_ts > newest_pub:
                    newest_pub = pub_ts
                    text = extract_dynamic_text(item)
                    name = (item.get("modules") or {}) \
                           .get("module_author", {}) \
                           .get("name", str(uid))
                    newest = {"user": name, "message": f"{text}\n\n🔗 直达链接: https://t.bilibili.com/{id_str}"}
                time.sleep(random.uniform(0.5, 1.5))
        except Exception as e:
            logging.error(f"动态获取异常 UID={uid}: {e}\n{traceback.format_exc()}")
        time.sleep(random.uniform(5, 10))
    if newest:
        try:
            notifier.send_webhook_notification(
                "💡 特别关注 UP 主发布新内容",
                [newest]
            )
            logging.info("推送最新动态")
        except Exception as e:
            logging.error(f"Webhook 发送失败: {e}\n{traceback.format_exc()}")
        return True
    return False

def scan_new_comments(oid, header, last_read_time, seen):
    new_list = []
    max_ctime = last_read_time
    safe_time = last_read_time - 300
    pn = 1
    while pn <= 10:
        data = wbi_request(
            "https://api.bilibili.com/x/v2/reply",
            {
                "oid": oid,
                "type": 1,
                "sort": 0,
                "pn": pn,
                "ps": 20
            },
            header
        )
        replies = (data.get("data") or {}).get("replies") or []
        if not replies:
            break
        page_old = True
        for r in replies:
            rpid = r["rpid_str"]
            ctime = r["ctime"]
            max_ctime = max(max_ctime, ctime)
            if ctime > safe_time:
                page_old = False
                if rpid not in seen:
                    seen.add(rpid)
                    new_list.append({
                        "user": r["member"]["uname"],
                        "message": r["content"]["message"],
                        "ctime": ctime
                    })
        if page_old:
            break
        pn += 1
        time.sleep(random.uniform(0.5, 1))
    if not new_list:
        return None, max_ctime
    newest = max(new_list, key=lambda x: x["ctime"])
    return newest, max_ctime

def start_monitoring(header):
    now = time.time()
    last_v_check = now
    last_hb = now
    last_d_check = now + random.uniform(5, 15)        # 动态首次检查延迟
    burst_end = 0
    oid, title = sync_latest_video(header)
    seen_comments = set()
    seen_dynamics = {}
    logging.info("监控服务已启动，进入主循环…")
    while True:
        try:
            now = time.time()
            if is_work_time() and oid:
                newest_comment, new_t = scan_new_comments(oid, header, int(last_hb), seen_comments)
                if new_t > last_hb:
                    last_hb = new_t
                if newest_comment:
                    try:
                        notifier.send_webhook_notification(title, [newest_comment])
                    except Exception as e:
                        logging.error(f"评论通知发送失败: {e}\n{traceback.format_exc()}")
                interval = DYNAMIC_BURST_INTERVAL if now < burst_end else DYNAMIC_CHECK_INTERVAL
                if now - last_d_check >= interval:
                    if check_new_dynamics(header, seen_dynamics):
                        burst_end = now + DYNAMIC_BURST_DURATION
                    last_d_check = now
                if now - last_hb >= HEARTBEAT_INTERVAL:
                    try:
                        notifier.send_webhook_notification(
                            "心跳",
                            [{"user": "系统", "message": "正常运行中"}]
                        )
                    except:
                        pass
                    last_hb = now
                time.sleep(random.uniform(10, 15))
            else:
                time.sleep(30)
            if now - last_v_check > VIDEO_CHECK_INTERVAL:
                res = sync_latest_video(header)
                if res:
                    oid, title = res
                last_v_check = now
        except Exception:
            logging.error(traceback.format_exc())
            time.sleep(60)

if __name__ == "__main__":
    init_logging()
    db.init_db()
    h = get_header()
    update_wbi_keys(h)
    start_monitoring(h)
