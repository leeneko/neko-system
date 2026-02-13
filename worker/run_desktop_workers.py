"""
Run crawler worker + ollama translator worker together.

Usage:
  python run_desktop_workers.py
"""

import os
import signal
import subprocess
import sys
import time
from typing import List, Tuple


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON_BIN = sys.executable
RESTART_DELAY_SEC = 3


def build_commands() -> List[Tuple[str, List[str]]]:
    return [
        ("crawler", [PYTHON_BIN, os.path.join(BASE_DIR, "combined_worker.py")]),
        ("translator", [PYTHON_BIN, os.path.join(BASE_DIR, "translate_worker_ollama.py")]),
    ]


def terminate_processes(processes):
    for _, proc in processes:
        if proc.poll() is None:
            proc.terminate()
    for _, proc in processes:
        try:
            proc.wait(timeout=10)
        except Exception:
            if proc.poll() is None:
                proc.kill()


def main() -> None:
    print("[runner] starting desktop workers")
    commands = build_commands()
    processes = []

    for name, cmd in commands:
        print(f"[runner] start {name}: {' '.join(cmd)}")
        proc = subprocess.Popen(cmd, cwd=BASE_DIR)
        processes.append((name, proc))

    def _handle_signal(signum, _frame):
        print(f"[runner] signal received ({signum}), shutting down")
        terminate_processes(processes)
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while True:
        for idx, (name, proc) in enumerate(processes):
            code = proc.poll()
            if code is None:
                continue
            print(f"[runner] {name} exited with code {code}, restarting in {RESTART_DELAY_SEC}s")
            time.sleep(RESTART_DELAY_SEC)
            new_proc = subprocess.Popen(commands[idx][1], cwd=BASE_DIR)
            processes[idx] = (name, new_proc)
        time.sleep(1)


if __name__ == "__main__":
    main()
