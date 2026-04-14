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
                logging.warning(f"❌ 获取 UID {uid} 动态失败，API 返回码: {data.get('code')}, 信息: {data.get('message')}")
                continue
                
            items = (data.get("data") or {}).get("items", [])
            if not items:
                logging.debug(f"ℹ️ UID {uid} 当前无动态数据")
                continue
                
            for item in items:
                id_str = item.get("id_str")
                if not id_str:
                    logging.warning(f"❌ 发现动态条目缺失 id_str 字段: {json.dumps(item, ensure_ascii=False)[:100]}...")
                    continue
                    
                if id_str in seen_dynamics[uid]:
                    continue
                    
                seen_dynamics[uid].add(id_str)
                modules = item.get("modules") or {}
                author = modules.get("module_author") or {}
                
                # --- 时间校验 ---
                try:
                    pub_ts = float(author.get("pub_ts", 0))
                except:
                    pub_ts = 0
                    
                time_diff = now_ts - pub_ts
                if time_diff > DYNAMIC_MAX_AGE:
                    logging.info(f"⏭️ 忽略超时动态 [{author.get('name')}] ID:{id_str}, 距今 {int(time_diff)} 秒")
                    continue
                
                # --- 内容提取 (核心) ---
                # 🔥 使用升级后的提取函数
                text = extract_dynamic_text(item) 
                
                # --- 拼接传送门链接 (修复点) ---
                # 🔥 🔥 🔥 关键：这里加上了链接，且做了异常隔离
                try:
                    link = f"https://t.bilibili.com/{id_str}"
                    final_msg = f"{text}\n\n🔗 动态直达: {link}"
                except Exception as e:
                    logging.error(f"❌ 拼接动态链接失败: {e}")
                    final_msg = f"{text}\n\n🔗 链接生成失败，请检查 ID: {id_str}"
                
                # --- 通知组装 ---
                name = author.get("name", f"UP主({uid})")
                has_new = True
                alerts.append({
                    "user": name,
                    "message": final_msg
                })
                logging.info(f"✅【成功】抓取到新动态 [{name}]:\n{final_msg[:200]}...") # 打印前200字符预览
                
                # 为了防止刷屏，每个 UP 主只取最新的一条
                break
                
        except Exception as e:
            # 单个 UID 的错误不应影响其他 UID
            logging.error(f"❌【致命】遍历 UID {uid} 时发生崩溃: {e}\n{traceback.format_exc()}")
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
            # 🔥 🔥 🔥 关键：打印具体的 Webhook 错误，防止静默失败
            logging.error(f"❌ Webhook 发送失败（可能是文本超长或含特殊字符）: {e}\n详细内容预览: {json.dumps(alerts, ensure_ascii=False)[:500]}")
    
    return has_new
