import asyncio
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from career_agent.provider import Provider
from career_agent.runtime import AgentRuntime
from career_agent.store import Store


class ContractServer(ThreadingHTTPServer):
    def __init__(self, address):
        super().__init__(address, ContractHandler)
        self.requests = []
        self.requests_lock = threading.Lock()

    def record(self, path, headers, payload):
        with self.requests_lock:
            self.requests.append({"path": path, "headers": dict(headers), "payload": payload})


class ContractHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.server.record(self.path, self.headers.items(), payload)
        if self.path == "/v1/chat/completions":
            body = self.openai_response(payload)
        elif self.path == "/v1/messages":
            body = self.anthropic_response(payload)
        else:
            self.send_error(404)
            return
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    @staticmethod
    def openai_response(payload):
        if payload["messages"][-1]["role"] == "tool":
            return {
                "choices": [{"message": {"role": "assistant", "content": "已查到你保存的 Python 记忆"},
                             "finish_reason": "stop"}],
            }
        return {
            "choices": [{
                "message": {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call-memory",
                    "type": "function",
                    "function": {"name": "search_memories",
                                 "arguments": json.dumps({"query": "Python"})},
                }]},
                "finish_reason": "tool_calls",
            }],
        }

    @staticmethod
    def anthropic_response(payload):
        last = payload["messages"][-1]
        if isinstance(last["content"], list) and last["content"][0]["type"] == "tool_result":
            question = {
                "feedback": "",
                "question": "请介绍一个能体现后端能力的项目。",
                "finished": False,
                "evidence": [],
                "summary": "",
            }
            return {
                "content": [{"type": "text", "text": json.dumps(question, ensure_ascii=False)}],
                "stop_reason": "end_turn",
            }
        return {
            "content": [{"type": "tool_use", "id": "tool-goal",
                         "name": "read_interview_goal", "input": {}}],
            "stop_reason": "tool_use",
        }

    def log_message(self, *_args):
        pass


class ModelContractTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).parent / ".tmp"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.store = Store(Path(self.temp.name) / "contracts.sqlite3")
        self.server = ContractServer(("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_openai_compatible_chat_tool_cycle_over_http(self):
        self.store.add_memory("熟悉 Python 和 SQL")
        provider = Provider(mode="openai_compatible", model="contract-chat", base_url=self.base_url)
        chat = self.store.create("chat", model_id="contract-chat")
        running = self.store.claim_run(chat["id"], "chat", "active", "查找我的 Python 记忆")

        result = asyncio.run(AgentRuntime(self.store, provider).run(running))

        self.assertIn("Python 记忆", result["message"])
        self.assertEqual([item["path"] for item in self.server.requests],
                         ["/v1/chat/completions", "/v1/chat/completions"])
        second_messages = self.server.requests[1]["payload"]["messages"]
        self.assertEqual(second_messages[-1]["role"], "tool")
        self.assertIn("熟悉 Python 和 SQL", second_messages[-1]["content"])
        event = next(item for item in self.store.events(chat["id"], "chat")
                     if item["kind"] == "model_run")
        detail = json.loads(event["detail"])
        self.assertEqual(detail["usage"]["request_count"], 2)
        self.assertEqual(detail["usage_coverage"], "unavailable")

    def test_anthropic_interview_tool_cycle_over_http(self):
        provider = Provider(mode="anthropic", model="contract-interview",
                            base_url=self.base_url, api_key="contract-key")
        interview = self.store.create("interview", "后端开发", max_questions=1,
                                      model_id="contract-interview")
        running = self.store.claim_run(interview["id"], "interview", "new")

        result = asyncio.run(AgentRuntime(self.store, provider).run(running))

        self.assertEqual(result["session"]["status"], "awaiting_answer")
        self.assertEqual(self.server.requests[0]["path"], "/v1/messages")
        self.assertIn("后端开发", self.server.requests[0]["payload"]["messages"][0]["content"])
        self.assertIn("read_interview_goal",
                      [tool["name"] for tool in self.server.requests[0]["payload"]["tools"]])
        second_messages = self.server.requests[1]["payload"]["messages"]
        self.assertEqual(second_messages[-2]["content"][0]["type"], "tool_use")
        self.assertEqual(second_messages[-1]["content"][0]["tool_use_id"], "tool-goal")
        report = self.store.interview_report(interview["id"])
        self.assertEqual(report["items"][0]["question"], "请介绍一个能体现后端能力的项目。")
        event = next(item for item in self.store.events(interview["id"], "interview")
                     if item["kind"] == "model_run")
        detail = json.loads(event["detail"])
        self.assertEqual(detail["usage"]["request_count"], 2)
        self.assertEqual(detail["usage_coverage"], "unavailable")


if __name__ == "__main__":
    unittest.main()
