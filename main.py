#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站监控系统（基于 bilibili-api-python 库）
"""

import asyncio
import os
import sys
import time
import json
import logging
import subprocess
from datetime import datetime
from typing import List, Dict, Set, Optional, Tuple

import database as db
import notifier

from bilibili_api import Credential, user, dynamic, video, comment, sync

# ======================== 配置 ========================
TARGET_UID = 1671203508
SOURCE_UID = 3706948578969654
VIDEO_CHECK_INTERVAL = 21600
COMMENT_SCAN_INTERVAL = 5
DYNAMIC_CHECK_INTERVAL = 15
FOLLOWING_REFRESH_INTERVAL = 3600

LOG_FILE = "bili_monitor.log"
STATE_FILE = "monitor_state.json"
FOLLOWING_CACHE_FILE = "following_cache.json"

# 企业微信机器人关键词（根据你的设置修改）
WEBHOOK_KEYWORD = "监控"
# =====================================================

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
    logging.info("B站监控系统启动 (bilibili-api-python版)")
    logging.info("=" * 60)

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
        if not item or '=' not in item:
            continue
        key, value = item.split('=', 1)
        cookies[key] = value
    return cookies

def get_credential() -> Credential:
    cookies = parse_cookie(get_cookie_str())
    return Credential(
        sessdata=cookies.get("SESSDATA", ""),
        bili_jct=cookies.get("bili_jct", ""),
        dedeuserid=cookies.get("DedeUserID", "")
    )

def format_time(timestamp: int) -> str:
    if timestamp:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    return "未知时间"

# ======================== 状态管理 ========================
class StateManager:
    def __init__(self):
        self.seen: Dict[int, Set[str]] = {}
        self.baseline: Dict[int, str] = {}
        self._load()
    
    def _load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    data = json.load(f)
                    for uid_str, info in data.items():
                        uid = int(uid_str)
                        self.seen[uid] = set(info.get("seen", []))
                        self.baseline[uid] = info.get("baseline", "")
                logging.info(f"加载状态成功，共 {len(self.seen)} 个UID")
            except Exception as e:
                logging.error(f"加载状态失败: {e}")
    
    def save(self):
        to_save = {}
        for uid, seen_set in self.seen.items():
            to_save[str(uid)] = {
                "seen": list(seen_set),
                "baseline": self.baseline.get(uid, "")
            }
        with open(STATE_FILE, "w") as f:
            json.dump(to_save, f, indent=2)
    
    def is_seen(self, uid: int, dyn_id: str) -> bool:
        return dyn_id in self.seen.get(uid, set())
    
    def add_seen(self, uid: int, dyn_id: str):
        if uid not in self.seen:
            self.seen[uid] = set()
        self.seen[uid].add(dyn_id)
    
    def get_baseline(self, uid: int) -> str:
        return self.baseline.get(uid, "")
    
    def set_baseline(self, uid: int, baseline: str):
        self.baseline[uid] = baseline
    
    def init_uid(self, uid: int, latest_id: str = "", seen_ids: Set[str] = None):
        if seen_ids:
            self.seen[uid] = set(seen_ids)
        else:
            self.seen[uid] = set()
        if latest_id:
            self.baseline[uid] = latest_id
            self.seen[uid].add(latest_id)
        logging.info(f"初始化 UID {uid}: baseline={latest_id}, 已收录 {len(self.seen[uid])} 条")
    
    def remove_uid(self, uid: int):
        self.seen.pop(uid, None)
        self.baseline.pop(uid, None)

# ======================== 关注列表 ========================
async def get_following_list(uid: int, credential: Credential) -> List[int]:
    u = user.User(uid, credential=credential)
    following = []
    page = 1
    while True:
        try:
            resp = await u.get_followings(page=page, ps=50)
            following.extend([f['mid'] for f in resp['list']])
            if page * 50 >= resp['total']:
                break
            page += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            logging.error(f"获取关注列表失败: {e}")
            break
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
async def init_user_dynamics(uid: int, credential: Credential, state: StateManager, limit: int = 30):
    """初始化用户动态，将所有最近动态标记为已见"""
    try:
        u = user.User(uid, credential=credential)
        dynamics = await u.get_dynamics(offset=None, need_top=False)
        items = dynamics.get('items', [])[:limit]
        seen_ids = set()
        latest_id = ""
        for item in items:
            dyn_id = item.get('id_str')
            if dyn_id:
                seen_ids.add(dyn_id)
                if not latest_id:
                    latest_id = dyn_id
        state.init_uid(uid, latest_id, seen_ids)
        logging.info(f"初始化 UID {uid}: 已收录 {len(seen_ids)} 条动态")
    except Exception as e:
        logging.error(f"初始化 UID {uid} 失败: {e}")

async def check_dynamics_incremental(uid: int, credential: Credential, state: StateManager) -> List[dict]:
    """增量检查新动态"""
    alerts = []
    baseline = state.get_baseline(uid)
    if not baseline:
        return alerts
    
    try:
        u = user.User(uid, credential=credential)
        dynamics = await u.get_dynamics(offset=None, need_top=False)
        items = dynamics.get('items', [])
        
        # 找到新动态（比 baseline 新的）
        new_items = []
        found = False
        for item in items:
            dyn_id = item.get('id_str')
            if not dyn_id:
                continue
            if dyn_id == baseline:
                found = True
                break
            new_items.append(item)
        
        # 如果没找到 baseline，重新初始化
        if not found and items:
            latest_id = items[0].get('id_str')
            if latest_id:
                await init_user_dynamics(uid, credential, state)
            return alerts
        
        # 处理新动态（按时间正序，最早的先处理）
        for item in reversed(new_items):
            dyn_id = item.get('id_str')
            if state.is_seen(uid, dyn_id):
                continue
            state.add_seen(uid, dyn_id)
            
            modules = item.get('modules', {})
            author = modules.get('module_author', {})
            name = author.get('name', str(uid))
            pub_ts = author.get('pub_ts', 0)
            
            # 提取文本
            dyn_module = modules.get('module_dynamic', {})
            desc = dyn_module.get('desc', {})
            text = desc.get('text', '')
            if not text:
                major = dyn_module.get('major', {})
                if major.get('type') == 'MAJOR_TYPE_ARCHIVE':
                    archive = major.get('archive', {})
                    text = f"【视频】{archive.get('title', '')}"
                elif major.get('type') == 'MAJOR_TYPE_ARTICLE':
                    article = major.get('article', {})
                    text = f"【专栏】{article.get('title', '')}"
                else:
                    text = "发布了新动态"
            
            # 处理转发
            if item.get('type') == 'DYNAMIC_TYPE_FORWARD':
                orig = item.get('orig')
                if orig:
                    orig_modules = orig.get('modules', {})
                    orig_desc = orig_modules.get('module_dynamic', {}).get('desc', {})
                    orig_text = orig_desc.get('text', '')
                    if orig_text:
                        text = f"{text}\n【转发原文】{orig_text}"
                    orig_id = orig.get('id_str')
                    if orig_id:
                        text = f"{text}\n【原动态链接】https://t.bilibili.com/{orig_id}"
            
            link = f"https://t.bilibili.com/{dyn_id}"
            pub_time_str = format_time(pub_ts)
            final_msg = f"🕐 {pub_time_str}\n📝 {text}\n\n🔗 {link}"
            alerts.append({"user": name, "message": final_msg})
            logging.info(f"✅ 新动态 [{name}]: {dyn_id}")
        
        # 更新 baseline
        if items:
            new_baseline = items[0].get('id_str')
            if new_baseline and new_baseline != baseline:
                state.set_baseline(uid, new_baseline)
    
    except Exception as e:
        logging.error(f"检查 UID {uid} 动态失败: {e}")
    
    return alerts

# ======================== 评论监控 ========================
async def check_comments(aid: int, credential: Credential, last_time: int, seen_set: Set[str]) -> Tuple[List[dict], int]:
    v = video.Video(aid, credential=credential)
    new_list = []
    max_ctime = last_time
    try:
        page = 1
        while page <= 3:
            resp = await v.get_comments(page=page, page_size=20, sort=comment.CommentResourceSort.TIME_DESC)
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
                        new_list.append({
                            "user": r['member']['uname'],
                            "message": f"🕐 {format_time(ctime)}\n{r['content']['message']}",
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
    try:
        u = user.User(TARGET_UID, credential=credential)
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
    
    # 获取关注列表
    following = load_following_cache()
    if not following:
        following = await get_following_list(SOURCE_UID, credential)
        if not following:
            logging.warning("获取关注列表失败，使用备选")
            following = [3546905852250875, 3546961271589219, 3546610447419885, 285340365, SOURCE_UID]
        else:
            save_following_cache(following)
    if SOURCE_UID not in following:
        following.append(SOURCE_UID)
    logging.info(f"监控 UID 列表 ({len(following)} 个)")
    
    # 初始化所有 UID
    for uid in following:
        await init_user_dynamics(uid, credential, state)
        await asyncio.sleep(0.3)
    state.save()
    
    # 初始化视频
    aid, title = await sync_latest_video(credential)
    
    # 定时器
    last_video = time.time()
    last_comment = int(time.time())
    last_dynamic = time.time()
    last_following = time.time()
    last_heartbeat = time.time()
    seen_comments = set()
    
    logging.info("监控服务已启动，等待新动态...")
    
    while True:
        try:
            now = time.time()
            
            # 刷新关注列表
            if now - last_following >= FOLLOWING_REFRESH_INTERVAL:
                new_list = await get_following_list(SOURCE_UID, credential)
                if new_list:
                    if SOURCE_UID not in new_list:
                        new_list.append(SOURCE_UID)
                    old_set, new_set = set(following), set(new_list)
                    added, removed = new_set - old_set, old_set - new_set
                    for uid in added:
                        await init_user_dynamics(uid, credential, state)
                        logging.info(f"新增监控 UID {uid}")
                    for uid in removed:
                        state.remove_uid(uid)
                        logging.info(f"移除监控 UID {uid}")
                    following = new_list
                    save_following_cache(following)
                    state.save()
                last_following = now
            
            # 动态监控
            if now - last_dynamic >= DYNAMIC_CHECK_INTERVAL:
                all_alerts = []
                for uid in following:
                    alerts = await check_dynamics_incremental(uid, credential, state)
                    if alerts:
                        all_alerts.extend(alerts)
                    await asyncio.sleep(0.3)
                if all_alerts:
                    # 添加关键词后发送
                    for alert in all_alerts:
                        alert['message'] = f"{WEBHOOK_KEYWORD}\n{alert['message']}"
                    try:
                        notifier.send_webhook_notification("💡 特别关注UP主发布新内容", all_alerts)
                        logging.info(f"🚀 发送 {len(all_alerts)} 条动态通知")
                    except Exception as e:
                        logging.error(f"动态通知失败: {e}")
                    state.save()
                last_dynamic = now
            
            # 评论监控
            if aid and (now - last_comment >= COMMENT_SCAN_INTERVAL):
                new_c, new_t = await check_comments(int(aid), credential, last_comment, seen_comments)
                if new_c:
                    new_c.sort(key=lambda x: x["ctime"])
                    for alert in new_c:
                        alert['message'] = f"{WEBHOOK_KEYWORD}\n{alert['message']}"
                    try:
                        notifier.send_webhook_notification(title, new_c)
                        logging.info(f"💬 发送 {len(new_c)} 条评论通知")
                    except Exception as e:
                        logging.error(f"评论通知失败: {e}")
                last_comment = max(last_comment, new_t)
            
            # 视频监控
            if now - last_video >= VIDEO_CHECK_INTERVAL:
                new_aid, new_title = await sync_latest_video(credential)
                if new_aid:
                    aid, title = new_aid
                last_video = now
            
            # 心跳
            if now - last_heartbeat >= 10:
                logging.info("💓 心跳: 监控系统正常运行中")
                last_heartbeat = now
            
            await asyncio.sleep(2)
            
        except KeyboardInterrupt:
            logging.info("用户中断")
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
