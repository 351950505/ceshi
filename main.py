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

EXTRA_DYNAMIC_UIDS =[
    3546905852250875,
    3546961271589219,
    3546610447419885,
    285340365,
    3706948578969654
]

DYNAMIC_CHECK_INTERVAL = 30
DYNAMIC_BURST_INTERVAL = 10
DYNAMIC_BURST_DURATION = 300
DYNAMIC_MAX_AGE = 1800 

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
    logging.info("B站监控系统启动 (精准屏蔽UI系统词的纯净解析版)")
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

mixinKeyEncTab =[
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

    items = (data.get("data") or {}).get("items",[])

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


# ---------------- 动态（全新深度净化解析引擎） ----------------
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
            for item in (data.get("data") or {}).get("items",[]):
                if item.get("id_str"):
                    seen[uid].add(item["id_str"])
    return seen


def extract_dynamic_text(item):
    try:
        content_list =[]
        
        dyn_type = item.get("type", "")
        if dyn_type == "DYNAMIC_TYPE_FORWARD":
            content_list.append("【🔄 转发动态】")
        elif dyn_type == "DYNAMIC_TYPE_LIVE_RCMD":
            content_list.append("【🔴 直播推送】")
            
        modules = item.get("modules") or {}
        # 🌟 修复关键：提取且严格锁定真实内容模块，绝对不碰 module_more 等 UI 模块
        dyn = modules.get("module_dynamic") or {}
        
        def parse_rich(nodes):
            if not nodes or not isinstance(nodes, list): return ""
            return "".join([str(n.get("text", "")) for n in nodes if isinstance(n, dict)])

        # 1. 提取最外层正文
        desc = dyn.get("desc") or {}
        desc_text = parse_rich(desc.get("rich_text_nodes"))
        if not desc_text:
            desc_text = desc.get("text")
        if desc_text and str(desc_text).strip():
            content_list.append(str(desc_text).strip())

        # 2. 提取主体特殊内容 (major)
        major = dyn.get("major") or {}
        if major:
            m_type = major.get("type", "")
            
            if m_type == "MAJOR_TYPE_ARCHIVE":
                arc = major.get("archive") or {}
                if arc.get("title"): content_list.append(f"▶️ 视频: 《{arc.get('title')}》")
                if arc.get("desc"): content_list.append(f"📝 简介: {arc.get('desc')}")
                
            elif m_type == "MAJOR_TYPE_OPUS":
                opus = major.get("opus") or {}
                if opus.get("title"): content_list.append(f"📰 图文: 《{opus.get('title')}》")
                sum_dict = opus.get("summary") or {}
                sum_text = parse_rich(sum_dict.get("rich_text_nodes")) if isinstance(sum_dict, dict) else ""
                if not sum_text: 
                    sum_text = sum_dict.get("text", "") if isinstance(sum_dict, dict) else str(sum_dict)
                if sum_text and str(sum_text).strip(): 
                    content_list.append(f"📝 正文: {str(sum_text).strip()}")
                # Opus 的附图
                pics = opus.get("pics") or[]
                if pics: content_list.append(f"🖼️ [附图 {len(pics)} 张]")
                
            elif m_type == "MAJOR_TYPE_DRAW":
                draw = major.get("draw") or {}
                items = draw.get("items") or []
                if items: content_list.append(f"🖼️[共 {len(items)} 张图片]")
                
            elif m_type == "MAJOR_TYPE_ARTICLE":
                art = major.get("article") or {}
                if art.get("title"): content_list.append(f"📚 专栏: 《{art.get('title')}》")
                if art.get("desc"): content_list.append(f"📝 摘要: {art.get('desc')}")
                
            elif m_type == "MAJOR_TYPE_LIVE_RCMD":
                live = major.get("live_rcmd") or {}
                try:
                    live_json = json.loads(live.get("content", "{}"))
                    live_title = live_json.get("live_play_info", {}).get("title", "")
                    if live_title: content_list.append(f"🔴 直播间: {live_title}")
                except: pass
                
            elif m_type == "MAJOR_TYPE_COMMON":
                common = major.get("common") or {}
                if common.get("title"): content_list.append(f"📌 卡片: {common.get('title')}")
                if common.get("desc"): content_list.append(f"💬 内容: {common.get('desc')}")

        # 3. 提取被转发的原始动态
        orig = item.get("orig")
        orig_dyn = {}
        if orig:
            content_list.append("\n------ 被转发内容 ------")
            orig_author = orig.get("modules", {}).get("module_author", {}).get("name", "某用户")
            content_list.append(f"@{orig_author}:")
            
            orig_dyn = orig.get("modules", {}).get("module_dynamic") or {}
            o_desc = orig_dyn.get("desc") or {}
            o_desc_text = parse_rich(o_desc.get("rich_text_nodes"))
            if not o_desc_text: o_desc_text = o_desc.get("text")
            if o_desc_text and str(o_desc_text).strip():
                content_list.append(str(o_desc_text).strip())
            
            orig_major = orig_dyn.get("major") or {}
            o_m_type = orig_major.get("type", "")
            if o_m_type == "MAJOR_TYPE_ARCHIVE":
                content_list.append(f"▶️ 视频: 《{orig_major.get('archive', {}).get('title', '')}》")
            elif o_m_type == "MAJOR_TYPE_OPUS":
                content_list.append(f"📰 图文: 《{orig_major.get('opus', {}).get('title', '')}》")

        # 4. 净网行动：只针对真实动态部分 (dyn 和 orig_dyn) 进行安全扫描提取
        raw_content = "".join(content_list).replace("【🔄 转发动态】", "").replace("【🔴 直播推送】", "").replace("------ 被转发内容 ------", "").strip()
        
        if len(raw_content) < 2:  
            fallback_texts =[]
            def safe_deep_extract(obj):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        # 强行解开字符串包装的 JSON（B站老喜欢这么干）
                        if isinstance(v, str) and (v.startswith("{") or v.startswith("[")):
                            try: safe_deep_extract(json.loads(v))
                            except: pass
                        
                        if k in ["title", "text", "desc", "summary", "content"]:
                            if isinstance(v, str) and v.strip() and not v.startswith("http"):
                                # 强力过滤 B 站 UI 提示词
                                if not any(bad in v for bad in["删除动态", "无法恢复", "谨慎操作", "转发动态", "发布了"]):
                                    if v.strip() not in fallback_texts:
                                        fallback_texts.append(v.strip())
                    for v in obj.values():
                        if isinstance(v, (dict, list)):
                            safe_deep_extract(v)
                elif isinstance(obj, list):
                    for i in obj:
                        safe_deep_extract(i)
            
            # 🌟 修复关键：仅仅只扫真实的动态模块，抛弃了容易污染的 module_more 和 module_author！
            safe_deep_extract(dyn)
            if orig_dyn:
                safe_deep_extract(orig_dyn)
                
            if fallback_texts:
                content_list.append("\n".join(fallback_texts))
            else:
                content_list.append("【无文本纯分享动态，或为未知的新活动卡片】")

        final_text = "\n".join(content_list).strip()
        
        if len(final_text) > 1500:
            final_text = final_text[:1500] + "\n\n...(内容过长，已安全截断)"
            
        return final_text

    except Exception as e:
        logging.error(f"提取动态文本发生异常: {e}\n{traceback.format_exc()}")
        return "发布了新动态 (内容解析安全兜底)"


def check_new_dynamics(header, seen_dynamics):
    alerts =[]
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

            items = (data.get("data") or {}).get("items",[])

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

                name = author.get("name", str(uid))

                time_diff = now_ts - pub_ts
                if time_diff > DYNAMIC_MAX_AGE:
                    logging.info(f"⏭️ 忽略超时动态[{name}] ID:{id_str}, 距今 {int(time_diff)} 秒")
                    continue

                text = extract_dynamic_text(item)
                final_msg = f"{text}\n\n🔗 直达链接: https://t.bilibili.com/{id_str}"

                has_new = True
                alerts.append({
                    "user": name,
                    "message": final_msg
                })

                logging.info(f"✅ 抓取到新动态并准备推送 [{name}]:\n{final_msg}")
                break

        except Exception as e:
            logging.error(f"❌ 动态获取循环异常 {uid}: {e}\n{traceback.format_exc()}")

        time.sleep(random.uniform(1, 2))

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


# ---------------- 评论（加入 10 分钟回捞容错机制） ----------------
def scan_new_comments(oid, header, last_read_time, seen):
    new_list =[]
    max_ctime = last_read_time
    now_ts = int(time.time())

    safe_time = min(last_read_time - 300, now_ts - 600)

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

        replies = (data.get("data") or {}).get("replies") or[]

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

        if page_old and pn >= 3:
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

                if now - last_hb >= HEARTBEAT_INTERVAL:
                    try:
                        notifier.send_webhook_notification(
                            "心跳",[{
                                "user": "系统",
                                "message": "正常运行中"
                            }]
                        )
                    except Exception:
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
