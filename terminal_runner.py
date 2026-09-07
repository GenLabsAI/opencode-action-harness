"""Terminal relay runner for opencode-action-harness.

Spawns a PTY with the specified command (crush, opencode, etc.),
connects to the relay via WebSocket, and pumps bytes bidirectionally.

Periodically syncs scrollback to the Deca API for session persistence.
Captures screenshots via the Deca API artifact registration endpoint.
"""

import argparse
import asyncio
import logging
import os
import pty
import signal
import struct
import sys
import fcntl
import termios
from typing import Optional

try:
    import websockets
except ImportError:
    print("Install websockets: pip install websockets", file=sys.stderr)
    sys.exit(1)

try:
    import aiohttp
except ImportError:
    aiohttp = None

logger = logging.getLogger(__name__)

# Protocol constants
MSG_DATA = 0x00
MSG_RESIZE = 0x01
MSG_EXIT = 0x02
MSG_PING = 0x03

SCROLLBACK_SYNC_INTERVAL = 30
MAX_SCROLLBACK_BYTES = 256 * 1024


def _parse_resize(data: bytes) -> Optional[tuple]:
    if len(data) < 5:
        return None
    cols, rows = struct.unpack("!HH", data[1:5])
    return cols, rows


def _read_pty(master_fd: int) -> Optional[bytes]:
    try:
        return os.read(master_fd, 4096)
    except OSError:
        return None


def _set_pty_size(master_fd: int, cols: int, rows: int):
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)


async def run_bridge(
    relay_url: str,
    command: str,
    session_token: str,
    api_base_url: str = "",
    deca_api_key: str = "",
    session_id: str = "",
):
    extra_headers = {"Authorization": f"Bearer {session_token}"}

    async with websockets.connect(
        relay_url,
        additional_headers=extra_headers,
        max_size=4 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=10,
    ) as ws:
        logger.info("terminal_runner: connected to relay")

        pid, master_fd = pty.fork()

        if pid == 0:
            os.environ["TERM"] = os.environ.get("TERM", "xterm-256color")
            os.execlp("/bin/sh", "/bin/sh", "-c", command)

        loop = asyncio.get_event_loop()

        try:
            _set_pty_size(master_fd, 80, 24)
        except Exception:
            pass

        scrollback_buf: list[bytes] = []
        scrollback_lock = asyncio.Lock()

        async def pty_to_ws():
            try:
                while True:
                    data = await loop.run_in_executor(None, _read_pty, master_fd)
                    if data is None:
                        break
                    async with scrollback_lock:
                        scrollback_buf.append(data)
                    await ws.send(bytes([MSG_DATA]) + data)
            except Exception as e:
                logger.debug(f"terminal_runner: pty_to_ws ended: {e}")

        async def ws_to_pty():
            try:
                async for message in ws:
                    if isinstance(message, bytes) and len(message) > 0:
                        msg_type = message[0]
                        payload = message[1:]
                        if msg_type == MSG_DATA:
                            os.write(master_fd, payload)
                        elif msg_type == MSG_RESIZE:
                            size = _parse_resize(message)
                            if size:
                                _set_pty_size(master_fd, size[0], size[1])
                        elif msg_type == MSG_PING:
                            await ws.send(bytes([MSG_PING]))
                    elif isinstance(message, str):
                        os.write(master_fd, message.encode("utf-8"))
            except Exception as e:
                logger.debug(f"terminal_runner: ws_to_pty ended: {e}")

        async def wait_child():
            _, status = await loop.run_in_executor(None, os.waitpid, pid, 0)
            exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
            logger.info(f"terminal_runner: child exited with code {exit_code}")
            try:
                await ws.send(bytes([MSG_EXIT, exit_code & 0xFF]))
            except Exception:
                pass
            return exit_code

        async def scrollback_sync():
            if not (api_base_url and deca_api_key and session_id and aiohttp):
                return
            while True:
                await asyncio.sleep(SCROLLBACK_SYNC_INTERVAL)
                async with scrollback_lock:
                    if not scrollback_buf:
                        continue
                    combined = b"".join(scrollback_buf)
                    if len(combined) > MAX_SCROLLBACK_BYTES:
                        combined = combined[-MAX_SCROLLBACK_BYTES:]
                    scrollback_buf.clear()
                    scrollback_buf.append(combined)
                try:
                    text = combined.decode("utf-8", errors="replace")
                    async with aiohttp.ClientSession() as http:
                        await http.post(
                            f"{api_base_url}/deca/v1/terminal/session/{session_id}/scrollback",
                            json={"scrollback": text},
                            headers={"Authorization": f"Bearer {deca_api_key}"},
                            timeout=aiohttp.ClientTimeout(total=10),
                        )
                except Exception as e:
                    logger.debug(f"terminal_runner: scrollback sync failed: {e}")

        tasks = [
            asyncio.create_task(pty_to_ws()),
            asyncio.create_task(ws_to_pty()),
            asyncio.create_task(wait_child()),
            asyncio.create_task(scrollback_sync()),
        ]

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        try:
            os.close(master_fd)
        except OSError:
            pass

        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

        # Final scrollback flush
        if api_base_url and deca_api_key and session_id and aiohttp:
            try:
                async with scrollback_lock:
                    combined = b"".join(scrollback_buf)
                    if len(combined) > MAX_SCROLLBACK_BYTES:
                        combined = combined[-MAX_SCROLLBACK_BYTES:]
                text = combined.decode("utf-8", errors="replace")
                async with aiohttp.ClientSession() as http:
                    await http.post(
                        f"{api_base_url}/deca/v1/terminal/session/{session_id}/scrollback",
                        json={"scrollback": text},
                        headers={"Authorization": f"Bearer {deca_api_key}"},
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="Terminal relay runner for opencode-action-harness")
    parser.add_argument("--relay-url", required=True, help="WSS URL of the relay server")
    parser.add_argument("--command", default="crush", help="Command to run in PTY")
    parser.add_argument("--session-token", required=True, help="One-time session auth token")
    parser.add_argument("--api-base-url", default="", help="Deca API base URL for scrollback sync")
    parser.add_argument("--deca-api-key", default="", help="Deca API key for scrollback sync")
    parser.add_argument("--session-id", default="", help="Session ID for scrollback sync")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_bridge(
        args.relay_url,
        args.command,
        args.session_token,
        api_base_url=args.api_base_url,
        deca_api_key=args.deca_api_key,
        session_id=args.session_id,
    ))


if __name__ == "__main__":
    main()
