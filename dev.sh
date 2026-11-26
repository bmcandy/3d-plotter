#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_PYTHON="$ROOT_DIR/.venv/bin/python"
SIMULATE=0
SERIAL_OVERRIDE=""
BACKEND_PID=""
FRONTEND_PID=""

usage() {
  cat <<'EOF'
Usage: ./dev.sh [--simulate] [SERIAL_DEVICE]

Start the backend and frontend for local testing.
By default, connect to a physical printer using SERIAL_DEVICE, SERIAL_PORT,
or the first /dev/ttyACM* or /dev/ttyUSB* device found.
Use --simulate to run with the fake printer instead.
EOF
}

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

for arg in "$@"; do
  case "$arg" in
    --simulate)
      (( SIMULATE == 0 )) || fail "--simulate was specified more than once"
      SIMULATE=1
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    -*)
      usage >&2
      fail "unknown option: $arg"
      ;;
    *)
      [[ -z "$SERIAL_OVERRIDE" ]] || {
        usage >&2
        fail "only one serial device may be specified"
      }
      SERIAL_OVERRIDE="$arg"
      ;;
  esac
done

[[ -x "$BACKEND_PYTHON" ]] ||
  fail "backend virtualenv not found; create it and install backend/requirements.txt (see README.md)"
[[ -x "$ROOT_DIR/frontend/node_modules/.bin/vite" ]] ||
  fail "frontend dependencies not found; run npm install in frontend/ (see README.md)"
command -v npm >/dev/null 2>&1 || fail "npm is required to start the frontend"
command -v node >/dev/null 2>&1 || fail "Node.js is required to start the frontend"
command -v curl >/dev/null 2>&1 || fail "curl is required for the backend readiness check"

if (( ! SIMULATE )); then
  if [[ -n "$SERIAL_OVERRIDE" ]]; then
    export SERIAL_PORT="$SERIAL_OVERRIDE"
  elif [[ -z "${SERIAL_PORT:-}" ]]; then
    for candidate in /dev/ttyACM* /dev/ttyUSB*; do
      if [[ -e "$candidate" ]]; then
        export SERIAL_PORT="$candidate"
        break
      fi
    done
  fi
  [[ -n "${SERIAL_PORT:-}" ]] ||
    fail "no printer serial device found; connect it or set SERIAL_PORT"
  printf 'Using printer serial device: %s\n' "$SERIAL_PORT"
fi

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  [[ -z "$FRONTEND_PID" ]] || kill "$FRONTEND_PID" 2>/dev/null || true
  [[ -z "$BACKEND_PID" ]] || kill "$BACKEND_PID" 2>/dev/null || true
  [[ -z "$FRONTEND_PID" ]] || wait "$FRONTEND_PID" 2>/dev/null || true
  [[ -z "$BACKEND_PID" ]] || wait "$BACKEND_PID" 2>/dev/null || true
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if (( SIMULATE )); then
  export CREALIPEN_SIMULATE=1
fi

(
  cd "$ROOT_DIR/backend"
  exec "$BACKEND_PYTHON" -m app.main
) &
BACKEND_PID=$!

printf 'Waiting for backend at http://localhost:8000/openapi.json'
for _ in $(seq 1 60); do
  if curl --silent --fail http://localhost:8000/openapi.json >/dev/null; then
    printf '\n'
    break
  fi
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    wait "$BACKEND_PID" || true
    fail "backend exited before becoming ready"
  fi
  printf '.'
  sleep 1
done

curl --silent --fail http://localhost:8000/openapi.json >/dev/null ||
  fail "backend did not become ready within 60 seconds"

(
  cd "$ROOT_DIR/frontend"
  exec npm run dev -- --host 0.0.0.0
) &
FRONTEND_PID=$!
printf 'Frontend: http://localhost:5173 (Ctrl-C to stop both)\n'

set +e
wait -n "$BACKEND_PID" "$FRONTEND_PID"
status=$?
set -e
if kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; then
  fail "a development process exited unexpectedly (status $status)"
fi
fail "a development process exited; stopping the other process (status $status)"
