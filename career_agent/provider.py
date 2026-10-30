import json
import os
import re

import httpx


QUESTIONS = (
    "请用两分钟介绍你自己，并说明你为什么申请这个岗位。",
    "挑选一个最能体现岗位能力的项目，说明你的具体职责和结果。",
    "讲一次你遇到困难或分歧的经历，你如何处理，最后有什么证据表明有效？",
)


class Provider:
    def __init__(self):
        self.mode = os.getenv("MODEL_PROVIDER", "demo")
        self.model = os.getenv("MODEL_NAME", "")
        self.base_url = os.getenv("MODEL_API_BASE_URL", "https://api.openai.com/v1")
        self.api_key = os.getenv("MODEL_API_KEY", "")

    async def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        if self.mode == "demo":
            return self._demo(messages, tools)
        if self.mode != "openai_compatible" or not self.model:
            raise ValueError("请设置 MODEL_PROVIDER=openai_compatible 和 MODEL_NAME")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.base_url.rstrip('/')}/chat/completions", headers=headers, json=payload
            )
            response.raise_for_status()
        return response.json()["choices"][0]["message"]

    def _demo(self, messages: list[dict], tools: list[dict]) -> dict:
        system = messages[0]["content"]
        if "INTERVIEW_MODE" in system:
            count = int(system.split("QUESTION_COUNT=")[1].splitlines()[0])
            max_questions = int(system.split("MAX_QUESTIONS=")[1].splitlines()[0])
            if count == 0:
                practice = next(
                    (json.loads(message["content"].split("\n", 1)[1]) for message in messages
                     if message["role"] == "user" and message["content"].startswith("[PRACTICE_SOURCE]\n")),
                    None,
                )
                resources = next(
                    (json.loads(message["content"].split("\n", 1)[1]) for message in messages
                     if message["role"] == "user" and message["content"].startswith("[INTERVIEW_RESOURCES]\n")),
                    {"materials": [], "claims": []},
                )
                if practice:
                    question = f"复练：换一个具体场景回答“{practice['question'][:80]}”，并说明你如何验证结果。"
                elif resources["claims"]:
                    question = f"你确认的经历中提到“{resources['claims'][0]['text'][:80]}”。请说明你的具体行动和结果。"
                elif resources["materials"]:
                    question = f"请结合你提供的“{resources['materials'][0]['title']}”，介绍一项与岗位相关的经历。"
                else:
                    question = QUESTIONS[0]
                result = {"feedback": "", "question": question, "finished": False, "evidence": []}
            elif count < max_questions:
                answer = next((message["content"] for message in reversed(messages) if message["role"] == "user"), "")
                result = {
                    "feedback": "演示反馈：请补充你的具体行动、可核实的结果和反思。",
                    "question": QUESTIONS[count % len(QUESTIONS)],
                    "finished": False,
                    "evidence": [answer[:80]],
                }
            else:
                answer = next((message["content"] for message in reversed(messages) if message["role"] == "user"), "")
                result = {
                    "feedback": f"演示总结：你已完成 {max_questions} 题。正式评价需要配置真实模型。",
                    "question": "",
                    "finished": True,
                    "evidence": [answer[:80]],
                }
            return {"role": "assistant", "content": json.dumps(result, ensure_ascii=False)}
        if messages[-1]["role"] == "tool":
            result = json.loads(messages[-1]["content"])
            name = messages[-2]["tool_calls"][0]["function"]["name"]
            if "error" in result:
                content = f"演示模式无法读取所选资源：{result['error']}。请检查 ID 和授权范围。"
            elif name == "read_confirmed_claims":
                claims = result["claims"]
                content = "已确认的经历：\n" + "\n".join(f"- {item['text']}" for item in claims) if claims else "目前没有已确认的经历。"
            elif name == "read_material":
                content = f"已读取“{result['title']}”。演示模式只展示原文片段：\n{result['content'][:300]}"
            else:
                lines = [f"第 {item['number']} 题：{item['feedback']}" for item in result["feedback"]]
                content = f"面试总结：{result['summary']}\n" + "\n".join(lines) + "\n可在面试报告中选择一题复练。"
            return {"role": "assistant", "content": content}
        latest = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        available = {tool["function"]["name"] for tool in tools}
        selected_id = re.search(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", latest, re.I)
        name, arguments = None, {}
        if selected_id and any(word in latest for word in ("面试", "反馈", "复盘")) and "read_interview_feedback" in available:
            name, arguments = "read_interview_feedback", {"interview_session_id": selected_id.group()}
        elif selected_id and "read_material" in available:
            name, arguments = "read_material", {"material_id": selected_id.group()}
        elif any(word in latest for word in ("已确认", "确认的经历", "我的经历")) and "read_confirmed_claims" in available:
            name = "read_confirmed_claims"
        if name:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "demo-tool-1", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }]}
        return {
            "role": "assistant",
            "content": f"演示模式已收到：{latest}\n\n配置真实模型后，我可以讨论岗位、整理你确认的经历，并解释指定的面试反馈。",
        }
