import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from benchmarks import run_baseline as benchmark


class BenchmarkSmokeTests(unittest.TestCase):
    def test_baseline_uses_fixed_data_checks_fake_model_and_cleans_temp_files(self):
        roots = []

        @contextmanager
        def tracked_temporary_root():
            with tempfile.TemporaryDirectory(prefix="nico-benchmark-test-") as directory:
                roots.append(Path(directory))
                yield directory

        with tempfile.TemporaryDirectory() as output_directory:
            output = Path(output_directory) / "baseline.json"
            with patch.object(benchmark, "temporary_data_root", tracked_temporary_root):
                result = benchmark.run_baseline(
                    output, message_count=24, session_count=16,
                    repeats=1, setup_repeats=1, writes_per_worker=1,
                )

            saved = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result["correctness"]["counts"], {
            "sessions": 16, "messages_per_session": {24: 24},
            "total_messages": 24, "memories": 200,
        })
        self.assertEqual(result["correctness"]["memory_recall_at_10"], {
            "50": 1.0, "200": 1.0,
        })
        self.assertEqual(result["metrics"]["model_http.loopback_fake"]["successful_calls"], 1)
        if benchmark.os.name == "nt" or Path("/proc/self/status").exists():
            self.assertGreater(result["process_rss_bytes"], 0)
        else:
            self.assertIsNone(result["process_rss_bytes"])
        self.assertEqual(
            result["metrics"]["model_http.loopback_fake"]["error_path"]["code"],
            "model_request_rejected",
        )
        self.assertEqual(saved["dataset"]["message_counts_per_session"], {"24": 24})
        self.assertFalse(roots[0].exists())


if __name__ == "__main__":
    unittest.main()
