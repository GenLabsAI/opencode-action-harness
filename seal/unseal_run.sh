#!/usr/bin/env bash
# Decrypt a sealed payload into a throwaway directory, run one command
# there, and wipe it — including on failure or interrupt.
#
# Fail-closed: a missing key is an error, never a silent plaintext run.
#
# Env:
#   SEAL_KEY        secret used when the payload was sealed (required)
#   SEALED_PAYLOAD  path to the .tar.seal blob (default: sealed/payload.tar.seal)
#
# Usage: unseal_run.sh 'bash runner.py'
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEALED_PAYLOAD="${SEALED_PAYLOAD:-$HERE/../sealed/payload.tar.seal}"

: "${SEAL_KEY:?SEAL_KEY is required to unseal ${SEALED_PAYLOAD}}"
if [ ! -f "$SEALED_PAYLOAD" ]; then
  echo "unseal_run: no payload at $SEALED_PAYLOAD" >&2
  exit 1
fi

WORK="$(mktemp -d)"
cleanup() {
  # Loaded layers and build caches may be read-only; be forgiving.
  chmod -R u+w "$WORK" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

python3 "$HERE/seal.py" unseal --in "$SEALED_PAYLOAD" --out "$WORK/src" >&2
cd "$WORK/src"

command="${1:?usage: unseal_run.sh <command>}"
set +e
bash -c "$command"
rc=$?
set -e
exit "$rc"
