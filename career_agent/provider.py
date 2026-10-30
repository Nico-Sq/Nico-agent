import json
import os

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
            return self._demo(messages)
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

    def _demo(self, messages: list[dict]) -> dict:
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
                result = {"feedback": "", "question": question, "finished": False}
            elif count < max_questions:
                result = {
                    "feedback": "演示反馈：请补充你的具体行动、可核实的结果和反思。",
                    "question": QUESTIONS[count % len(QUESTIONS)],
                    "finished": False,
                }
            else:
                result = {
                    "feedback": f"演示总结：你已完成 {max_questions} 题。正式评价需要配置真实模型。",
                    "question": "",
                    "finished": True,
                }
            return {"role": "assistant", "content": json.dumps(result, ensure_ascii=False)}
        latest = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        return {
            "role": "assistant",
            "content": f"演示模式已收到：{latest}\n\n配置真实模型后，我可以讨论岗位、整理你确认的经历，并解释指定的面试反馈。",
        }
