def deep_find_text(obj):
    # 原版兜底搜索函数保持不动
    result = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ["text", "content", "desc", "title", "words"]:
                    if isinstance(v, str) and v.strip():
                        result.append(v.strip())
                walk(v)
        elif isinstance(x, list):
            for i in x:
                walk(i)

    walk(obj)

    uniq = []
    for x in result:
        if x not in uniq:
            uniq.append(x)

    return " ".join(uniq).strip()


def extract_dynamic_text(item):
    """
    极简且绝对安全的升级版提取器：
    解决截断打乱问题 + 防止 Webhook 超长崩溃 + 新增传送门
    """
    try:
        content_list = []
        
        # 1. 优先提取新版富文本（完美保留换行和段落，解决原版被打乱的问题）
        dyn = item.get("modules", {}).get("module_dynamic", {})
        rich_nodes = dyn.get("desc", {}).get("rich_text_nodes", [])
        
        if rich_nodes:
            node_texts = []
            for node in rich_nodes:
                node_texts.append(str(node.get("text", "")))
            parsed = "".join(node_texts).strip()
            if parsed:
                content_list.append(parsed)
                
        # 2. 如果富文本里没东西，退回原版的深度搜索兜底
        if not content_list:
            text = deep_find_text(dyn)
            if text:
                content_list.append(text)
            else:
                # 原版终极兜底：直接输出 JSON
                raw = json.dumps(item, ensure_ascii=False)
                content_list.append(raw)
                
        # 3. 组合正文文本
        final_text = "\n".join(content_list).strip()
        
        # 4. ⚠️ 【致命防御：安全截断】
        # 突破原版的 500 字，放宽到 1000 字！
        # 绝不能完全不截断，否则会导致 Webhook 拒收，引发“无法监听”的假象！
        if len(final_text) > 1000:
            final_text = final_text[:1000] + "\n\n... (后续内容过长，为确保通知成功已保护性截断)"
            
        # 5. 附加直达传送门
        id_str = item.get("id_str")
        if id_str:
            final_text += f"\n\n🔗 直达链接: https://t.bilibili.com/{id_str}"
            
        return final_text
        
    except Exception as e:
        # 万一发生异常，退回最稳妥的字符串，确保流程不中断
        return "发布了新动态 (内容解析安全兜底)"
