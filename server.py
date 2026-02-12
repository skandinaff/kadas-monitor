#!/usr/bin/env python3
import argparse
import http.client
import json
import os
import socket
import threading
import time
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
PROC_NET_DEV_PATH = Path(os.getenv("PROC_NET_DEV_PATH", "/proc/net/dev"))
DOCKER_SOCKET_PATH = os.getenv("DOCKER_SOCKET_PATH", "/var/run/docker.sock")
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


class DockerSocketConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 3.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class MetricsCollector:
    def __init__(self) -> None:
        self.prev_total: int | None = None
        self.prev_idle: int | None = None
        self._prev_net: dict[str, tuple[float, int, int]] = {}  # iface -> (timestamp, rx, tx)
        self._docker_cache: list[dict] = []
        self._docker_lock = threading.Lock()
        self._start_docker_poller()

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

    def _start_docker_poller(self) -> None:
        t = threading.Thread(target=self._docker_poll_loop, daemon=True)
        t.start()

    def _docker_poll_loop(self) -> None:
        while True:
            try:
                data = self._fetch_docker_data()
                with self._docker_lock:
                    self._docker_cache = data
            except Exception:
                pass
            time.sleep(5)

    @staticmethod
    def _fetch_container_stats(container_id: str) -> dict | None:
        try:
            conn = DockerSocketConnection(DOCKER_SOCKET_PATH, timeout=5.0)
            conn.request("GET", f"/containers/{container_id}/stats?stream=false&one-shot=true")
            resp = conn.getresponse()
            if resp.status != 200:
                conn.close()
                return None
            stats = json.loads(resp.read())
            conn.close()
        except Exception:
            return None

        # Calculate CPU %
        cpu_delta = stats.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0) - \
                    stats.get("precpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0)
        sys_delta = stats.get("cpu_stats", {}).get("system_cpu_usage", 0) - \
                    stats.get("precpu_stats", {}).get("system_cpu_usage", 0)
        n_cpus = stats.get("cpu_stats", {}).get("online_cpus", 1) or 1
        cpu_pct = round((cpu_delta / sys_delta) * n_cpus * 100.0, 1) if sys_delta > 0 else 0.0

        # Memory
        mem_stats = stats.get("memory_stats", {})
        mem_usage = mem_stats.get("usage", 0)
        mem_limit = mem_stats.get("limit", 0)

        def fmt_mem(b: int) -> str:
            if b >= 1073741824:
                return f"{b / 1073741824:.2f}GiB"
            return f"{b / 1048576:.1f}MiB"

        mem_str = f"{fmt_mem(mem_usage)} / {fmt_mem(mem_limit)}" if mem_limit else fmt_mem(mem_usage)

        # Network
        networks = stats.get("networks", {})
        rx = sum(n.get("rx_bytes", 0) for n in networks.values())
        tx = sum(n.get("tx_bytes", 0) for n in networks.values())

        def fmt_net(b: int) -> str:
            if b >= 1073741824:
                return f"{b / 1073741824:.2f}GB"
            if b >= 1048576:
                return f"{b / 1048576:.1f}MB"
            return f"{b / 1024:.1f}kB"

        net_str = f"{fmt_net(rx)} / {fmt_net(tx)}"

        return {
            "cpu_percent": cpu_pct,
            "mem_usage": mem_str,
            "net_io": net_str,
        }

    def _fetch_docker_data(self) -> list[dict]:
        try:
            conn = DockerSocketConnection(DOCKER_SOCKET_PATH)
            conn.request("GET", "/containers/json?all=true")
            resp = conn.getresponse()
            if resp.status != 200:
                conn.close()
                return []
            containers = json.loads(resp.read())
            conn.close()
        except Exception:
            return []

        results: list[dict] = []
        for c in containers:
            names = c.get("Names", [])
            name = names[0].lstrip("/") if names else c.get("Id", "")[:12]
            state = c.get("State", "unknown")
            status_text = c.get("Status", "")
            cid = c.get("Id", "")
            entry: dict = {
                "name": name,
                "state": state,
                "status": status_text,
                "image": c.get("Image", ""),
            }
            if state == "running" and cid:
                stats = self._fetch_container_stats(cid)
                if stats:
                    entry.update(stats)
            results.append(entry)

        return results

    def docker_containers(self) -> list[dict]:
        with self._docker_lock:
            return list(self._docker_cache)

    def network_traffic(self) -> list[dict]:
        try:
            content = PROC_NET_DEV_PATH.read_text(encoding="utf-8")
        except OSError:
            return []

        now = time.monotonic()
        results: list[dict] = []
        skip_prefixes = ("lo", "dummy", "docker", "br-", "veth")

        for line in content.splitlines()[2:]:  # skip header lines
            line = line.strip()
            if ":" not in line:
                continue
            iface, data = line.split(":", 1)
            iface = iface.strip()
            if iface.startswith(skip_prefixes):
                continue

            fields = data.split()
            if len(fields) < 10:
                continue

            rx_bytes = int(fields[0])
            tx_bytes = int(fields[8])

            rx_rate_kbps = 0.0
            tx_rate_kbps = 0.0
            prev = self._prev_net.get(iface)
            if prev is not None:
                dt = now - prev[0]
                if dt > 0:
                    rx_rate_kbps = round(((rx_bytes - prev[1]) / dt) / 1024.0, 1)
                    tx_rate_kbps = round(((tx_bytes - prev[2]) / dt) / 1024.0, 1)

            self._prev_net[iface] = (now, rx_bytes, tx_bytes)

            def fmt_bytes(b: int) -> str:
                if b >= 1073741824:
                    return f"{b / 1073741824:.2f} GB"
                return f"{b / 1048576:.1f} MB"

            results.append({
                "interface": iface,
                "rx_bytes": rx_bytes,
                "tx_bytes": tx_bytes,
                "rx_total": fmt_bytes(rx_bytes),
                "tx_total": fmt_bytes(tx_bytes),
                "rx_rate_kbps": max(rx_rate_kbps, 0.0),
                "tx_rate_kbps": max(tx_rate_kbps, 0.0),
            })

        return results

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
            "docker": self.docker_containers(),
            "network": self.network_traffic(),
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
