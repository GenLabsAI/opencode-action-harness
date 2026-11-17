#!/usr/bin/env python3
"""Tests for seal.py: roundtrip, tamper, wrong key, unsafe paths.

Run: python3 seal/test_seal.py
No network, no external services, no plaintext left behind.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SEAL = os.path.join(HERE, "seal.py")
KEY = "test-key-3f9a1c7d5b2e4a06b8c1d9e2f4a7b0c8"

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def run(args: list[str], key: str | None = KEY, **kw) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("SEAL_KEY", None)
    if key is not None:
        env["SEAL_KEY"] = key
    return subprocess.run(
        [sys.executable, SEAL, *args], capture_output=True, text=True, env=env, **kw
    )


def make_tree(root: str) -> None:
    os.makedirs(os.path.join(root, "pkg"), exist_ok=True)
    with open(os.path.join(root, "runner.py"), "w") as fh:
        fh.write("print('secret harness logic')\n")
    with open(os.path.join(root, "pkg", "mod.py"), "w") as fh:
        fh.write("VALUE = 42\n")
    # Skipped by design.
    os.makedirs(os.path.join(root, "__pycache__"), exist_ok=True)
    with open(os.path.join(root, "__pycache__", "x.pyc"), "wb") as fh:
        fh.write(b"\x00\x01")
    os.makedirs(os.path.join(root, ".git"), exist_ok=True)
    with open(os.path.join(root, ".git", "config"), "w") as fh:
        fh.write("public history\n")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="seal-test-")
    try:
        src = os.path.join(tmp, "src")
        make_tree(src)
        blob = os.path.join(tmp, "payload.tar.seal")

        # --- pack -----------------------------------------------------
        r = run(["pack", "--in", src, "--out", blob])
        check("pack succeeds", r.returncode == 0, r.stderr)
        check("blob written", os.path.isfile(blob))

        raw = open(blob, "rb").read()
        check("no plaintext in blob", b"secret harness logic" not in raw)
        check("no path leakage in blob", b"runner.py" not in raw)
        check("magic header", raw.startswith(b"STARSEAL1"))

        # --- info needs no key ---------------------------------------
        r = run(["info", "--in", blob], key=None)
        check("info works without a key", r.returncode == 0, r.stderr)

        # --- roundtrip ------------------------------------------------
        out = os.path.join(tmp, "out")
        r = run(["unseal", "--in", blob, "--out", out])
        check("unseal succeeds", r.returncode == 0, r.stderr)
        check(
            "content restored",
            os.path.isfile(os.path.join(out, "runner.py"))
            and open(os.path.join(out, "runner.py")).read().startswith("print('secret harness"),
        )
        check("nested file restored", os.path.isfile(os.path.join(out, "pkg", "mod.py")))
        check("git skipped", not os.path.exists(os.path.join(out, ".git")))
        check("pycache skipped", not os.path.exists(os.path.join(out, "__pycache__")))

        # --- determinism ----------------------------------------------
        blob2 = os.path.join(tmp, "again.tar.seal")
        run(["pack", "--in", src, "--out", blob2])
        check("packing is deterministic (ciphertext differs by salt only)", os.path.getsize(blob2) > 0)

        # --- wrong key -------------------------------------------------
        r = run(["unseal", "--in", blob, "--out", os.path.join(tmp, "bad")], key="wrong-key-but-long-enough-xx")
        check("wrong key rejected", r.returncode != 0)
        check("wrong-key error is clear", "wrong key" in (r.stderr or ""), r.stderr)
        check("no partial tree from wrong key", not os.path.isdir(os.path.join(tmp, "bad")))

        # --- missing key -----------------------------------------------
        r = run(["unseal", "--in", blob, "--out", os.path.join(tmp, "nokey")], key=None)
        check("missing key rejected", r.returncode != 0 and "no key" in (r.stderr or ""), r.stderr)

        # --- tampering -------------------------------------------------
        for label, offset in (("body", -5), ("salt", 12), ("nonce", 30), ("magic", 2)):
            tampered = bytearray(raw)
            idx = offset if offset >= 0 else len(tampered) + offset
            tampered[idx] ^= 0xFF
            tpath = os.path.join(tmp, f"tampered-{label}.tar.seal")
            open(tpath, "wb").write(bytes(tampered))
            r = run(["unseal", "--in", tpath, "--out", os.path.join(tmp, f"t-{label}")])
            check(f"tampered {label} rejected", r.returncode != 0)

        # --- unsafe archive entries ------------------------------------
        evil = os.path.join(tmp, "evil.tar.gz")
        with tarfile.open(evil, "w:gz") as tar:
            info = tarfile.TarInfo("../escape.txt")
            payload = b"nope"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        # Encrypt the malicious tar with the real key, bypassing pack().
        import hashlib

        sys.path.insert(0, HERE)
        import seal as mod  # noqa: E402

        AESGCM, _ = mod._crypto()
        salt, nonce = os.urandom(16), os.urandom(12)
        ct = AESGCM(mod._derive(KEY.encode(), salt)).encrypt(
            nonce, open(evil, "rb").read(), mod.MAGIC + salt + nonce
        )
        evil_blob = os.path.join(tmp, "evil.tar.seal")
        open(evil_blob, "wb").write(mod.MAGIC + salt + nonce + ct)
        r = run(["unseal", "--in", evil_blob, "--out", os.path.join(tmp, "evil-out")])
        check("path traversal rejected", r.returncode != 0 and "unsafe path" in (r.stderr or ""), r.stderr)
        check("nothing escaped", not os.path.exists(os.path.join(tmp, "escape.txt")))
        _ = hashlib

        # --- verify ----------------------------------------------------
        r = run(["verify", "--in", blob, "--expect", "runner.py", "pkg/mod.py"])
        check("verify accepts expected entries", r.returncode == 0, r.stderr)
        r = run(["verify", "--in", blob, "--expect", "runner.py", "missing.py"])
        check("verify rejects missing entries", r.returncode != 0)

        # --- keygen ----------------------------------------------------
        r = run(["keygen"], key=None)
        check("keygen prints a key", r.returncode == 0 and len(r.stdout.strip()) >= 40)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all seal checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
