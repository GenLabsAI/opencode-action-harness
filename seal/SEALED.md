# Sealed payloads

Keep the repository public while its runtime payload stays encrypted.
The blob is committed; the key is a repository secret; the runner
decrypts into a throwaway directory, runs, and wipes it.

Same shape as the encrypted orb state snapshots (`*.tar.zst.enc`), but
authenticated and bound to a header, so a tampered blob fails loudly
instead of producing garbage plaintext.

## What it protects

- Browsing, cloning, grep, code search, license scanners: they see one
  opaque `.tar.seal` file.
- Running the payload without the key: impossible.
- Silent modification by anyone who can write to the repo: AES-GCM tag
  covers ciphertext and header.

## What it does NOT protect

- **The runner.** Plaintext exists on the VM for the life of the job.
  Anyone who can edit the workflow (anyone with write access) can
  exfiltrate it. Sealing is not a permission boundary.
- **Fork PRs.** They get no secrets, so the job fails closed. That is
  intentional: it also denies strangers a path to the plaintext.
- **Leaks you make yourself.** Artifacts, caches, `set -x`, and printed
  files are public on a public repo.
- **History.** Earlier plaintext commits, forks, and cached artifacts
  from previous runs remain readable forever. Sealing changes what
  lands *next*; it cannot retract what is already pushed.
- **Metadata.** Blob size, change cadence, the Dockerfile, and workflow
  logic all stay visible and are often enough to fingerprint an
  architecture.

If you need real secrecy rather than obscurity, keep the repository
private and let the public repo hold only a thin, secret-free shim that
pulls a private image or release asset at runtime. See the README's
"public shell" note.

## Format

```
magic  "STARSEAL1"   9 bytes
salt   random       16 bytes    scrypt salt
nonce  random       12 bytes    AES-256-GCM nonce
body   ciphertext   ...         AAD = magic || salt || nonce
```

Key = `scrypt(passphrase, salt, n=2^15, r=8, p=1, dklen=32)`. Human-chosen
secrets are fine; `seal.py keygen` produces a strong one. The header is
authenticated, so flipping a salt or nonce byte fails the tag check.

Payloads are deterministic gzipped tars (sorted, mtime zeroed, no
uid/gid) with `.git`, `__pycache__`, `node_modules`, virtualenvs and
`*.pyc` skipped.

## Usage

One-time setup:

```sh
python3 artifacts/harness/seal/seal.py keygen      # -> paste into repo secrets as SEAL_KEY
```

Seal a payload (the directory must contain `runner.py` at its root,
because `Dockerfile.agent` copies it from the build context):

```sh
SEAL_KEY=... python3 artifacts/harness/seal/seal.py pack \
  --in artifacts/harness --out artifacts/harness/sealed/payload.tar.seal
```

Commit the blob, never the key. Verify what shipped:

```sh
python3 artifacts/harness/seal/seal.py info  --in artifacts/harness/sealed/payload.tar.seal
SEAL_KEY=... python3 artifacts/harness/seal/seal.py verify \
  --in artifacts/harness/sealed/payload.tar.seal --expect runner.py Dockerfile.agent
```

In CI, `run_containerized.sh` unseals the blob before `docker build` and
removes the plaintext on exit. `seal/unseal_run.sh 'bash runner.py'` is
the stand-alone variant for one-off commands.

## Rotation

Re-`pack` with a new key, commit, then replace `SEAL_KEY`. Old blobs stay
readable by anyone who kept the old key — that is a property of
committed ciphertext, not a bug. If a key leaks, rotate and treat the
payload as public from that moment.

## Requirements

`python3` plus the `cryptography` package. `run_containerized.sh`
installs it on demand and only when a sealed payload is present, so
unsealed setups keep working unchanged.
