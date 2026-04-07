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

        hello = {
            "type": "hello",
            "token": self.config["auth_token"],
            "server_name": self.config.get("server_name") or socket.gethostname(),
            "hostname": socket.gethostname(),
            "local_ip": self.get_local_ip(),
            "platform": "Ubuntu container",
            "version": "2.0-container",
        }
        self.send_json(hello)

        heartbeat_thread = threading.Thread(target=self.heartbeat_loop, daemon=True)
        heartbeat_thread.start()

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

    def process_message(self, payload: dict) -> None:
        msg_type = payload.get("type")
        if msg_type == "ack":
            print("[agent] authenticated and connected", flush=True)
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
                self.send_json(
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

        self.send_json({"type": "status", "event": "command_started", "command": command})

        try:
            assert process.stdout is not None
            for line in iter(process.stdout.readline, ""):
                clean_line = line.rstrip("\n")
                self.send_json({"type": "log", "line": clean_line})
            process.stdout.close()
            exit_code = process.wait()
            self.send_json({"type": "status", "event": "command_finished", "exit_code": exit_code})
        except Exception as exc:
            self.send_json({"type": "status", "event": "error", "message": str(exc)})
        finally:
            with self.process_lock:
                self.current_process = None

    def interrupt_command(self) -> None:
        with self.process_lock:
            process = self.current_process
            if not process or process.poll() is not None:
                self.send_json({"type": "status", "event": "interrupt_not_needed"})
                return
            os.killpg(os.getpgid(process.pid), signal.SIGINT)
            self.send_json({"type": "status", "event": "interrupt_sent"})

        end_time = time.time() + 5
        while time.time() < end_time:
            if process.poll() is not None:
                return
            time.sleep(0.2)

        with self.process_lock:
            if self.current_process and self.current_process.poll() is None:
                os.killpg(os.getpgid(self.current_process.pid), signal.SIGTERM)
                self.send_json({"type": "status", "event": "forced_terminate"})

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



def main() -> None:
    agent = UbuntuAgent()
    agent.connect_loop()


if __name__ == "__main__":
    main()
