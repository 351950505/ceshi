# ------------------------
# 纯净扫描：仅抓取视频主评论（去除了子评论/回复）
# 修复重点：将 sort 改为 2 (按时间排序)，确保第一时间抓取最新内容
# ------------------------
def scan_new_comments(oid, header, last_read_time, seen):
    new_list = []
    max_ctime_in_this_round = last_read_time
    safe_read_time = last_read_time - 300  # 5分钟回溯冗余，防CDN缓存延迟
    
    # 监控最新 3 页即可，按时间排序下，3页已覆盖极短时间内的大量评论
    pn = 1
    while pn <= 3:  
        # type: 1 代表视频评论
        # sort: 2 代表按时间排序 (修复关键点)
        params = {"oid": oid, "type": 1, "sort": 2, "pn": pn, "ps": 20}
        
        try:
            data = wbi_request("https://api.bilibili.com/x/v2/reply", params, header)
            
            # 如果请求失败或被拦截，wbi_request 会返回 {"code": -1}
            if data.get("code") != 0: 
                break
                
            replies = data.get("data", {}).get("replies") or []
            if not replies: 
                break
                
            page_all_older = True  
            for r_obj in replies:
                rpid = r_obj["rpid_str"]
                r_ctime = r_obj["ctime"]
                
                # 记录本轮看到的最大时间戳
                if r_ctime > max_ctime_in_this_round:
                    max_ctime_in_this_round = r_ctime
                
                # 如果评论时间在安全回溯线之后
                if r_ctime > safe_read_time:
                    page_all_older = False
                    if rpid not in seen:
                        seen.add(rpid)
                        new_list.append({
                            "user": r_obj["member"]["uname"], 
                            "message": r_obj["content"]["message"], 
                            "ctime": r_ctime
                        })
                            
            # 如果这一页全是旧评论，就没必要翻下一页了
            if page_all_older: 
                break
                
            pn += 1
            # 严格遵守极简请求原则，翻页间歇稍微拉长，降低被 WAF 标记的风险
            time.sleep(random.uniform(1.5, 2.5))
        except Exception:
            # 遇到任何解析错误，静默跳过
            break
            
    return new_list, max_ctime_in_this_round
