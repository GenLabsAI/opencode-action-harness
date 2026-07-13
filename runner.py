from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any
import threading


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


def emit(api_base_url: str, job_id: str, token: str, event_type: str, content: str = "", payload: dict[str, Any] | None = None) -> None:
    event = {
        "event_type": event_type,
        "content": content,
        "payload": payload or {},
        "created_at": now_iso(),
    }
    print(json.dumps(event, ensure_ascii=False), flush=True)
    try:
        post_json(f"{api_base_url.rstrip('/')}/deca-agents/v1/jobs/{job_id}/events", token, event)
    except Exception:
        pass


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


def ack_command(api_base_url: str, job_id: str, token: str, command_id: Any, status: str) -> None:
    try:
        post_json(f"{api_base_url.rstrip('/')}/deca-agents/v1/jobs/{job_id}/commands/{command_id}", token, {"status": status}, method="PATCH")
    except Exception:
        pass


def command_poller(api_base_url: str, job_id: str, token: str, session_id: str) -> None:
    url = f"{api_base_url.rstrip('/')}/deca-agents/v1/jobs/{job_id}/commands/stream"
    request = urllib.request.Request(
        url,
        headers={
            "X-Worker-Token": token,
            "User-Agent": "deca-opencode-action-harness",
            "Accept": "text/event-stream",
        },
    )
    try:
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
                        if data.get("command_type") == "prompt":
                            msg = data.get("payload", {}).get("message", "")
                            if msg:
                                print(f"Steering agent: {msg}", file=sys.stderr)
                                post_json(f"http://localhost:4096/session/{session_id}/prompt_async", None, {"parts": [{"type": "text", "text": msg}]})
                                ack_command(api_base_url, job_id, token, command_id, "acked")
                        elif data.get("command_type") == "abort":
                            print("Aborting agent task", file=sys.stderr)
                            post_json(f"http://localhost:4096/session/{session_id}/abort", None, {})
                            ack_command(api_base_url, job_id, token, command_id, "acked")
                    except Exception as e:
                        if "data" in locals():
                            ack_command(api_base_url, job_id, token, data.get("id"), "failed")
                        print(f"Error parsing command: {e}", file=sys.stderr)
    except Exception as e:
        print(f"Command poller error: {e}", file=sys.stderr)


def event_streamer(api_base_url: str, job_id: str, token: str, completion_event: threading.Event) -> None:
    url = "http://localhost:4096/global/event"
    request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    try:
        with urllib.request.urlopen(request) as response:
            for line in response:
                line = line.decode("utf-8").strip()
                if line.startswith("data: "):
                    data_str = line[len("data: "):]
                    if not data_str:
                        continue
                    try:
                        data = json.loads(data_str)
                        # Pass events back to deca API
                        event_type = "unknown"
                        if "payload" in data and isinstance(data["payload"], dict):
                            event_type = data["payload"].get("type", "unknown")
                        elif "type" in data:
                            event_type = data["type"]
                        
                        if event_type in ("server.heartbeat", "plugin.added"):
                            continue
                            
                        emit(api_base_url, job_id, token, event_type, json.dumps(data), data)
                        
                        # Detect completion
                        if event_type in ("session.idle", "session.completed", "session.aborted", "session.failed", "session.error"):
                            completion_event.set()
                        elif event_type in ("message.updated", "message.part.updated"):
                            # For some clients, message part update marks the end
                            payload = data.get("payload", {})
                            if payload.get("finish") == "stop" or payload.get("part", {}).get("finish") == "stop":
                                completion_event.set()
                    except Exception as e:
                        pass
    except Exception as e:
        print(f"Event streamer error: {e}", file=sys.stderr)


def main() -> int:
    job_id = env_required("DECA_AGENT_JOB_ID")
    task = env_required("DECA_AGENT_TASK")
    api_base_url = env_required("DECA_AGENT_API_BASE_URL")
    token = env_required("DECA_AGENT_WORKER_TOKEN")
    env_required("DECA_API_KEY")
    model = os.environ.get("DECA_AGENT_MODEL", "deca/deca-2.5-ultra").strip()
    model_aliases = {
        "mini": "deca/deca-2.5-mini",
        "pro": "deca/deca-2.5-pro",
        "ultra": "deca/deca-2.5-ultra",
        "deca-mini": "deca/deca-2.5-mini",
        "deca-pro": "deca/deca-2.5-pro",
        "deca-ultra": "deca/deca-2.5-ultra",
        "deca-2.5-mini": "deca/deca-2.5-mini",
        "deca-2.5-pro": "deca/deca-2.5-pro",
        "deca-2.5-ultra": "deca/deca-2.5-ultra",
    }
    model = model_aliases.get(model, model)
    if "/" not in model:
        model = f"deca/{model}"

    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": model,
        "provider": {
            "deca": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Deca",
                "options": {
                    "baseURL": f"{api_base_url.rstrip('/')}/deca/v1",
                    "apiKey": "{env:DECA_API_KEY}"
                },
                "models": {
                    "deca-2.5-mini": {}, "deca-2.5-pro": {}, "deca-2.5-ultra": {}
                }
            }
        }
    }
    os.environ["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)

    started_at = time.time()
    emit(api_base_url, job_id, token, "status", "starting serve", {})
    patch_status(api_base_url, job_id, token, "running")

    process = None
    try:
        # Start opencode serve
        process = subprocess.Popen(
            ["opencode", "serve", "--port", "4096"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Poll health
        ready = False
        for _ in range(30):
            try:
                req = urllib.request.Request("http://localhost:4096/api/health")
                with urllib.request.urlopen(req, timeout=1) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                pass
            time.sleep(1)

        if not ready:
            raise RuntimeError("opencode serve failed to start on port 4096")

        # Find or create session
        sessions = []
        try:
            req = urllib.request.Request("http://localhost:4096/session")
            with urllib.request.urlopen(req) as resp:
                sessions = json.loads(resp.read().decode("utf-8"))
        except Exception:
            pass
        
        if sessions and len(sessions) > 0:
            session_id = sessions[0]["id"]
            print(f"Reusing existing session {session_id}", file=sys.stderr)
        else:
            session_data = post_json("http://localhost:4096/session", None, {})
            session_id = session_data["id"]

        completion_event = threading.Event()
        # Start event streamer
        threading.Thread(target=event_streamer, args=(api_base_url, job_id, token, completion_event), daemon=True).start()

        # Start command poller
        threading.Thread(target=command_poller, args=(api_base_url, job_id, token, session_id), daemon=True).start()

        # Prompt agent async
        prompt_response = post_json(f"http://localhost:4096/session/{session_id}/prompt_async", None, {
            "parts": [{"type": "text", "text": task}],
        })
        emit(api_base_url, job_id, token, "debug", "prompt_async accepted", prompt_response)

        # Wait for the task to finish by checking session status
        status = "running"
        idle_seconds = 0
        last_state = ""
        while time.time() - started_at < 600:
            if completion_event.wait(timeout=2):
                status = "completed"
                break
                
            try:
                req = urllib.request.Request(f"http://localhost:4096/session/{session_id}")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    state = json.loads(resp.read().decode("utf-8"))
                    state_text = json.dumps(state, sort_keys=True)
                    if state_text != last_state:
                        emit(api_base_url, job_id, token, "debug", "session state changed", state)
                        last_state = state_text
                        idle_seconds = 0
                    else:
                        idle_seconds += 2
                    status = state.get("status") or state.get("state") or status
                    if status in ["completed", "failed", "aborted", "error", "resolved", "rejected"]:
                        break
                    if idle_seconds >= 120:
                        status = "idle_timeout"
                        break
            except Exception as e:
                print(f"Error checking status: {e}", file=sys.stderr)
                status = "status_check_error"
                break
        else:
            status = "timeout"

        elapsed = time.time() - started_at
        result = {"elapsed_seconds": elapsed, "final_status": status}
        
        if status in ["completed", "resolved"]:
            emit(api_base_url, job_id, token, "result", "completed", result)
            patch_status(api_base_url, job_id, token, "completed", result=result)
            return 0
        else:
            emit(api_base_url, job_id, token, "error", f"Task ended with status {status}", result)
            patch_status(api_base_url, job_id, token, "failed", result=result, error=f"Task ended with status {status}")
            return 1

    except Exception as exc:
        error = "".join(traceback.format_exception(exc))
        emit(api_base_url, job_id, token, "error", str(exc), {"traceback": error})
        patch_status(api_base_url, job_id, token, "failed", error=str(exc))
        return 1
    finally:
        try:
            if process:
                process.terminate()
            process.wait(timeout=5)
        except Exception:
            pass

if __name__ == "__main__":
    raise SystemExit(main())
