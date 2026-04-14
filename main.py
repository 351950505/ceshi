# ---------------- 动态（修复与安全诊断版） ----------------
def init_extra_dynamics(header):
    seen = {}
    for uid in EXTRA_DYNAMIC_UIDS:
        seen[uid] = set()
        try:
            data = safe_request(
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
                {"host_mid": uid},
                header
            )
            if data.get("code") == 0:
                for item in (data.get("data") or {}).get("items", []):
                    if item.get("id_str"):
                        seen[uid].add(item["id_str"])
            else:
                logging.warning(f"初始化动态失败 UID: {uid}, 返回: {data}")
        except Exception as e:
            logging.error(f"初始化动态异常 UID: {uid}, 错误: {e}")
            
    return seen


def extract_dynamic_text(item):
    """安全解析无截断，支持富文本换行"""
    try:
        modules = item.get("modules", {})
        dyn = modules.get("module_dynamic", {})
        desc = dyn.get("desc", {})
        
        text_parts = []
        
        # 1. 优先提取富文本（Rich Text Nodes），完美保留换行和表情
        rich_nodes = desc.get("rich_text_nodes", [])
        if rich_nodes:
            for node in rich_nodes:
                text_parts.append(str(node.get("text", "")))
                
        # 2. 如果没富文本，退化使用普通的 text
        if not text_parts and desc.get("text"):
            text_parts.append(str(desc.get("text", "")))
            
        # 3. 如果连 text 都没有（比如纯分享视频/专栏），去主要内容里找标题和摘要
        if not text_parts:
            major = dyn.get("major", {})
            # 安全递归查找常见的文本字段
            def safe_walk(x):
                if isinstance(x, dict):
                    for k, v in x.items():
                        if k in ["title", "summary", "desc"] and isinstance(v, str) and v.strip():
                            text_parts.append(v.strip())
                        safe_walk(v)
                elif isinstance(x, list):
                    for i in x:
                        safe_walk(i)
            safe_walk(major)

        # 4. 组装结果，不加 [:500] 的限制！
        final_text = "".join(text_parts).strip()
        
        if final_text:
            return final_text
            
        return "【发布了新内容】（可能是纯图片/转发/视频）"

    except Exception as e:
        logging.error(f"动态文本解析异常: {e}")
        return "【动态解析异常兜底】发布了新动态"


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

            # --- 诊断 1：API 报错排查 ---
            if data.get("code") != 0:
                logging.warning(f"⚠️ 获取动态列表失败 UID: {uid}, 响应码: {data.get('code')}, 信息: {data.get('message')}")
                continue

            items = (data.get("data") or {}).get("items", [])
            if not items:
                logging.warning(f"⚠️ 动态列表为空 UID: {uid} (可能被B站风控或无动态)")
                continue

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

                # --- 诊断 2：时间拦截排查 ---
                time_diff = now_ts - pub_ts
                if time_diff > DYNAMIC_MAX_AGE:
                    logging.info(f"⏳ 忽略历史动态 [{name}], 距今 {int(time_diff)} 秒 (大于限制的 {DYNAMIC_MAX_AGE} 秒)")
                    continue

                # --- 解析与组装传送门 ---
                raw_text = extract_dynamic_text(item)
                portal_url = f"https://t.bilibili.com/{id_str}"
                
                # 在通知中加上送门
                final_msg = f"{raw_text}\n\n🔗 {portal_url}"
                
                has_new = True
                alerts.append({
                    "user": name,
                    "message": final_msg
                })

                logging.info(f"🆕 发现并准备推送新动态 [{name}]:\n{final_msg}")
                break # 每次仅推送最新的一条，防止刷屏

        except Exception as e:
            logging.error(f"❌ 动态请求循环抛出异常 {uid}:\n{traceback.format_exc()}")

        time.sleep(random.uniform(1, 2))

    # --- 诊断 3：Webhook 失败排查 ---
    if alerts:
        try:
            logging.info(f"🚀 开始发送 Webhook 通知，共 {len(alerts)} 条...")
            notifier.send_webhook_notification("💡 特别关注UP主发布新内容", alerts)
            logging.info("✅ Webhook 通知发送成功！")
        except Exception as e:
            logging.error(f"❌ Webhook 发送崩溃/失败 (可能是文本过长或包含非法字符): {e}\n{traceback.format_exc()}")

    return has_new
