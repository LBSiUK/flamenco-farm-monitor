#!/usr/bin/env python3
"""Render-farm monitor: live temps / CPU / GPU / RAM per machine plus Flamenco frame progress.

Linux workers are sampled by piping probe_linux.py over ssh; the Mac (which also hosts the
Flamenco Manager) is sampled with `macmon pipe`. The browser polls /api/state every 2 s.
Standard library only. Machines and the Manager URL come from farm.json.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Machines, manager URL and port live in farm.json (see farm.example.json); FARM_CONFIG overrides the path.
CONFIG = json.loads(Path(os.environ.get("FARM_CONFIG", HERE / "farm.json")).read_text())
PORT = CONFIG.get("port", 8091)
MANAGER = CONFIG["manager"].rstrip("/") + "/api/v3"
HISTORY = 150  # samples kept per machine (5 minutes at 2 s)
MACHINES = {m["key"]: m for m in CONFIG["machines"]}
LABELS = {m["worker"]: m["label"] for m in CONFIG["machines"]}

lock = threading.Lock()
metrics = {k: {"online": False, "latest": None, "history": deque(maxlen=HISTORY), "error": None}
           for k in MACHINES}
farm = {"workers": {}, "jobs": [], "updated": None, "error": None}


def record(key, sample):
    with lock:
        m = metrics[key]
        m["online"], m["latest"], m["error"] = True, sample, None
        m["history"].append({k: sample.get(k) for k in ("t", "cpu_pct", "gpu_pct", "cpu_temp", "gpu_temp")})


def mark_offline(key, error):
    with lock:
        metrics[key]["online"], metrics[key]["error"] = False, error


def watch_linux(key, host):
    probe = (HERE / "probe_linux.py").read_bytes()
    while True:
        proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5",
             "-o", "ServerAliveCountMax=2", host, "python3 -u -"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            proc.stdin.write(probe)
            proc.stdin.close()
        except BrokenPipeError:  # ssh already gave up; its stderr below says why
            pass
        for line in proc.stdout:
            try:
                record(key, json.loads(line))
            except ValueError:
                pass
        err = proc.stderr.read().decode(errors="replace").strip().splitlines()
        mark_offline(key, err[-1] if err else "connection closed")
        proc.wait()
        time.sleep(5)


def chip_name():
    try:
        return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                              capture_output=True, text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def watch_mac(key):
    # launchd's PATH has no /opt/homebrew/bin, so fall back to Homebrew's usual location.
    macmon = shutil.which("macmon") or "/opt/homebrew/bin/macmon"
    chip = chip_name()
    while True:
        try:
            proc = subprocess.Popen([macmon, "pipe", "-i", "2000"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as exc:
            mark_offline(key, f"cannot run macmon: {exc.strerror} (install it with: brew install macmon)")
            time.sleep(30)
            continue
        for line in proc.stdout:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            p, e = len(d["pcpu_cores"]), len(d["ecpu_cores"])
            record(key, {
                "t": time.time(),
                "cpu_pct": 100 * (d["pcpu_active_ratio"] * p + d["ecpu_active_ratio"] * e) / (p + e),
                "cpu_temp": d["temp"]["cpu_temp_avg"],
                "gpu_pct": 100 * d["gpu_active_ratio"],
                "gpu_temp": d["temp"]["gpu_temp_avg"],
                "gpu_name": f"{chip} GPU" if chip else None,
                "gpu_power": d["gpu_power"],
                "ram_used": d["memory"]["ram_usage"], "ram_total": d["memory"]["ram_total"],
                "cores": p + e,
            })
        mark_offline(key, "macmon exited")
        proc.wait()
        time.sleep(5)


def api(path):
    with urllib.request.urlopen(MANAGER + path, timeout=5) as r:
        return json.load(r)


def frames_in(spec):
    """'1-24' / '1,2' / '5' -> number of frames."""
    count = 0
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        lo, _, hi = part.partition("-")
        count += int(hi) - int(lo) + 1 if hi else 1
    return count


def task_frames(task):
    m = re.match(r"render-(.+)$", task["name"])
    return frames_in(m.group(1)) if m and task.get("task_type") == "blender" else 0


LOG_FRAME = re.compile(r"Fra: (\d+)")
LOG_SAMPLE = re.compile(r"Sample (\d+)/(\d+)")
LOG_REMAIN = re.compile(r"Remaining: ([\d:.]+)")
# Stage markers for tasks that are not frame renders (bakes, card renders). Scripts can print
# "STAGE <text>" to show their own; the BAKE/CARD lines are the Onward web3d scripts' markers.
LOG_STAGES = [
    (re.compile(r"STAGE (.+)"), lambda m: m.group(1).strip()),
    (re.compile(r"BAKE start (\S+) (\d+) px (\d+) spp"), lambda m: f"Baking {m.group(1)} · {m.group(2)} px · {m.group(3)} spp"),
    (re.compile(r"CARD saved .*_(front|side|top)\.png"), lambda m: {"front": "Cards 1/3", "side": "Cards 2/3", "top": "Cards 3/3"}[m.group(1)]),
    (re.compile(r"Read blend: "), lambda m: "Loading scene…"),
]


def live_progress(task_id):
    """Current frame / sample / time remaining parsed from the task's Blender log tail."""
    try:
        req = urllib.request.urlopen(f"{MANAGER}/tasks/{task_id}/logtail", timeout=5)
        tail = req.read().decode(errors="replace")
    except Exception:
        return {}
    out = {}
    for line in reversed(tail.splitlines()):
        if "frame" not in out and (m := LOG_FRAME.search(line)):
            out["frame"] = int(m.group(1))
        if "sample" not in out and (m := LOG_SAMPLE.search(line)):
            out["sample"], out["samples"] = int(m.group(1)), int(m.group(2))
        if "remaining" not in out and (m := LOG_REMAIN.search(line)):
            out["remaining"] = m.group(1)
        if "stage" not in out:
            for rx, label in LOG_STAGES:
                if m := rx.search(line):
                    out["stage"] = label(m)
                    break
        if len(out) >= 5:
            break
    return out


def watch_flamenco():
    done_tasks = {}  # job id -> task list, for jobs that can no longer change
    while True:
        try:
            workers = {}
            for w in api("/worker-mgt/workers")["workers"]:
                detail = api(f"/worker-mgt/workers/{w['id']}")
                task = detail.get("task")
                active = task and task.get("status") == "active"
                workers[w["name"]] = {
                    "status": w["status"], "last_seen": w.get("last_seen"),
                    "task": task if active else None,
                    "progress": live_progress(task["id"]) if active and task["task_type"] == "blender" else {},
                    "frames_done": 0, "tasks_done": 0,
                }
            jobs = []
            for job in api("/jobs")["jobs"]:
                tasks = done_tasks.get(job["id"]) or api(f"/jobs/{job['id']}/tasks")["tasks"]
                if job["status"] in ("completed", "canceled", "failed"):
                    done_tasks[job["id"]] = tasks
                per_worker, frames_done, tasks_done = {}, 0, 0
                work = [t for t in tasks if t["task_type"] != "ffmpeg"]
                framed = any(task_frames(t) for t in work)
                for t in work:
                    if t["status"] != "completed":
                        continue
                    n, name = task_frames(t), (t.get("worker") or {}).get("name")
                    frames_done += n
                    tasks_done += 1
                    if name:
                        # frame jobs are split by frames; script jobs (bakes etc.) by tasks
                        per_worker[name] = per_worker.get(name, 0) + (n if framed else 1)
                        if name in workers:
                            workers[name]["frames_done"] += n
                            workers[name]["tasks_done"] += 1
                settings = job.get("settings", {})
                jobs.append({
                    "id": job["id"], "name": job["name"], "status": job["status"], "type": job["type"],
                    "created": job["created"], "updated": job["updated"],
                    "frames_total": frames_in(settings.get("frames", "")) if settings.get("frames") else 0,
                    "frames_done": frames_done, "per_worker": per_worker, "framed": framed,
                    "tasks_done": tasks_done, "tasks_total": len(work),
                    "steps_done": job.get("steps_completed"), "steps_total": job.get("steps_total"),
                })
            jobs.sort(key=lambda j: j["created"], reverse=True)
            with lock:
                farm.update(workers=workers, jobs=jobs, updated=time.time(), error=None)
        except Exception as exc:  # manager down: keep the last picture, flag it
            with lock:
                farm["error"] = str(exc)
        time.sleep(2)


def snapshot():
    with lock:
        machines = []
        for key, cfg in MACHINES.items():
            m = metrics[key]
            machines.append({
                "key": key, "label": cfg["label"], "role": cfg.get("role", ""), "worker_name": cfg["worker"], "local": not cfg.get("ssh"),
                "online": m["online"], "error": m["error"], "latest": m["latest"],
                "history": list(m["history"]), "worker": farm["workers"].get(cfg["worker"]),
            })
        return {"now": time.time(), "machines": machines, "jobs": farm["jobs"],
                "farm_updated": farm["updated"], "farm_error": farm["error"], "labels": LABELS}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(HERE / "web"), **kwargs)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/state":
            body = json.dumps(snapshot()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def log_message(self, *args):
        pass


def main():
    for key, cfg in MACHINES.items():
        target = (watch_linux, (key, cfg["ssh"])) if cfg.get("ssh") else (watch_mac, (key,))
        threading.Thread(target=target[0], args=target[1], daemon=True).start()
    threading.Thread(target=watch_flamenco, daemon=True).start()
    print(f"farm monitor on http://0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
