#!/usr/bin/env python3
import argparse
import json
import os
import socket
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
THERMAL_ROOT = Path(os.getenv("THERMAL_ROOT", "/sys/class/thermal"))
PROC_STAT_PATH = Path(os.getenv("PROC_STAT_PATH", "/proc/stat"))
PROC_MEMINFO_PATH = Path(os.getenv("PROC_MEMINFO_PATH", "/proc/meminfo"))
PROC_UPTIME_PATH = Path(os.getenv("PROC_UPTIME_PATH", "/proc/uptime"))
PROC_LOADAVG_PATH = Path(os.getenv("PROC_LOADAVG_PATH", "/proc/loadavg"))
PROC_HOSTNAME_PATH = Path(os.getenv("PROC_HOSTNAME_PATH", "/etc/hostname"))
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST")


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def classify_temp(temp_c: float) -> str:
    if temp_c >= 85:
        return "critical"
    if temp_c >= 75:
        return "hot"
    if temp_c >= 60:
        return "warm"
    return "normal"


class MetricsCollector:
    def __init__(self) -> None:
        self.prev_total: int | None = None
        self.prev_idle: int | None = None

    def cpu_usage_percent(self) -> float | None:
        stat = read_text(PROC_STAT_PATH)
        if not stat:
            return None
        fields = stat.splitlines()[0].split()
        if len(fields) < 5 or fields[0] != "cpu":
            return None

        values = [int(v) for v in fields[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)

        if self.prev_total is None or self.prev_idle is None:
            self.prev_total = total
            self.prev_idle = idle
            return None

        total_delta = total - self.prev_total
        idle_delta = idle - self.prev_idle
        self.prev_total = total
        self.prev_idle = idle

        if total_delta <= 0:
            return None

        usage = (1.0 - (idle_delta / total_delta)) * 100.0
        return round(max(0.0, min(usage, 100.0)), 1)

    def thermal_zones(self) -> list[dict]:
        zones: list[dict] = []
        for zone in sorted(THERMAL_ROOT.glob("thermal_zone*")):
            sensor_type = read_text(zone / "type") or zone.name
            raw = read_text(zone / "temp")
            if raw is None:
                continue
            try:
                value = float(raw)
            except ValueError:
                continue

            temp_c = value / 1000.0 if value > 200 else value
            zones.append(
                {
                    "zone": zone.name,
                    "sensor": sensor_type,
                    "temp_c": round(temp_c, 1),
                    "state": classify_temp(temp_c),
                }
            )
        return zones

    def memory(self) -> dict:
        total_kb = 0
        available_kb = 0
        swap_total_kb = 0
        swap_free_kb = 0
        try:
            with PROC_MEMINFO_PATH.open(encoding="utf-8") as handle:
                for line in handle:
                    key, value = line.split(":", 1)
                    kb = int(value.strip().split()[0])
                    if key == "MemTotal":
                        total_kb = kb
                    elif key == "MemAvailable":
                        available_kb = kb
                    elif key == "SwapTotal":
                        swap_total_kb = kb
                    elif key == "SwapFree":
                        swap_free_kb = kb
        except (OSError, ValueError):
            return {}

        if total_kb <= 0:
            return {}

        used_kb = max(total_kb - available_kb, 0)
        swap_used_kb = max(swap_total_kb - swap_free_kb, 0)

        ram_total_gb = round(total_kb / (1024.0 * 1024.0), 2)
        ram_used_gb = round(used_kb / (1024.0 * 1024.0), 2)
        ram_used_percent = round((used_kb / total_kb) * 100.0, 1)

        swap_total_gb = round(swap_total_kb / (1024.0 * 1024.0), 2)
        swap_used_gb = round(swap_used_kb / (1024.0 * 1024.0), 2)
        swap_used_percent = round((swap_used_kb / swap_total_kb) * 100.0, 1) if swap_total_kb > 0 else 0.0

        return {
            "total_mb": round(total_kb / 1024.0, 1),
            "used_mb": round(used_kb / 1024.0, 1),
            "used_percent": ram_used_percent,
            "ram": {
                "total_mb": round(total_kb / 1024.0, 1),
                "used_mb": round(used_kb / 1024.0, 1),
                "total_gb": ram_total_gb,
                "used_gb": ram_used_gb,
                "used_percent": ram_used_percent,
            },
            "swap": {
                "total_mb": round(swap_total_kb / 1024.0, 1),
                "used_mb": round(swap_used_kb / 1024.0, 1),
                "total_gb": swap_total_gb,
                "used_gb": swap_used_gb,
                "used_percent": swap_used_percent,
            },
        }

    def uptime(self) -> dict:
        raw = read_text(PROC_UPTIME_PATH)
        if not raw:
            return {"seconds": 0, "human": "unknown"}
        try:
            seconds = int(float(raw.split()[0]))
        except (ValueError, IndexError):
            return {"seconds": 0, "human": "unknown"}

        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)

        parts: list[str] = []
        if days:
            parts.append(f"{days}d")
        parts.append(f"{hours:02}h")
        parts.append(f"{minutes:02}m")
        return {"seconds": seconds, "human": " ".join(parts)}

    def cpu_pressure(self, loadavg: list[float]) -> dict:
        cores = os.cpu_count() or 1
        if not loadavg or cores <= 0:
            return {}

        one_min_load = float(loadavg[0])
        queue_per_core_1m = one_min_load / cores
        demand_percent = round(queue_per_core_1m * 100.0, 1)

        if demand_percent < 70:
            state = "normal"
        elif demand_percent < 100:
            state = "busy"
        elif demand_percent < 130:
            state = "high"
        else:
            state = "overloaded"

        return {
            "cores": int(cores),
            "one_min_load": round(one_min_load, 2),
            "queue_per_core_1m": round(queue_per_core_1m, 2),
            "demand_percent": demand_percent,
            "state": state,
        }

    def collect(self) -> dict:
        zones = self.thermal_zones()
        max_temp = max((z["temp_c"] for z in zones), default=None)
        cpu_usage = self.cpu_usage_percent()
        loadavg = []
        loadavg_raw = read_text(PROC_LOADAVG_PATH)
        if loadavg_raw:
            parts = loadavg_raw.split()
            if len(parts) >= 3:
                try:
                    loadavg = [round(float(parts[0]), 2), round(float(parts[1]), 2), round(float(parts[2]), 2)]
                except ValueError:
                    loadavg = []
        if not loadavg:
            try:
                load_1m, load_5m, load_15m = os.getloadavg()
                loadavg = [round(load_1m, 2), round(load_5m, 2), round(load_15m, 2)]
            except OSError:
                loadavg = []

        host_name = DASHBOARD_HOST or read_text(PROC_HOSTNAME_PATH) or socket.gethostname()

        return {
            "host": host_name,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "zones": zones,
            "max_temp_c": max_temp,
            "cpu_usage_percent": cpu_usage,
            "loadavg": loadavg,
            "cpu_pressure": self.cpu_pressure(loadavg),
            "memory": self.memory(),
            "uptime": self.uptime(),
        }


class DashboardHandler(BaseHTTPRequestHandler):
    collector: MetricsCollector | None = None

    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            self.serve_index()
            return
        if path == "/api/metrics":
            if self.collector is None:
                self.send_json({"error": "collector unavailable"}, status=500)
                return
            self.send_json(self.collector.collect())
            return
        if path == "/health":
            self.send_text("ok\n")
            return

        self.send_json({"error": "not found"}, status=404)

    def serve_index(self) -> None:
        try:
            body = INDEX_FILE.read_bytes()
        except OSError:
            self.send_text("index.html missing\n", status=500)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, payload: str, status: int = 200) -> None:
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Khadas headless thermal dashboard")
    parser.add_argument("--host", default="0.0.0.0", help="listen host")
    parser.add_argument("--port", default=8088, type=int, help="listen port")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    collector = MetricsCollector()
    collector.collect()

    DashboardHandler.collector = collector
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)

    print(f"Serving dashboard on http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
