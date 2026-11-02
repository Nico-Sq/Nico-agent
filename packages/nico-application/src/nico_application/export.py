"""Markdown rendering for a chat or interview session export."""


def render_session_markdown(session: dict, messages: list[dict], report: dict | None = None) -> str:
    kind = session["kind"]
    lines = ["# 聊天记录" if kind == "chat" else "# 模拟面试记录", ""]
    if session["title"]:
        lines.extend([f"会话名称：{session['title']}", ""])
    if kind == "chat":
        if session["chat_memory"]:
            lines.extend(["## 固定备注", "", session["chat_memory"], ""])
        for message in messages:
            lines.extend([f"## {'用户' if message['role'] == 'user' else '聊天助手'}", "",
                          message["content"], ""])
    else:
        lines.extend([f"岗位目标：{session['goal']}", f"状态：{session['status']}", ""])
        if report["practice_source"]:
            source = report["practice_source"]
            lines.extend(["## 复练来源", "", f"原题：{source['question']}", "",
                          f"原回答：{source['answer'] or '无'}", "",
                          f"原反馈：{source['feedback'] or '无'}", ""])
        for item in report["items"]:
            lines.extend([f"## 第 {item['number']} 题", "", item["question"], "",
                          "### 回答", "", "已跳过" if item["skipped"] else item["answer"] or "未回答", "",
                          "### 反馈", "", item["feedback"] or "尚无反馈", ""])
            if item["evidence"]:
                lines.extend(["回答依据：" + "；".join(item["evidence"]), ""])
        if report["summary"]:
            lines.extend(["## 全场总结", "", report["summary"], ""])
        lines.extend(["## 完整对话", ""])
        for message in messages:
            lines.extend([f"### {'用户' if message['role'] == 'user' else '面试官'}", "",
                          "已跳过本题" if message["content"] == "[SKIPPED_QUESTION]" else message["content"], ""])
    return "\n".join(lines)
