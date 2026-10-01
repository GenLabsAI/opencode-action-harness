from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Any

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def env_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value

def post_json(url: str, token: str | None, payload: dict[str, Any], method: str = "POST") -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "deca-opencode-action-harness"}
    if token:
        headers["X-Worker-Token"] = token
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            if body:
                return json.loads(body)
            return {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"API request failed {exc.code}: {body}", file=sys.stderr)
        raise

EVENT_BUFFER: list[dict[str, Any]] = []
EVENT_LOCK = threading.Lock()

def flush_events(api_base_url: str, job_id: str, token: str) -> None:
    with EVENT_LOCK:
        if not EVENT_BUFFER:
            return
        events = EVENT_BUFFER[:200]
        del EVENT_BUFFER[:200]
    try:
        post_json(f"{api_base_url.rstrip('/')}/deca-agents/v1/jobs/{job_id}/events", token, {"events": events})
    except Exception:
        with EVENT_LOCK:
            EVENT_BUFFER[:0] = events

def event_flusher(api_base_url: str, job_id: str, token: str) -> None:
    while True:
        flush_events(api_base_url, job_id, token)
        time.sleep(1)

def patch_status(api_base_url: str, job_id: str, token: str, status: str, result: dict[str, Any] | None = None, error: str | None = None) -> None:
    payload: dict[str, Any] = {"status": status}
    if result is not None:
        payload["result"] = result
    if error is not None:
        payload["error"] = error
    try:
        post_json(f"{api_base_url.rstrip('/')}/deca-agents/v1/jobs/{job_id}", token, payload, method="PATCH")
    except Exception:
        pass

def command_poller(api_base_url: str, job_id: str, token: str, process: subprocess.Popen) -> None:
    url = f"{api_base_url.rstrip('/')}/deca-agents/v1/jobs/{job_id}/commands/stream"
    while True:
        try:
            request = urllib.request.Request(
                url,
                headers={"X-Worker-Token": token, "User-Agent": "deca-opencode-action-harness", "Accept": "text/event-stream"}
            )
            with urllib.request.urlopen(request) as response:
                for line in response:
                    line = line.decode("utf-8").strip()
                    if line.startswith("data: "):
                        data_str = line[len("data: "):]
                        if data_str == "{}":
                            continue
                        try:
                            data = json.loads(data_str)
                            command_id = data.get("id")
                            payload = data.get("payload")
                            if payload and process.stdin:
                                # Write ACP JSON-RPC message verbatim to crush stdin
                                process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
                                process.stdin.flush()
                            post_json(f"{api_base_url.rstrip('/')}/deca-agents/v1/jobs/{job_id}/commands/{command_id}", token, {"status": "acked"}, method="PATCH")
                        except Exception as e:
                            print(f"Error parsing command: {e}", file=sys.stderr)
        except Exception as e:
            print(f"Command poller error: {e}", file=sys.stderr)
            time.sleep(2)

def main() -> int:
    job_id = env_required("DECA_AGENT_JOB_ID")
    task = env_required("DECA_AGENT_TASK")
    api_base_url = env_required("DECA_AGENT_API_BASE_URL")
    token = env_required("DECA_AGENT_WORKER_TOKEN")
    api_key = env_required("DECA_API_KEY")
    model = os.environ.get("DECA_AGENT_MODEL", "deca-2.5-ultra").strip()
    
    # Generate crush config
    crush_config = {
        "providers": {
            "deca": {
                "type": "openai",
                "api_key": api_key,
                "base_url": f"{api_base_url.rstrip('/')}/deca/v1"
            }
        },
        "models": [
            {
                "id": model,
                "provider": "deca"
            }
        ]
    }
    crush_dir = os.path.expanduser("~/.crush")
    os.makedirs(crush_dir, exist_ok=True)
    with open(os.path.join(crush_dir, "crush.json"), "w") as f:
        json.dump(crush_config, f)

    patch_status(api_base_url, job_id, token, "running")

    # Start event flusher
    threading.Thread(target=event_flusher, args=(api_base_url, job_id, token), daemon=True).start()

    process = None
    try:
        print("Starting crush acp...", file=sys.stderr)
        process = subprocess.Popen(
            ["crush", "acp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
        )

        # Start command poller to forward api commands -> crush stdin
        threading.Thread(target=command_poller, args=(api_base_url, job_id, token, process), daemon=True).start()

        # Start by sending the initialization payload if necessary
        # (Depending on ACP version we might need to send initialize request first, but we assume UI handles it)
        
        # Read crush stdout (ACP JSON-RPC messages) and buffer them to flush
        for line in iter(process.stdout.readline, b""):
            line_str = line.decode("utf-8").strip()
            if not line_str:
                continue
            try:
                msg = json.loads(line_str)
                with EVENT_LOCK:
                    EVENT_BUFFER.append(msg)
            except Exception as e:
                print(f"Failed to parse crush output: {e}", file=sys.stderr)
                
        process.wait()
        if process.returncode != 0:
            print(f"Crush exited with code {process.returncode}", file=sys.stderr)
            patch_status(api_base_url, job_id, token, "failed", error=f"Exited {process.returncode}")
        else:
            patch_status(api_base_url, job_id, token, "completed")
            
        # Give flusher time to drain
        time.sleep(2)
        return process.returncode
    except Exception as e:
        traceback = __import__("traceback")
        print(f"Runner exception: {traceback.format_exc()}", file=sys.stderr)
        patch_status(api_base_url, job_id, token, "failed", error=str(e))
        return 1
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

if __name__ == "__main__":
    sys.exit(main())
