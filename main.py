# ===================== 仅修改动态模块（评论保持不动） =====================

def check_new_dynamics(header, seen_dynamics, active_dynamics):
    new_alerts = []
    has_new_dynamic = False

    for uid in EXTRA_DYNAMIC_UIDS:
        url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
        params = {"host_mid": uid}

        try:
            r = session.get(url, headers=header, params=params, timeout=10)
            data = r.json()

            if data.get("code") != 0:
                continue

            items = (data.get("data") or {}).get("items", [])
            if not items:
                continue

            for item in items:
                id_str = item.get("id_str")
                if not id_str:
                    continue

                if id_str in seen_dynamics[uid]:
                    continue

                seen_dynamics[uid].add(id_str)
                has_new_dynamic = True

                modules = item.get("modules") or {}
                author = modules.get("module_author") or {}

                name = author.get("name", str(uid))

                # ================== 核心修复：完整动态内容解析 ==================
                dyn = modules.get("module_dynamic") or {}
                major = dyn.get("major") or {}

                dyn_text = ""

                # 1. 普通文本
                desc = dyn.get("desc") or {}
                if desc.get("text"):
                    dyn_text = desc["text"]

                # 2. Opus / 图文
                if not dyn_text and major.get("opus"):
                    opus = major["opus"]
                    dyn_text = (
                        (opus.get("summary") or {}).get("text")
                        or ""
                    )

                # 3. draw 图片动态（重点修复）
                if not dyn_text and major.get("draw"):
                    items_img = major["draw"].get("items") or []
                    dyn_text = f"图片动态（{len(items_img)}张）"

                # 4. article 专栏
                if not dyn_text and major.get("article"):
                    art = major["article"]
                    dyn_text = art.get("title", "专栏动态")

                # 5. live / forward
                if not dyn_text:
                    dyn_type = item.get("type")
                    if dyn_type == "DYNAMIC_TYPE_FORWARD":
                        dyn_text = "转发动态"
                    elif dyn_type == "DYNAMIC_TYPE_LIVE_RCMD":
                        dyn_text = "直播动态"
                    else:
                        dyn_text = "发布了新动态"

                # ================== 传送门 ==================
                jump_url = f"https://t.bilibili.com/{id_str}"

                final_msg = (
                    f"【{name}】\n"
                    f"{dyn_text}\n"
                    f"🔗 点击查看原动态：{jump_url}"
                )

                new_alerts.append({
                    "user": name,
                    "message": final_msg
                })

                logging.info(f"动态抓取 [{name}] {dyn_text} -> {id_str}")

                # 动态进入评论监控池（不动原逻辑结构）
                basic = item.get("basic") or {}
                c_oid = basic.get("comment_id_str")
                c_type = basic.get("comment_type")

                if c_oid and c_type:
                    active_dynamics[uid][id_str] = {
                        "oid": c_oid,
                        "type": c_type,
                        "ctime": time.time()
                    }

                break

        except Exception as e:
            logging.error(f"动态监控异常 UID:{uid} {e}")

    if new_alerts:
        try:
            notifier.send_webhook_notification(
                "💡 特别关注UP主发布新内容",
                new_alerts
            )
        except:
            pass

    return has_new_dynamic
