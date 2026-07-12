#!/usr/bin/env bash
set -euo pipefail

IMAGE="deca-agent-base"
CONTAINER="deca-agent-${DECA_AGENT_JOB_ID}"
SNAPSHOT_REPO="${SNAPSHOT_REPO:-GenLabsAI/opencode-action-harness-state}"
SNAPSHOT_TAG="${DECA_AGENT_CHAT_SESSION_ID}"
SNAPSHOT_FILE="agent-state.tar.zst.enc"
SNAPSHOT_DIR="/tmp/deca-agent-state"
SNAPSHOT_PATH="$SNAPSHOT_DIR/$SNAPSHOT_FILE"

mkdir -p "$SNAPSHOT_DIR"

if [ -n "${DECA_AGENT_STATE_PASSWORD:-}" ]; then
  gh release download "$SNAPSHOT_TAG" --repo "$SNAPSHOT_REPO" --pattern "$SNAPSHOT_FILE" --dir "$SNAPSHOT_DIR" --clobber || true
fi

if [ -s "$SNAPSHOT_PATH" ]; then
  openssl enc -d -aes-256-cbc -pbkdf2 -salt -in "$SNAPSHOT_PATH" -out "$SNAPSHOT_DIR/agent-state.tar.zst" -pass env:DECA_AGENT_STATE_PASSWORD
  zstd -d -q -f "$SNAPSHOT_DIR/agent-state.tar.zst" -o "$SNAPSHOT_DIR/agent-state.tar"
  docker load -i "$SNAPSHOT_DIR/agent-state.tar"
  IMAGE="deca-agent-state:${SNAPSHOT_TAG}"
else
  docker build -f "$GITHUB_ACTION_PATH/Dockerfile.agent" -t "$IMAGE" "$GITHUB_ACTION_PATH"
fi

docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

docker run --name "$CONTAINER" \
  -e DECA_AGENT_JOB_ID \
  -e DECA_AGENT_CHAT_SESSION_ID \
  -e DECA_AGENT_TASK \
  -e DECA_AGENT_API_BASE_URL \
  -e DECA_AGENT_WORKER_TOKEN \
  -e DECA_AGENT_MODEL \
  -e DECA_API_KEY \
  -v "$GITHUB_WORKSPACE:/agent/workspace" \
  -w /agent/workspace \
  "$IMAGE" \
  python3 /agent/harness/runner.py
STATUS=$?

docker commit "$CONTAINER" "deca-agent-state:${SNAPSHOT_TAG}"
docker save "deca-agent-state:${SNAPSHOT_TAG}" -o "$SNAPSHOT_DIR/agent-state.tar"
zstd -q -f "$SNAPSHOT_DIR/agent-state.tar" -o "$SNAPSHOT_DIR/agent-state.tar.zst"
openssl enc -aes-256-cbc -pbkdf2 -salt -in "$SNAPSHOT_DIR/agent-state.tar.zst" -out "$SNAPSHOT_PATH" -pass env:DECA_AGENT_STATE_PASSWORD

gh release delete "$SNAPSHOT_TAG" --repo "$SNAPSHOT_REPO" --yes || true
gh release create "$SNAPSHOT_TAG" "$SNAPSHOT_PATH" --repo "$SNAPSHOT_REPO" --title "$SNAPSHOT_TAG" --notes "Encrypted Deca agent state snapshot" --latest=false

exit "$STATUS"
