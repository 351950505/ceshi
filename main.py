import sys
import os
import time
import json
import random
import logging
import traceback
import hashlib
import urllib.parse
import subprocess
import requests

import notifier
import database as db

# ==========================================================
# 配置区
# ==========================================================
SOURCE_UID = 3706948578969654          # 用于获取关注列表的 UID
FALLBACK_DYNAMIC_UIDS = [
    3546905852250875,
    3546961271589219,
    3546610447419885,
    285340365,
    3706948578969654
]

# 间隔配置
DYNAMIC_CHECK_INTERVAL = 15            # 动态扫描间隔（秒）
FOLLOWING_REFRESH_INTERVAL = 3600      # 关注列表刷新间隔（秒）
VIDEO_CHECK_INTERVAL = 21600           # 视频检查间隔（秒）
COMMENT_SCAN_INTERVAL = 5              # 评论扫描间隔（秒）

# 时间补偿（服务器快 2 分钟）
TIME_OFFSET = -120

# 动态最大有效时间（秒），超过此时间的动态将被忽略（避免推送过于陈旧的动态）
DYNAMIC_MAX_AGE = 300                  # 5分钟，可根据需要调整

# 评论扫描参数
COMMENT_MAX_PAGES = 3
COMMENT_SAFE_WINDOW = 60

LOG_FILE = "bili_monitor.log"
STATE_FILE = "dynamic_state.json"      # 仅用于保存 seen 集合（可选）
FOLLOW_FILE = "following_cache.json"

# ==========================================================
# 日志初始化
# ==========================================================
def init_logging():
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        encoding="utf-8",
        filemode="w"
    )
    logging.info("=" * 60)
    logging.info("B站监控系统 (动态简化版 - 基于ID对比)")
    logging.info("=" * 60)

# ==========================================================
# Cookie 管理
# ==========================================================
def refresh_cookie():
    logging.warning("Cookie失效，尝试重新登录...")
    try:
        subprocess.run([sys.executable, "login_bilibili.py"], check=True)
        logging.info("Cookie刷新成功")
        return True
    except Exception as e:
        logging.error(f"Cookie刷新失败: {e}")
        return False

def get_header():
    if not os.path.exists("bili_cookie.txt"):
        refresh_cookie()
    with open("bili_cookie.txt", "r", encoding="utf-8") as f:
        cookie = f.read().strip()
    return {
        "Cookie": cookie,
        "Referer": "https://www.bilibili.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Connection": "close"
    }

# ==========================================================
# 请求层（带风控处理和自动重试）
# ==========================================================
def safe_request(url, params=None, header=None, retry=3):
    for i in range(retry):
        try:
            r = requests.get(url, params=params, headers=header, timeout=10)
            if not r.text.strip():
                time.sleep(2)
                continue
            data = r.json()
            code = data.get("code", -999)
            if code == 0:
                return data
            if code == -101:
                logging.error("API返回-101，尝试刷新Cookie")
                if refresh_cookie():
                    return safe_request(url, params, get_header(), retry)
                return data
            if code in (-352, -799, -509):
                wait = (2 ** i) + random.uniform(1, 3)
                logging.warning(f"触发风控 {code}，等待 {wait:.1f}s 后重试")
                time.sleep(wait)
                continue
            # 其他错误码直接返回
            return data
        except Exception as e:
            logging.error(f"请求异常: {e}")
            time.sleep(2)
    return {"code": -500}

# ==========================================================
# WBI 签名（含时间补偿）
# ==========================================================
WBI_KEYS = {"img": "", "sub": "", "time": 0}
mixinKeyEncTab = [
    46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,
    27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,
    37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,
    22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52
]

def mixin(orig):
    return ''.join([orig[i] for i in mixinKeyEncTab])[:32]

def update_wbi(header):
    data = safe_request("https://api.bilibili.com/x/web-interface/nav", header=header)
    if data.get("code") == 0:
        img = data["data"]["wbi_img"]
        WBI_KEYS["img"] = img["img_url"].split("/")[-1].split(".")[0]
        WBI_KEYS["sub"] = img["sub_url"].split("/")[-1].split(".")[0]
        WBI_KEYS["time"] = time.time()
        logging.info("WBI密钥已更新")

def sign(params):
    if time.time() - WBI_KEYS["time"] > 21600:
        update_wbi(get_header())
    key = mixin(WBI_KEYS["img"] + WBI_KEYS["sub"])
    params["wts"] = int(time.time() + TIME_OFFSET)
    params = dict(sorted(params.items()))
    query = urllib.parse.urlencode(params)
    params["w_rid"] = hashlib.md5((query + key).encode()).hexdigest()
    return params

def wbi_request(url, params, header):
    return safe_request(url, sign(params), header)

# ==========================================================
# 文件缓存工具
# ==========================================================
def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return default
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ==========================================================
# 关注列表获取
# ==========================================================
def get_followings(uid, header):
    result = []
    pn = 1
    while True:
        data = safe_request(
            "https://api.bilibili.com/x/relation/followings",
            {"vmid": uid, "pn": pn, "ps": 50},
            header
        )
        if data.get("code") != 0:
            break
        items = data.get("data", {}).get("list", [])
        if not items:
            break
        for x in items:
            mid = x.get("mid")
            if mid:
                result.append(mid)
        pn += 1
        time.sleep(random.uniform(0.5, 1))
    logging.info(f"获取关注列表完成，共 {len(result)} 人")
    return result

# ==========================================================
# 动态接口（直接拉取第一页）
# ==========================================================
def fetch_dynamic(uid, header):
    """拉取指定用户的第一页动态（最多20条）"""
    return wbi_request(
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all",
        {"host_mid": uid, "type": "all", "offset": ""},
        header
    )

def extract_dynamic_text(item):
    """从动态item中提取可读文本"""
    try:
        modules = item.get("modules", {})
        dyn = modules.get("module_dynamic", {})
        desc = dyn.get("desc", {})
        nodes = desc.get("rich_text_nodes", [])
        if nodes:
            text_parts = []
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                node_type = node.get("type", "")
                if node_type in ("RICH_TEXT_NODE_TYPE_TEXT", "RICH_TEXT_NODE_TYPE_TOPIC",
                                 "RICH_TEXT_NODE_TYPE_AT", "RICH_TEXT_NODE_TYPE_EMOJI"):
                    text_parts.append(node.get("text", ""))
                elif node_type == "RICH_TEXT_NODE_TYPE_LOTTERY":
                    text_parts.append(node.get("text", ""))
            full_text = "".join(text_parts).strip()
            if full_text:
                return full_text
        major = dyn.get("major", {})
        major_type = major.get("type", "")
        if major_type == "MAJOR_TYPE_ARCHIVE":
            archive = major.get("archive", {})
            title = archive.get("title", "")
            return f"【视频】{title}"
        elif major_type == "MAJOR_TYPE_ARTICLE":
            article = major.get("article", {})
            title = article.get("title", "")
            return f"【专栏】{title}"
        elif major_type == "MAJOR_TYPE_OPUS":
            return "【图文动态】"
        else:
            return "发布了新动态"
    except Exception as e:
        logging.error(f"提取文本异常: {e}")
        return "发布了新动态"

# ==========================================================
# 视频监控
# ==========================================================
def get_latest_video(header):
    data = safe_request(
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
        {"host_mid": SOURCE_UID},
        header
    )
    if data.get("code") != 0:
        return None
    items = data.get("data", {}).get("items", [])
    for item in items:
        if item.get("type") == "DYNAMIC_TYPE_AV":
            try:
                return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
            except:
                continue
    return None

def get_video_info(bv, header):
    data = safe_request(f"https://api.bilibili.com/x/web-interface/view?bvid={bv}", None, header)
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

# ==========================================================
# 评论监控（保持原样，稳定版本）
# ==========================================================
def scan_new_comments(oid, header, last_read_time, seen):
    new_list = []
    max_ctime = last_read_time
    safe_time = last_read_time - COMMENT_SAFE_WINDOW
    for pn in range(1, COMMENT_MAX_PAGES + 1):
        params = {"oid": oid, "type": 1, "sort": 0, "pn": pn, "ps": 20}
        data = wbi_request("https://api.bilibili.com/x/v2/reply", params, header)
        if data.get("code") != 0:
            logging.warning(f"评论接口第{pn}页返回错误: {data.get('code')}")
            break
        replies = data.get("data", {}).get("replies") or []
        if not replies:
            break
        all_old = True
        for r in replies:
            ctime = r.get("ctime", 0)
            if ctime > max_ctime:
                max_ctime = ctime
            if ctime > safe_time:
                all_old = False
                rpid = r.get("rpid_str")
                if rpid and rpid not in seen:
                    seen.add(rpid)
                    new_list.append({
                        "user": r["member"]["uname"],
                        "message": r["content"]["message"],
                        "ctime": ctime
                    })
        if all_old:
            break
        time.sleep(random.uniform(0.3, 0.6))
    return new_list, max_ctime

# ==========================================================
# 主循环
# ==========================================================
def start_monitor():
    header = get_header()
    update_wbi(header)

    last_v_check = 0
    last_comment_check = 0
    last_follow = 0
    last_hb = time.time()

    # 视频和评论初始化
    oid, title = sync_latest_video(header)
    last_read_time = int(time.time())
    seen_comments = set()

    # 获取关注列表
    follow_list = load_json(FOLLOW_FILE, [])
    if not follow_list:
        follow_list = get_followings(SOURCE_UID, header)
        if not follow_list:
            follow_list = FALLBACK_DYNAMIC_UIDS
        save_json(FOLLOW_FILE, follow_list)
    if SOURCE_UID not in follow_list:
        follow_list.append(SOURCE_UID)
    logging.info(f"监控UID数量: {len(follow_list)}")

    # 动态 seen 集合初始化（首次拉取，只记录ID，不推送）
    seen_dyn = {}
    for uid in follow_list:
        seen_dyn[uid] = set()
        data = fetch_dynamic(uid, header)
        if data.get("code") == 0:
            items = data.get("data", {}).get("items", [])
            for item in items:
                dyn_id = item.get("id_str")
                if dyn_id:
                    seen_dyn[uid].add(dyn_id)
            logging.info(f"UID {uid} 初始化完成，已收录 {len(seen_dyn[uid])} 条动态")
        else:
            logging.warning(f"UID {uid} 初始化失败: {data.get('message')}")
        time.sleep(random.uniform(0.5, 1))

    logging.info("监控服务启动完成，开始轮询...")

    while True:
        try:
            now = time.time()
            adjusted_now = now + TIME_OFFSET   # 补偿后的时间戳

            # 1. 定时刷新关注列表（每小时）
            if now - last_follow > FOLLOWING_REFRESH_INTERVAL:
                new_follow = get_followings(SOURCE_UID, header)
                if new_follow:
                    if SOURCE_UID not in new_follow:
                        new_follow.append(SOURCE_UID)
                    # 检查是否有新增或移除
                    old_set = set(follow_list)
                    new_set = set(new_follow)
                    added = new_set - old_set
                    removed = old_set - new_set
                    if added or removed:
                        logging.info(f"关注列表变化: 新增 {len(added)}，移除 {len(removed)}")
                        for uid in added:
                            seen_dyn[uid] = set()
                        for uid in removed:
                            if uid in seen_dyn:
                                del seen_dyn[uid]
                        follow_list = new_follow
                        save_json(FOLLOW_FILE, follow_list)
                        # 为新 UID 初始化 seen
                        for uid in added:
                            data = fetch_dynamic(uid, header)
                            if data.get("code") == 0:
                                items = data.get("data", {}).get("items", [])
                                for item in items:
                                    dyn_id = item.get("id_str")
                                    if dyn_id:
                                        seen_dyn[uid].add(dyn_id)
                                logging.info(f"新UID {uid} 初始化完成，收录 {len(seen_dyn[uid])} 条动态")
                    else:
                        logging.info("关注列表无变化")
                else:
                    logging.warning("刷新关注列表失败")
                last_follow = now

            # 2. 评论监控
            if oid and (now - last_comment_check > COMMENT_SCAN_INTERVAL):
                new_c, new_t = scan_new_comments(oid, header, last_read_time, seen_comments)
                if new_t > last_read_time:
                    last_read_time = new_t
                if new_c:
                    new_c.sort(key=lambda x: x["ctime"])
                    try:
                        notifier.send_webhook_notification(title, new_c)
                        logging.info(f"💬 成功发送 {len(new_c)} 条新评论通知")
                    except Exception as e:
                        logging.error(f"评论通知发送失败: {e}")
                last_comment_check = now

            # 3. 动态监控（核心：直接拉取最新动态，对比ID）
            # 每 DYNAMIC_CHECK_INTERVAL 秒扫描一次所有关注用户
            if int(now) % DYNAMIC_CHECK_INTERVAL < 2:   # 近似每15秒执行一次，避免使用last_d_check变量
                # 为了更精确，建议使用 last_d_check 变量，但为简化，使用取模方式
                # 这里改用准确的时间判断（下面会修正）
                pass

            # 更可靠的方式：使用 last_d_check 变量
            # 但为了代码简洁，我们直接在主循环中每次循环都检查所有用户？不，那样频率太高。
            # 这里恢复使用 last_d_check 变量。
            # 下面重新实现带 last_d_check 的版本。
            # 由于上面没有定义 last_d_check，我们在循环外部定义。
            # 为保持代码完整性，重新整理一下循环。

            # 实际上，为了清晰，我将把主循环重新写一下，包含 last_d_check。
            # 由于当前函数内没有 last_d_check，我们在此处补上（但代码已经运行，需要调整）。
            # 为了不破坏结构，我们直接用下面的方式（在循环外部定义 last_d_check）。
            # 由于代码是顺序执行的，我们可以提前定义 last_d_check 变量。
            # 注意：下面的代码实际上不会被执行，因为函数已经开始了。
            # 所以需要重新整理整个 start_monitor 函数。
            # 为避免混乱，我将直接给出一个完整的 start_monitor 函数，包含 last_d_check。
            # 但为了节省篇幅，我将在最终代码中给出完整版。
            # 这里只是说明，最终提供的代码会包含所有必要的变量。

            # 为了避免解析错误，我在最终代码中会包含正确的 last_d_check 逻辑。
            # 请直接使用最终提供的完整代码。

        except Exception as e:
            logging.error(traceback.format_exc())
            time.sleep(10)

# 由于上面的 start_monitor 函数缺少 last_d_check 和动态扫描的完整逻辑，下面提供修正后的完整版本。
# 我会重新整理整个文件，确保动态扫描部分正确。

# ==========================================================
# 重新整理 start_monitor（包含动态扫描和 last_d_check）
# ==========================================================
def start_monitor_fixed():
    header = get_header()
    update_wbi(header)

    last_v_check = 0
    last_comment_check = 0
    last_d_check = 0
    last_follow = 0
    last_hb = time.time()

    oid, title = sync_latest_video(header)
    last_read_time = int(time.time())
    seen_comments = set()

    # 关注列表
    follow_list = load_json(FOLLOW_FILE, [])
    if not follow_list:
        follow_list = get_followings(SOURCE_UID, header)
        if not follow_list:
            follow_list = FALLBACK_DYNAMIC_UIDS
        save_json(FOLLOW_FILE, follow_list)
    if SOURCE_UID not in follow_list:
        follow_list.append(SOURCE_UID)
    logging.info(f"监控UID数量: {len(follow_list)}")

    # 动态 seen 集合初始化
    seen_dyn = {}
    for uid in follow_list:
        seen_dyn[uid] = set()
        data = fetch_dynamic(uid, header)
        if data.get("code") == 0:
            items = data.get("data", {}).get("items", [])
            for item in items:
                dyn_id = item.get("id_str")
                if dyn_id:
                    seen_dyn[uid].add(dyn_id)
            logging.info(f"UID {uid} 初始化完成，已收录 {len(seen_dyn[uid])} 条动态")
        else:
            logging.warning(f"UID {uid} 初始化失败: {data.get('message')}")
        time.sleep(random.uniform(0.5, 1))

    logging.info("监控服务启动完成，开始轮询...")

    while True:
        try:
            now = time.time()
            adjusted_now = now + TIME_OFFSET

            # 刷新关注列表
            if now - last_follow > FOLLOWING_REFRESH_INTERVAL:
                new_follow = get_followings(SOURCE_UID, header)
                if new_follow:
                    if SOURCE_UID not in new_follow:
                        new_follow.append(SOURCE_UID)
                    old_set = set(follow_list)
                    new_set = set(new_follow)
                    added = new_set - old_set
                    removed = old_set - new_set
                    if added or removed:
                        logging.info(f"关注列表变化: 新增 {len(added)}，移除 {len(removed)}")
                        for uid in added:
                            seen_dyn[uid] = set()
                        for uid in removed:
                            if uid in seen_dyn:
                                del seen_dyn[uid]
                        follow_list = new_follow
                        save_json(FOLLOW_FILE, follow_list)
                        for uid in added:
                            data = fetch_dynamic(uid, header)
                            if data.get("code") == 0:
                                items = data.get("data", {}).get("items", [])
                                for item in items:
                                    dyn_id = item.get("id_str")
                                    if dyn_id:
                                        seen_dyn[uid].add(dyn_id)
                                logging.info(f"新UID {uid} 初始化完成，收录 {len(seen_dyn[uid])} 条动态")
                    else:
                        logging.info("关注列表无变化")
                else:
                    logging.warning("刷新关注列表失败")
                last_follow = now

            # 评论监控
            if oid and (now - last_comment_check > COMMENT_SCAN_INTERVAL):
                new_c, new_t = scan_new_comments(oid, header, last_read_time, seen_comments)
                if new_t > last_read_time:
                    last_read_time = new_t
                if new_c:
                    new_c.sort(key=lambda x: x["ctime"])
                    try:
                        notifier.send_webhook_notification(title, new_c)
                        logging.info(f"💬 成功发送 {len(new_c)} 条新评论通知")
                    except Exception as e:
                        logging.error(f"评论通知发送失败: {e}")
                last_comment_check = now

            # 动态监控（每15秒一次）
            if now - last_d_check >= DYNAMIC_CHECK_INTERVAL:
                logging.info(f"开始动态扫描（间隔 {DYNAMIC_CHECK_INTERVAL} 秒）")
                all_alerts = []
                for uid in follow_list:
                    try:
                        data = fetch_dynamic(uid, header)
                        if data.get("code") != 0:
                            logging.warning(f"UID {uid} 拉取动态失败: {data.get('message')}")
                            continue
                        items = data.get("data", {}).get("items", [])
                        if not items:
                            continue
                        # 按顺序处理，最新的在前面
                        for item in items:
                            dyn_id = item.get("id_str")
                            if not dyn_id or dyn_id in seen_dyn[uid]:
                                continue
                            # 检查发布时间是否超时
                            pub_ts = item.get("modules", {}).get("module_author", {}).get("pub_ts", 0)
                            if adjusted_now - pub_ts > DYNAMIC_MAX_AGE:
                                logging.debug(f"忽略超时动态 {dyn_id} (发布 {adjusted_now - pub_ts:.0f} 秒前)")
                                continue
                            # 提取文本和用户信息
                            name = item.get("modules", {}).get("module_author", {}).get("name", str(uid))
                            text = extract_dynamic_text(item)
                            # 处理转发动态
                            if item.get("type") == "DYNAMIC_TYPE_FORWARD":
                                orig = item.get("orig")
                                if orig:
                                    orig_text = extract_dynamic_text(orig)
                                    if orig_text:
                                        text = f"{text}\n【转发原文】{orig_text}" if text else f"【转发原文】{orig_text}"
                                    orig_id = orig.get("id_str")
                                    if orig_id:
                                        text = f"{text}\n【原动态链接】https://t.bilibili.com/{orig_id}"
                            final_msg = f"{text}\n\n🔗 直达链接: https://t.bilibili.com/{dyn_id}" if text else f"🔗 直达链接: https://t.bilibili.com/{dyn_id}"
                            all_alerts.append({"user": name, "message": final_msg})
                            seen_dyn[uid].add(dyn_id)
                            logging.info(f"✅ 抓取到新动态 [{name}]: {dyn_id}")
                    except Exception as e:
                        logging.error(f"UID {uid} 动态检查异常: {e}\n{traceback.format_exc()}")
                    time.sleep(random.uniform(0.3, 0.6))  # 用户间延迟
                if all_alerts:
                    try:
                        notifier.send_webhook_notification("💡 特别关注UP主发布新内容", all_alerts)
                        logging.info(f"🚀 成功发送 {len(all_alerts)} 条动态通知")
                    except Exception as e:
                        logging.error(f"动态通知发送失败: {e}")
                else:
                    logging.info("本次动态扫描未发现新动态")
                last_d_check = now

            # 视频检查
            if now - last_v_check > VIDEO_CHECK_INTERVAL:
                res = sync_latest_video(header)
                if res:
                    oid, title = res
                last_v_check = now

            # 心跳
            if now - last_hb > 60:
                logging.info("💓 心跳: 监控系统正常运行中")
                last_hb = now

            time.sleep(random.uniform(2, 4))

        except Exception:
            logging.error(traceback.format_exc())
            time.sleep(10)

# 使用修正后的主函数
if __name__ == "__main__":
    init_logging()
    db.init_db()
    start_monitor_fixed()
