#!/bin/bash
set -euo pipefail
# Run nginx server to serve artifacts with bind mounts

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/../config.yaml"

PREFIX=$(yq -e '.provider.domain_prefix' "$CONFIG_FILE")
ARTIFACT_SERVER_URL=$(yq -e '.artifact_server_url' "$CONFIG_FILE")
ENDPOINT=$(python3 "$SCRIPT_DIR/server-endpoint.py" "$ARTIFACT_SERVER_URL")
CONFIGURED_PORT="${ENDPOINT##*:}"
if [[ -n "${PORT:-}" && "$PORT" != "$CONFIGURED_PORT" ]]; then
  echo "Error: PORT=$PORT conflicts with artifact_server_url ($ARTIFACT_SERVER_URL). Update artifact_server_url and its artifact URLs instead." >&2
  exit 1
fi

CONTAINER_NAME="${PREFIX}-artifacts-server"
IMAGE_NAME="${PREFIX}-artifacts-server"

# Stop and remove existing container if running
CONTAINER_NAMES=$(docker ps -a --format '{{.Names}}')
if grep -Fxq -- "$CONTAINER_NAME" <<< "$CONTAINER_NAMES"; then
  echo "Stopping and removing existing container..."
  docker stop "$CONTAINER_NAME" >/dev/null 2>&1 || true
  docker rm "$CONTAINER_NAME" >/dev/null 2>&1 || true
fi


#opt-in to BuildKit for better performance and caching
export DOCKER_BUILDKIT=1

# Build the image
echo "Building Docker image..."
docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"

# in case empty
mkdir -p "$SCRIPT_DIR/images"
mkdir -p "$SCRIPT_DIR/isos"

# Run the container with bind mounts
echo "Starting artifacts server at $ARTIFACT_SERVER_URL..."
docker run -d \
  --name "$CONTAINER_NAME" \
  -p "$ENDPOINT:80" \
  -v "$SCRIPT_DIR/images:/usr/share/nginx/html/images:ro" \
  -v "$SCRIPT_DIR/isos:/usr/share/nginx/html/isos:ro" \
  --restart unless-stopped \
  "$IMAGE_NAME"

echo "Artifacts server is running at $ARTIFACT_SERVER_URL"
echo ""
echo "To stop the server, run:"
echo "  docker stop $CONTAINER_NAME"
echo ""
echo "To view logs, run:"
echo "  docker logs -f $CONTAINER_NAME"
