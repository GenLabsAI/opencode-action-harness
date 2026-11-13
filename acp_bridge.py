"""Reference ACP agent bridge: the GitHub-Actions side of a cloud ACP session.

Mirrors what a harness `cloud_runner` would do on the runner: connect to
the relay's runner WS URL, read the session config frame Star sends over
the relay, speak ACP as the agent, call the configured model for
inference, and stream the reply back as ACP session/update frames.

This is the piece genlabsapi's relay would pair with once it accepts
model config; here it runs against mock_terminal_relay.py.

Run: python3 acp_bridge.py <runner-ws-url>
Env: none required (model key arrives in the config frame). Logs redact
     any api_key value before printing.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import websockets

# Seconds between keepalive frames while inference runs. The relay path
# drops an idle direction quickly, so keep traffic flowing faster than
# that. Overridable per run without editing the file.
KEEPALIVE_S = float(os.environ.get("ACP_KEEPALIVE_S", "0.5"))

try:
    import requests
except ImportError:
    requests = None


def redact(obj, secret: str | None):
    if not secret:
        return obj
    text = json.dumps(obj)
    return json.loads(text.replace(secret, "***REDACTED***"))


def call_model_sync(model: str, base_url: str, api_key: str, prompt: str) -> str:
    """Blocking OpenAI-compatible chat call. Runs in a worker thread so
    the relay loop can keep sending keepalives while inference runs."""
    if not (requests and model and base_url and api_key):
        return "CLOUD-ACP-OK (canned: no model configured)"
    try:
        resp = requests.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 256,
                "temperature": 0,
            },
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        text = (data["choices"][0]["message"].get("content") or "").strip()
        if text:
            return text
        return "CLOUD-ACP-OK (empty model reply)"
    except Exception as e:
        print(f"bridge: model call failed: {type(e).__name__}; using canned reply", flush=True)
        return "CLOUD-ACP-OK (fallback: model unreachable)"


async def call_model(model: str, base_url: str, api_key: str, prompt: str,
                     keepalive=None) -> str:
    """Call the model in a thread, invoking keepalive() every few seconds.

    The relay path closes an idle browser connection within seconds, so
    inference (which takes longer) must be accompanied by traffic.
    """
    import asyncio as _asyncio

    task = _asyncio.create_task(
        _asyncio.to_thread(call_model_sync, model, base_url, api_key, prompt))
    while not task.done():
        try:
            return await _asyncio.wait_for(_asyncio.shield(task), timeout=KEEPALIVE_S)
        except _asyncio.TimeoutError:
            if keepalive is not None:
                await keepalive()
    return await task


MSG_DATA = 0x00
MSG_EXIT = 0x02


def to_json(raw) -> dict:
    """Parse a relay frame: raw JSON text, or PTY-framed bytes."""
    data = raw.encode() if isinstance(raw, str) else bytes(raw)
    if data and data[0] <= 0x03:
        data = data[1:]
    return json.loads(data.decode())


async def main(url: str):
    async with websockets.connect(url, max_size=4 * 1024 * 1024) as ws:
        print("bridge: connected to relay, waiting for config", flush=True)
        config = None
        while config is None:
            raw = await asyncio.wait_for(ws.recv(), timeout=120)
            try:
                candidate = to_json(raw)
            except Exception:
                continue  # ping/heartbeat frame: ignore
            if isinstance(candidate, dict) and candidate.get("type") == "acp_init":
                config = candidate
        secret = config.get("api_key")
        print(f"bridge: config: {json.dumps(redact(config, secret))}", flush=True)
        prompt = config.get("prompt", "")
        model = config.get("model", "")
        base_url = config.get("base_url", "")

        agent_session = "sess-cloud-1"

        async for raw in ws:
            try:
                msg = json.loads(raw if isinstance(raw, str) else raw.decode())
            except Exception:
                continue  # ping/heartbeat frame: ignore
            if "method" not in msg:
                continue
            method, mid = msg["method"], msg.get("id")
            print(f"bridge: <- {method} id={mid}", flush=True)

            async def send(payload, quiet=False):
                # PTY-style framing (MSG_DATA prefix) like terminal_runner,
                # so any relay viewer handles the bytes uniformly.
                await ws.send(bytes([MSG_DATA]) + json.dumps(payload).encode())
                if not quiet:
                    logged = redact(payload, secret)
                    print(f"bridge: -> {json.dumps(logged)[:300]}", flush=True)

            if method == "initialize":
                await send({"jsonrpc": "2.0", "id": mid,
                            "result": {"protocolVersion": 1,
                                       "agentCapabilities": {},
                                       "agentInfo": {"name": "cloud-acp-bridge", "version": "0.1.0"},
                                       "authMethods": []}})
            elif method == "session/new":
                await send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": agent_session}})
            elif method == "session/prompt":
                async def _keepalive():
                    await send({"jsonrpc": "2.0", "method": "session/update",
                                "params": {"sessionId": agent_session,
                                          "update": {"sessionUpdate": "agent_message_chunk",
                                                    "content": {"type": "text", "text": ""}}}},
                               quiet=True)
                text = await call_model(model, base_url, secret, prompt, keepalive=_keepalive)
                halves = [text[: len(text) // 2], text[len(text) // 2:]] or [text]
                for part in halves:
                    if part:
                        await send({"jsonrpc": "2.0", "method": "session/update",
                                    "params": {"sessionId": agent_session,
                                               "update": {"sessionUpdate": "agent_message_chunk",
                                                          "content": {"type": "text", "text": part}}}})
                await send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
                await ws.send(bytes([MSG_EXIT, 0]))
                # Grace period: the relay tears down the browser side when
                # the runner goes away, so linger to let Star drain every
                # frame before the session ends.
                print("bridge: prompt complete, lingering 20s for drain", flush=True)
                await asyncio.sleep(20)
                print("bridge: exiting")
                return
            else:
                await send({"jsonrpc": "2.0", "id": mid,
                            "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
