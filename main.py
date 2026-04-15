import sys
import os
import time
import datetime
import subprocess
import random
import logging
import traceback
import hashlib
import urllib.parse
import json
import requests

import database as db
import notifier

# ================= 核心配置区 =================
TARGET_UID = 1671203508
VIDEO_CHECK_INTERVAL = 21600
HEARTBEAT_INTERVAL = 10          # 心跳间隔10秒，改为日志输出

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
DYNAMIC_STATE_FILE = "dynamic_state.json"   # 保存每个UP主的baseline和offset
# ==============================================


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
    logging.info("B站监控系统启动 (24小时全天候监控模式)")
    logging.info("=" * 60)


def safe_request(url, params, header, retries=3):
    h = header.copy()
    h["Connection"] = "close"

    for i in range(retries):
        try:
            r = requests.get(
                url,
                headers=h,
                params=params,
                timeout=10
            )

            txt = r.text.strip()

            if not txt:
                time.sleep(2)
                continue

            return r.json()

        except:
            time.sleep(2 + i)

    return {"code": -500}


# ---------------- WBI ----------------
WBI_KEYS = {
    "img_key": "",
    "sub_key": "",
    "last_update": 0
}

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

        if data.get("code") == 0:
            img = data["data"]["wbi_img"]

            WBI_KEYS["img_key"] = img["img_url"].rsplit("/", 1)[1].split(".")[0]
            WBI_KEYS["sub_key"] = img["sub_url"].rsplit("/", 1)[1].split(".")[0]
            WBI_KEYS["last_update"] = time.time()

            logging.info("WBI密钥已更新")

    except:
        pass


def wbi_request(url, params, header):
    if (
        not WBI_KEYS["img_key"]
        or time.time() - WBI_KEYS["last_update"] > 21600
    ):
        update_wbi_keys(header)

    signed = encWbi(
        params.copy(),
        WBI_KEYS["img_key"],
        WBI_KEYS["sub_key"]
    )

    return safe_request(url, signed, header)


# ---------------- 基础 ----------------
def get_header():
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()
    except:
        subprocess.run([sys.executable, "login_bilibili.py"])

        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            cookie = f.read().strip()

    return {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.bilibili.com/"
    }


def is_work_time():
    # 已解除时间封印，强制 24H 全天候运行
    return True


# ---------------- 视频 ----------------
def get_latest_video(header):
    data = safe_request(
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
        {"host_mid": TARGET_UID},
        header
    )

    if data.get("code") != 0:
        return None

    items = (data.get("data") or {}).get("items", [])

    for item in items:
        try:
            if item.get("type") == "DYNAMIC_TYPE_AV":
                return item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
        except:
            pass

    return None


def get_video_info(bv, header):
    data = safe_request(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bv}",
        None,
        header
    )

    if data.get("code") == 0:
        return (
            str(data["data"]["aid"]),
            data["data"]["title"]
        )

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


# ---------------- 动态（优化版：增量拉取+精准文本提取+多动态处理） ----------------

def load_dynamic_state():
    """加载每个UP主的baseline和offset"""
    if os.path.exists(DYNAMIC_STATE_FILE):
        try:
            with open(DYNAMIC_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_dynamic_state(state):
    with open(DYNAMIC_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def extract_dynamic_text(item):
    """
    根据 all.md 文档结构精准提取动态正文
    优先级：rich_text_nodes > major > 空字符串（避免垃圾信息）
    """
    try:
        modules = item.get("modules") or {}
        dyn = modules.get("module_dynamic") or {}
        
        # 1. 从 desc.rich_text_nodes 拼接
        desc = dyn.get("desc") or {}
        nodes = desc.get("rich_text_nodes") or []
        if nodes:
            text_parts = []
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                node_type = node.get("type", "")
                # 提取所有可能携带文本的节点类型
                if node_type in ("RICH_TEXT_NODE_TYPE_TEXT", "RICH_TEXT_NODE_TYPE_TOPIC",
                                 "RICH_TEXT_NODE_TYPE_AT", "RICH_TEXT_NODE_TYPE_EMOJI"):
                    text_parts.append(node.get("text", ""))
                # 抽奖类型也尝试提取描述
                elif node_type == "RICH_TEXT_NODE_TYPE_LOTTERY":
                    text_parts.append(node.get("text", ""))
            full_text = "".join(text_parts).strip()
            if full_text:
                return full_text
        
        # 2. 没有富文本时，从 major 中提取（视频、专栏、图文等）
        major = dyn.get("major") or {}
        major_type = major.get("type", "")
        if major_type == "MAJOR_TYPE_ARCHIVE":
            archive = major.get("archive") or {}
            title = archive.get("title", "")
            desc_text = archive.get("desc", "")
            return f"【视频】{title}\n{desc_text}".strip()
        elif major_type == "MAJOR_TYPE_DRAW":
            # 图片动态通常正文已经在 desc 中，如果走到这里说明没有正文
            return ""
        elif major_type == "MAJOR_TYPE_ARTICLE":
            article = major.get("article") or {}
            title = article.get("title", "")
            return f"【专栏】{title}".strip()
        elif major_type == "MAJOR_TYPE_OPUS":
            opus = major.get("opus") or {}
            summary = opus.get("summary") or {}
            nodes = summary.get("rich_text_nodes") or []
            if nodes:
                return "".join([n.get("text", "") for n in nodes if isinstance(n, dict)]).strip()
        elif major_type == "MAJOR_TYPE_FORWARD":
            # 转发类型的内容在 item.orig 中，由调用方处理
            pass
        
        # 3. 兜底：返回空（避免推送无意义的JSON）
        return ""
    except Exception as e:
        logging.error(f"提取动态文本异常: {e}\n{traceback.format_exc()}")
        return ""

def init_extra_dynamics(header):
    """
    初始化 seen 集合和每个UP主的增量状态
    使用 /feed/all 接口获取最新的 update_baseline 和 offset
    """
    seen = {}
    state = load_dynamic_state()
    
    for uid in EXTRA_DYNAMIC_UIDS:
        uid_str = str(uid)
        seen[uid] = set()
        
        # 尝试获取或初始化状态
        if uid_str not in state:
            state[uid_str] = {"baseline": "", "offset": ""}
        
        # 拉取一次最新动态列表，用于获取 update_baseline 和初始化 seen（避免重复推送已存在的动态）
        try:
            params = {
                "host_mid": uid,
                "type": "all",
                "timezone_offset": "-480",
                "platform": "web",
                "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,decorationCard,onlyfansAssetsV2,forwardListHidden,ugcDelete",
                "web_location": "333.1365",
                "offset": ""
            }
            data = wbi_request(
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all",
                params,
                header
            )
            if data.get("code") == 0:
                feed_data = data.get("data") or {}
                new_baseline = feed_data.get("update_baseline", "")
                new_offset = feed_data.get("offset", "")
                # 只有当 new_baseline 非空字符串时才更新
                if new_baseline:
                    state[uid_str]["baseline"] = new_baseline
                if new_offset:
                    state[uid_str]["offset"] = new_offset
                # 将当前列表中的所有动态id加入seen，避免重复推送
                for item in feed_data.get("items", []):
                    dyn_id = item.get("id_str")
                    if dyn_id:
                        seen[uid].add(dyn_id)
                logging.info(f"初始化 UP {uid} 状态: baseline={state[uid_str]['baseline']}, offset={state[uid_str]['offset']}, 已收录 {len(seen[uid])} 条动态")
            else:
                logging.warning(f"初始化 UP {uid} 失败: {data.get('message')}")
        except Exception as e:
            logging.error(f"初始化 UP {uid} 异常: {e}\n{traceback.format_exc()}")
        
        time.sleep(random.uniform(0.5, 1))
    
    save_dynamic_state(state)
    return seen

def fetch_dynamics_page(uid, offset, header):
    """拉取一页动态（不传 update_baseline）"""
    params = {
        "host_mid": uid,
        "type": "all",
        "timezone_offset": "-480",
        "platform": "web",
        "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,decorationCard,onlyfansAssetsV2,forwardListHidden,ugcDelete",
        "web_location": "333.1365",
        "offset": offset
    }
    return wbi_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all", params, header)

def check_new_dynamics(header, seen_dynamics):
    """
    使用增量接口检测并拉取新动态，处理多条新动态，支持转发递归提取
    修复：
      1. update_baseline 参数仅在非空时传递，避免API报错
      2. 当 baseline 为空时，不使用检测接口，直接拉取并尝试建立基线
      3. 去除超时动态的日志输出（改为debug级别，默认不显示）
    """
    alerts = []
    has_new = False
    now_ts = time.time()
    
    state = load_dynamic_state()
    updated = False
    
    for uid in EXTRA_DYNAMIC_UIDS:
        uid_str = str(uid)
        current_state = state.get(uid_str, {"baseline": "", "offset": ""})
        baseline = current_state.get("baseline", "")
        offset = current_state.get("offset", "")
        
        # 情况1：baseline 非空，正常增量模式
        if baseline:
            # 检测是否有新动态
            try:
                update_params = {"type": "all", "web_location": "333.1365"}
                update_params["update_baseline"] = baseline
                update_data = wbi_request(
                    "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all/update",
                    update_params,
                    header
                )
                if update_data.get("code") != 0:
                    logging.warning(f"UP {uid} 检测更新失败: {update_data.get('message')}")
                    # 检测失败时，直接拉取（带 baseline）尝试
                else:
                    update_num = update_data.get("data", {}).get("update_num", 0)
                    if update_num == 0:
                        continue  # 无新动态
            except Exception as e:
                logging.error(f"UP {uid} 检测更新异常: {e}")
                # 继续拉取
            
            # 拉取增量动态（带 update_baseline）
            try:
                fetch_params = {
                    "host_mid": uid,
                    "type": "all",
                    "timezone_offset": "-480",
                    "platform": "web",
                    "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,decorationCard,onlyfansAssetsV2,forwardListHidden,ugcDelete",
                    "web_location": "333.1365",
                    "offset": offset,
                    "update_baseline": baseline
                }
                data = wbi_request(
                    "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all",
                    fetch_params,
                    header
                )
                if data.get("code") != 0:
                    logging.warning(f"UP {uid} 拉取动态失败: {data.get('message')}")
                    continue
                
                feed_data = data.get("data") or {}
                items = feed_data.get("items", [])
                new_baseline = feed_data.get("update_baseline", baseline)
                new_offset = feed_data.get("offset", offset)
                
                # 更新状态
                if new_baseline != baseline or new_offset != offset:
                    state[uid_str] = {"baseline": new_baseline, "offset": new_offset}
                    updated = True
                    logging.info(f"UP {uid} 状态更新: baseline={new_baseline}, offset={new_offset}")
                
                # 处理新动态
                for item in items:
                    dyn_id = item.get("id_str")
                    if not dyn_id or dyn_id in seen_dynamics[uid]:
                        continue
                    seen_dynamics[uid].add(dyn_id)
                    # 超时过滤（仅 debug 日志）
                    modules = item.get("modules") or {}
                    author = modules.get("module_author") or {}
                    pub_ts = author.get("pub_ts", 0)
                    if now_ts - pub_ts > DYNAMIC_MAX_AGE:
                        logging.debug(f"忽略超时动态 [{author.get('name', uid)}] ID:{dyn_id}, 距今 {int(now_ts - pub_ts)} 秒")
                        continue
                    
                    name = author.get("name", str(uid))
                    text = extract_dynamic_text(item)
                    
                    # 处理转发动态
                    if item.get("type") == "DYNAMIC_TYPE_FORWARD":
                        orig = item.get("orig")
                        if orig:
                            orig_text = extract_dynamic_text(orig)
                            if orig_text:
                                if text:
                                    text = f"{text}\n【转发原文】{orig_text}"
                                else:
                                    text = f"【转发原文】{orig_text}"
                            orig_id = orig.get("id_str")
                            if orig_id:
                                text = f"{text}\n【原动态链接】https://t.bilibili.com/{orig_id}"
                    
                    final_msg = f"{text}\n\n🔗 直达链接: https://t.bilibili.com/{dyn_id}" if text else f"🔗 直达链接: https://t.bilibili.com/{dyn_id}"
                    alerts.append({"user": name, "message": final_msg})
                    has_new = True
                    logging.info(f"✅ 抓取到新动态 [{name}]: {dyn_id}")
            except Exception as e:
                logging.error(f"UP {uid} 处理动态异常: {e}\n{traceback.format_exc()}")
        
        # 情况2：baseline 为空，需要先建立基线（拉取最新一页，处理新动态，并尝试获取 baseline）
        else:
            logging.info(f"UP {uid} baseline 为空，尝试建立基线...")
            try:
                # 直接拉取第一页（不带 update_baseline）
                data = fetch_dynamics_page(uid, "", header)
                if data.get("code") != 0:
                    logging.warning(f"UP {uid} 建立基线失败: {data.get('message')}")
                    continue
                
                feed_data = data.get("data") or {}
                items = feed_data.get("items", [])
                new_baseline = feed_data.get("update_baseline", "")
                new_offset = feed_data.get("offset", "")
                
                # 更新状态（只有当 new_baseline 非空时才保存，否则只保存 offset）
                if new_baseline:
                    state[uid_str] = {"baseline": new_baseline, "offset": new_offset}
                    updated = True
                    logging.info(f"UP {uid} 基线建立成功: baseline={new_baseline}, offset={new_offset}")
                else:
                    # 如果还是没有 baseline，至少保存 offset，下次继续尝试
                    if new_offset:
                        state[uid_str]["offset"] = new_offset
                        updated = True
                        logging.info(f"UP {uid} offset 更新为 {new_offset}，但 baseline 仍为空，下次继续尝试")
                    else:
                        logging.warning(f"UP {uid} 无法获取 baseline 和 offset，跳过本次")
                        continue
                
                # 处理当前页的所有动态（视为新动态？谨慎：为了避免推送大量旧动态，需要检查时间）
                # 注意：这里 items 可能包含大量旧动态，我们只处理距离现在不超过 DYNAMIC_MAX_AGE 的
                for item in items:
                    dyn_id = item.get("id_str")
                    if not dyn_id or dyn_id in seen_dynamics[uid]:
                        continue
                    seen_dynamics[uid].add(dyn_id)
                    modules = item.get("modules") or {}
                    author = modules.get("module_author") or {}
                    pub_ts = author.get("pub_ts", 0)
                    if now_ts - pub_ts > DYNAMIC_MAX_AGE:
                        logging.debug(f"基线建立时忽略超时动态 [{author.get('name', uid)}] ID:{dyn_id}")
                        continue
                    name = author.get("name", str(uid))
                    text = extract_dynamic_text(item)
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
                    alerts.append({"user": name, "message": final_msg})
                    has_new = True
                    logging.info(f"✅ 基线建立时抓取到近期动态 [{name}]: {dyn_id}")
            except Exception as e:
                logging.error(f"UP {uid} 建立基线异常: {e}\n{traceback.format_exc()}")
        
        time.sleep(random.uniform(1, 2))
    
    if updated:
        save_dynamic_state(state)
    
    if alerts:
        try:
            notifier.send_webhook_notification(
                "💡 特别关注UP主发布新内容",
                alerts
            )
            logging.info(f"🚀 成功发送 {len(alerts)} 条 Webhook 动态通知！")
        except Exception as e:
            logging.error(f"❌ Webhook 发送失败: {e}\n{traceback.format_exc()}")
    
    return has_new


# ---------------- 评论 ----------------
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

    return new_list, max_ctime


# ---------------- 主循环 ----------------
def start_monitoring(header):
    last_v_check = 0
    last_hb = time.time()
    last_d_check = 0
    burst_end = 0

    oid, title = sync_latest_video(header)

    last_read_time = int(time.time())
    seen_comments = set()
    seen_dynamics = init_extra_dynamics(header)

    logging.info("监控服务已启动，正在扫描新数据...")

    while True:
        try:
            now = time.time()

            if is_work_time():

                if oid:
                    new_c, new_t = scan_new_comments(
                        oid,
                        header,
                        last_read_time,
                        seen_comments
                    )

                    if new_t > last_read_time:
                        last_read_time = new_t

                    if new_c:
                        new_c.sort(key=lambda x: x["ctime"])
                        try:
                            notifier.send_webhook_notification(
                                title,
                                new_c
                            )
                        except Exception as e:
                            logging.error(f"评论通知发送失败: {e}\n{traceback.format_exc()}")

                interval = (
                    DYNAMIC_BURST_INTERVAL
                    if now < burst_end
                    else DYNAMIC_CHECK_INTERVAL
                )

                if now - last_d_check >= interval:
                    if check_new_dynamics(
                        header,
                        seen_dynamics
                    ):
                        burst_end = now + DYNAMIC_BURST_DURATION

                    last_d_check = now

                # 心跳：改为仅日志输出，不再发送webhook
                if now - last_hb >= HEARTBEAT_INTERVAL:
                    logging.info("💓 心跳: 监控系统正常运行中")
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
