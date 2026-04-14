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
    logging.info("B站监控系统启动")
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
    now = datetime.datetime.now(
        datetime.timezone.utc
    ) + datetime.timedelta(hours=8)

    return now.weekday() < 5 and 9 <= now.hour < 19


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


# ---------------- 动态（全新升级版） ----------------
def init_extra_dynamics(header):
    seen = {}

    for uid in EXTRA_DYNAMIC_UIDS:
        seen[uid] = set()

        data = safe_request(
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
            {"host_mid": uid},
            header
        )

        if data.get("code") == 0:
            for item in (data.get("data") or {}).get("items", []):
                if item.get("id_str"):
                    seen[uid].add(item["id_str"])

    return seen


def extract_dynamic_text(item):
    """
    终极版动态解析器：支持富文本、Opus图文、专栏、视频、转发、None Safe兜底
    """
    try:
        content_list = []
        
        # 1. 识别是否为转发 (Forward)
        dyn_type = item.get("type", "")
        if dyn_type == "DYNAMIC_TYPE_FORWARD":
            content_list.append("【🔄 转发动态】")

        modules = item.get("modules", {})
        module_dynamic = modules.get("module_dynamic", {})
        
        # 2. 提取外层正文 (desc) - 优先使用富文本节点保证完整性
        desc = module_dynamic.get("desc", {})
        rich_nodes = desc.get("rich_text_nodes", [])
        
        if rich_nodes:
            node_texts = []
            for node in rich_nodes:
                # 安全提取节点文字（包含普通文本、@、表情文字表达等）
                node_texts.append(str(node.get("text", "")))
            if node_texts:
                content_list.append("".join(node_texts).strip())
        else:
            # Fallback：没有富文本节点则取基础 text
            text = desc.get("text", "")
            if text:
                content_list.append(str(text).strip())

        # 3. 提取特殊主体内容 (major)
        major = module_dynamic.get("major", {})
        if major:
            major_type = major.get("type", "")
            
            # 🖼️ 老版图文 (DRAW)
            if major_type == "MAJOR_TYPE_DRAW":
                draw = major.get("draw", {})
                items = draw.get("items", [])
                if items:
                    content_list.append(f"\n[🖼️ 组图：共 {len(items)} 张]")
            
            # 📰 新版通用图文/文章 (OPUS)
            elif major_type == "MAJOR_TYPE_OPUS":
                opus = major.get("opus", {})
                title = opus.get("title", "")
                if title:
                    content_list.append(f"\n[📰 标题] 《{title}》")
                    
                summary = opus.get("summary", {})
                # B站有两套 Opus summary 格式，做安全兼容
                summary_text = summary.get("text", "") if isinstance(summary, dict) else opus.get("summary", "")
                if summary_text:
                    content_list.append(f"[📝 摘要] {summary_text}")
                    
                pics = opus.get("pics", [])
                if pics:
                    content_list.append(f"[🖼️ 附图：共 {len(pics)} 张]")
            
            # 📚 老版专栏 (ARTICLE)
            elif major_type == "MAJOR_TYPE_ARTICLE":
                article = major.get("article", {})
                title = article.get("title", "")
                article_desc = article.get("desc", "")
                if title:
                    content_list.append(f"\n[📚 专栏] 《{title}》")
                if article_desc:
                    content_list.append(str(article_desc))
            
            # 🎬 视频投稿 (ARCHIVE)
            elif major_type == "MAJOR_TYPE_ARCHIVE":
                archive = major.get("archive", {})
                title = archive.get("title", "")
                archive_desc = archive.get("desc", "")
                if title:
                    content_list.append(f"\n[🎬 投稿视频] 《{title}》")
                if archive_desc:
                    content_list.append(str(archive_desc))
                    
            # 🔴 直播推荐 (LIVE_RCMD)
            elif major_type == "MAJOR_TYPE_LIVE_RCMD":
                live = major.get("live_rcmd", {})
                try:
                    content = json.loads(live.get("content", "{}"))
                    live_play_info = content.get("live_play_info", {})
                    title = live_play_info.get("title", "")
                    if title:
                        content_list.append(f"\n[🔴 直播中] {title}")
                except:
                    pass

        # 4. 组装最终文本并过滤空行
        final_text = "\n".join([c for c in content_list if c]).strip()
        
        # 兜底 fallback
        if not final_text:
            final_text = "【无文本动态】发布了新内容（可能为纯分享或未知类型）"
            
        return final_text

    except Exception as e:
        # 绝对防御：0 crash
        logging.error(f"动态文本提取发生异常: {e}")
        return "【解析异常兜底】发布了新动态"


def check_new_dynamics(header, seen_dynamics):
    alerts = []
    has_new = False
    now_ts = time.time()

    for uid in EXTRA_DYNAMIC_UIDS:
        try:
            data = safe_request(
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
                {"host_mid": uid},
                header
            )

            if data.get("code") != 0:
                continue

            items = (data.get("data") or {}).get("items", [])

            for item in items:
                id_str = item.get("id_str")

                if not id_str:
                    continue

                if id_str in seen_dynamics[uid]:
                    continue

                seen_dynamics[uid].add(id_str)

                modules = item.get("modules") or {}
                author = modules.get("module_author") or {}

                try:
                    pub_ts = float(author.get("pub_ts", 0))
                except:
                    pub_ts = 0

                if now_ts - pub_ts > DYNAMIC_MAX_AGE:
                    continue

                name = author.get("name", str(uid))
                
                # 1. 抽取完整无损动态内容
                parsed_text = extract_dynamic_text(item)
                
                # 2. 组装“传送门模式” (Webhook 输出的最终 Message)
                portal_url = f"https://t.bilibili.com/{id_str}"
                final_message = f"{parsed_text}\n\n🔗 {portal_url}"
                
                has_new = True

                alerts.append({
                    "user": name,
                    "message": final_message
                })

                # 3. 完整的多行日志查看排版
                logging.info("-" * 50)
                logging.info(f"🆕 发现新动态 | UP主: {name}")
                logging.info(f"📝 完整内容:\n{final_message}")
                logging.info("-" * 50)

                break

        except Exception as e:
            logging.error(f"动态请求异常 {uid}: {traceback.format_exc()}")

        time.sleep(random.uniform(1, 2))

    if alerts:
        try:
            notifier.send_webhook_notification(
                "💡 特别关注UP主发布新内容",
                alerts
            )
        except:
            pass

    return has_new


# ---------------- 评论（稳定版：禁止修改） ----------------
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

    logging.info("监控服务已启动")

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
                        notifier.send_webhook_notification(
                            title,
                            new_c
                        )

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

                if now - last_hb >= HEARTBEAT_INTERVAL:
                    notifier.send_webhook_notification(
                        "心跳",
                        [{
                            "user": "系统",
                            "message": "正常运行中"
                        }]
                    )
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
