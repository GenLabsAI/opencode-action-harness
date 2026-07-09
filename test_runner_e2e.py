import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
import pytest
import threading

import runner

# Run only if LIVE_OPENCODE_E2E is set
pytestmark = pytest.mark.skipif(
    not os.environ.get("LIVE_OPENCODE_E2E"),
    reason="Needs LIVE_OPENCODE_E2E=1"
)


def test_runner_e2e_live_opencode():
    # We test that we can start `opencode serve`, verify health, create a session, 
    # prompt it, and read its status back.
    # This simulates the runner's lifecycle logic.
    
    started_at = time.time()
    process = subprocess.Popen(
        ["opencode", "serve", "--port", "4096"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
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

        assert ready, "opencode serve failed to start"

        # Create session
        session_data = runner.post_json("http://localhost:4096/session", None, {})
        session_id = session_data["id"]
        assert session_id.startswith("ses_")

        # Prompt agent async
        runner.post_json(f"http://localhost:4096/session/{session_id}/prompt_async", None, {
            "parts": [{"type": "text", "text": "echo hello world"}]
        })

        # Wait for completion or timeout
        status = "pending"
        for _ in range(30):
            req = urllib.request.Request(f"http://localhost:4096/session/{session_id}")
            with urllib.request.urlopen(req, timeout=5) as resp:
                state = json.loads(resp.read().decode("utf-8"))
                status = state.get("status")
                if status in ["completed", "failed", "aborted", "error", "resolved", "rejected"]:
                    break
            time.sleep(2)
            
        assert status in ["completed", "resolved"], f"Expected completed/resolved, got {status}"

    finally:
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            pass
