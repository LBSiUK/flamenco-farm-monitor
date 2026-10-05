"""Run with: python3 -m unittest discover tests"""
import json
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import demo  # noqa: E402
import server  # noqa: E402


class FrameCounting(unittest.TestCase):
    def test_frames_in(self):
        self.assertEqual(server.frames_in("1-24"), 24)
        self.assertEqual(server.frames_in("1,2"), 2)
        self.assertEqual(server.frames_in("5"), 1)
        self.assertEqual(server.frames_in("1-10, 20-29"), 20)

    def test_task_frames(self):
        self.assertEqual(server.task_frames({"name": "render-1-4", "task_type": "blender"}), 4)
        self.assertEqual(server.task_frames({"name": "render-7", "task_type": "blender"}), 1)
        self.assertEqual(server.task_frames({"name": "create-video", "task_type": "ffmpeg"}), 0)
        self.assertEqual(server.task_frames({"name": "bake-floor", "task_type": "blender"}), 0)


class LogParsing(unittest.TestCase):
    def test_blender_5(self):
        tail = ("00:01.189  render | Fra: 12 | Mem: 1M | Updating Scene\n"
                "00:01.196  render | Fra: 12 | Remaining: 00:45.67 | Mem: 1M | Sample 128/512\n")
        self.assertEqual(server.parse_log_tail(tail),
                         {"frame": 12, "sample": 128, "samples": 512, "remaining": "00:45.67"})

    def test_blender_4(self):
        tail = ("pid=4120 > Fra:12 Mem:1234.56M (Peak 2345.67M) | Time:00:12.34 | Remaining:00:45.67 | "
                "Mem:12.38M, Peak:12.38M | Scene, ViewLayer | Sample 128/512\n")
        self.assertEqual(server.parse_log_tail(tail),
                         {"frame": 12, "sample": 128, "samples": 512, "remaining": "00:45.67"})

    def test_newest_line_wins(self):
        tail = "Fra: 3 | Sample 512/512\nFra: 4 | Sample 10/512\n"
        self.assertEqual(server.parse_log_tail(tail)["frame"], 4)

    def test_stages(self):
        self.assertEqual(server.parse_log_tail("Read blend: /x/shot.blend\n")["stage"], "Loading scene…")
        self.assertEqual(server.parse_log_tail("Read blend: /x.blend\nSTAGE Exporting glTF\n")["stage"],
                         "Exporting glTF")


class DemoFarm(unittest.TestCase):
    def setUp(self):
        self.start = 1_700_000_000.0
        self.farm = demo.DemoFarm(demo.CONFIG["machines"], start=self.start)

    def active_frames(self):
        jobs = self.farm.api("/jobs")["jobs"]
        job = next(j for j in jobs if j["status"] == "active")
        tasks = self.farm.api(f"/jobs/{job['id']}/tasks")["tasks"]
        return sum(server.task_frames(t) for t in tasks if t["status"] == "completed")

    def test_samples_have_the_probe_fields(self):
        samples = self.farm.step(self.start + 2)
        self.assertEqual(set(samples), {m["key"] for m in demo.CONFIG["machines"]})
        for sample in samples.values():
            for field in ("t", "cpu_pct", "cpu_temp", "ram_used", "ram_total", "cores"):
                self.assertIn(field, sample)
            self.assertTrue(0 <= sample["cpu_pct"] <= 100)
        self.assertNotIn("gpu_pct", samples["laptop"])
        self.assertIn("vram_total", samples["render-node-1"])

    def test_frames_keep_coming(self):
        before = self.active_frames()
        for i in range(1, 301):  # ten minutes
            self.farm.step(self.start + 2 * i)
        self.assertGreater(self.active_frames(), before)

    def test_workers_and_log_tail(self):
        self.farm.step(self.start + 2)
        busy = 0
        for w in self.farm.api("/worker-mgt/workers")["workers"]:
            task = self.farm.api(f"/worker-mgt/workers/{w['id']}")["task"]
            if task:
                busy += 1
                self.assertEqual(task["status"], "active")
                self.assertIn("frame", server.parse_log_tail(self.farm.log_tail(task["id"])))
        self.assertEqual(busy, 3)  # the laptop sits out GPU-only jobs

    def test_the_demo_keeps_going_after_a_job_finishes(self):
        for i in range(1, 2400):  # 80 minutes: longer than the active job takes
            self.farm.step(self.start + 2 * i)
        jobs = {j["name"]: j["status"] for j in self.farm.api("/jobs")["jobs"]}
        self.assertEqual(jobs["Courtyard flythrough · shot 2"], "completed")
        self.assertIn("active", jobs.values())
        self.assertLessEqual(len(jobs), 8)


class DemoServer(unittest.TestCase):
    def test_serves_the_page_and_state(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        proc = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--demo", "--port", str(port)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            state = None
            for _ in range(50):
                time.sleep(0.2)
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
                        state = json.load(r)
                    if state["jobs"]:
                        break
                except OSError:
                    continue
            self.assertIsNotNone(state, "server did not answer")
            self.assertEqual([m["online"] for m in state["machines"]], [True] * 4)
            self.assertEqual(len(state["machines"][0]["history"]), server.HISTORY)
            self.assertIsNone(state["farm_error"])
            self.assertTrue(any(j["status"] == "active" for j in state["jobs"]))
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
                self.assertIn(b"Render farm", r.read())
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
