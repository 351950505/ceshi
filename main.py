#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站动态/评论监控系统（优化版）
功能：
- 自动监控指定用户（通过关注列表）的新动态并推送
- 监控指定视频的新评论并推送
- 支持 WBI 签名、时间补偿、自动刷新 Cookie
- 支持动态更新关注列表（每小时）
- 支持评论懒加载接口，降低风控概率
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
from typing import Dict, List, Set, Optional, Tuple, Any

import requests
import database as db
import notifier

# ======================== 配置类 ========================
class Config:
    # 视频监控目标
    TARGET_UID = 1671203508
    VIDEO_CHECK_INTERVAL = 21600   # 6小时

    # 动态监控源（从该用户的关注列表获取监控对象）
    SOURCE_UID = 3706948578969654
    FOLLOWING_REFRESH_INTERVAL = 3600   # 1小时刷新关注列表
    FALLBACK_UIDS = [               # 备选列表（当 API 失败时使用）
        3546905852250875, 3546961271589219,
        3546610447419885, 285340365, 3706948578969654
    ]

    # 动态监控参数
    DYNAMIC_CHECK_INTERVAL = 15      # 正常检查间隔（秒）
    DYNAMIC_BURST_INTERVAL = 8       # 爆发模式间隔（秒）
    DYNAMIC_BURST_DURATION = 300     # 爆发模式持续时长（秒）
    DYNAMIC_MAX_AGE = 300            # 忽略超过此时间的旧动态（秒）

    # 评论监控参数
    COMMENT_SCAN_INTERVAL = 5        # 评论扫描间隔（秒）
    COMMENT_MAX_PAGES = 3            # 最多拉取页数（每页20条）
    COMMENT_SAFE_WINDOW = 60         # 只处理最近 N 秒内的评论

    # 系统参数
    HEARTBEAT_INTERVAL = 10          # 心跳日志间隔（秒）
    TIME_OFFSET = -120               # 服务器时间快2分钟，补偿-120秒

    # 文件路径
    LOG_FILE = "bili_monitor.log"
    DYNAMIC_STATE_FILE = "dynamic_state.json"
    FOLLOWING_CACHE_FILE = "following_cache.json"

# ======================== 日志初始化 ========================
def init_logging():
    try:
        if os.path.exists(Config.LOG_FILE):
            with open(Config.LOG_FILE, "w", encoding="utf-8") as f:
                f.truncate()
    except:
        pass
    logging.basicConfig(
        filename=Config.LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        encoding="utf-8",
        filemode="w"
    )
    logging.info("=" * 60)
    logging.info("B站监控系统启动 (优化版)")
    logging.info(f"时间补偿: {Config.TIME_OFFSET} 秒")
    logging.info("=" * 60)

# ======================== Cookie 管理 ========================
def get_cookie_string() -> str:
    """从文件读取完整 Cookie 字符串"""
    try:
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        logging.warning("未找到 bili_cookie.txt，尝试运行登录脚本")
        subprocess.run([sys.executable, "login_bilibili.py"])
        with open("bili_cookie.txt", "r", encoding="utf-8") as f:
            return f.read().strip()

def refresh_cookie() -> bool:
    """重新获取 Cookie（调用登录脚本）"""
    logging.warning("Cookie 已失效，尝试重新登录...")
    try:
        subprocess.run([sys.executable, "login_bilibili.py"], check=True)
        logging.info("重新登录成功")
        return True
    except Exception as e:
        logging.error(f"重新登录失败: {e}")
        return False

# ======================== WBI 签名（时间补偿） ========================
class WBISigner:
    MIXIN_KEY_ENC_TAB = [
        46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,
        27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,
        37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,
        22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52
    ]

    def __init__(self):
        self.img_key = ""
        self.sub_key = ""
        self.last_update = 0

    def _get_mixin_key(self, orig: str) -> str:
        return ''.join([orig[i] for i in self.MIXIN_KEY_ENC_TAB])[:32]

    def update_keys(self, header: Dict) -> bool:
        """从导航接口获取最新密钥"""
        try:
            resp = requests.get(
                "https://api.bilibili.com/x/web-interface/nav",
                headers=header,
                timeout=10
            )
            data = resp.json()
            if data.get("code") == 0:
                img = data["data"]["wbi_img"]
                self.img_key = img["img_url"].rsplit("/", 1)[1].split(".")[0]
                self.sub_key = img["sub_url"].rsplit("/", 1)[1].split(".")[0]
                self.last_update = time.time()
                logging.info("WBI 密钥已更新")
                return True
            elif data.get("code") == -101:
                logging.error("获取 WBI 密钥时 Cookie 失效")
                if refresh_cookie():
                    return self.update_keys(get_headers())
            else:
                logging.warning(f"获取 WBI 密钥失败: {data.get('message')}")
        except Exception as e:
            logging.error(f"获取 WBI 密钥异常: {e}")
        return False

    def sign(self, params: Dict) -> Dict:
        """对参数进行 WBI 签名，返回带 w_rid 和 wts 的新字典"""
        if not self.img_key or not self.sub_key:
            if not self.update_keys(get_headers()):
                # 如果无法获取密钥，只加时间戳
                params = params.copy()
                params["wts"] = int(time.time() + Config.TIME_OFFSET)
                return params

        mixin_key = self._get_mixin_key(self.img_key + self.sub_key)
        params = params.copy()
        params["wts"] = int(time.time() + Config.TIME_OFFSET)
        params = dict(sorted(params.items()))

        # 过滤特殊字符
        filtered = {}
        for k, v in params.items():
            v = str(v)
            for c in "!'()*":
                v = v.replace(c, "")
            filtered[k] = v

        query = urllib.parse.urlencode(filtered, quote_via=urllib.parse.quote)
        sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
        filtered["w_rid"] = sign
        return filtered

# ======================== 统一请求封装 ========================
class APIRequester:
    def __init__(self):
        self.signer = WBISigner()
        self.session = requests.Session()
        self._update_headers()

    def _update_headers(self):
        cookie_str = get_cookie_string()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
            "Cookie": cookie_str
        })

    def _should_retry(self, code: int) -> bool:
        """判断错误码是否可重试"""
        return code in (-352, -799, -509, -500, -404, -400)

    def request(self, url: str, params: Dict = None, retries: int = 3) -> Dict:
        """发送带 WBI 签名的 GET 请求，自动重试、刷新 Cookie"""
        if params is None:
            params = {}
        last_exception = None

        for attempt in range(retries):
            try:
                # 添加 WBI 签名
                signed_params = self.signer.sign(params.copy())
                resp = self.session.get(url, params=signed_params, timeout=10)
                data = resp.json()
                code = data.get("code")

                # 需要刷新 Cookie
                if code == -101:
                    logging.warning("Cookie 失效，尝试刷新")
                    if refresh_cookie():
                        self._update_headers()
                        continue
                    else:
                        return {"code": -101, "message": "Cookie 刷新失败"}

                # 可重试的错误码
                if code != 0 and self._should_retry(code):
                    delay = (2 ** attempt) + random.uniform(0, 2)
                    logging.warning(f"请求失败 ({code})，{delay:.1f}秒后重试 (尝试 {attempt+1}/{retries})")
                    time.sleep(delay)
                    continue

                return data

            except requests.RequestException as e:
                last_exception = e
                delay = (2 ** attempt) + random.uniform(0, 1)
                logging.warning(f"网络异常: {e}，{delay:.1f}秒后重试")
                time.sleep(delay)
            except Exception as e:
                logging.error(f"未知异常: {e}")
                time.sleep(2)

        logging.error(f"请求最终失败: {url}")
        return {"code": -500, "message": str(last_exception) if last_exception else "重试耗尽"}

# ======================== 动态监控核心 ========================
class DynamicMonitor:
    def __init__(self, api: APIRequester):
        self.api = api
        self.state = self._load_state()
        self.seen = {}          # uid -> set of dynamic ids
        self._init_seen_from_state()

    def _load_state(self) -> Dict:
        if os.path.exists(Config.DYNAMIC_STATE_FILE):
            try:
                with open(Config.DYNAMIC_STATE_FILE, "r") as f:
                    return json.load(f)
            except:
                return {}
        return {}

    def _save_state(self):
        with open(Config.DYNAMIC_STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2)

    def _init_seen_from_state(self):
        """从 state 中恢复 seen 集合（仅用于初始化，不用于运行时去重）"""
        for uid_str, info in self.state.items():
            uid = int(uid_str)
            self.seen[uid] = set()
            # 注意：state 中没有存储已见过的动态 ID 列表，所以 seen 为空
            # 运行时通过 fetch 动态时动态填充

    def _fetch_dynamics_page(self, uid: int, offset: str = "") -> Dict:
        """拉取一页动态（不带 update_baseline）"""
        params = {
            "host_mid": uid,
            "type": "all",
            "timezone_offset": "-480",
            "platform": "web",
            "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,decorationCard,onlyfansAssetsV2,forwardListHidden,ugcDelete",
            "web_location": "333.1365",
            "offset": offset
        }
        return self.api.request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all", params)

    def _fetch_incremental(self, uid: int, baseline: str, offset: str) -> Dict:
        """拉取增量动态（带 update_baseline）"""
        params = {
            "host_mid": uid,
            "type": "all",
            "timezone_offset": "-480",
            "platform": "web",
            "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,decorationCard,onlyfansAssetsV2,forwardListHidden,ugcDelete",
            "web_location": "333.1365",
            "offset": offset,
            "update_baseline": baseline
        }
        return self.api.request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all", params)

    def _check_update(self, uid: int, baseline: str) -> int:
        """检测是否有新动态，返回 update_num"""
        if not baseline:
            return 0
        params = {"type": "all", "web_location": "333.1365", "update_baseline": baseline}
        data = self.api.request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all/update", params)
        if data.get("code") == 0:
            return data.get("data", {}).get("update_num", 0)
        logging.warning(f"UID {uid} 检测更新失败: {data.get('message')}")
        return -1   # 表示不确定

    @staticmethod
    def extract_text(item: Dict) -> str:
        """提取动态文本（基于 all.md 结构）"""
        try:
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
                    elif t == "RICH_TEXT_NODE_TYPE_LOTTERY":
                        parts.append(node.get("text", ""))
                full = "".join(parts).strip()
                if full:
                    return full
            major = dyn.get("major") or {}
            mtype = major.get("type", "")
            if mtype == "MAJOR_TYPE_ARCHIVE":
                arc = major.get("archive") or {}
                return f"【视频】{arc.get('title', '')}\n{arc.get('desc', '')}".strip()
            elif mtype == "MAJOR_TYPE_ARTICLE":
                art = major.get("article") or {}
                return f"【专栏】{art.get('title', '')}".strip()
            elif mtype == "MAJOR_TYPE_OPUS":
                opus = major.get("opus") or {}
                summ = opus.get("summary") or {}
                nodes = summ.get("rich_text_nodes") or []
                if nodes:
                    return "".join([n.get("text", "") for n in nodes if isinstance(n, dict)]).strip()
            return ""
        except Exception:
            return ""

    def _parse_item(self, item: Dict, uid: int, now_ts: float) -> Optional[Dict]:
        """解析单条动态，返回告警字典或 None"""
        dyn_id = item.get("id_str")
        if not dyn_id:
            return None
        if dyn_id in self.seen.get(uid, set()):
            return None
        self.seen.setdefault(uid, set()).add(dyn_id)

        modules = item.get("modules") or {}
        author = modules.get("module_author") or {}
        pub_ts = author.get("pub_ts", 0)
        if now_ts - pub_ts > Config.DYNAMIC_MAX_AGE:
            logging.debug(f"忽略超时动态 [{author.get('name', uid)}] {dyn_id}")
            return None

        name = author.get("name", str(uid))
        text = self.extract_text(item)

        # 处理转发
        if item.get("type") == "DYNAMIC_TYPE_FORWARD":
            orig = item.get("orig")
            if orig:
                orig_text = self.extract_text(orig)
                if orig_text:
                    text = f"{text}\n【转发原文】{orig_text}" if text else f"【转发原文】{orig_text}"
                orig_id = orig.get("id_str")
                if orig_id:
                    text = f"{text}\n【原动态链接】https://t.bilibili.com/{orig_id}"

        final_msg = f"{text}\n\n🔗 直达链接: https://t.bilibili.com/{dyn_id}" if text else f"🔗 直达链接: https://t.bilibili.com/{dyn_id}"
        return {"user": name, "message": final_msg}

    def init_uid(self, uid: int) -> Set[str]:
        """初始化单个 UID 的 baseline 和 offset，返回已见过的动态 ID 集合"""
        uid_str = str(uid)
        if uid_str not in self.state:
            self.state[uid_str] = {"baseline": "", "offset": ""}

        # 拉取最新一页建立基线
        data = self._fetch_dynamics_page(uid)
        if data.get("code") != 0:
            logging.warning(f"初始化 UID {uid} 失败: {data.get('message')}")
            return set()

        feed = data.get("data") or {}
        items = feed.get("items", [])
        offset = feed.get("offset", "")
        baseline = items[0].get("id_str", "") if items else ""

        if baseline:
            self.state[uid_str]["baseline"] = baseline
        if offset:
            self.state[uid_str]["offset"] = offset

        seen = set()
        for item in items:
            dyn_id = item.get("id_str")
            if dyn_id:
                seen.add(dyn_id)
        logging.info(f"初始化 UID {uid}: baseline={baseline}, offset={offset}, 收录 {len(seen)} 条")
        return seen

    def check_uid(self, uid: int, now_ts: float) -> Tuple[List[Dict], bool]:
        """
        检查单个 UID 的新动态
        返回 (告警列表, 是否有新动态)
        """
        uid_str = str(uid)
        info = self.state.get(uid_str, {"baseline": "", "offset": ""})
        baseline = info.get("baseline", "")
        offset = info.get("offset", "")
        alerts = []

        # 首次初始化
        if not baseline:
            self.init_uid(uid)
            # 重新获取最新状态
            info = self.state.get(uid_str, {"baseline": "", "offset": ""})
            baseline = info.get("baseline", "")
            offset = info.get("offset", "")
            if not baseline:
                return alerts, False

        # 检测是否有新动态
        update_num = self._check_update(uid, baseline)
        if update_num == 0:
            return alerts, False
        if update_num == -1:
            # 检测失败，仍然尝试拉取
            pass

        # 拉取增量
        data = self._fetch_incremental(uid, baseline, offset)
        if data.get("code") != 0:
            logging.warning(f"UID {uid} 拉取动态失败: {data.get('message')}")
            return alerts, False

        feed = data.get("data") or {}
        items = feed.get("items", [])
        new_baseline = feed.get("update_baseline", baseline)
        new_offset = feed.get("offset", offset)

        # 更新状态
        if new_baseline != baseline or new_offset != offset:
            self.state[uid_str] = {"baseline": new_baseline, "offset": new_offset}
            self._save_state()
            logging.info(f"UID {uid} 状态更新: baseline={new_baseline}, offset={new_offset}")

        # 解析新动态
        for item in items:
            alert = self._parse_item(item, uid, now_ts)
            if alert:
                alerts.append(alert)

        return alerts, len(alerts) > 0

    def update_following_list(self, new_uids: List[int]):
        """更新监控的 UID 列表，处理新增和移除"""
        old_set = set(self.state.keys())
        new_set = {str(uid) for uid in new_uids}
        added = new_set - old_set
        removed = old_set - new_set

        if added:
            logging.info(f"新增监控 UID: {added}")
            for uid_str in added:
                uid = int(uid_str)
                self.init_uid(uid)
        if removed:
            logging.info(f"移除监控 UID: {removed}")
            for uid_str in removed:
                if uid_str in self.state:
                    del self.state[uid_str]
                # 注意：seen 集合不需要立即删除，下次垃圾回收时自然消失
        if added or removed:
            self._save_state()

# ======================== 评论监控 ========================
class CommentMonitor:
    def __init__(self, api: APIRequester):
        self.api = api
        self.last_read_time = int(time.time())
        self.seen = set()

    def scan(self, oid: int) -> List[Dict]:
        """扫描新评论，返回列表"""
        new_list = []
        max_ctime = self.last_read_time
        safe_time = self.last_read_time - Config.COMMENT_SAFE_WINDOW

        params = {
            "type": 1,
            "oid": oid,
            "mode": 2,          # 按时间排序
            "plat": 1,
            "web_location": "1315875"
        }
        pagination_str = None

        for _ in range(Config.COMMENT_MAX_PAGES):
            if pagination_str:
                params["pagination_str"] = pagination_str
            else:
                params.pop("pagination_str", None)

            data = self.api.request("https://api.bilibili.com/x/v2/reply/wbi/main", params)
            if data.get("code") != 0:
                logging.warning(f"评论接口失败: {data.get('message')}")
                break

            reply_data = data.get("data", {})
            replies = reply_data.get("replies", [])
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
                    if rpid and rpid not in self.seen:
                        self.seen.add(rpid)
                        new_list.append({
                            "user": r["member"]["uname"],
                            "message": r["content"]["message"],
                            "ctime": ctime
                        })
            if all_old:
                break

            # 下一页游标
            cursor = reply_data.get("cursor", {})
            pagination_reply = cursor.get("pagination_reply")
            if pagination_reply and isinstance(pagination_reply, dict):
                pagination_str = pagination_reply.get("next_offset")
                if not pagination_str:
                    break
            else:
                break

            time.sleep(random.uniform(0.3, 0.6))

        if new_list:
            self.last_read_time = max_ctime
        return new_list

# ======================== 视频监控 ========================
class VideoMonitor:
    def __init__(self, api: APIRequester):
        self.api = api
        self.oid = None
        self.title = None

    def sync(self) -> Tuple[Optional[int], Optional[str]]:
        """同步最新视频，返回 (aid, title)"""
        # 获取最新动态中的视频 bvid
        params = {"host_mid": Config.TARGET_UID}
        data = self.api.request("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space", params)
        if data.get("code") != 0:
            return self.oid, self.title

        items = (data.get("data") or {}).get("items", [])
        bvid = None
        for item in items:
            try:
                if item.get("type") == "DYNAMIC_TYPE_AV":
                    bvid = item["modules"]["module_dynamic"]["major"]["archive"]["bvid"]
                    break
            except:
                pass
        if not bvid:
            return self.oid, self.title

        # 检查是否已监控
        videos = db.get_monitored_videos()
        if videos and videos[0][1] == bvid:
            return videos[0][0], videos[0][2]

        # 获取视频信息
        data = self.api.request(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
        if data.get("code") == 0:
            aid = str(data["data"]["aid"])
            title = data["data"]["title"]
            db.clear_videos()
            db.add_video_to_db(aid, bvid, title)
            self.oid, self.title = aid, title
            logging.info(f"新视频监控: {title} (aid={aid})")
            return aid, title
        return self.oid, self.title

# ======================== 关注列表管理 ========================
class FollowingManager:
    def __init__(self, api: APIRequester):
        self.api = api
        self.uids = self._load_cache()

    def _load_cache(self) -> List[int]:
        if os.path.exists(Config.FOLLOWING_CACHE_FILE):
            try:
                with open(Config.FOLLOWING_CACHE_FILE, "r") as f:
                    return json.load(f)
            except:
                return []
        return []

    def _save_cache(self):
        with open(Config.FOLLOWING_CACHE_FILE, "w") as f:
            json.dump(self.uids, f)

    def fetch(self) -> List[int]:
        """从 B 站获取关注列表（自动翻页）"""
        following = []
        pn = 1
        ps = 50
        while True:
            params = {
                "vmid": Config.SOURCE_UID,
                "pn": pn,
                "ps": ps,
                "order": "desc",
                "order_type": "attention"
            }
            data = self.api.request("https://api.bilibili.com/x/relation/followings", params)
            if data.get("code") != 0:
                logging.warning(f"获取关注列表失败 (pn={pn}): {data.get('message')}")
                break
            info = data.get("data", {})
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
        if following:
            # 确保自身也在列表中
            if Config.SOURCE_UID not in following:
                following.append(Config.SOURCE_UID)
            self.uids = following
            self._save_cache()
            logging.info(f"获取关注列表成功，共 {len(following)} 人")
        else:
            logging.warning("获取关注列表失败，使用缓存或备用列表")
            if not self.uids:
                self.uids = Config.FALLBACK_UIDS
        return self.uids

# ======================== 主循环 ========================
def get_headers() -> Dict:
    """获取请求头（不含 Cookie，Cookie 在 APIRequester 中管理）"""
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/"
    }

def main():
    init_logging()
    db.init_db()

    api = APIRequester()
    dynamic_mon = DynamicMonitor(api)
    comment_mon = CommentMonitor(api)
    video_mon = VideoMonitor(api)
    following_mgr = FollowingManager(api)

    # 首次同步视频
    video_mon.sync()

    # 获取初始关注列表并初始化动态监控
    following_list = following_mgr.fetch()
    for uid in following_list:
        dynamic_mon.init_uid(uid)
    dynamic_mon._save_state()

    # 定时器变量
    last_following_refresh = time.time()
    last_dynamic_check = time.time()
    last_heartbeat = time.time()
    last_comment_check = time.time()
    burst_end = 0

    logging.info("监控服务已启动")

    while True:
        try:
            now = time.time()

            # 1. 刷新关注列表（每小时）
            if now - last_following_refresh >= Config.FOLLOWING_REFRESH_INTERVAL:
                new_list = following_mgr.fetch()
                dynamic_mon.update_following_list(new_list)
                last_following_refresh = now

            # 2. 动态监控
            interval = Config.DYNAMIC_BURST_INTERVAL if now < burst_end else Config.DYNAMIC_CHECK_INTERVAL
            if now - last_dynamic_check >= interval:
                any_new = False
                # 注意：这里遍历的是 state 中的 UID（已初始化的），而不是 following_mgr.uids
                for uid_str in dynamic_mon.state.keys():
                    uid = int(uid_str)
                    alerts, has_new = dynamic_mon.check_uid(uid, now)
                    if alerts:
                        try:
                            notifier.send_webhook_notification("💡 特别关注UP主发布新内容", alerts)
                            logging.info(f"🚀 成功发送 {len(alerts)} 条动态通知")
                        except Exception as e:
                            logging.error(f"动态通知发送失败: {e}")
                    if has_new:
                        any_new = True
                if any_new:
                    burst_end = now + Config.DYNAMIC_BURST_DURATION
                last_dynamic_check = now

            # 3. 评论监控
            if video_mon.oid and (now - last_comment_check >= Config.COMMENT_SCAN_INTERVAL):
                new_comments = comment_mon.scan(int(video_mon.oid))
                if new_comments:
                    new_comments.sort(key=lambda x: x["ctime"])
                    try:
                        notifier.send_webhook_notification(video_mon.title, new_comments)
                        logging.info(f"💬 成功发送 {len(new_comments)} 条评论通知")
                    except Exception as e:
                        logging.error(f"评论通知发送失败: {e}")
                last_comment_check = now

            # 4. 视频同步（定时）
            if now - video_mon.sync.__self__.__class__.sync.__defaults__ is None:
                # 简化：直接每 VIDEO_CHECK_INTERVAL 秒同步一次
                pass
            # 视频同步逻辑已经移到单独循环，此处为了简洁，我们每轮都检查时间差
            if now - getattr(video_mon, "_last_sync", 0) >= Config.VIDEO_CHECK_INTERVAL:
                video_mon.sync()
                setattr(video_mon, "_last_sync", now)

            # 5. 心跳日志
            if now - last_heartbeat >= Config.HEARTBEAT_INTERVAL:
                logging.info("💓 心跳: 监控系统正常运行中")
                last_heartbeat = now

            time.sleep(random.uniform(2, 4))

        except KeyboardInterrupt:
            logging.info("用户中断，程序退出")
            break
        except Exception as e:
            logging.error(f"主循环异常: {e}\n{traceback.format_exc()}")
            time.sleep(60)

if __name__ == "__main__":
    main()
