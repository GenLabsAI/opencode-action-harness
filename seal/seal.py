#!/usr/bin/env python3
"""Authenticated sealing for harness payloads.

Format (concatenated, no length prefixes needed because each is fixed):

    magic    b"STARSEAL1"    9 bytes
    salt     random         16 bytes   scrypt salt
    nonce    random         12 bytes   AES-256-GCM nonce
    body     ciphertext+tag            AAD = magic || salt || nonce

The key is scrypt(passphrase, salt, n=32768, r=8, p=1, dklen=32), so a
human-chosen secret is fine. The header is bound as AAD, so tampering
with the salt or nonce fails authentication loudly instead of yielding
garbage plaintext.

What this does NOT do: hide plaintext from the machine that runs it.
The payload is decrypted on the runner for the life of the job. It only
keeps the content out of the public repository. See SEALED.md.

Usage:
    seal.py keygen                       # print a fresh secret
    seal.py pack   --in DIR --out FILE    # encrypt a tree
    seal.py unseal --in FILE --out DIR    # decrypt a tree
    seal.py info   --in FILE              # header facts, no key needed
    seal.py verify --in FILE --out DIR    # unseal into a temp dir, compare
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import shutil
import sys
import tarfile
import tempfile

MAGIC = b"STARSEAL1"
SALT_LEN = 16
NONCE_LEN = 12
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LEN = 32

# Never sealed: VCS metadata, caches, secrets and the output itself.
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".ruff_cache"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".seal", ".key")


def _die(msg: str) -> None:
    sys.exit(f"seal: {msg}")


def _crypto():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError:
        _die("needs the 'cryptography' package (pip install cryptography)")
    return AESGCM, Scrypt


def _read_secret(key_env: str, key_file: str | None) -> bytes:
    if key_file:
        try:
            raw = open(key_file, "rb").read()
        except OSError as err:
            _die(f"cannot read --key-file: {err}")
    else:
        raw = os.environ.get(key_env, "").encode()
    secret = raw.strip()
    if not secret:
        _die(f"no key: set ${key_env} or pass --key-file")
    if len(secret) < 16:
        _die(f"key in ${key_env} is too short ({len(secret)} bytes); use 'seal.py keygen'")
    return secret


def _derive(secret: bytes, salt: bytes) -> bytes:
    _, Scrypt = _crypto()
    kdf = Scrypt(salt=salt, length=KEY_LEN, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return kdf.derive(secret)


def _payload_paths(root: str, exclude: set[str]) -> list[str]:
    """Relative paths to seal, sorted so the blob is reproducible."""
    root_abs = os.path.abspath(root)
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root_abs):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and os.path.join(dirpath, d) not in exclude
        )
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if full in exclude:
                continue
            if name.endswith(SKIP_SUFFIXES):
                continue
            if os.path.islink(full):
                continue
            out.append(os.path.relpath(full, root_abs))
    return out


def _make_tar(root: str, exclude: set[str]) -> bytes:
    """Deterministic gzipped tar so identical trees seal identically."""
    buf = tempfile.SpooledTemporaryFile(max_size=64 << 20)
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=9) as tar:
        for rel in _payload_paths(root, exclude):
            full = os.path.join(root, rel)
            info = tar.gettarinfo(full, arcname=rel)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o755 if info.mode & 0o100 else 0o644
            with open(full, "rb") as fh:
                tar.addfile(info, fh)
    buf.seek(0)
    data = buf.read()
    buf.close()
    return data


def _safe_extract(blob: bytes, dest: str) -> None:
    os.makedirs(dest, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp.write(blob)
        tar_path = tmp.name
    try:
        with tarfile.open(tar_path, mode="r:gz") as tar:
            members = []
            for m in tar.getmembers():
                name = os.path.normpath(m.name)
                if os.path.isabs(name) or name.startswith(".."):
                    _die(f"refusing unsafe path in payload: {m.name!r}")
                if not (m.isfile() or m.isdir()):
                    _die(f"refusing non-regular entry in payload: {m.name!r}")
                members.append(m)
            try:
                tar.extractall(dest, members=members, filter="data")
            except TypeError:  # Python < 3.12
                tar.extractall(dest, members=members)
    finally:
        os.unlink(tar_path)


def cmd_keygen(_args: argparse.Namespace) -> int:
    print(base64.urlsafe_b64encode(secrets.token_bytes(KEY_LEN)).decode())
    return 0


def cmd_pack(args: argparse.Namespace) -> int:
    AESGCM, _ = _crypto()
    if not os.path.isdir(args.in_dir):
        _die(f"--in is not a directory: {args.in_dir}")
    secret = _read_secret(args.key_env, args.key_file)
    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    exclude = {out_path, os.path.abspath(args.in_dir)}
    tar_bytes = _make_tar(args.in_dir, exclude)
    salt = secrets.token_bytes(SALT_LEN)
    nonce = secrets.token_bytes(NONCE_LEN)
    aad = MAGIC + salt + nonce
    ct = AESGCM(_derive(secret, salt)).encrypt(nonce, tar_bytes, aad)
    header = aad
    with open(out_path, "wb") as fh:
        fh.write(header)
        fh.write(ct)
    os.chmod(out_path, 0o644)
    print(f"sealed {len(tar_bytes)} bytes of payload -> {out_path} ({len(header) + len(ct)} bytes)")
    return 0


def _open(blob_path: str, args: argparse.Namespace) -> bytes:
    AESGCM, _ = _crypto()
    try:
        raw = open(blob_path, "rb").read()
    except OSError as err:
        _die(f"cannot read {blob_path}: {err}")
    if len(raw) < len(MAGIC) + SALT_LEN + NONCE_LEN + 16:
        _die(f"{blob_path} is too short to be a sealed payload")
    if not raw.startswith(MAGIC):
        _die(f"{blob_path} is not a STARSEAL1 payload")
    salt = raw[len(MAGIC) : len(MAGIC) + SALT_LEN]
    nonce = raw[len(MAGIC) + SALT_LEN : len(MAGIC) + SALT_LEN + NONCE_LEN]
    ct = raw[len(MAGIC) + SALT_LEN + NONCE_LEN :]
    secret = _read_secret(args.key_env, args.key_file)
    try:
        return AESGCM(_derive(secret, salt)).decrypt(nonce, ct, MAGIC + salt + nonce)
    except Exception:
        _die("decryption failed: wrong key or the payload was modified")


def cmd_unseal(args: argparse.Namespace) -> int:
    blob = _open(args.in_file, args)
    _safe_extract(blob, args.out_dir)
    print(f"unsealed -> {args.out_dir}")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    raw = open(args.in_file, "rb").read()
    ok = raw.startswith(MAGIC)
    print(f"file:    {args.in_file}")
    print(f"format:  {'STARSEAL1' if ok else 'UNKNOWN'}")
    print(f"bytes:   {len(raw)}")
    print(f"payload: {max(0, len(raw) - len(MAGIC) - SALT_LEN - NONCE_LEN)} bytes (ciphertext+tag)")
    return 0 if ok else 1


def cmd_verify(args: argparse.Namespace) -> int:
    """Unseal a payload and check the expected files are present."""
    blob = _open(args.in_file, args)
    tmp = tempfile.mkdtemp(prefix="seal-verify-")
    try:
        _safe_extract(blob, tmp)
        missing = [name for name in args.expect if not os.path.exists(os.path.join(tmp, name))]
        if missing:
            _die(f"payload is missing expected entries: {', '.join(missing)}")
        print(f"verified: {len(args.expect)} expected entries present ({len(blob)} bytes)")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Seal and unseal harness payloads.")
    parser.add_argument("--key-env", default="SEAL_KEY", help="env var holding the secret")
    parser.add_argument("--key-file", default=None, help="file holding the secret instead")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("keygen", help="print a fresh secret").set_defaults(fn=cmd_keygen)

    p = sub.add_parser("pack", help="encrypt a directory")
    p.add_argument("--in", dest="in_dir", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_pack)

    p = sub.add_parser("unseal", help="decrypt a payload into a directory")
    p.add_argument("--in", dest="in_file", required=True)
    p.add_argument("--out", dest="out_dir", required=True)
    p.set_defaults(fn=cmd_unseal)

    p = sub.add_parser("info", help="describe a payload without a key")
    p.add_argument("--in", dest="in_file", required=True)
    p.set_defaults(fn=cmd_info)

    p = sub.add_parser("verify", help="unseal and check expected entries")
    p.add_argument("--in", dest="in_file", required=True)
    p.add_argument("--expect", nargs="+", required=True)
    p.set_defaults(fn=cmd_verify)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
