#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站监控系统（纯 requests 实现，不依赖 bilibili_api）
功能：
- 自动获取指定用户的关注列表，监控所有关注用户的新动态
- 监控指定视频的新评论
- 支持增量拉取（baseline 机制）
- 推送消息包含发布时间
"""

import sys
import os
import time
import json
import random
import hashlib
import urllib.parse
import subprocess
import logging
import traceback
from datetime import datetime
from typing import List, Dict, Set, Optional, Tuple

import requests
import database as db
import notifier

# ======================== 配置 ========================
TARGET_UID = 1671203508
SOURCE_UID = 3706948578969654    # 用于获取关注列表的 UID
VIDEO_CHECK_INTERVAL = 21600
COMMENT_SCAN_INTERVAL = 5
DYNAMIC_CHECK_INTERVAL = 15
FOLLOWING_REFRESH_INTERVAL = 3600

# 备选列表（当 API 失败时使用）
FALLBACK_UIDS = [3546905852250875, 3546961271589219, 3546610447419885, 285340365, SOURCE_UID]

# 服务器时间补偿（快2分钟 = -120秒）
TIME_OFFSET = -120

LOG_FILE = "bili_monitor.log"
STATE_FILE = "monitor_state.json"
FOLLOWING_CACHE_FILE = "following_cache.json"
# ============================================================

# ======================== 日志 ========================
def init_logging():
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "w") as f:
                f.truncate()
    except:
        pass
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        filemode="w"
    )
    logging.info("=" * 60)
    logging.info("B站监控系统启动")
    logging.info(f"时间补偿: {TIME_OFFSET} 秒")
    logging.info("=" * 60)

# ======================== Cookie 管理 ========================
def get_cookie_str() -> str:
    try:
        with open("bili_cookie.txt", "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        subprocess.run([sys.executable, "login_bilibili.py"])
        with open("bili_cookie.txt", "r") as f:
            return f.read().strip()

def get_headers() -> dict:
    cookie_str = get_cookie_str()
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
        "Cookie": cookie_str
    }

def refresh_cookie() -> bool:
    logging.warning("Cookie 失效，尝试重新登录...")
    try:
        subprocess.run([sys.executable, "login_bilibili.py"], check=True)
        logging.info("重新登录成功")
        return True
    except Exception as e:
        logging.error(f"重新登录失败: {e}")
        return False

# ======================== WBI 签名 ========================
WBI_KEYS = {"img_key": "", "sub_key": "", "last_update": 0}
MIXIN_KEY_ENC_TAB = [
    46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,
    27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,
    37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,
    22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52
]

def get_mixin_key(orig: str) -> str:
    return ''.join([orig[i] for i in MIXIN_KEY_ENC_TAB])[:32]

def update_wbi_keys():
    try:
        resp = requests.get("https://api.bilibili.com/x/web-interface/nav", headers=get_headers(), timeout=10)
        data = resp.json()
        if data.get("code") == 0:
            img = data["data"]["wbi_img"]
            WBI_KEYS["img_key"] = img["img_url"].rsplit("/", 1)[1].split(".")[0]
            WBI_KEYS["sub_key"] = img["sub_url"].rsplit("/", 1)[1].split(".")[0]
            WBI_KEYS["last_update"] = time.time()
            logging.info("WBI密钥已更新")
            return True
        elif data.get("code") == -101:
            logging.error("获取WBI密钥时Cookie失效")
            if refresh_cookie():
                return update_wbi_keys()
    except Exception as e:
        logging.error(f"更新WBI密钥异常: {e}")
    return False

def enc_wbi(params: dict) -> dict:
    if not WBI_KEYS["img_key"] or time.time() - WBI_KEYS["last_update"] > 21600:
        update_wbi_keys()
    
    params = params.copy()
    params["wts"] = int(time.time() + TIME_OFFSET)
    params = dict(sorted(params.items()))
    
    filtered = {}
    for k, v in params.items():
        v = str(v)
        for c in "!'()*":
            v = v.replace(c, "")
        filtered[k] = v
    
    query = urllib.parse.urlencode(filtered, quote_via=urllib.parse.quote)
    mixin_key = get_mixin_key(WBI_KEYS["img_key"] + WBI_KEYS["sub_key"])
    sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
    filtered["w_rid"] = sign
    return filtered

def wbi_request(url: str, params: dict = None, retries: int = 3) -> dict:
    if params is None:
        params = {}
    for attempt in range(retries):
        try:
            signed = enc_wbi(params)
            resp = requests.get(url, headers=get_headers(), params=signed, timeout=10)
            data = resp.json()
            code = data.get("code")
            if code == -101:
                if refresh_cookie():
                    continue
                return {"code": -101, "message": "Cookie失效"}
            if code == -352:
                wait = (2 ** attempt) + random.uniform(0, 2)
                logging.warning(f"WBI签名错误(-352)，{wait:.1f}秒后重试")
                time.sleep(wait)
                continue
            if code != 0 and attempt < retries - 1:
                wait = (2 ** attempt) + random.uniform(0, 1)
                time.sleep(wait)
                continue
            return data
        except Exception as e:
            logging.error(f"请求异常: {e}")
            time.sleep(2)
    return {"code": -500, "message": "请求失败"}

# ======================== 状态管理 ========================
class StateManager:
    def __init__(self):
        self.data: Dict[str, Dict] = {}
        self._load()
    
    def _load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    raw = json.load(f)
                    for uid_str, info in raw.items():
                        self.data[uid_str] = {
                            "baseline": info.get("baseline", ""),
                            "seen": set(info.get("seen", []))
                        }
                logging.info(f"加载状态成功，共 {len(self.data)} 个UID")
            except Exception as e:
                logging.error(f"加载状态失败: {e}")
    
    def save(self):
        to_save = {}
        for uid_str, info in self.data.items():
            to_save[uid_str] = {
                "baseline": info["baseline"],
                "seen": list(info["seen"])
            }
        with open(STATE_FILE, "w") as f:
            json.dump(to_save, f, indent=2)
    
    def get_baseline(self, uid: int) -> str:
        uid_str = str(uid)
        if uid_str not in self.data:
            self.data[uid_str] = {"baseline": "", "seen": set()}
        return self.data[uid_str]["baseline"]
    
    def set_baseline(self, uid: int, baseline: str):
        uid_str = str(uid)
        if uid_str not in self.data:
            self.data[uid_str] = {"baseline": "", "seen": set()}
        self.data[uid_str]["baseline"] = baseline
    
    def is_seen(self, uid: int, dyn_id: str) -> bool:
        uid_str = str(uid)
        if uid_str not in self.data:
            return False
        return dyn_id in self.data[uid_str]["seen"]
    
    def add_seen(self, uid: int, dyn_id: str):
        uid_str = str(uid)
        if uid_str not in self.data:
            self.data[uid_str] = {"baseline": "", "seen": set()}
        self.data[uid_str]["seen"].add(dyn_id)
    
    def init_uid(self, uid: int, latest_dyn_id: str = ""):
        uid_str = str(uid)
        self.data[uid_str] = {"baseline": latest_dyn_id, "seen": set()}
        if latest_dyn_id:
            self.data[uid_str]["seen"].add(latest_dyn_id)
        logging.info(f"初始化 UID {uid}: baseline={latest_dyn_id}")
    
    def remove_uid(self, uid: int):
        uid_str = str(uid)
        if uid_str in self.data:
            del self.data[uid_str]

# ======================== 工具函数 ========================
def format_time(timestamp: int) -> str:
    if timestamp:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    return "未知时间"

def extract_dynamic_text(item: dict) -> str:
    modules = item.get("modules") or {}
    dyn = modules.get("module_dynamic") or {}
    desc = dyn.get("desc") or {}
    nodes = desc.get("rich_text_nodes") or []
    if nodes:
        parts = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            t = node.get("type", "")
            if t in ("RICH_TEXT_NODE_TYPE_TEXT", "RICH_TEXT_NODE_TYPE_TOPIC",
                     "RICH_TEXT_NODE_TYPE_AT", "RICH_TEXT_NODE_TYPE_EMOJI"):
                parts.append(node.get("text", ""))
        full = "".join(parts).strip()
        if full:
            return full
    major = dyn.get("major") or {}
    mtype = major.get("type", "")
    if mtype == "MAJOR_TYPE_ARCHIVE":
        archive = major.get("archive") or {}
        return f"【视频】{archive.get('title', '')}\n{archive.get('desc', '')}"
    elif mtype == "MAJOR_TYPE_ARTICLE":
        article = major.get("article") or {}
        return f"【专栏】{article.get('title', '')}"
    elif mtype == "MAJOR_TYPE_OPUS":
        opus = major.get("opus") or {}
        summary = opus.get("summary") or {}
        nodes = summary.get("rich_text_nodes") or []
        if nodes:
            return "".join([n.get("text", "") for n in nodes if isinstance(n, dict)]).strip()
    return "发布了新动态"

# ======================== 关注列表获取 ========================
def get_following_list(uid: int) -> List[int]:
    following = []
    pn = 1
    ps = 50
    while True:
        params = {
            "vmid": uid,
            "pn": pn,
            "ps": ps,
            "order": "desc",
            "order_type": "attention"
        }
        data = wbi_request("https://api.bilibili.com/x/relation/followings", params)
        if data.get("code") != 0:
            logging.warning(f"获取关注列表失败 (pn={pn}): {data.get('message')}")
            break
        info = data.get("data") or {}
        items = info.get("list", [])
        if not items:
            break
        for item in items:
            mid = item.get("mid")
            if mid:
                following.append(mid)
        if info.get("total", 0) <= pn * ps:
            break
        pn += 1
        time.sleep(random.uniform(0.5, 1))
    return following

def load_following_cache() -> List[int]:
    if os.path.exists(FOLLOWING_CACHE_FILE):
        try:
            with open(FOLLOWING_CACHE_FILE, "r") as f:
                return json.load(f)
        except:
            return []
    return []

def save_following_cache(uids: List[int]):
    with open(FOLLOWING_CACHE_FILE, "w") as f:
        json.dump(uids, f)

# ======================== 动态监控 ========================
def fetch_user_dynamics(uid: int, offset: str = "", baseline: str = "") -> dict:
    params = {
        "host_mid": uid,
        "type": "all",
        "timezone_offset": "-480",
        "platform": "web",
        "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,decorationCard,onlyfansAssetsV2,forwardListHidden,ugcDelete",
        "web_location": "333.1365",
        "offset": offset
    }
    if baseline:
        params["update_baseline"] = baseline
    return wbi_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all", params)

def check_dynamics_incremental(uid: int, state: StateManager) -> List[dict]:
    alerts = []
    baseline = state.get_baseline(uid)
    
    # 没有 baseline，先拉取最新一条建立基线
    if not baseline:
        data = fetch_user_dynamics(uid)
        if data.get("code") == 0:
            feed = data.get("data") or {}
            items = feed.get("items", [])
            if items:
                latest_id = items[0].get("id_str")
                if latest_id:
                    state.init_uid(uid, latest_id)
                    logging.info(f"UID {uid} 建立基线: {latest_id}")
        return alerts
    
    # 有 baseline，增量拉取
    data = fetch_user_dynamics(uid, baseline=baseline)
    if data.get("code") != 0:
        logging.warning(f"UID {uid} 拉取动态失败: {data.get('message')}")
        return alerts
    
    feed = data.get("data") or {}
    items = feed.get("items", [])
    new_baseline = feed.get("update_baseline", baseline)
    
    # 更新 baseline
    if new_baseline != baseline:
        state.set_baseline(uid, new_baseline)
        logging.debug(f"UID {uid} baseline 更新: {baseline} -> {new_baseline}")
    
    # 处理新动态（items 按时间倒序，最新在前）
    for item in items:
        dyn_id = item.get("id_str")
        if not dyn_id or state.is_seen(uid, dyn_id):
            continue
        state.add_seen(uid, dyn_id)
        
        modules = item.get("modules") or {}
        author = modules.get("module_author") or {}
        name = author.get("name", str(uid))
        pub_ts = author.get("pub_ts", 0)
        pub_time_str = format_time(pub_ts)
        
        text = extract_dynamic_text(item)
        
        # 处理转发
        if item.get("type") == "DYNAMIC_TYPE_FORWARD":
            orig = item.get("orig")
            if orig:
                orig_text = extract_dynamic_text(orig)
                if orig_text:
                    text = f"{text}\n【转发原文】{orig_text}" if text else f"【转发原文】{orig_text}"
                orig_id = orig.get("id_str")
                if orig_id:
                    text = f"{text}\n【原动态链接】https://t.bilibili.com/{orig_id}"
        
        link = f"https://t.bilibili.com/{dyn_id}"
        final_msg = f"🕐 发布时间：{pub_time_str}\n📝 {text}\n\n🔗 {link}" if text else f"🕐 发布时间：{pub_time_str}\n🔗 {link}"
        alerts.append({"user": name, "message": final_msg})
        logging.info(f"✅ 新动态 [{name}]: {dyn_id} (发布时间: {pub_time_str})")
    
    return alerts

# ======================== 评论监控 ========================
def scan_new_comments(oid: int, last_read_time: int, seen: Set[str]) -> Tuple[List[dict], int]:
    new_list = []
    max_ctime = last_read_time
    safe_time = last_read_time - 60
    
    pn = 1
    while pn <= 3:
        data = wbi_request("https://api.bilibili.com/x/v2/reply", {
            "oid": oid,
            "type": 1,
            "sort": 0,
            "pn": pn,
            "ps": 20
        })
        if data.get("code") != 0:
            break
        replies = (data.get("data") or {}).get("replies") or []
        if not replies:
            break
        all_old = True
        for r in replies:
            ctime = r.get("ctime", 0)
            if ctime > max_ctime:
                max_ctime = ctime
            if ctime > safe_time:
                all_old = False
                rpid = r.get("rpid_str", "")
                if rpid and rpid not in seen:
                    seen.add(rpid)
                    pub_time_str = format_time(ctime)
                    new_list.append({
                        "user": r["member"]["uname"],
                        "message": f"🕐 {pub_time_str}\n{r['content']['message']}",
                        "ctime": ctime
                    })
        if all_old:
            break
        pn += 1
        time.sleep(random.uniform(0.3, 0.6))
    return new_list, max_ctime

# ======================== 视频监控 ========================
def get_latest_video() -> Optional[str]:
    data = wbi_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space", {"host_mid": TARGET_UID})
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

def get_video_info(bvid: str) -> Tuple[Optional[str], Optional[str]]:
    data = wbi_request(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
    if data.get("code") == 0:
        return str(data["data"]["aid"]), data["data"]["title"]
    return None, None

def sync_latest_video() -> Tuple[Optional[int], Optional[str]]:
    bvid = get_latest_video()
    if not bvid:
        return None, None
    videos = db.get_monitored_videos()
    if videos and videos[0][1] == bvid:
        return videos[0][0], videos[0][2]
    oid, title = get_video_info(bvid)
    if oid:
        db.clear_videos()
        db.add_video_to_db(oid, bvid, title)
        return oid, title
    return None, None

# ======================== 主循环 ========================
def main():
    init_logging()
    db.init_db()
    
    # 初始化 WBI 密钥
    update_wbi_keys()
    
    state = StateManager()
    
    # 获取初始关注列表
    following = load_following_cache()
    if not following:
        following = get_following_list(SOURCE_UID)
        if not following:
            logging.warning("获取关注列表失败，使用备选列表")
            following = FALLBACK_UIDS
        else:
            save_following_cache(following)
    if SOURCE_UID not in following:
        following.append(SOURCE_UID)
    logging.info(f"监控 UID 列表 ({len(following)} 个)")
    
    # 初始化每个 UID 的 baseline
    for uid in following:
        data = fetch_user_dynamics(uid)
        if data.get("code") == 0:
            items = data.get("data", {}).get("items", [])
            if items:
                latest_id = items[0].get("id_str")
                if latest_id:
                    state.init_uid(uid, latest_id)
                    logging.info(f"初始化 UID {uid}: baseline={latest_id}")
        time.sleep(0.3)
    state.save()
    
    # 初始化视频监控
    aid, title = sync_latest_video()
    
    # 定时器变量
    last_video_check = time.time()
    last_comment_time = int(time.time())
    seen_comments = set()
    last_dynamic_check = time.time()
    last_following_refresh = time.time()
    last_heartbeat = time.time()
    
    logging.info("监控服务已启动，正在扫描新数据...")
    
    while True:
        try:
            now = time.time()
            
            # 1. 刷新关注列表（每小时）
            if now - last_following_refresh >= FOLLOWING_REFRESH_INTERVAL:
                new_following = get_following_list(SOURCE_UID)
                if new_following:
                    if SOURCE_UID not in new_following:
                        new_following.append(SOURCE_UID)
                    old_set = set(following)
                    new_set = set(new_following)
                    added = new_set - old_set
                    removed = old_set - new_set
                    
                    for uid in added:
                        data = fetch_user_dynamics(uid)
                        items = data.get("data", {}).get("items", [])
                        latest_id = items[0].get("id_str") if items else ""
                        state.init_uid(uid, latest_id)
                        logging.info(f"新增监控 UID {uid}")
                    
                    for uid in removed:
                        state.remove_uid(uid)
                        logging.info(f"移除监控 UID {uid}")
                    
                    following = new_following
                    save_following_cache(following)
                    state.save()
                    logging.info(f"关注列表已刷新，共 {len(following)} 个 (新增 {len(added)}, 移除 {len(removed)})")
                last_following_refresh = now
            
            # 2. 动态监控
            if now - last_dynamic_check >= DYNAMIC_CHECK_INTERVAL:
                all_alerts = []
                for uid in following:
                    alerts = check_dynamics_incremental(uid, state)
                    all_alerts.extend(alerts)
                    time.sleep(0.5)
                if all_alerts:
                    try:
                        notifier.send_webhook_notification("💡 特别关注UP主发布新内容", all_alerts)
                        logging.info(f"🚀 成功发送 {len(all_alerts)} 条动态通知")
                    except Exception as e:
                        logging.error(f"动态通知发送失败: {e}")
                    state.save()
                last_dynamic_check = now
            
            # 3. 评论监控
            if aid and (now - last_comment_time >= COMMENT_SCAN_INTERVAL):
                new_comments, new_time = scan_new_comments(int(aid), last_comment_time, seen_comments)
                if new_comments:
                    new_comments.sort(key=lambda x: x["ctime"])
                    try:
                        notifier.send_webhook_notification(title, new_comments)
                        logging.info(f"💬 成功发送 {len(new_comments)} 条评论通知")
                    except Exception as e:
                        logging.error(f"评论通知发送失败: {e}")
                last_comment_time = max(last_comment_time, new_time)
            
            # 4. 视频监控
            if now - last_video_check >= VIDEO_CHECK_INTERVAL:
                new_aid, new_title = sync_latest_video()
                if new_aid:
                    aid, title = new_aid
                last_video_check = now
            
            # 5. 心跳
            if now - last_heartbeat >= 10:
                logging.info("💓 心跳: 监控系统正常运行中")
                last_heartbeat = now
            
            time.sleep(2)
            
        except KeyboardInterrupt:
            logging.info("用户中断，程序退出")
            break
        except Exception as e:
            logging.error(f"主循环异常: {e}\n{traceback.format_exc()}")
            time.sleep(60)

if __name__ == "__main__":
    main()
