#!/usr/bin/env bash
set -euo pipefail

: "${DECA_AGENT_STATE_PASSWORD:?DECA_AGENT_STATE_PASSWORD is required}"
: "${DECA_AGENT_CHAT_SESSION_ID:?DECA_AGENT_CHAT_SESSION_ID is required}"
: "${DECA_AGENT_JOB_ID:?DECA_AGENT_JOB_ID is required}"

HASH_INPUT="${DECA_AGENT_CHAT_SESSION_ID}"
SNAPSHOT_ID="$(printf '%s' "$HASH_INPUT" | sha256sum | cut -c1-32)"
JOB_ID_SAFE="$(printf '%s' "$DECA_AGENT_JOB_ID" | sha256sum | cut -c1-16)"
IMAGE="deca-agent-base"
STATE_IMAGE="deca-agent-state:${SNAPSHOT_ID}"
CONTAINER="deca-agent-${JOB_ID_SAFE}"
SNAPSHOT_REPO="${SNAPSHOT_REPO:-${GITHUB_REPOSITORY}}"
SNAPSHOT_TAG="agent-state-${SNAPSHOT_ID}"
SNAPSHOT_FILE="agent-state.tar.zst.enc"
SNAPSHOT_DIR="$(mktemp -d)"
SNAPSHOT_PATH="$SNAPSHOT_DIR/$SNAPSHOT_FILE"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -rf "$SNAPSHOT_DIR"
}
trap cleanup EXIT

if gh release download "$SNAPSHOT_TAG" --repo "$SNAPSHOT_REPO" --pattern "$SNAPSHOT_FILE" --dir "$SNAPSHOT_DIR" --clobber 2>/dev/null; then
  openssl enc -d -aes-256-cbc -pbkdf2 -salt -in "$SNAPSHOT_PATH" -out "$SNAPSHOT_DIR/agent-state.tar.zst" -pass env:DECA_AGENT_STATE_PASSWORD
  zstd -d -q -f "$SNAPSHOT_DIR/agent-state.tar.zst" -o "$SNAPSHOT_DIR/agent-state.tar"
  docker load -i "$SNAPSHOT_DIR/agent-state.tar"
  IMAGE="$STATE_IMAGE"
else
  echo "No existing encrypted snapshot found. Starting fresh."
  docker build -f "$GITHUB_ACTION_PATH/Dockerfile.agent" -t "$IMAGE" "$GITHUB_ACTION_PATH"
fi

docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

# Decrypt DECA_API_KEY
export DECA_API_KEY_DECRYPTED="$(echo "$DECA_API_KEY" | openssl enc -d -aes-256-cbc -pbkdf2 -a -salt -md sha256 -pass env:DECA_AGENT_STATE_PASSWORD)"

set +e
docker run --name "$CONTAINER" \
  -e DECA_AGENT_JOB_ID \
  -e DECA_AGENT_CHAT_SESSION_ID \
  -e DECA_AGENT_TASK \
  -e DECA_AGENT_API_BASE_URL \
  -e DECA_AGENT_WORKER_TOKEN \
  -e DECA_AGENT_MODEL \
  -e DECA_API_KEY="$DECA_API_KEY_DECRYPTED" \
  -w /agent/workspace \
  "$IMAGE" \
  python3 /agent/harness/runner.py
STATUS=$?
set -e

# Revoke DECA_API_KEY on terminal job completion
if [ "$STATUS" -eq 0 ] || [ "$STATUS" -eq 1 ]; then
  curl -s -X DELETE "${DECA_AGENT_API_BASE_URL%/}/deca-agents/v1/jobs/${DECA_AGENT_JOB_ID}/runner_key" \
    -H "X-Worker-Token: ${DECA_AGENT_WORKER_TOKEN}" >/dev/null || true
fi

docker commit "$CONTAINER" "$STATE_IMAGE"
docker save "$STATE_IMAGE" -o "$SNAPSHOT_DIR/agent-state.tar"
zstd -q -f "$SNAPSHOT_DIR/agent-state.tar" -o "$SNAPSHOT_DIR/agent-state.tar.zst"
openssl enc -aes-256-cbc -pbkdf2 -salt -in "$SNAPSHOT_DIR/agent-state.tar.zst" -out "$SNAPSHOT_PATH" -pass env:DECA_AGENT_STATE_PASSWORD

gh release delete "$SNAPSHOT_TAG" --repo "$SNAPSHOT_REPO" --yes 2>/dev/null || true
gh release create "$SNAPSHOT_TAG" "$SNAPSHOT_PATH" --repo "$SNAPSHOT_REPO" --title "$SNAPSHOT_TAG" --notes "Encrypted Deca agent state snapshot" --latest=false

exit "$STATUS"
