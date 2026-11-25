#!/usr/bin/env bash
# gptapi installer: builds the Docker image, starts the container, waits until it is healthy,
# and sets up opencode if it has no config yet. Safe to run again (it rebuilds and restarts,
# keeping all data).
#
#   ./install.sh            build + start + set up opencode
#   ./install.sh --test     same, then send one test message through the API
#
# Optional environment variables: GPTAPI_API_PORT (8000), GPTAPI_NOVNC_PORT (6080).
set -euo pipefail

cd "$(dirname "$0")"
API_PORT="${GPTAPI_API_PORT:-8000}"
NOVNC_PORT="${GPTAPI_NOVNC_PORT:-6080}"
COMPOSE=(docker compose -f docker/compose.yaml)
TEST=0
[ "${1:-}" = "--test" ] && TEST=1

say()  { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m    ok: %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    note: %s\033[0m\n' "$*"; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

say "gptapi $(cat VERSION 2>/dev/null || echo '?'): checking Docker"
command -v docker >/dev/null || die "Docker is not installed. Install Docker Desktop (https://www.docker.com/products/docker-desktop/), start it, then run ./install.sh again."
docker info >/dev/null 2>&1 || die "Docker is installed but not running. Start Docker Desktop, wait until it says 'running', then run ./install.sh again."
docker compose version >/dev/null 2>&1 || die "'docker compose' is missing. Update Docker Desktop."
ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null) on $(docker info --format '{{.Architecture}}' 2>/dev/null)"

for p in "$API_PORT" "$NOVNC_PORT"; do
  if lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1 && ! docker ps --format '{{.Names}}' | grep -qx gptapi; then
    die "Port $p is already in use by another program. Free it, or pick another port, e.g. GPTAPI_API_PORT=8001 ./install.sh"
  fi
done

say "Building the image (first time: about 3-5 minutes, downloads ~400 MB)"
docker build -t gptapi:latest -f docker/Dockerfile . || die "Image build failed (see the output above)."
ok "image gptapi:latest"

say "Starting the container"
GPTAPI_API_PORT="$API_PORT" GPTAPI_NOVNC_PORT="$NOVNC_PORT" "${COMPOSE[@]}" up -d --no-build
ok "container gptapi"

say "Waiting for the browser and the API (up to 3 minutes)"
healthy=0
for _ in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1; then healthy=1; break; fi
  sleep 2
done
[ "$healthy" = 1 ] || die "The API did not become healthy. Look at: docker logs gptapi   and   docker exec gptapi tail -50 /var/log/gptapi/api.log"
ok "http://127.0.0.1:$API_PORT/health"

if [ "$TEST" = 1 ]; then
  say "Sending one test message (uses one of chatgpt.com's logged-out messages)"
  reply=$(curl -sS --max-time 120 "http://127.0.0.1:$API_PORT/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d '{"model":"chat-web","messages":[{"role":"user","content":"Reply with exactly the word: ready"}]}')
  if printf '%s' "$reply" | grep -q '"content"'; then
    ok "answer: $(printf '%s' "$reply" | python3 -c 'import json,sys; print(json.load(sys.stdin)["choices"][0]["message"]["content"])' 2>/dev/null || echo "$reply")"
  else
    warn "no answer: $reply"
    warn "If it mentions a human check, open http://127.0.0.1:$NOVNC_PORT and click the check, then retry."
  fi
fi

say "Setting up opencode"
OC_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
OC_FILE="$OC_DIR/opencode.json"
if ! command -v opencode >/dev/null; then
  warn "opencode is not installed. Install it with:  brew install opencode   then run ./install.sh again (or copy opencode.json to $OC_FILE)."
elif [ ! -e "$OC_FILE" ]; then
  mkdir -p "$OC_DIR"
  sed "s#127.0.0.1:8000#127.0.0.1:$API_PORT#" opencode.json > "$OC_FILE"
  ok "wrote $OC_FILE (gptapi is the default model)"
  opencode service restart >/dev/null 2>&1 || true
elif grep -q '"gptapi"' "$OC_FILE"; then
  ok "$OC_FILE already has a gptapi provider; left unchanged"
  if grep -q '"chatgpt-web"' "$OC_FILE"; then
    warn "It still uses the old model id chatgpt-web. Rename it to chat-web in $OC_FILE (both places):"
    warn "with \"gpt\" in the id, opencode offers its patch tool, which chatgpt.com handles badly."
  fi
else
  warn "$OC_FILE already exists, so it was left unchanged."
  warn "Add the \"gptapi\" entry from $(pwd)/opencode.json to it (see INSTALL.md, step 4)."
fi

cat <<EOF

Done. gptapi is running.
  API for clients:   http://127.0.0.1:$API_PORT/v1   (model: chat-web, no API key)
  Watch the browser: http://127.0.0.1:$NOVNC_PORT    (click any "verify you are human" check here)
  Try it:            opencode run "hello"
EOF
