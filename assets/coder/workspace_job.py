#!/usr/bin/env python3
"""Run a command in a bounded systemd job and persist its outcome."""
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time

action, job, *command = sys.argv[1:]
result = Path.home() / ".local/state/coder-jobs" / job
result.mkdir(parents=True, exist_ok=True)
deadline_file = result / "deadline"
if action == "extend":
    temp = result / "deadline.tmp"
    temp.write_text(str(time.time() + int(command[0])))
    os.replace(temp, deadline_file)
    sys.exit(0)
if action == "finalize":
    record_path = result / "result.json"
    record = json.loads(record_path.read_text()) if record_path.exists() else {"job": job}
    record.update(service_result=os.environ.get("SERVICE_RESULT"),
                  service_exit_code=os.environ.get("EXIT_CODE"),
                  service_exit_status=os.environ.get("EXIT_STATUS"))
    record.setdefault("finished", time.time())
    record_path.write_text(json.dumps(record))
    subprocess.run(["sudo", "/usr/local/bin/workspace-lease", "complete"], check=True)
    sys.exit(0)
seconds, *command = command
deadline_file.write_text(str(time.time() + int(seconds)))
record = {"job": job, "command": command, "cwd": os.getcwd(), "started": time.time()}
(result / "result.json").write_text(json.dumps(record))
status = 1
try:
    with (result / "output.log").open("wb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        while process.poll() is None:
            if time.time() >= float(deadline_file.read_text()):
                record["timed_out"] = True
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                break
            time.sleep(0.25)
        status = 124 if record.get("timed_out") else process.returncode
    record.update(exit_status=status, finished=time.time())
    (result / "result.json").write_text(json.dumps(record))
except OSError as error:
    record.update(error=str(error), exit_status=1, finished=time.time())
    (result / "result.json").write_text(json.dumps(record))
sys.exit(status)
