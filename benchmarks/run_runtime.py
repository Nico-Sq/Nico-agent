"""Measure reshaped local run costs and loopback HTTP connection reuse."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import socket
import sqlite3
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import perf_counter

import httpx
from nico_agent_core import (AgentCore, FrozenToolSnapshot, PreparedRun, RunKey,
                             ToolBinding)
from nico_adapters.models.provider import Provider
from nico_adapters.models.transport import post_json
from nico_application.context import build_context
from nico_application.tooling import ToolCatalog
from nico_application.tools import schemas

from .run_baseline import _rss_bytes, _summary, create_fixture, temporary_data_root


@contextmanager
def fake_server():
    state = {"connections": 0, "requests": 0, "auth": [],
             "request_versions": [], "close_flags": []}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            with lock:
                state["connections"] += 1

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            with lock:
                state["requests"] += 1
                state["auth"].append(self.headers.get("Authorization", ""))
                state["request_versions"].append(self.request_version)
                state["close_flags"].append(self.close_connection)
            body = b'{"choices":[{"message":{"content":"benchmark-ok"},"finish_reason":"stop"}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def measure_sync(operation, repeats: int) -> dict:
    samples = []
    for index in range(repeats):
        started = perf_counter()
        operation(index)
        samples.append((perf_counter() - started) * 1000)
    return {**_summary(samples), "count": repeats, "errors": 0}


async def measure_async(operation, repeats: int) -> dict:
    samples = []
    for index in range(repeats):
        started = perf_counter()
        await operation(index)
        samples.append((perf_counter() - started) * 1000)
    return {**_summary(samples), "count": repeats, "errors": 0}


def measure_http(repeats: int) -> dict:
    result = {}
    payload = {"model": "fake", "messages": [{"role": "user", "content": "ping"}]}
    for mode in ("fresh", "reused"):
        with fake_server() as (state, base_url):
            async def calls():
                if mode == "fresh":
                    async def one(index):
                        response = await post_json(base_url, "chat/completions",
                                                   {"Authorization": f"Bearer sample-{index}"}, payload)
                        assert response["choices"][0]["message"]["content"] == "benchmark-ok"

                    return await measure_async(one, repeats), True
                async with httpx.AsyncClient(timeout=60) as client:
                    async def one(index):
                        response = await post_json(base_url, "chat/completions",
                                                   {"Authorization": f"Bearer sample-{index}"}, payload,
                                                   client=client)
                        assert response["choices"][0]["message"]["content"] == "benchmark-ok"

                    samples = await measure_async(one, repeats)
                return samples, client.is_closed

            samples, closed = asyncio.run(calls())
            assert state["requests"] == repeats
            assert state["auth"] == [f"Bearer sample-{index}" for index in range(repeats)]
            result[mode] = {**samples, "connections": state["connections"],
                            "closed": closed, "endpoint": "loopback fake HTTP",
                            "request_versions": sorted(set(state["request_versions"])),
                            "server_close_flags": state["close_flags"]}
    with fake_server() as (state, base_url):
        port = int(base_url.split(":")[2].split("/")[0])

        def connect(_index):
            with socket.create_connection(("127.0.0.1", port), timeout=5):
                pass

        result["connect_only"] = measure_sync(connect, repeats)
    return result


def run(output: Path, repeats: int = 20) -> dict:
    if repeats < 2:
        raise ValueError("repeats must be at least two")
    with temporary_data_root() as directory:
        fixture = create_fixture(Path(directory) / "fixture.sqlite3")
        store, session_id = fixture["store"], fixture["session_id"]
        session = store.get(session_id, "chat")
        rss_before = _rss_bytes()
        metrics = {
            "context": measure_sync(lambda _: build_context(store, session), repeats),
        }
        builtin = schemas("chat")
        external = [{"name": f"read_extra_{index}", "description": "read",
                     "read_only": True, "parameters": {"type": "object"}}
                    for index in range(8)]

        async def noop(_args):
            return {"ok": True}

        def snapshot(_index):
            catalog = ToolCatalog().snapshot(builtin, external,
                                             [item["name"] for item in external],
                                             {"read_confirmed_claims"})
            return FrozenToolSnapshot(tuple(ToolBinding(
                item, noop, parallel=True,
            ) for item in catalog.definitions()))

        metrics["tool_snapshot"] = measure_sync(snapshot, repeats)
        frozen = snapshot(0)
        calls = [{"id": str(index), "function": {"name": "read_confirmed_claims"}}
                 for index in range(2)]

        async def dispatch(_index):
            results = await AgentCore().execute_tools(
                calls, lambda call: frozen.execute(call["function"]["name"], {}),
                lambda call: frozen.can_execute_in_parallel(call["function"]["name"]),
            )
            assert len(results) == 2 and all(item["ok"] for item in results)

        metrics["tool_dispatch_two_reads"] = asyncio.run(measure_async(dispatch, repeats))

        def commit(index):
            current = store.create("chat")
            claimed = store.claim_run(current["id"], "chat", "active", f"question-{index}")
            assert claimed is not None and store.begin_run(current["id"], claimed["version"])
            started = perf_counter()
            committed = store.finish_run(current["id"], claimed["version"], "answer", "active", 0)
            assert committed
            return (perf_counter() - started) * 1000

        metrics["sqlite_commit"] = {**_summary([commit(index) for index in range(repeats)]),
                                    "count": repeats, "errors": 0}
        metrics["http"] = measure_http(repeats)
        with fake_server() as (state, base_url):
            provider = Provider(mode="openai_compatible", model="fake", base_url=base_url)
            engine = AgentCore()

            async def full_run(index):
                outcome = await engine.execute(PreparedRun(
                    key=RunKey("benchmark", str(index), 1), model=provider,
                    messages=({"role": "user", "content": "ping"},),
                    tools=FrozenToolSnapshot(()), is_current=lambda: True,
                ))
                assert outcome.content == "benchmark-ok"

            metrics["model_end_to_end_fake"] = asyncio.run(measure_async(full_run, repeats))
            metrics["model_end_to_end_fake"]["connections"] = state["connections"]
        with fake_server() as (state, base_url):
            async def reused_model_runs():
                async with httpx.AsyncClient(timeout=60) as client:
                    provider = Provider(mode="openai_compatible", model="fake",
                                        base_url=base_url, http_client=client)
                    engine = AgentCore()

                    async def full_run(index):
                        outcome = await engine.execute(PreparedRun(
                            key=RunKey("benchmark", str(index), 1), model=provider,
                            messages=({"role": "user", "content": "ping"},),
                            tools=FrozenToolSnapshot(()), is_current=lambda: True,
                        ))
                        assert outcome.content == "benchmark-ok"

                    return await measure_async(full_run, repeats)

            metrics["model_end_to_end_fake_reused"] = asyncio.run(reused_model_runs())
            metrics["model_end_to_end_fake_reused"]["connections"] = state["connections"]
        result = {
            "environment": {"os": platform.platform(), "python": platform.python_version(),
                            "sqlite": sqlite3.sqlite_version, "cpu_count": os.cpu_count()},
            "dataset": {"seed": 20261101, "sessions": 1000, "messages": 11100,
                        "repeats": repeats, "concurrency": 1, "endpoint": "127.0.0.1 fake HTTP"},
            "metrics": metrics,
            "rss_before_bytes": rss_before,
            "rss_after_bytes": _rss_bytes(),
            "real_model_measured": False,
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=20)
    args = parser.parse_args()
    result = run(args.output, args.repeats)
    for name, values in result["metrics"].items():
        if name == "http":
            for mode, metric in values.items():
                connections = f" connections={metric['connections']}" if "connections" in metric else ""
                print(f"http.{mode}: p50={metric['p50']} p95={metric['p95']}{connections}")
        else:
            print(f"{name}: p50={values['p50']} p95={values['p95']}")


if __name__ == "__main__":
    main()
