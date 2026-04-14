def extract_dynamic_text(item):
    try:
        id_str = item.get("id_str", "unknown")

        logging.info("=" * 80)
        logging.info(f"🔍 FINAL解析 START ID: {id_str}")

        modules = item.get("modules") or {}
        dyn = modules.get("module_dynamic") if isinstance(modules, dict) else None

        if not isinstance(dyn, dict):
            logging.warning("module_dynamic缺失")
            return "【DEBUG】无module_dynamic"

        content = []

        # =========================
        # ① desc（最重要入口）
        # =========================
        desc = dyn.get("desc") or {}
        logging.info(f"desc类型: {type(desc)} 内容: {str(desc)[:300]}")

        if isinstance(desc, dict):
            for k in ["text", "orig_text"]:
                v = desc.get(k)
                if isinstance(v, str) and v.strip():
                    content.append(v.strip())
                    logging.info(f"✅ desc.{k} 命中")

            nodes = desc.get("rich_text_nodes") or []
            if isinstance(nodes, list):
                for n in nodes:
                    if isinstance(n, dict):
                        t = n.get("text") or n.get("orig_text")
                        if isinstance(t, str) and t.strip():
                            content.append(t.strip())

        # =========================
        # ② major（必须“验证内容”）
        # =========================
        major = dyn.get("major") or {}
        logging.info(f"major.type: {major.get('type') if isinstance(major, dict) else None}")

        if isinstance(major, dict):
            for k in ["draw", "opus", "archive", "article"]:
                blk = major.get(k)

                if not isinstance(blk, dict):
                    continue

                logging.info(f"检查 major.{k}")

                # ---------- draw ----------
                if k == "draw":
                    items = blk.get("items") or []
                    for it in items:
                        if isinstance(it, dict):
                            t = it.get("text")
                            if isinstance(t, str) and t.strip():
                                content.append(t.strip())
                                logging.info("✅ draw.items.text 命中")

                # ---------- opus/article/archive ----------
                for path in [
                    ("desc", "text"),
                    ("desc", "content"),
                    ("title",)
                ]:
                    cur = blk
                    for p in path:
                        if isinstance(cur, dict):
                            cur = cur.get(p)
                        else:
                            cur = None

                    if isinstance(cur, str) and cur.strip():
                        content.append(cur.strip())
                        logging.info(f"✅ path命中 {path}")

        # =========================
        # ③ 强制兜底（关键升级）
        # =========================
        if not content:
            logging.warning("⚠️ deep_find_text兜底启动")

            try:
                def walk(x):
                    res = []

                    if isinstance(x, dict):
                        for k, v in x.items():
                            if k in ["text", "content", "desc", "title", "words"]:
                                if isinstance(v, str) and v.strip():
                                    res.append(v.strip())
                            res.extend(walk(v))

                    elif isinstance(x, list):
                        for i in x:
                            res.extend(walk(i))

                    return res

                result = walk(dyn)

                logging.info(f"deep_find_text结果数量: {len(result)}")

                if result:
                    content.extend(result)

            except Exception as e:
                logging.error(f"deep_find_text失败: {e}")

        # =========================
        # ④ 最终兜底（绝不空）
        # =========================
        if not content:
            logging.error("❌ 完全解析失败")
            return "【DEBUG】无法解析动态（结构未知）"

        final = "\n".join(list(dict.fromkeys(content)))

        logging.info("=========== FINAL OUTPUT ===========")
        logging.info(final[:2000])

        return final

    except Exception:
        logging.error(traceback.format_exc())
        return "【DEBUG】异常兜底"
