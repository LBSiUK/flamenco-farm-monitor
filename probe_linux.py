# Runs on each Linux worker (piped over ssh): prints one JSON line of metrics every INTERVAL seconds.
import glob, json, os, subprocess, sys, time

INTERVAL = 2.0


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def cpu_times():
    fields = [int(x) for x in read("/proc/stat").splitlines()[0].split()[1:]]
    idle = fields[3] + fields[4]
    return sum(fields), idle


def mem():
    info = {}
    for line in read("/proc/meminfo").splitlines():
        key, val = line.split(":", 1)
        info[key] = int(val.split()[0]) * 1024
    return info["MemTotal"], info["MemTotal"] - info["MemAvailable"]


def cpu_temp():
    by_name = {}
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        t = read(d + "/temp1_input")
        if t:
            by_name.setdefault(read(d + "/name"), int(t) / 1000)
    return by_name.get("coretemp") or by_name.get("k10temp") or by_name.get("acpitz")


def nvidia():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total,power.draw",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not out.strip():
        return None
    name, util, temp, used, total, power = [x.strip() for x in out.splitlines()[0].split(",")]
    num = lambda s: float(s) if s.replace(".", "", 1).isdigit() else None
    return {"gpu_name": name, "gpu_pct": num(util), "gpu_temp": num(temp),
            "vram_used": num(used) * 2**20, "vram_total": num(total) * 2**20, "gpu_power": num(power)}



HAS_NVIDIA = subprocess.run(["sh", "-c", "command -v nvidia-smi"], capture_output=True).returncode == 0
prev = cpu_times()
while True:
    time.sleep(INTERVAL)
    cur = cpu_times()
    dt, didle = cur[0] - prev[0], cur[1] - prev[1]
    prev = cur
    total, used = mem()
    sample = {"t": time.time(), "cpu_pct": 100 * (1 - didle / dt) if dt else 0.0, "cpu_temp": cpu_temp(),
              "ram_used": used, "ram_total": total, "cores": os.cpu_count(),
              "load1": os.getloadavg()[0]}
    sample.update((nvidia() if HAS_NVIDIA else None) or {})
    print(json.dumps(sample), flush=True)
