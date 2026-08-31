#!/usr/bin/env python3

"""Wait for a remote UDT benchmark campaign using one five-minute poll loop."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time


REMOTE_PROBE = r"""
import json
from pathlib import Path
import subprocess
import sys

status_path = Path(sys.argv[1])
container_id, ctr, address, namespace = sys.argv[2:]
snapshot = {
    "status_file": str(status_path),
    "container_id": container_id,
    "state": "starting",
    "terminal": False,
}
try:
    if status_path.is_file():
        status = json.loads(status_path.read_text(encoding="utf-8"))
        snapshot["status"] = status
        snapshot["state"] = str(status.get("state", status.get("status", "unknown"))).lower()
except Exception as error:
    snapshot["status_error"] = repr(error)

terminal_states = {"completed", "finished", "failed", "invalid", "cancelled", "canceled"}
if snapshot["state"] in terminal_states:
    snapshot["terminal"] = True
else:
    command = [
        "sudo", "-n", ctr, "--address", address, "--namespace", namespace,
        "tasks", "list",
    ]
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    snapshot["task_probe_returncode"] = result.returncode
    snapshot["task_probe_stderr"] = result.stderr.strip()
    task_status = "missing"
    for line in result.stdout.splitlines()[1:]:
        fields = line.split()
        if fields and fields[0] == container_id:
            task_status = fields[-1].lower()
            break
    snapshot["task_status"] = task_status
    if result.returncode == 0 and task_status != "running":
        snapshot["state"] = "failed"
        snapshot["terminal"] = True
        snapshot["reason"] = "container task stopped without a terminal campaign status"

print(json.dumps(snapshot, sort_keys=True))
"""


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument(
        "--ctr",
        default="/home/iakhmura/clickhouse-container-runtime/extracted/usr/bin/ctr",
    )
    parser.add_argument(
        "--containerd-address",
        default="/home/iakhmura/clickhouse-container-runtime/containerd.sock",
    )
    parser.add_argument("--namespace", default="moby")
    args = parser.parse_args()
    if not args.status_file.is_absolute():
        parser.error("--status-file must be absolute")
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    return args


def probe(args: argparse.Namespace) -> dict[str, object]:
    values = (
        str(args.status_file),
        args.container_id,
        args.ctr,
        args.containerd_address,
        args.namespace,
    )
    remote_command = "python3 -c " + shlex.quote(REMOTE_PROBE)
    remote_command += " " + " ".join(shlex.quote(value) for value in values)
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", args.host, remote_command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        return {
            "state": "poll_error",
            "terminal": False,
            "returncode": result.returncode,
            "stderr": result.stderr.strip(),
        }
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        return {
            "state": "poll_error",
            "terminal": False,
            "error": str(error),
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }


def progress(snapshot: dict[str, object]) -> str:
    status = snapshot.get("status")
    fields = [f"state={snapshot.get('state', 'unknown')}"]
    if isinstance(status, dict):
        for key in ("current_phase", "phase", "current", "completed", "total", "failed"):
            value = status.get(key)
            if value is not None:
                fields.append(f"{key}={value}")
    task_status = snapshot.get("task_status")
    if task_status is not None:
        fields.append(f"task={task_status}")
    reason = snapshot.get("reason") or snapshot.get("stderr") or snapshot.get("status_error")
    if reason:
        fields.append(f"reason={reason}")
    return " ".join(fields)


def main() -> int:
    args = parse_args()
    while True:
        snapshot = probe(args)
        print(f"[{now()}] {progress(snapshot)}", flush=True)
        if snapshot.get("terminal"):
            print(json.dumps(snapshot, indent=2, sort_keys=True), flush=True)
            return 0 if snapshot.get("state") in {"completed", "finished"} else 1
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    sys.exit(main())
