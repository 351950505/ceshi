#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站监控系统（基于 bilibili-api-python 库）
功能：
- 自动获取指定用户的关注列表，监控所有关注用户的新动态
- 监控指定视频的新评论
- 自动处理 Cookie、签名、重试
"""

import asyncio
import os
import sys
import time
import json
import logging
import subprocess
from typing import List, Set

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
STATE_FILE = "monitor_state.json"  # 保存已处理过的动态 ID

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
    """解析 cookie 字符串为字典"""
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

# ======================== 状态管理 ========================
class StateManager:
    def __init__(self):
        self.seen_dynamics: Set[str] = set()
        self._load()

    def _load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    data = json.load(f)
                    self.seen_dynamics = set(data.get("seen_dynamics", []))
            except:
                pass

    def save(self):
        with open(STATE_FILE, "w") as f:
            json.dump({"seen_dynamics": list(self.seen_dynamics)}, f)

    def is_seen(self, dyn_id: str) -> bool:
        return dyn_id in self.seen_dynamics

    def add_seen(self, dyn_id: str):
        self.seen_dynamics.add(dyn_id)

# ======================== 关注列表获取 ========================
async def get_following_list(uid: int, credential: Credential) -> List[int]:
    """获取用户关注的 UID 列表（异步）"""
    u = user.User(uid, credential=credential)
    followings = []
    page = 1
    while True:
        try:
            resp = await u.get_followings(page=page, ps=50)
            followings.extend(resp['list'])
            if page * 50 >= resp['total']:
                break
            page += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            logging.error(f"获取关注列表失败: {e}")
            break
    return [f['mid'] for f in followings]

# ======================== 动态监控 ========================
async def check_dynamics(uid: int, credential: Credential, state: StateManager) -> List[dict]:
    """检查单个用户的新动态，返回需要推送的消息列表"""
    u = user.User(uid, credential=credential)
    alerts = []
    try:
        # 获取动态列表（最多10条，按时间倒序）
        dynamics = await u.get_dynamics(offset=None, need_top=False)
        items = dynamics.get('items', [])
        for item in items:
            dyn_id = item.get('id_str')
            if not dyn_id or state.is_seen(dyn_id):
                continue
            state.add_seen(dyn_id)

            # 提取作者信息
            modules = item.get('modules', {})
            author = modules.get('module_author', {})
            name = author.get('name', str(uid))
            pub_ts = author.get('pub_ts', 0)
            # 忽略太旧的动态（超过5分钟）
            if time.time() - pub_ts > 300:
                continue

            # 提取文本内容
            dyn_module = modules.get('module_dynamic', {})
            desc = dyn_module.get('desc', {})
            text = desc.get('text', '')
            if not text:
                # 尝试从 major 中提取
                major = dyn_module.get('major', {})
                if major.get('type') == 'MAJOR_TYPE_ARCHIVE':
                    archive = major.get('archive', {})
                    text = f"【视频】{archive.get('title', '')}\n{archive.get('desc', '')}"
                elif major.get('type') == 'MAJOR_TYPE_ARTICLE':
                    article = major.get('article', {})
                    text = f"【专栏】{article.get('title', '')}"
                else:
                    text = "发布了新动态"

            # 处理转发
            if item.get('type') == 'DYNAMIC_TYPE_FORWARD':
                orig = item.get('orig')
                if orig:
                    orig_text = ""
                    orig_modules = orig.get('modules', {})
                    orig_desc = orig_modules.get('module_dynamic', {}).get('desc', {})
                    orig_text = orig_desc.get('text', '')
                    if orig_text:
                        text = f"{text}\n【转发原文】{orig_text}" if text else f"【转发原文】{orig_text}"
                    orig_id = orig.get('id_str')
                    if orig_id:
                        text = f"{text}\n【原动态链接】https://t.bilibili.com/{orig_id}"

            link = f"https://t.bilibili.com/{dyn_id}"
            final_msg = f"{text}\n\n🔗 直达链接: {link}" if text else f"🔗 直达链接: {link}"
            alerts.append({"user": name, "message": final_msg})
            logging.info(f"✅ 新动态 [{name}]: {dyn_id}")
    except Exception as e:
        logging.error(f"检查 UID {uid} 动态失败: {e}")
    return alerts

# ======================== 评论监控 ========================
async def check_comments(aid: int, credential: Credential, last_time: int, seen_set: Set[str]) -> tuple:
    """检查视频的新评论，返回 (新评论列表, 最新评论时间)"""
    v = video.Video(aid, credential=credential)
    new_list = []
    max_ctime = last_time
    try:
        # 获取评论（按时间倒序）
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
                if ctime > last_time - 60:  # 只取最近60秒内的
                    all_old = False
                    rpid = str(r['rpid'])
                    if rpid not in seen_set:
                        seen_set.add(rpid)
                        new_list.append({
                            "user": r['member']['uname'],
                            "message": r['content']['message'],
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
async def sync_latest_video(credential: Credential) -> tuple:
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
                    # 存入数据库
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
    
    # 初始化关注列表
    following = await get_following_list(SOURCE_UID, credential)
    if not following:
        logging.warning("获取关注列表失败，使用备选列表")
        following = [3546905852250875, 3546961271589219, 3546610447419885, 285340365, SOURCE_UID]
    logging.info(f"监控 UID 列表 ({len(following)} 个)")

    # 初始化已见动态（避免启动时推送旧动态）
    for uid in following:
        try:
            dynamics = await user.User(uid, credential=credential).get_dynamics(offset=None, need_top=False)
            for item in dynamics.get('items', []):
                dyn_id = item.get('id_str')
                if dyn_id:
                    state.add_seen(dyn_id)
            logging.info(f"初始化 UID {uid} 完成，已记录 {len([d for d in state.seen_dynamics if d.startswith(str(uid))])} 条")
            await asyncio.sleep(0.5)
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
                    # 新增的 UID 需要初始化 seen
                    for uid in new_following:
                        if uid not in following:
                            try:
                                dynamics = await user.User(uid, credential=credential).get_dynamics(offset=None, need_top=False)
                                for item in dynamics.get('items', []):
                                    dyn_id = item.get('id_str')
                                    if dyn_id:
                                        state.add_seen(dyn_id)
                            except:
                                pass
                    following = new_following
                    logging.info(f"关注列表已刷新，共 {len(following)} 个")
                last_following_refresh = now

            # 2. 动态监控（每15秒）
            if now - last_dynamic_check >= DYNAMIC_CHECK_INTERVAL:
                all_alerts = []
                for uid in following:
                    alerts = await check_dynamics(uid, credential, state)
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
