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
    logging.info("B站监控系统启动 (24小时全天候监控模式)")
    logging.info("=" * 60)

def safe_request(url, params, header, retries=3):
    h = header.copy()
    h["Connection"] = "close"
    for i in range(retries):
        try:
            r = requests.get(
                url, headers=h, params=params, timeout=10
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
    if (not WBI_KEYS["img_key"] or time.time() - WBI_KEYS["last_update"] > 21600):
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
    # 💥 已解除时间封印，强制 24H 全天候运行！
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

# ---------------- 动态（终极防崩版） ----------------
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

def deep_find_text(obj):
    """兜底深度搜索"""
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
    """
    🔴 终极防崩版动态提取
    策略：多重 Try-Except 包裹，优先提取富文本，其次提取纯文本，最后兜底 JSON
    """
    try:
        # --- 第一步：基础安全检查 ---
        if not item or not isinstance(item, dict):
            return "【错误】动态数据为空或格式异常"
            
        modules = item.get("modules") or {}
        dyn = modules.get("module_dynamic") or {}
        
        # --- 第二步：提取动态 ID (用于后续排查) ---
        id_str = item.get("id_str", "未知ID")
        
        # --- 第三步：提取正文内容 (核心逻辑) ---
        content_list = []
        
        # 1. 尝试提取新版富文本 (Rich Text Nodes) - 优先级最高
        try:
            rich_nodes = dyn.get("desc", {}).get("rich_text_nodes", [])
            if rich_nodes:
                node_texts = []
                for node in rich_nodes:
                    # 安全获取 text 字段
                    text = node.get("text", "")
                    if text and isinstance(text, str):
                        node_texts.append(text)
                if node_texts:
                    parsed = "".join(node_texts).strip()
                    if parsed:
                        content_list.append(f"📝【内容】\n{parsed}")
        except Exception as e:
            logging.warning(f"⚠️ [{id_str}] 尝试提取富文本失败: {str(e)}")
        
        # 2. 尝试提取 Opus 类型 (新版图文)
        try:
            major = dyn.get("major") or {}
            if major.get("type") == "MAJOR_TYPE_OPUS":
                opus = major.get("opus", {}) or {}
                title = opus.get("title", "")
                summary = opus.get("summary", "")
                if title:
                    content_list.append(f"🎨【标题】\n{title}")
                if summary:
                    content_list.append(f"📝【摘要】\n{summary}")
        except Exception as e:
            logging.warning(f"⚠️ [{id_str}] 尝试提取 Opus 类型失败: {str(e)}")
        
        # 3. 尝试提取纯文本 (Desc)
        try:
            desc_text = dyn.get("desc", {}).get("text", "")
            if desc_text and not any([c in desc_text for c in content_list]):
                content_list.append(f"💬【文本】\n{desc_text}")
        except Exception as e:
            logging.warning(f"⚠️ [{id_str}] 尝试提取纯文本失败: {str(e)}")
        
        # --- 第四步：结果组装与兜底 ---
        final_text = "\n\n".join(content_list).strip()
        
        # 如果以上都失败，进行深度搜索兜底
        if not final_text:
            final_text = deep_find_text(dyn) # 复用你原有的 deep_find_text 作为最后防线
            if final_text:
                final_text = f"🔍【兜底提取】\n{final_text}"
            else:
                final_text = "📢【无文字内容】"
        
        # --- 第五步：安全截断 (防止 Webhook 崩溃) ---
        MAX_LEN = 1500
        if len(final_text) > MAX_LEN:
            final_text = final_text[:MAX_LEN] + f"\n\n⚠️ 内容过长，已截断..."
            
        return final_text
        
    except Exception as e:
        # 🔥 🔥 🔥 最终核保护：绝不能让主循环崩溃
        error_msg = f"💥【严重】extract_dynamic_text 发生未捕获异常: {str(e)}"
        logging.error(f"{error_msg}\n详细堆栈: {traceback.format_exc()}")
        return f"【系统错误】内容解析失败。"

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
                logging.warning(f"❌ 获取 UID {uid} 动态失败: {data.get('message')}")
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
                    
                time_diff = now_ts - pub_ts
                if time_diff > DYNAMIC_MAX_AGE:
                    continue
                
                # --- 内容提取 ---
                text = extract_dynamic_text(item) 
                
                # --- 拼接传送门链接 ---
                try:
                    link = f"https://t.bilibili.com/{id_str}"
                    final_msg = f"{text}\n\n🔗 直达链接: {link}"
                except Exception as e:
                    final_msg = f"{text}\n\n🔗 链接生成失败"
                
                name = author.get("name", str(uid))
                has_new = True
                alerts.append({
                    "user": name,
                    "message": final_msg
                })
                logging.info(f"✅ 抓取到新动态 [{name}]:\n{final_msg[:100]}...") # 打印预览
                break
                
        except Exception as e:
            logging.error(f"❌ 遍历 UID {uid} 时发生崩溃: {e}")
            continue
            
        time.sleep(random.uniform(1, 2))
    
    # --- 通知发送 ---
    if alerts:
        try:
            notifier.send_webhook_notification(
                "💡 特别关注UP主发布新内容",
                alerts
            )
            logging.info(f"🚀 成功发送 {len(alerts)} 条 Webhook 动态通知！")
        except Exception as e:
            logging.error(f"❌ Webhook 发送失败: {e}")
    
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
                        oid, header, last_read_time, seen_comments
                    )
                    if new_t > last_read_time:
                        last_read_time = new_t
                    if new_c:
                        new_c.sort(key=lambda x: x["ctime"])
                        try:
                            notifier.send_webhook_notification(
                                title, new_c
                            )
                        except Exception as e:
                            logging.error(f"评论通知发送失败: {e}")
                
                interval = (
                    DYNAMIC_BURST_INTERVAL
                    if now < burst_end
                    else DYNAMIC_CHECK_INTERVAL
                )
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
