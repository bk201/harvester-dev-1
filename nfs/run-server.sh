#!/bin/bash -eu
# Run a user-space NFS server (nfs-ganesha) to export a share to the cluster

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/../config.yaml"

PREFIX=$(yq '.provider.domain_prefix' "$CONFIG_FILE")
MGMT_IP=$(yq '.networks.mgmt.bridge_ip' "$CONFIG_FILE")
MOUNT_DIR=$(yq '.nfs.mount_dir // ""' "$CONFIG_FILE")

CONTAINER_NAME="${PREFIX}-nfs-server"
IMAGE_NAME="${PREFIX}-nfs-server"
PORT="${PORT:-2049}"

# Stop and remove existing container if running
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  echo "Stopping and removing existing container..."
  docker stop "$CONTAINER_NAME" >/dev/null 2>&1 || true
  docker rm "$CONTAINER_NAME" >/dev/null 2>&1 || true
fi

# opt-in to BuildKit for better performance and caching
export DOCKER_BUILDKIT=1

# Build the image
echo "Building Docker image..."
docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"

# bind-mount a host dir only if .nfs.mount_dir is configured; otherwise data
# stays in the container's own storage
VOLUME_ARGS=()
if [[ -n "$MOUNT_DIR" ]]; then
  mkdir -p "$MOUNT_DIR"
  VOLUME_ARGS=(-v "$MOUNT_DIR:/backup_target")
fi

# DAC_READ_SEARCH is required by ganesha's VFS FSAL (open_by_handle_at)
echo "Starting NFS server on port $PORT..."
docker run -d \
  --name "$CONTAINER_NAME" \
  --cap-add DAC_READ_SEARCH \
  -p "$PORT:2049" \
  "${VOLUME_ARGS[@]}" \
  --restart unless-stopped \
  "$IMAGE_NAME"

echo "NFS server (NFSv4-only) is running at ${MGMT_IP}:${PORT}"
echo ""
echo "Mount it with:"
echo "  mount -t nfs4 ${MGMT_IP}:/backup_target /mnt"
echo ""
echo "Use it as a Harvester backup target:"
echo "  nfs://${MGMT_IP}:/backup_target"
echo ""
echo "To stop the server, run:"
echo "  docker stop $CONTAINER_NAME"
echo ""
echo "To view logs, run:"
echo "  docker logs -f $CONTAINER_NAME"
