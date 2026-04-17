#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站监控系统（基于 bilibili-api-python 库）
功能：
- 自动获取指定用户的关注列表，监控所有关注用户的新动态
- 监控指定视频的新评论
- 支持增量拉取（baseline 机制），避免重复处理
- 推送消息包含发布时间
"""

import asyncio
import os
import sys
import time
import json
import logging
import subprocess
from datetime import datetime
from typing import List, Set, Dict, Optional, Tuple

import database as db
import notifier

from bilibili_api import sync, Credential, dynamic, comment, video, user

# ======================== 配置 ========================
TARGET_UID = 1671203508          # 视频监控目标 UID
SOURCE_UID = 3706948578969654    # 用于获取关注列表的 UID（即 Cookie 对应的用户）
VIDEO_CHECK_INTERVAL = 21600     # 视频检查间隔（秒）
COMMENT_SCAN_INTERVAL = 5        # 评论扫描间隔（秒）
DYNAMIC_CHECK_INTERVAL = 15      # 动态检查间隔（秒）
FOLLOWING_REFRESH_INTERVAL = 3600  # 刷新关注列表间隔（秒）

LOG_FILE = "bili_monitor.log"
STATE_FILE = "monitor_state.json"  # 保存 baseline 和 offset
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
    logging.info("B站监控系统启动 (基于 bilibili-api-python)")
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

def parse_cookie(cookie_str: str) -> dict:
    cookies = {}
    for item in cookie_str.split(';'):
        item = item.strip()
        if not item:
            continue
        if '=' in item:
            key, value = item.split('=', 1)
            cookies[key] = value
    return cookies

def get_credential() -> Credential:
    cookie_str = get_cookie_str()
    cookies = parse_cookie(cookie_str)
    return Credential(
        sessdata=cookies.get("SESSDATA", ""),
        bili_jct=cookies.get("bili_jct", ""),
        dedeuserid=cookies.get("DedeUserID", "")
    )

# ======================== 状态管理（支持 baseline） ========================
class StateManager:
    """管理每个 UID 的 baseline 和已处理的动态 ID"""
    def __init__(self):
        self.data: Dict[str, Dict] = {}  # uid_str -> {"baseline": "", "seen": set()}
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
                logging.info(f"加载状态文件成功，共 {len(self.data)} 个 UID")
            except Exception as e:
                logging.error(f"加载状态文件失败: {e}")

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
        """初始化 UID 的状态（设置 baseline 并清空 seen）"""
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
    """时间戳转可读格式"""
    if timestamp:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    return "未知时间"

def extract_dynamic_text(item: dict) -> str:
    """提取动态文本"""
    modules = item.get('modules', {})
    dyn = modules.get('module_dynamic', {})
    desc = dyn.get('desc', {})
    text = desc.get('text', '')
    if text:
        return text
    # 尝试从 major 提取
    major = dyn.get('major', {})
    major_type = major.get('type', '')
    if major_type == 'MAJOR_TYPE_ARCHIVE':
        archive = major.get('archive', {})
        return f"【视频】{archive.get('title', '')}\n{archive.get('desc', '')}"
    elif major_type == 'MAJOR_TYPE_ARTICLE':
        article = major.get('article', {})
        return f"【专栏】{article.get('title', '')}"
    elif major_type == 'MAJOR_TYPE_OPUS':
        opus = major.get('opus', {})
        summary = opus.get('summary', {})
        nodes = summary.get('rich_text_nodes', [])
        if nodes:
            return ''.join([n.get('text', '') for n in nodes if isinstance(n, dict)])
    return "发布了新动态"

# ======================== 关注列表获取 ========================
async def get_following_list(uid: int, credential: Credential) -> List[int]:
    """获取用户关注的 UID 列表"""
    u = user.User(uid, credential=credential)
    followings = []
    page = 1
    while True:
        try:
            resp = await u.get_followings(page=page, ps=50)
            followings.extend([f['mid'] for f in resp['list']])
            if page * 50 >= resp['total']:
                break
            page += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            logging.error(f"获取关注列表失败: {e}")
            break
    return followings

# ======================== 动态监控（增量拉取） ========================
async def check_dynamics_incremental(uid: int, credential: Credential, state: StateManager) -> List[dict]:
    """
    增量检查用户的新动态
    利用 baseline（最新一条动态 ID）拉取比它更新的动态
    """
    alerts = []
    baseline = state.get_baseline(uid)
    
    try:
        u = user.User(uid, credential=credential)
        
        # 如果没有 baseline，先拉取最新一条动态建立基线
        if not baseline:
            dynamics = await u.get_dynamics(offset=None, need_top=False)
            items = dynamics.get('items', [])
            if items:
                latest = items[0]
                latest_id = latest.get('id_str')
                if latest_id:
                    state.init_uid(uid, latest_id)
                    logging.info(f"UID {uid} 建立基线: {latest_id}")
            return alerts
        
        # 有 baseline，拉取所有动态（最多20条），找出比 baseline 更新的
        dynamics = await u.get_dynamics(offset=None, need_top=False)
        items = dynamics.get('items', [])
        
        # 找到 baseline 的位置
        new_items = []
        found_baseline = False
        for item in items:
            dyn_id = item.get('id_str')
            if not dyn_id:
                continue
            if dyn_id == baseline:
                found_baseline = True
                break
            new_items.append(item)
        
        # 如果没有找到 baseline，说明 baseline 太旧，重新初始化
        if not found_baseline and items:
            latest_id = items[0].get('id_str')
            if latest_id:
                state.init_uid(uid, latest_id)
                logging.info(f"UID {uid} 基线丢失，重新初始化: {latest_id}")
            return alerts
        
        # 处理新动态（按时间顺序，旧的在前，需要反转）
        new_items.reverse()  # 最早的先处理
        
        for item in new_items:
            dyn_id = item.get('id_str')
            if state.is_seen(uid, dyn_id):
                continue
            
            state.add_seen(uid, dyn_id)
            
            # 提取信息
            modules = item.get('modules', {})
            author = modules.get('module_author', {})
            name = author.get('name', str(uid))
            pub_ts = author.get('pub_ts', 0)
            pub_time_str = format_time(pub_ts)
            
            text = extract_dynamic_text(item)
            
            # 处理转发
            if item.get('type') == 'DYNAMIC_TYPE_FORWARD':
                orig = item.get('orig')
                if orig:
                    orig_text = extract_dynamic_text(orig)
                    if orig_text:
                        text = f"{text}\n【转发原文】{orig_text}" if text else f"【转发原文】{orig_text}"
                    orig_id = orig.get('id_str')
                    if orig_id:
                        text = f"{text}\n【原动态链接】https://t.bilibili.com/{orig_id}"
            
            link = f"https://t.bilibili.com/{dyn_id}"
            final_msg = f"🕐 发布时间：{pub_time_str}\n📝 {text}\n\n🔗 {link}" if text else f"🕐 发布时间：{pub_time_str}\n🔗 {link}"
            alerts.append({"user": name, "message": final_msg})
            logging.info(f"✅ 新动态 [{name}]: {dyn_id} (发布时间: {pub_time_str})")
        
        # 更新 baseline 为最新一条动态的 ID
        if items:
            new_baseline = items[0].get('id_str')
            if new_baseline and new_baseline != baseline:
                state.set_baseline(uid, new_baseline)
                logging.debug(f"UID {uid} baseline 更新: {baseline} -> {new_baseline}")
        
    except Exception as e:
        logging.error(f"检查 UID {uid} 动态失败: {e}")
    
    return alerts

# ======================== 评论监控 ========================
async def check_comments(aid: int, credential: Credential, last_time: int, seen_set: Set[str]) -> Tuple[List[dict], int]:
    """检查视频的新评论，返回 (新评论列表, 最新评论时间)"""
    v = video.Video(aid, credential=credential)
    new_list = []
    max_ctime = last_time
    try:
        page = 1
        while True:
            resp = await v.get_comments(
                page=page,
                page_size=20,
                sort=comment.CommentResourceSort.TIME_DESC
            )
            replies = resp.get('replies', [])
            if not replies:
                break
            all_old = True
            for r in replies:
                ctime = r['ctime']
                if ctime > max_ctime:
                    max_ctime = ctime
                if ctime > last_time - 60:
                    all_old = False
                    rpid = str(r['rpid'])
                    if rpid not in seen_set:
                        seen_set.add(rpid)
                        pub_time_str = format_time(ctime)
                        new_list.append({
                            "user": r['member']['uname'],
                            "message": f"🕐 {pub_time_str}\n{r['content']['message']}",
                            "ctime": ctime
                        })
            if all_old or len(replies) < 20:
                break
            page += 1
            await asyncio.sleep(0.5)
    except Exception as e:
        logging.error(f"检查评论失败: {e}")
    return new_list, max_ctime

# ======================== 视频监控 ========================
async def sync_latest_video(credential: Credential) -> Tuple[Optional[int], Optional[str]]:
    """获取目标用户的最新视频，返回 (aid, title)"""
    u = user.User(TARGET_UID, credential=credential)
    try:
        dynamics = await u.get_dynamics(offset=None, need_top=False)
        for item in dynamics.get('items', []):
            if item.get('type') == 'DYNAMIC_TYPE_AV':
                modules = item.get('modules', {})
                major = modules.get('module_dynamic', {}).get('major', {})
                archive = major.get('archive', {})
                bvid = archive.get('bvid')
                if bvid:
                    v = video.Video(bvid=bvid, credential=credential)
                    info = await v.get_info()
                    aid = str(info['aid'])
                    title = info['title']
                    videos = db.get_monitored_videos()
                    if not videos or videos[0][1] != bvid:
                        db.clear_videos()
                        db.add_video_to_db(aid, bvid, title)
                    return aid, title
    except Exception as e:
        logging.error(f"同步最新视频失败: {e}")
    return None, None

# ======================== 主循环 ========================
async def main_loop():
    credential = get_credential()
    state = StateManager()
    
    # 获取初始关注列表
    following = await get_following_list(SOURCE_UID, credential)
    if not following:
        logging.warning("获取关注列表失败，使用备选列表")
        following = [3546905852250875, 3546961271589219, 3546610447419885, 285340365, SOURCE_UID]
    # 确保自身在列表中
    if SOURCE_UID not in following:
        following.append(SOURCE_UID)
    logging.info(f"监控 UID 列表 ({len(following)} 个)")
    
    # 初始化每个 UID 的 baseline
    for uid in following:
        try:
            u = user.User(uid, credential=credential)
            dynamics = await u.get_dynamics(offset=None, need_top=False)
            items = dynamics.get('items', [])
            if items:
                latest_id = items[0].get('id_str')
                if latest_id:
                    state.init_uid(uid, latest_id)
                    logging.info(f"初始化 UID {uid}: baseline={latest_id}")
            await asyncio.sleep(0.3)
        except Exception as e:
            logging.error(f"初始化 UID {uid} 失败: {e}")
    state.save()
    
    # 初始化视频监控
    aid, title = await sync_latest_video(credential)
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
                new_following = await get_following_list(SOURCE_UID, credential)
                if new_following:
                    # 确保自身在列表中
                    if SOURCE_UID not in new_following:
                        new_following.append(SOURCE_UID)
                    old_set = set(following)
                    new_set = set(new_following)
                    added = new_set - old_set
                    removed = old_set - new_set
                    
                    # 初始化新增的 UID
                    for uid in added:
                        try:
                            u = user.User(uid, credential=credential)
                            dynamics = await u.get_dynamics(offset=None, need_top=False)
                            items = dynamics.get('items', [])
                            latest_id = items[0].get('id_str') if items else ""
                            state.init_uid(uid, latest_id)
                            logging.info(f"新增监控 UID {uid}, baseline={latest_id}")
                        except Exception as e:
                            logging.error(f"初始化新增 UID {uid} 失败: {e}")
                    
                    # 移除不再关注的 UID
                    for uid in removed:
                        state.remove_uid(uid)
                        logging.info(f"移除监控 UID {uid}")
                    
                    following = new_following
                    state.save()
                    logging.info(f"关注列表已刷新，共 {len(following)} 个 (新增 {len(added)}, 移除 {len(removed)})")
                last_following_refresh = now
            
            # 2. 动态监控（每15秒，遍历所有关注的 UID）
            if now - last_dynamic_check >= DYNAMIC_CHECK_INTERVAL:
                all_alerts = []
                for uid in following:
                    alerts = await check_dynamics_incremental(uid, credential, state)
                    all_alerts.extend(alerts)
                    await asyncio.sleep(0.5)  # 避免请求过快
                if all_alerts:
                    try:
                        notifier.send_webhook_notification("💡 特别关注UP主发布新内容", all_alerts)
                        logging.info(f"🚀 成功发送 {len(all_alerts)} 条动态通知")
                    except Exception as e:
                        logging.error(f"动态通知发送失败: {e}")
                    state.save()
                last_dynamic_check = now
            
            # 3. 评论监控（每5秒）
            if aid and (now - last_comment_time >= COMMENT_SCAN_INTERVAL):
                new_comments, new_time = await check_comments(int(aid), credential, last_comment_time, seen_comments)
                if new_comments:
                    new_comments.sort(key=lambda x: x['ctime'])
                    try:
                        notifier.send_webhook_notification(title, new_comments)
                        logging.info(f"💬 成功发送 {len(new_comments)} 条评论通知")
                    except Exception as e:
                        logging.error(f"评论通知发送失败: {e}")
                last_comment_time = max(last_comment_time, new_time)
            
            # 4. 视频监控（每6小时）
            if now - last_video_check >= VIDEO_CHECK_INTERVAL:
                new_aid, new_title = await sync_latest_video(credential)
                if new_aid:
                    aid, title = new_aid
                last_video_check = now
            
            # 5. 心跳
            if now - last_heartbeat >= 10:
                logging.info("💓 心跳: 监控系统正常运行中")
                last_heartbeat = now
            
            await asyncio.sleep(2)
            
        except KeyboardInterrupt:
            logging.info("用户中断，程序退出")
            break
        except Exception as e:
            logging.error(f"主循环异常: {e}", exc_info=True)
            await asyncio.sleep(60)

def main():
    init_logging()
    db.init_db()
    asyncio.run(main_loop())

if __name__ == "__main__":
    main()
