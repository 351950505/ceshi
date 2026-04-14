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

                if now_ts - pub_ts > DYNAMIC_MAX_AGE:
                    continue

                name = author.get("name", str(uid))
                text = extract_dynamic_text(item)

                # 动态传送门
                jump_url = f"https://t.bilibili.com/{id_str}"

                # 图片动态没正文时，直接给传送门
                if text in ["发布了图片动态", "发布了新动态"]:
                    msg = f"🔗 动态传送门：{jump_url}"
                else:
                    msg = f"{text}\n🔗 动态传送门：{jump_url}"

                alerts.append({
                    "user": name,
                    "message": msg[:1000]
                })

                logging.info(f"动态抓取 [{name}] {msg[:500]}")

                has_new = True
                break

        except Exception as e:
            logging.error(f"动态异常 {uid}: {e}")

        time.sleep(random.uniform(1, 2))

    if alerts:
        try:
            notifier.send_webhook_notification(
                "💡 特别关注UP主发布新内容",
                alerts
            )
        except:
            pass

    return has_new
