"""Run fixed-size Nico storage and local fake-model performance baselines."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import perf_counter

from career_agent.context import build_context
from nico_adapters.models.transport import post_json
from nico_contracts.errors import ModelError
from nico_storage import MAX_MEMORIES, Store


ROOT = Path(__file__).resolve().parents[1]
SEED = 20261101
MEMORY_QUERIES = (
    ("通勤路线", "通勤路线：地铁二号线转四号线，单程约四十分钟。"),
    ("项目职责", "项目职责：负责支付服务的接口设计和发布回滚。"),
    ("学习计划", "学习计划：每周三晚完成数据库课程复习。"),
    ("饮食偏好", "饮食偏好：午餐少油，不吃香菜。"),
    ("年度目标", "年度目标：完成云平台认证并带领一次项目复盘。"),
    ("紧急联系人", "紧急联系人：工作日优先联系家人手机。"),
    ("面试复盘", "面试复盘：系统设计回答需要先澄清容量和延迟要求。"),
    ("代码规范", "代码规范：公共接口变更要保留旧导入兼容。"),
    ("运动安排", "运动安排：周二和周六进行力量训练。"),
    ("工作地点", "工作地点：每周一、三到杭州办公室。"),
)
MESSAGE_SEARCH = "benchmark-message-needle"
SESSION_SEARCH = "benchmark-session-needle"
BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def temporary_data_root():
    return tempfile.TemporaryDirectory(prefix="nico-benchmark-")


def _created_at(index: int) -> str:
    return (BASE_TIME + timedelta(microseconds=index)).isoformat()


def create_fixture(path: Path, *, message_count: int = 10_000,
                   session_count: int = 1_000) -> dict:
    """Create deterministic rows using the current schema in a disposable database."""
    randomizer = random.Random(SEED)
    store = Store(path)
    session_ids = [f"bench-session-{index:04d}" for index in range(session_count)]
    message_counts = tuple(dict.fromkeys((min(100, message_count), min(1_000, message_count),
                                          message_count)))
    message_session_ids = {
        count: session_ids[index] for index, count in enumerate(message_counts)
    }
    main_session_id = message_session_ids[message_count]
    sessions = [
        (session_id, "chat", "active",
         f"Fixture session {index:04d}" + (f" {SESSION_SEARCH}" if index % 100 == 0 else ""),
         f"synthetic goal {index % 17}", _created_at(index))
        for index, session_id in enumerate(session_ids)
    ]
    messages = []
    for count, message_session_id in message_session_ids.items():
        for index in range(count):
            content = f"fixture scale {count} turn {index:05d} topic-{randomizer.randrange(100):02d} stable sample"
            if message_session_id == main_session_id and index == count - 1:
                content += f" {MESSAGE_SEARCH}"
            messages.append((message_session_id, "user" if index % 2 == 0 else "assistant",
                             content, _created_at(index)))
    with store.connect() as db:
        db.executemany(
            "INSERT INTO sessions(id, kind, status, title, goal, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            sessions,
        )
        db.executemany(
            "INSERT INTO messages(session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            messages,
        )
    store.set_tool_grants(main_session_id, [f"read_tool_{index:02d}" for index in range(8)])
    return {
        "store": store,
        "session_id": main_session_id,
        "session_ids": session_ids,
        "message_sessions": message_session_ids,
        "message_counts": {count: count for count in message_counts},
        "total_message_count": sum(message_counts),
        "session_count": session_count,
    }


def add_memories(store: Store, start: int, end: int) -> None:
    for index in range(start, end):
        if index < len(MEMORY_QUERIES):
            content = MEMORY_QUERIES[index][1]
        else:
            content = f"通用记录 {index:03d}：固定种子生成的测试记忆标签 {index % 13:02d}。"
        store.add_memory(content)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 4)


def _summary(samples: list[float], *, unit: str = "ms") -> dict:
    return {
        "unit": unit,
        "p50": _percentile(samples, 0.50),
        "p95": _percentile(samples, 0.95),
        "p99": _percentile(samples, 0.99),
        "samples": [round(value, 4) for value in samples],
    }


def _measure(operation, repeats: int, validate=lambda _value: None) -> dict:
    samples = []
    for _ in range(repeats):
        started = perf_counter()
        result = operation()
        samples.append((perf_counter() - started) * 1000)
        validate(result)
    return _summary(samples)


def _measure_memory_search(store: Store, memory_count: int, repeats: int) -> dict:
    samples = []
    hits = 0
    total = 0
    for _ in range(repeats):
        for query, expected in MEMORY_QUERIES:
            started = perf_counter()
            result = store.search_memories(query)
            samples.append((perf_counter() - started) * 1000)
            total += 1
            if any(item["content"] == expected for item in result[:10]):
                hits += 1
    if hits != total:
        raise AssertionError(f"memory Recall@10 was {hits}/{total} at {memory_count} rows")
    result = _summary(samples)
    result["row_count"] = memory_count
    result["recall_at_10"] = hits / total
    result["query_count"] = len(MEMORY_QUERIES)
    return result


def _clone_as_v1(source: Path, destination: Path) -> None:
    with closing(sqlite3.connect(source)) as source_db:
        with closing(sqlite3.connect(destination)) as target_db:
            source_db.backup(target_db)
    with closing(sqlite3.connect(destination)) as db:
        db.execute("DROP INDEX channel_bindings_session_id")
        db.execute("DROP TABLE channel_messages")
        db.execute("DROP TABLE channel_bindings")
        db.execute("PRAGMA user_version = 1")


def _measure_migrations(source: Path, root: Path, repeats: int, session_id: str,
                        expected_messages: int) -> dict:
    samples = []
    for index in range(repeats):
        path = root / f"migration-{index}.sqlite3"
        _clone_as_v1(source, path)
        started = perf_counter()
        migrated = Store(path)
        samples.append((perf_counter() - started) * 1000)
        if len(migrated.messages(session_id, "chat")) != expected_messages:
            raise AssertionError("schema migration did not retain fixture messages")
    result = _summary(samples)
    result["source_version"] = 1
    result["target_version"] = 2
    return result


def _measure_concurrent_writes(path: Path, session_ids: list[str], workers: int,
                               writes_per_worker: int) -> dict:
    barrier = threading.Barrier(workers)
    lock_waits = []
    transaction_times = []
    errors = []

    def worker(worker_id: int) -> None:
        db = sqlite3.connect(path, timeout=10, isolation_level=None)
        try:
            barrier.wait()
            for sequence in range(writes_per_worker):
                started = perf_counter()
                try:
                    db.execute("BEGIN IMMEDIATE")
                    acquired = perf_counter()
                    db.execute(
                        "INSERT INTO events(session_id, kind, detail, created_at) "
                        "VALUES (?, 'benchmark', ?, ?)",
                        (session_ids[worker_id], json.dumps({"sequence": sequence}),
                         _created_at(worker_id * writes_per_worker + sequence)),
                    )
                    db.commit()
                    finished = perf_counter()
                    lock_waits.append((acquired - started) * 1000)
                    transaction_times.append((finished - started) * 1000)
                except sqlite3.Error as exc:
                    db.rollback()
                    errors.append(type(exc).__name__)
        finally:
            db.close()

    started = perf_counter()
    threads = [threading.Thread(target=worker, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = perf_counter() - started
    expected = workers * writes_per_worker
    if errors or len(transaction_times) != expected:
        raise AssertionError(f"concurrent writes failed: errors={errors}, writes={len(transaction_times)}")
    return {
        "workers": workers,
        "writes": expected,
        "throughput_per_second": round(expected / elapsed, 2),
        "lock_wait": _summary(lock_waits),
        "transaction": _summary(transaction_times),
        "errors": errors,
    }


def _rss_bytes() -> int | None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class MemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("page_fault_count", wintypes.DWORD),
                ("peak_working_set_size", ctypes.c_size_t),
                ("working_set_size", ctypes.c_size_t),
                ("quota_peak_paged_pool_usage", ctypes.c_size_t),
                ("quota_paged_pool_usage", ctypes.c_size_t),
                ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
                ("quota_non_paged_pool_usage", ctypes.c_size_t),
                ("pagefile_usage", ctypes.c_size_t),
                ("peak_pagefile_usage", ctypes.c_size_t),
            ]

        counters = MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        process = kernel32.GetCurrentProcess()
        ok = psapi.GetProcessMemoryInfo(
            process, ctypes.byref(counters), counters.cb,
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(counters.working_set_size)
    try:
        with open("/proc/self/status", encoding="utf-8") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def _pythonpath() -> str:
    package_paths = [str(ROOT), *(
        str(path / "src") for path in sorted((ROOT / "packages").iterdir()) if (path / "src").is_dir()
    )]
    return os.pathsep.join(package_paths)


def _startup_environment(data_dir: Path) -> dict[str, str]:
    inherited = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "WINDIR")
                 if key in os.environ}
    inherited.update({
        "PYTHONPATH": _pythonpath(),
        "DATA_DIR": str(data_dir),
        "AUTH_REQUIRED": "false",
        "MODEL_PROVIDER": "demo",
    })
    return inherited


def _measure_app_startup(root: Path, repeats: int, existing_db: Path | None = None) -> dict:
    samples = []
    for index in range(repeats):
        data_dir = root / (f"startup-existing-{index}" if existing_db else f"startup-new-{index}")
        data_dir.mkdir()
        if existing_db is not None:
            shutil.copy2(existing_db, data_dir / "career_agent.sqlite3")
        started = perf_counter()
        subprocess.run(
            [sys.executable, "-c", "import career_agent.main"],
            cwd=ROOT,
            env=_startup_environment(data_dir),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        samples.append((perf_counter() - started) * 1000)
    return _summary(samples)


@contextmanager
def fake_model_server():
    state = {"calls": 0, "status": 200}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state["calls"] += 1
            status = state["status"]
            body = (b'{"choices":[{"message":{"content":"benchmark-ok"},'
                    b'"finish_reason":"stop"}]}')
            self.send_response(status)
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


def _measure_fake_model(repeats: int) -> dict:
    with fake_model_server() as (state, base_url):
        async def calls() -> list[float]:
            samples = []
            for index in range(repeats):
                started = perf_counter()
                response = await post_json(
                    base_url, "chat/completions", {},
                    {"model": "deterministic-fake", "messages": [{"role": "user", "content": str(index)}]},
                )
                samples.append((perf_counter() - started) * 1000)
                if response["choices"][0]["message"]["content"] != "benchmark-ok":
                    raise AssertionError("fake model response was invalid")
            return samples

        success_samples = asyncio.run(calls())
        state["status"] = 400
        try:
            asyncio.run(post_json(base_url, "chat/completions", {}, {"model": "fake"}))
        except ModelError as exc:
            error_code = exc.code
        else:
            raise AssertionError("fake model error path did not raise ModelError")
        if state["calls"] != repeats + 1:
            raise AssertionError(f"fake model called {state['calls']} times, expected {repeats + 1}")
    result = _summary(success_samples)
    result["successful_calls"] = repeats
    result["error_path"] = {"calls": 1, "code": error_code}
    result["transport"] = "loopback fake HTTP; no external endpoint"
    return result


def run_baseline(output: Path, *, message_count: int = 10_000,
                 session_count: int = 1_000,
                 repeats: int = 20, setup_repeats: int = 3,
                 writes_per_worker: int = 40) -> dict:
    memory_count = MAX_MEMORIES
    if session_count < 16 or message_count < 1:
        raise ValueError("基准规模必须至少有 16 个会话和 1 条消息")
    if repeats < 1 or setup_repeats < 1 or writes_per_worker < 1:
        raise ValueError("重复次数和并发写入次数必须大于零")

    with temporary_data_root() as directory:
        root = Path(directory)
        database = root / "fixture.sqlite3"
        fixture = create_fixture(
            database, message_count=message_count, session_count=session_count,
        )
        store = fixture["store"]
        session_id = fixture["session_id"]
        metrics = {}
        correctness = {}

        add_memories(store, 0, 50)
        metrics["memory.search_50"] = _measure_memory_search(store, 50, repeats)
        add_memories(store, 50, memory_count)
        metrics[f"memory.search_{memory_count}"] = _measure_memory_search(
            store, memory_count, repeats,
        )
        correctness["memory_recall_at_10"] = {
            "50": metrics["memory.search_50"]["recall_at_10"],
            str(memory_count): metrics[f"memory.search_{memory_count}"]["recall_at_10"],
        }

        metrics["sessions.list"] = _measure(
            lambda: store.list_sessions("chat"), repeats,
            lambda value: _assert_count(value, session_count),
        )
        metrics["sessions.search_title"] = _measure(
            lambda: store.list_sessions("chat", SESSION_SEARCH), repeats,
            lambda value: _assert_count(value, math.ceil(session_count / 100)),
        )
        metrics["sessions.search_message"] = _measure(
            lambda: store.list_sessions("chat", MESSAGE_SEARCH), repeats,
            lambda value: _assert_count(value, 1),
        )
        for count, message_session_id in fixture["message_sessions"].items():
            session = store.get(message_session_id, "chat")
            metrics[f"messages.read_recent_16.{count}"] = _measure(
                lambda selected=message_session_id: store.messages(selected, "chat", limit=16),
                repeats,
                lambda value, selected_count=count: _assert_count(value, min(selected_count, 16)),
            )
            metrics[f"messages.read_all.{count}"] = _measure(
                lambda selected=message_session_id: store.messages(selected, "chat"),
                setup_repeats,
                lambda value, selected_count=count: _assert_count(value, selected_count),
            )
            metrics[f"context.recent_16.{count}"] = _measure(
                lambda current_session=session: build_context(store, current_session), repeats,
                lambda value, selected_count=count: _assert_context(
                    value, selected_count, require_latest_marker=message_session_id == session_id,
                ),
            )
        metrics["tools.read_grants"] = _measure(
            lambda: store.tool_grants(session_id), repeats,
            lambda value: _assert_count(value, 8),
        )

        metrics["backup.snapshot"] = _measure(
            store.backup, setup_repeats,
            lambda value: _assert_backup(value),
        )
        metrics["migration.v1_to_v2"] = _measure_migrations(
            database, root, setup_repeats, session_id, message_count,
        )
        metrics["store.open_existing"] = _measure(lambda: Store(database), repeats)
        metrics["app.startup_new"] = _measure_app_startup(root, setup_repeats)
        metrics["app.startup_existing"] = _measure_app_startup(
            root, setup_repeats, existing_db=database,
        )
        metrics["model_http.loopback_fake"] = _measure_fake_model(repeats)

        metrics["sqlite.concurrent_writes"] = {
            str(workers): _measure_concurrent_writes(
                database, fixture["session_ids"], workers, writes_per_worker,
            )
            for workers in (1, 4, 16)
        }
        correctness["counts"] = {
            "sessions": session_count,
            "messages_per_session": dict(fixture["message_counts"]),
            "total_messages": fixture["total_message_count"],
            "memories": memory_count,
        }
        result = {
            "format_version": 1,
            "generated_at": datetime.now().astimezone().isoformat(),
            "command": "python -m benchmarks.run_baseline",
            "seed": SEED,
            "environment": {
                "os": platform.platform(),
                "python": platform.python_version(),
                "implementation": platform.python_implementation(),
                "sqlite": sqlite3.sqlite_version,
                "cpu_count": os.cpu_count(),
                "data_volume": ROOT.drive,
                "free_bytes_before_run": shutil.disk_usage(ROOT).free,
            },
            "dataset": {
                "session_count": session_count,
                "message_counts_per_session": dict(fixture["message_counts"]),
                "total_message_count": fixture["total_message_count"],
                "memory_count": memory_count,
                "memory_queries": len(MEMORY_QUERIES),
                "repeats": repeats,
                "setup_repeats": setup_repeats,
                "writes_per_worker": writes_per_worker,
            },
            "metrics": metrics,
            "correctness": correctness,
            "process_rss_bytes": _rss_bytes(),
        }

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def _assert_count(value: list, expected: int) -> None:
    if len(value) != expected:
        raise AssertionError(f"expected {expected} rows, got {len(value)}")


def _assert_context(value: list, message_count: int, *, require_latest_marker: bool = False) -> None:
    expected = min(message_count, 16)
    if len(value) != expected + 1:
        raise AssertionError("context did not retain the latest whole-message window")
    if require_latest_marker and MESSAGE_SEARCH not in value[-1]["content"]:
        raise AssertionError("context did not include the latest fixture message")


def _assert_backup(value: bytes) -> None:
    if not value.startswith(b"SQLite format 3\x00"):
        raise AssertionError("backup is not a SQLite snapshot")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "benchmarks" / "results" / "stage8-baseline.json")
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--setup-repeats", type=int, default=3)
    parser.add_argument("--writes-per-worker", type=int, default=40)
    args = parser.parse_args()
    result = run_baseline(
        args.output, repeats=args.repeats, setup_repeats=args.setup_repeats,
        writes_per_worker=args.writes_per_worker,
    )
    print(f"baseline={args.output}")
    print(f"p95_context_10000_ms={result['metrics']['context.recent_16.10000']['p95']}")
    print(f"p95_memory_200_ms={result['metrics']['memory.search_200']['p95']}")
    print(f"rss_bytes={result['process_rss_bytes']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
