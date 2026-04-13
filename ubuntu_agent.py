#!/usr/bin/env python3
"""Ubuntu/container agent for the Windows Control Center.

Designed for container-like environments where the agent must create an
outbound TCP connection back to the controller. No systemd is required.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_CONFIG = {
    "controller_host": "YOUR_WINDOWS_PUBLIC_IP_OR_DDNS",
    "controller_port": 5050,
    "auth_token": "CHANGE_ME_TO_A_LONG_RANDOM_TOKEN",
    "server_name": "gameserver-01",
    "reconnect_seconds": 5,
    "working_directory": ".",
    "shell_executable": "/bin/bash",
}


class UbuntuAgent:
    def __init__(self) -> None:
        self.config = self.load_config()
        self.sock: Optional[socket.socket] = None
        self.sock_file = None
        self.send_lock = threading.Lock()
        self.process_lock = threading.Lock()
        self.current_process: Optional[subprocess.Popen] = None
        self.running = True
        self._bytes_sent_total = 0
        self._bytes_sent_lock = threading.Lock()

    def load_config(self) -> dict:
        if not CONFIG_PATH.exists():
            CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for key, value in DEFAULT_CONFIG.items():
            data.setdefault(key, value)
        return data

    def send_json(self, payload: dict) -> None:
        if not self.sock:
            raise ConnectionError("Socket is not connected.")
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with self.send_lock:
            self.sock.sendall(raw)
        with self._bytes_sent_lock:
            self._bytes_sent_total += len(raw)

    def send_json_best_effort(self, payload: dict) -> bool:
        try:
            self.send_json(payload)
            return True
        except Exception:
            return False

    def get_running_command_snapshot(self) -> tuple[bool, str]:
        with self.process_lock:
            process = self.current_process
            if not process:
                return False, ""
            if process.poll() is not None:
                self.current_process = None
                return False, ""
            return True, getattr(process, "command_text", "")

    def send_state_snapshot(self) -> None:
        busy, command = self.get_running_command_snapshot()
        self.send_json_best_effort(
            {
                "type": "status",
                "event": "state_sync",
                "busy": busy,
                "command": command,
            }
        )

    def connect_loop(self) -> None:
        reconnect_seconds = int(self.config.get("reconnect_seconds", 5))
        while self.running:
            try:
                self.connect_once()
            except Exception as exc:
                print(f"[agent] disconnected: {exc}", flush=True)
            self.cleanup_connection()
            time.sleep(reconnect_seconds)

    def connect_once(self) -> None:
        host = self.config["controller_host"]
        port = int(self.config["controller_port"])
        print(f"[agent] connecting to {host}:{port}", flush=True)
        self.sock = socket.create_connection((host, port), timeout=15)
        self.sock.settimeout(None)
        self.sock_file = self.sock.makefile("r", encoding="utf-8")
        busy, current_command = self.get_running_command_snapshot()

        hello = {
            "type": "hello",
            "token": self.config["auth_token"],
            "server_name": self.config.get("server_name") or socket.gethostname(),
            "hostname": socket.gethostname(),
            "local_ip": self.get_local_ip(),
            "platform": "Ubuntu container",
            "version": "2.0-container",
            "busy": busy,
            "current_command": current_command,
        }
        self.send_json(hello)

        heartbeat_thread = threading.Thread(target=self.heartbeat_loop, daemon=True)
        heartbeat_thread.start()
        metrics_thread = threading.Thread(target=self.metrics_loop, daemon=True)
        metrics_thread.start()

        while self.running:
            line = self.sock_file.readline()
            if not line:
                raise ConnectionError("Controller socket closed.")
            payload = json.loads(line)
            self.process_message(payload)

    def heartbeat_loop(self) -> None:
        while self.running and self.sock:
            try:
                self.send_json({"type": "heartbeat", "ts": time.time()})
                time.sleep(10)
            except Exception:
                break

    def metrics_loop(self) -> None:
        cpu_sampler = CpuSampler()
        network_sampler = NetworkSampler()
        last_bytes_sent = 0
        last_ts = time.time()
        while self.running and self.sock:
            try:
                time.sleep(1.0)
                now = time.time()
                upload_mbps = network_sampler.sample_upload_mbps(now)
                if upload_mbps is None:
                    interval = max(0.2, now - last_ts)
                    with self._bytes_sent_lock:
                        total = self._bytes_sent_total
                    delta_bytes = max(0, total - last_bytes_sent)
                    upload_mbps = (delta_bytes * 8.0 / 1_000_000.0) / interval
                    last_bytes_sent = total
                    last_ts = now
                else:
                    with self._bytes_sent_lock:
                        last_bytes_sent = self._bytes_sent_total
                    last_ts = now
                cpu_percent = cpu_sampler.sample_percent()
                self.send_json(
                    {
                        "type": "metrics",
                        "ts": now,
                        "upload_mbps": round(upload_mbps, 3),
                        "cpu_percent": round(cpu_percent, 1),
                    }
                )
            except Exception:
                break

    def process_message(self, payload: dict) -> None:
        msg_type = payload.get("type")
        if msg_type == "ack":
            print("[agent] authenticated and connected", flush=True)
            self.send_state_snapshot()
            return
        if msg_type == "run":
            command = str(payload.get("command", "")).strip()
            if command:
                threading.Thread(target=self.run_command, args=(command,), daemon=True).start()
            return
        if msg_type == "interrupt":
            self.interrupt_command()
            return
        if msg_type == "error":
            raise RuntimeError(payload.get("message", "Controller rejected the connection."))

    def run_command(self, command: str) -> None:
        with self.process_lock:
            if self.current_process and self.current_process.poll() is None:
                self.send_json_best_effort(
                    {
                        "type": "status",
                        "event": "busy",
                        "command": getattr(self.current_process, "command_text", "<unknown>"),
                    }
                )
                return

            process = subprocess.Popen(
                command,
                shell=True,
                executable=self.config.get("shell_executable", "/bin/bash"),
                cwd=self.config.get("working_directory") or None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                text=True,
                bufsize=1,
                preexec_fn=os.setsid,
            )
            process.command_text = command  # type: ignore[attr-defined]
            self.current_process = process

        self.send_json_best_effort({"type": "status", "event": "command_started", "command": command})

        try:
            assert process.stdout is not None
            for line in iter(process.stdout.readline, ""):
                clean_line = line.rstrip("\n")
                self.send_json_best_effort({"type": "log", "line": clean_line})
            process.stdout.close()
            exit_code = process.wait()
            self.send_json_best_effort({"type": "status", "event": "command_finished", "exit_code": exit_code})
        except Exception as exc:
            self.send_json_best_effort({"type": "status", "event": "error", "message": str(exc)})
        finally:
            with self.process_lock:
                if self.current_process is process:
                    self.current_process = None

    def interrupt_command(self) -> None:
        with self.process_lock:
            process = self.current_process
            if not process or process.poll() is not None:
                self.current_process = None
                self.send_json_best_effort({"type": "status", "event": "interrupt_not_needed"})
                return
            os.killpg(os.getpgid(process.pid), signal.SIGINT)
            self.send_json_best_effort({"type": "status", "event": "interrupt_sent"})

        end_time = time.time() + 5
        while time.time() < end_time:
            if process.poll() is not None:
                return
            time.sleep(0.2)

        with self.process_lock:
            if self.current_process and self.current_process.poll() is None:
                os.killpg(os.getpgid(self.current_process.pid), signal.SIGTERM)
                self.send_json_best_effort({"type": "status", "event": "forced_terminate"})

    def cleanup_connection(self) -> None:
        try:
            if self.sock_file:
                self.sock_file.close()
        except OSError:
            pass
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        self.sock = None
        self.sock_file = None

    @staticmethod
    def get_local_ip() -> str:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        except OSError:
            return "unknown"
        finally:
            sock.close()


class CpuSampler:
    def __init__(self) -> None:
        self._prev_total = None
        self._prev_idle = None

    @staticmethod
    def _read_proc_stat() -> Optional[tuple[int, int]]:
        try:
            with open("/proc/stat", "r", encoding="utf-8") as handle:
                first = handle.readline()
        except OSError:
            return None

        parts = first.split()
        if len(parts) < 5 or parts[0] != "cpu":
            return None

        values = []
        for item in parts[1:]:
            try:
                values.append(int(item))
            except ValueError:
                values.append(0)

        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        return total, idle

    def sample_percent(self) -> float:
        reading = self._read_proc_stat()
        if not reading:
            return 0.0

        total, idle = reading
        if self._prev_total is None or self._prev_idle is None:
            self._prev_total = total
            self._prev_idle = idle
            return 0.0

        total_delta = total - self._prev_total
        idle_delta = idle - self._prev_idle
        self._prev_total = total
        self._prev_idle = idle
        if total_delta <= 0:
            return 0.0
        usage = (total_delta - idle_delta) / total_delta * 100.0
        if usage < 0:
            return 0.0
        if usage > 100:
            return 100.0
        return usage


class NetworkSampler:
    def __init__(self) -> None:
        self._prev_tx_bytes: Optional[int] = None
        self._prev_ts: Optional[float] = None

    @staticmethod
    def _read_total_tx_bytes() -> Optional[int]:
        try:
            with open("/proc/net/dev", "r", encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            return None

        total_tx_bytes = 0
        found_interface = False
        for raw_line in lines[2:]:
            if ":" not in raw_line:
                continue
            iface_name, metrics = raw_line.split(":", 1)
            iface_name = iface_name.strip()
            if iface_name == "lo":
                continue
            fields = metrics.split()
            if len(fields) < 16:
                continue
            try:
                total_tx_bytes += int(fields[8])
            except ValueError:
                continue
            found_interface = True

        if not found_interface:
            return None
        return total_tx_bytes

    def sample_upload_mbps(self, now: Optional[float] = None) -> Optional[float]:
        total_tx_bytes = self._read_total_tx_bytes()
        if total_tx_bytes is None:
            return None

        timestamp = now if now is not None else time.time()
        if self._prev_tx_bytes is None or self._prev_ts is None:
            self._prev_tx_bytes = total_tx_bytes
            self._prev_ts = timestamp
            return 0.0

        interval = max(0.2, timestamp - self._prev_ts)
        delta_bytes = max(0, total_tx_bytes - self._prev_tx_bytes)
        self._prev_tx_bytes = total_tx_bytes
        self._prev_ts = timestamp
        return (delta_bytes * 8.0 / 1_000_000.0) / interval



def main() -> None:
    agent = UbuntuAgent()
    agent.connect_loop()


if __name__ == "__main__":
    main()
