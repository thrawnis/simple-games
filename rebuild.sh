#!/usr/bin/env bash
# Pull the latest code and redeploy the Simple Games Engine with Docker Compose.
# Run on the Docker host from anywhere; it operates on its own git checkout.
#
#   ./rebuild.sh                     # rebuild only if something changed
#   ./rebuild.sh --force             # rebuild and restart regardless
#   ./rebuild.sh --pull-base-images  # also check for a newer python base image
set -euo pipefail

# Resolve the repo from the script's location so cwd doesn't matter.
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
BRANCH="main"
COMPOSE_FILE="$REPO_DIR/docker-compose.yml"
# Must match `image:` of the app service in docker-compose.yml, so that
# `up --no-build` runs the image built here.
IMAGE="simple-games-engine:latest"
# Host directories bind-mounted by the compose file (./nginx.conf is a tracked
# file, not a directory, so it isn't listed).
BIND_DIRS=("$REPO_DIR/data")
# UID the app container runs as; it must own ./data to write uploads.
APP_UID=10001

usage() {
  cat <<EOF
Usage: $(basename "$0") [--force] [--pull-base-images] [-h|--help]

Syncs $REPO_DIR to origin/$BRANCH (discarding local edits to tracked files),
then rebuilds $IMAGE and restarts the stack if anything changed.

  --force             rebuild and restart even if nothing changed
  --pull-base-images  pass --pull to the build to check for newer base images
  -h, --help          show this help
EOF
}

FORCE=0
PULL_BASE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1 ;;
    --pull-base-images) PULL_BASE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

cd "$REPO_DIR"

# The stack can't start without settings.env (it holds the admin password),
# so fail before touching the running containers.
if [ ! -f settings.env ]; then
  echo "settings.env not found in $REPO_DIR - run ./setup.sh first." >&2
  exit 1
fi

# --- 1. Sync code (every run) ----------------------------------------------
echo "==> Syncing $BRANCH"
git checkout "$BRANCH"
# Discard local edits to tracked files only. No `git clean`: untracked files
# such as settings.env and data/ must survive.
git reset --hard
OLD_HEAD="$(git rev-parse HEAD)"
# --ff-only: fail loudly rather than create a merge commit on the server if
# the local branch has somehow diverged.
git pull --ff-only origin "$BRANCH"
NEW_HEAD="$(git rev-parse HEAD)"
# Fixed length so the value compares equal across runs even as the repo grows.
SHORT_SHA="$(git rev-parse --short=12 HEAD)"

# --- 2. Decide whether a rebuild is needed ---------------------------------
# Commit baked into the current image; empty if the image doesn't exist
# (`|| true` stops set -e/pipefail from aborting on a missing image).
IMAGE_COMMIT="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$IMAGE" 2>/dev/null \
  | sed -n 's/^GIT_COMMIT=//p' || true)"

if [ "$OLD_HEAD" = "$NEW_HEAD" ]; then
  if [ "$IMAGE_COMMIT" = "$SHORT_SHA" ] && [ "$FORCE" -eq 0 ] && [ "$PULL_BASE" -eq 0 ]; then
    echo "==> Already up to date ($SHORT_SHA); image matches. Nothing to do."
    echo "    (Use --force to rebuild anyway.)"
    exit 0
  fi
  if [ "$IMAGE_COMMIT" != "$SHORT_SHA" ]; then
    # A previous build failed or was skipped, so the image lags the code.
    echo "==> Code unchanged, but image is from '${IMAGE_COMMIT:-<none>}' not $SHORT_SHA; rebuilding."
  else
    echo "==> Code unchanged; rebuilding because --force/--pull-base-images was given."
  fi
else
  echo "==> Updated ${OLD_HEAD:0:12} -> $SHORT_SHA"
fi

# --- 3. Host directories -----------------------------------------------------
# Create bind-mount sources ourselves; otherwise Docker creates them as root.
for d in "${BIND_DIRS[@]}"; do
  mkdir -p "$d"
done
# Warn (don't fail) if the app user can't write ./data; setup.sh fixes this.
if [ "$(stat -c %u "$REPO_DIR/data")" != "$APP_UID" ]; then
  echo "WARNING: $REPO_DIR/data is not owned by UID $APP_UID; uploads will fail." >&2
  echo "         Fix with: sudo chown -R $APP_UID:$APP_UID $REPO_DIR/data" >&2
fi

# --- 4. Build ----------------------------------------------------------------
# buildx directly (not `compose build`), since Compose can silently fall back
# to the legacy builder. --load puts the result in the local image store.
BUILD_ARGS=(--tag "$IMAGE" --build-arg "GIT_COMMIT=$SHORT_SHA" --load)
if [ "$PULL_BASE" -eq 1 ]; then
  BUILD_ARGS+=(--pull)
fi
echo "==> Building $IMAGE ($SHORT_SHA)"
docker buildx build "${BUILD_ARGS[@]}" "$REPO_DIR"

# --- 5. Restart ----------------------------------------------------------------
echo "==> Restarting stack"
# No -v: keep named volumes. Data lives in ./data on the host either way.
docker compose -f "$COMPOSE_FILE" down
# --no-build so Compose runs the image we just built instead of rebuilding.
docker compose -f "$COMPOSE_FILE" up -d --no-build
docker compose -f "$COMPOSE_FILE" ps
echo "==> Deployed $SHORT_SHA"
