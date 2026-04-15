def extract_dynamic_text(item):
    """修复版：严防None，保守提取，详细日志"""
    try:
        modules = item.get("modules") or {}
        dyn = modules.get("module_dynamic") or {}
        content_list = []
        id_str = item.get("id_str", "unknown")

        logging.info(f"动态解析开始 ID:{id_str}")

        # 1. 安全提取 rich_text_nodes
        rich_nodes = []
        if dyn and isinstance(dyn, dict):
            desc = dyn.get("desc") or {}
            if isinstance(desc, dict):
                rich_nodes = desc.get("rich_text_nodes") or []
            if not rich_nodes:
                rich_nodes = dyn.get("rich_text_nodes") or []

        if rich_nodes and isinstance(rich_nodes, list):
            node_texts = []
            for node in rich_nodes:
                if isinstance(node, dict):
                    txt = str(node.get("text") or node.get("orig_text") or "").strip()
                    if txt:
                        node_texts.append(txt)
            if node_texts:
                content_list.append("\n".join(node_texts).strip())
                logging.info(f"✅ rich_text_nodes 提取成功 {len(node_texts)}段")

        # 2. major 类型补充
        if dyn and isinstance(dyn, dict):
            major = dyn.get("major") or {}
            if isinstance(major, dict):
                mtype = major.get("type")
                if mtype in ["MAJOR_TYPE_OPUS", "MAJOR_TYPE_DRAW"]:
                    opus = major.get("opus") or major.get("draw")
                    if isinstance(opus, dict):
                        desc = opus.get("desc") or {}
                        if isinstance(desc, dict):
                            opus_text = str(desc.get("text") or desc.get("content") or "").strip()
                            if opus_text:
                                content_list.append(opus_text)
                                logging.info(f"✅ MAJOR_TYPE_OPUS/DRAW 提取成功")

        # 3. 原deep_find_text兜底
        if not content_list:
            text = deep_find_text(dyn)
            if text:
                content_list.append(text)
                logging.info("✅ deep_find_text兜底成功")

        # 4. 终极兜底
        if not content_list:
            content_list.append("发布了新动态")
            logging.warning(f"⚠️ 动态ID:{id_str} 解析失败，使用兜底")

        final_text = "\n\n".join(content_list).strip()

        if len(final_text) > 1800:
            final_text = final_text[:1800] + "\n\n...(内容过长，已安全截断)"

        return final_text

    except Exception as e:
        logging.error(f"❌ extract_dynamic_text异常 ID:{item.get('id_str','unknown')}: {e}")
        return "发布了新动态 (解析异常，已安全兜底)"
