import re
import sys
import requests
import json
import hashlib
import urllib.parse
import time
import datetime
import pandas as pd
import subprocess
import platform
if platform.system() == "Windows":
    import msvcrt
else:
    import select

import database as db
import notifier
import bvget

def get_header():
    try:
        with open('bili_cookie.txt', 'r', encoding='utf-8') as f:
            cookie = f.read().strip()
        if not cookie:
            raise FileNotFoundError("Cookie 文件为空。")
    except FileNotFoundError:
        print("提示：'bili_cookie.txt' 文件未找到或为空。")
        print("正在尝试调用 'login_bilibili.py' 进行自动登录...")
        try:
            subprocess.run([sys.executable, 'login_bilibili.py'], check=False)
            with open('bili_cookie.txt', 'r', encoding='utf-8') as f:
                cookie = f.read().strip()
            if not cookie:
                print("错误：登录后 Cookie 仍为空")
                sys.exit(1)

            temp_header = {
                "Cookie": cookie,
                "User-Agent": 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                "Referer": "https://www.bilibili.com"
            }
            all_bvids = bvget.get_all_bvids_from_api()
            if all_bvids:
                print(f"获取到 {len(all_bvids)} 个视频，正在添加...")
                added = 0
                for bv in all_bvids:
                    oid, title = get_information(bv, temp_header)
                    if oid and title and db.add_video_to_db(oid, bv, title):
                        added += 1
                    time.sleep(0.5)
                print(f"成功添加 {added} 个新视频")
        except Exception as e:
            print(f"登录/读取失败: {e}")
            sys.exit(1)

    return {
        "Cookie": cookie,
        "User-Agent": 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        "Referer": "https://www.bilibili.com"
    }

def get_information(bv, header):
    url = f"https://api.bilibili.com/x/web-interface/view?bvid={bv}"
    try:
        resp = requests.get(url, headers=header, timeout=5)
        data = resp.json()
        if data.get('code') == 0:
            return str(data['data']['aid']), data['data']['title'].strip()
    except:
        pass
    return None, None

def md5(code):
    return hashlib.md5(code.encode('utf-8')).hexdigest()

def fetch_latest_comments(oid, header):
    if not oid: return []
    mixin_key_salt = "ea1db124af3c7062474693fa704f4ff8"
    params = {'oid': oid, 'type': 1, 'mode': 2, 'plat': 1, 'web_location': 1315875, 'wts': int(time.time())}
    w_rid = md5(urllib.parse.urlencode(sorted(params.items())) + mixin_key_salt)
    params['w_rid'] = w_rid
    url = f"https://api.bilibili.com/x/v2/reply/wbi/main?{urllib.parse.urlencode(params)}"
    try:
        resp = requests.get(url, headers=header, timeout=5)
        data = resp.json()
        return data.get('data', {}).get('replies', []) or []
    except:
        return []

def fetch_all_sub_replies(oid, root_rpid, header):
    all_replies = []
    pn = 1
    while True:
        url = f"https://api.bilibili.com/x/v2/reply/reply?oid={oid}&type=1&root={root_rpid}&pn={pn}&ps=20"
        try:
            resp = requests.get(url, headers=header, timeout=5)
            data = resp.json()
            if data.get('code') != 0 or not data.get('data'):
                break
            replies = data['data'].get('replies', [])
            if not replies:
                break
            all_replies.extend(replies)
            pn += 1
            time.sleep(0.8)
        except:
            break
    return all_replies

def display_main_menu():
    header = get_header()
    selected = {}
    while True:
        print("\n=== B站评论监控菜单 ===")
        saved = db.get_monitored_videos()
        if saved:
            for i, (_, bv, title) in enumerate(saved):
                print(f" [{i+1}] {title} ({bv})")
        print("\na. 添加 BV 号")
        print("r. 移除视频")
        print("s. 开始监控")
        print("q. 退出")
        if selected:
            print("\n已选:", ", ".join(d['title'] for d in selected.values()))
        choice = input("选择: ").strip().lower()
        if choice == 'a':
            bvs = input("输入 BV 号 (逗号/空格分隔): ").strip().split()
            for bv in bvs:
                oid, title = get_information(bv.upper(), header)
                if oid and db.add_video_to_db(oid, bv.upper(), title):
                    print(f"添加: {title}")
        elif choice == 'r':
            idx = input("移除编号: ").strip()
            try:
                i = int(idx) - 1
                if 0 <= i < len(saved):
                    db.remove_video_from_db(saved[i][0])
                    print("已移除")
            except:
                pass
        elif choice == 's':
            if not selected and saved:
                selected = {oid: {"title": title, "bv_id": bv} for oid, bv, title in saved}
            return [(oid, d) for oid, d in selected.items()]
        elif choice == 'q':
            sys.exit(0)

def process_and_notify_comment(reply, oid, seen_ids, parent_user=None):
    rpid = reply['rpid_str']
    if rpid in seen_ids:
        return None
    seen_ids.add(rpid)
    db.add_comment_to_db(rpid, oid)
    if parent_user:
        at_name = parent_user
        if reply.get('at_details'):
            at_name = next((item['uname'] for item in reply['at_details']), parent_user)
        ctype = f"回复@{at_name}"
    else:
        ctype = "主评论"
    return {
        "user": reply['member']['uname'],
        "message": reply['content']['message'],
        "time": pd.to_datetime(reply["ctime"], unit='s', utc=True).tz_convert('Asia/Shanghai'),
        "type": ctype
    }

def wait_with_manual_trigger(interval_seconds):
    print(f"\n等待 {interval_seconds//60}分{interval_seconds%60}秒... 按 Enter 立即检查")
    start = time.time()
    while time.time() - start < interval_seconds:
        if platform.system() == "Windows":
            if msvcrt.kbhit() and msvcrt.getch() in [b'\r', b'\n']:
                print("手动触发")
                return
        else:
            r, _, _ = select.select([sys.stdin], [], [], 0.1)
            if r:
                sys.stdin.readline()
                print("手动触发")
                return
        time.sleep(0.1)

def start_monitoring(targets, header, interval, webhook_enabled):
    video_targets = {}
    for oid, data in targets:
        video_targets[oid] = {
            "title": data['title'],
            "seen_ids": db.load_seen_comments_for_video(oid)
        }
        print(f"加载 {data['title']} 历史评论: {len(video_targets[oid]['seen_ids'])} 条")

    while True:
        print(f"\n[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] 开始检查")
        for oid, data in video_targets.items():
            title = data['title']
            seen = data['seen_ids']
            print(f"检查 {title}")
            comments = fetch_latest_comments(oid, header)
            new_comments = []
            for c in comments:
                nc = process_and_notify_comment(c, oid, seen)
                if nc:
                    new_comments.append(nc)

                print(f"  获取 {c['member']['uname']} 的所有回复...")
                subs = fetch_all_sub_replies(oid, c['rpid_str'], header)
                for sub in subs:
                    ns = process_and_notify_comment(sub, oid, seen, c['member']['uname'])
                    if ns:
                        new_comments.append(ns)

            if new_comments:
                new_comments.sort(key=lambda x: x['time'])
                print(f"\n🔥 {title} 新增 {len(new_comments)} 条")
                for nc in new_comments:
                    print(f"{nc['type']} | {nc['user']} : {nc['message']}")
                    print(f"  {nc['time']:%Y-%m-%d %H:%M:%S}")
                if webhook_enabled:
                    notifier.send_webhook_notification(title, new_comments)
            time.sleep(2)
        wait_with_manual_trigger(interval)

if __name__ == "__main__":
    db.init_db()
    targets = display_main_menu()
    if not targets:
        sys.exit(0)

    interval_min = 5
    try:
        inp = input(f"检查间隔(分钟，默认{interval_min}): ").strip()
        if inp:
            interval_min = float(inp)
    except:
        pass
    interval = max(30, int(interval_min * 60))

    webhook_enabled = False
    if notifier.check_webhook_configured():
        choice = input("启用 Webhook 通知? (y/n): ").lower()
        webhook_enabled = choice == 'y'

    header = get_header()
    start_monitoring(targets, header, interval, webhook_enabled)
