"""A made-up render farm for `python3 server.py --demo`.

Four fictional machines work through fictional Flamenco jobs, so the dashboard can be tried (or
screenshotted) without a farm, ssh access or a Flamenco Manager. Nothing here touches the network.
DemoFarm stands in for both things the real monitor talks to: step() plays the part of the metric
probes, and api() / log_tail() answer the same Manager REST calls server.py makes, so the demo runs
through the real job aggregation and Blender log parsing.
"""
import math
import random
import re
import threading
import uuid
from datetime import datetime, timezone

GB = 2 ** 30
SAMPLES = 512        # Cycles samples per frame
LOAD_SECONDS = 9     # opening the .blend at the start of each task
SYNC_SECONDS = 1.5   # scene sync between frames
LOAD_CPU = 70        # CPU use while loading or syncing (BVH builds are multi-threaded)

CONFIG = {
    "port": 8091,
    "manager": "http://flamenco-demo.invalid:8080",
    "machines": [
        {"key": "workstation", "label": "workstation", "worker": "workstation",
         "role": "Apple Silicon · Metal"},
        {"key": "render-node-1", "label": "render-node-1", "ssh": "render@render-node-1",
         "worker": "render-node-1", "role": "RTX 4070 · OptiX"},
        {"key": "render-node-2", "label": "render-node-2", "ssh": "render@render-node-2",
         "worker": "render-node-2", "role": "RTX 3060 · OptiX"},
        {"key": "laptop", "label": "laptop", "ssh": "render@laptop", "worker": "laptop",
         "role": "CPU only · skips GPU jobs"},
    ],
}

# Per machine: Cycles samples per second, and (idle, busy) pairs for usage %, temperature °C,
# GPU power in W and memory in GB. A machine without "gpu" has no usable GPU and sits out GPU jobs.
PROFILES = {
    "workstation": {"rate": 6.0, "cores": 12, "ram": 36, "gpu": "Apple Silicon GPU",
                    "cpu": (6, 21), "gpu_pct": (2, 95), "cpu_temp": (45, 64), "gpu_temp": (40, 72),
                    "gpu_power": (0.4, 26), "ram_used": (12.5, 20.5)},
    "render-node-1": {"rate": 11.0, "cores": 16, "ram": 32, "vram": 12, "gpu": "NVIDIA GeForce RTX 4070",
                      "cpu": (1, 8), "gpu_pct": (0, 98), "cpu_temp": (32, 46), "gpu_temp": (35, 73),
                      "gpu_power": (11, 187), "ram_used": (2.1, 9.4), "vram_used": (0.4, 7.9)},
    "render-node-2": {"rate": 7.5, "cores": 8, "ram": 16, "vram": 12, "gpu": "NVIDIA GeForce RTX 3060",
                      "cpu": (1, 13), "gpu_pct": (0, 99), "cpu_temp": (30, 44), "gpu_temp": (34, 81),
                      "gpu_power": (13, 162), "ram_used": (1.8, 8.1), "vram_used": (0.3, 7.6)},
    "laptop": {"rate": 1.1, "cores": 4, "ram": 16,
               "cpu": (3, 100), "cpu_temp": (42, 88), "ram_used": (3.4, 6.2)},
}

# New jobs queued whenever one finishes, so the demo keeps going: name, frames, chunk size, GPU only.
ROTATION = [
    ("Courtyard flythrough · shot 3", "1-180", 4, True),
    ("Product turntable · 8K", "1-120", 2, False),
    ("Studio stills · 6K", "1-12", 1, True),
    ("Logo sting · 4K", "1-150", 5, False),
]


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def clock(seconds):
    """Blender's "Remaining:" format, MM:SS.ss."""
    return f"{int(seconds // 60):02d}:{seconds % 60:05.2f}"


class DemoFarm:
    def __init__(self, machines, start, seed=7):
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.t = start
        self.jobs = []
        self.next_job = 0
        self.workers = {}
        for m in machines:
            prof = PROFILES[m["key"]]
            self.workers[m["worker"]] = {
                "key": m["key"], "name": m["worker"], "id": self._id(), "prof": prof, "local": not m.get("ssh"),
                "task": None, "frame": 0, "sample": 0.0, "rate": prof["rate"], "wait": 0.0, "stage": None,
                "cpu_temp": prof["cpu_temp"][0], "gpu_temp": prof.get("gpu_temp", (0, 0))[0],
                "ram": prof["ram_used"][0], "vram": prof.get("vram_used", (0, 0))[0],
            }
        self._history(start)

    def _id(self):
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    # ---- jobs ----

    def _job(self, name, created, frames=None, chunk=4, gpu_only=True, scripts=None):
        job = {"id": self._id(), "name": name, "status": "queued", "created": created, "updated": created,
               "type": "simple-blender-render" if frames else "bake-lightmaps", "frames": frames,
               "gpu_only": gpu_only, "slug": re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"), "tasks": []}
        if frames:
            lo, hi = (int(x) for x in frames.split("-"))
            for a in range(lo, hi + 1, chunk):
                b = min(a + chunk - 1, hi)
                job["tasks"].append(self._task(job, f"render-{a}-{b}" if b > a else f"render-{a}", a, b))
        for script in scripts or []:
            job["tasks"].append(self._task(job, script, 0, 0))
        self.jobs.append(job)
        return job

    def _task(self, job, name, first, last):
        return {"id": self._id(), "name": name, "status": "queued", "task_type": "blender",
                "worker": None, "first": first, "last": last, "job": job}

    def _finish_tasks(self, job, count, workers):
        """Mark the first `count` tasks completed, shared out by machine speed."""
        weights = [self.workers[w]["prof"]["rate"] for w in workers]
        for task in job["tasks"][:count]:
            task["status"], task["worker"] = "completed", self.rng.choices(workers, weights)[0]

    def _history(self, start):
        """A few finished jobs, one job part-way through, and one waiting in the queue."""
        everyone, gpus = list(self.workers), [w for w in self.workers if self.workers[w]["prof"].get("gpu")]
        h = 3600
        for name, ago, frames, chunk, done, status, gpu_only, scripts in [
            ("Logo sting · 1080p", 3.4 * h, "1-150", 5, 30, "completed", False, None),
            ("Kitchen stills · draft", 2.3 * h, "1-36", 3, 5, "canceled", True, None),
            ("Product turntable · 4K", 1.2 * h, "1-240", 4, 60, "completed", False, None),
            ("Lookdev · light bake", 0.6 * h, None, 0, 8, "completed", False,
             [f"bake-{part}" for part in ("floor", "walls", "ceiling", "props", "glass", "foliage", "car", "sky")]),
        ]:
            job = self._job(name, start - ago, frames, chunk, gpu_only, scripts)
            self._finish_tasks(job, done, gpus if gpu_only else everyone)
            job["status"], job["updated"] = status, start - ago + self.rng.uniform(0.25, 0.6) * h
            if status == "canceled":
                for task in job["tasks"][done:]:
                    task["status"] = "canceled"
        job = self._job("Courtyard flythrough · shot 2", start - 0.4 * h, "1-240", 4)
        self._finish_tasks(job, 21, gpus)
        job["status"], job["updated"] = "active", start - 40
        self._job("Studio stills · 6K", start - 180, "1-12", 1)
        for w in self.workers.values():  # start the GPU machines part-way through a frame
            if self._claim(w, start):
                w["frame"] += self.rng.randint(0, 3)
                w["sample"], w["wait"], w["stage"] = self.rng.uniform(0, SAMPLES), 0.0, "render"

    def _claim(self, w, t):
        """Give the worker the next queued task it may run, oldest job first, as Flamenco does."""
        for job in sorted(self.jobs, key=lambda j: j["created"]):
            if job["status"] not in ("active", "queued") or (job["gpu_only"] and not w["prof"].get("gpu")):
                continue
            task = next((x for x in job["tasks"] if x["status"] == "queued"), None)
            if task:
                task["status"], task["worker"] = "active", w["name"]
                job["status"], job["updated"] = "active", t
                w.update(task=task, frame=task["first"], sample=0.0, wait=LOAD_SECONDS, stage="load",
                         rate=w["prof"]["rate"] * self.rng.uniform(0.92, 1.08))
                return task
        return None

    def _frame_done(self, w, t):
        task, job = w["task"], w["task"]["job"]
        if w["frame"] < task["last"]:
            w.update(frame=w["frame"] + 1, sample=0.0, wait=SYNC_SECONDS, stage="sync",
                     rate=w["prof"]["rate"] * self.rng.uniform(0.92, 1.08))
            return
        task["status"], job["updated"], w["task"], w["stage"] = "completed", t, None, None
        if all(x["status"] == "completed" for x in job["tasks"]):
            job["status"] = "completed"
            name, frames, chunk, gpu_only = ROTATION[self.next_job % len(ROTATION)]
            self.next_job += 1
            self._job(name, t, frames, chunk, gpu_only)
            finished = [j for j in self.jobs if j["status"] in ("completed", "canceled")]
            if len(self.jobs) > 8 and finished:  # keep the list short: drop the oldest finished job
                self.jobs.remove(min(finished, key=lambda j: j["created"]))

    # ---- metrics ----

    def step(self, t):
        """Advance to time t; return one metrics sample per machine, keyed like farm.json."""
        with self.lock:
            dt = max(0.001, t - self.t)
            self.t = t
            return {w["key"]: self._advance(w, t, dt) for w in self.workers.values()}

    def _advance(self, w, t, dt):
        render = load = 0.0
        left = dt
        while left > 1e-6:
            if w["task"] is None and not self._claim(w, t - left):
                break
            if w["wait"] > 0:
                used = min(left, w["wait"])
                w["wait"] -= used
                load += used
                left -= used
                continue
            w["stage"] = "render"
            need = (SAMPLES - w["sample"]) / w["rate"]
            used = min(left, need)
            w["sample"] += used * w["rate"]
            render += used
            left -= used
            if used >= need:
                self._frame_done(w, t - left)
        return self._sample(w, t, dt, render / dt, load / dt)

    def _sample(self, w, t, dt, f_render, f_load):
        p, rng = w["prof"], self.rng
        lerp = lambda pair, f: pair[0] + (pair[1] - pair[0]) * f
        clamp = lambda v: max(0.0, min(100.0, v))
        ease = lambda cur, target, tau: cur + (target - cur) * (1 - math.exp(-dt / tau))

        cpu = lerp(p["cpu"], f_render) + (LOAD_CPU - p["cpu"][0]) * f_load
        cpu += rng.gauss(0, 1.5 if f_render else 0.8)
        if w["local"] and rng.random() < 0.06:  # someone is using the workstation too
            cpu += rng.uniform(8, 25)
        activity = f_render + 0.5 * f_load
        w["cpu_temp"] = ease(w["cpu_temp"], lerp(p["cpu_temp"], activity), 30) + rng.gauss(0, 0.3)
        loaded = 1.0 if w["task"] else 0.0
        w["ram"] = ease(w["ram"], lerp(p["ram_used"], loaded), 6) + rng.gauss(0, 0.02)
        sample = {"t": t, "cpu_pct": clamp(cpu), "cpu_temp": w["cpu_temp"], "cores": p["cores"],
                  "ram_used": w["ram"] * GB, "ram_total": p["ram"] * GB}
        if not w["local"]:
            sample["load1"] = p["cores"] * clamp(cpu) / 100
        if p.get("gpu"):
            gpu = clamp(lerp(p["gpu_pct"], f_render) + rng.gauss(0, 1.2 if f_render else 0.6))
            w["gpu_temp"] = ease(w["gpu_temp"], lerp(p["gpu_temp"], gpu / 100), 40) + rng.gauss(0, 0.25)
            sample.update(gpu_name=p["gpu"], gpu_pct=gpu, gpu_temp=w["gpu_temp"],
                          gpu_power=max(0.0, lerp(p["gpu_power"], gpu / 100) + rng.gauss(0, 1.5)))
        if p.get("vram"):
            w["vram"] = ease(w["vram"], lerp(p["vram_used"], loaded), 6)
            sample.update(vram_used=w["vram"] * GB, vram_total=p["vram"] * GB)
        return sample

    # ---- the Flamenco Manager API, as far as server.py uses it ----

    def api(self, path):
        with self.lock:
            if path == "/worker-mgt/workers":
                return {"workers": [{"id": w["id"], "name": w["name"], "status": "awake", "last_seen": iso(self.t)}
                                    for w in self.workers.values()]}
            if m := re.fullmatch(r"/worker-mgt/workers/([^/]+)", path):
                w = next(w for w in self.workers.values() if w["id"] == m.group(1))
                task = w["task"]
                return {"id": w["id"], "name": w["name"], "status": "awake", "task": task and {
                    "id": task["id"], "name": task["name"], "status": task["status"],
                    "task_type": task["task_type"], "job_id": task["job"]["id"]}}
            if path == "/jobs":
                return {"jobs": [{"id": j["id"], "name": j["name"], "status": j["status"], "type": j["type"],
                                  "created": iso(j["created"]), "updated": iso(j["updated"]),
                                  "settings": {"frames": j["frames"]} if j["frames"] else {}}
                                 for j in self.jobs]}
            if m := re.fullmatch(r"/jobs/([^/]+)/tasks", path):
                job = next(j for j in self.jobs if j["id"] == m.group(1))
                return {"tasks": [{"id": x["id"], "name": x["name"], "status": x["status"],
                                   "task_type": x["task_type"], **({"worker": {"name": x["worker"]}} if x["worker"] else {})}
                                  for x in job["tasks"]]}
            raise LookupError(f"demo Manager has no {path}")

    def log_tail(self, task_id):
        """The end of the task's log, written the way Blender 5 prints it."""
        with self.lock:
            w = next((w for w in self.workers.values() if w["task"] and w["task"]["id"] == task_id), None)
            if w is None:
                return ""
            lines = [f"Read blend: /farm/jobs/{w['task']['job']['slug']}.blend"]
            if w["stage"] == "load":
                return "\n".join(lines)
            mem = round(w["vram"] * 1024 if w["prof"].get("vram") else w["ram"] * 160)
            lines.append(f"render | Fra: {w['frame']} | Mem: {mem}M | Synchronizing object | Courtyard")
            if w["sample"] >= 1:
                left = (SAMPLES - w["sample"]) / w["rate"]
                lines.append(f"render | Fra: {w['frame']} | Remaining: {clock(left)} | Mem: {mem}M | "
                             f"Sample {int(w['sample'])}/{SAMPLES}")
            return "\n".join(lines)
