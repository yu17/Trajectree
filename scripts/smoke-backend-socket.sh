#!/usr/bin/env bash
# Container smoke test for local backends through a socket: Claude Code, started through
# trajectree.runner.confine inside the hardened container (read-only root, non-root, no
# capabilities, no-new-privileges), must send its API requests to ANTHROPIC_UNIX_SOCKET.
#
# A stdlib HTTP server on a Unix socket records each request line and answers 500. The socket
# sits outside every Landlock rule, as /host/sockets does in a real run, and ANTHROPIC_BASE_URL
# names a closed port, so a request can only arrive through the socket.
#
# Needs Docker and a built image. Usage: bash scripts/smoke-backend-socket.sh IMAGE
#   (for example trajectree:audit; SMOKE_VERBOSE=1 prints Claude Code's output)
set -euo pipefail

image=${1:?usage: scripts/smoke-backend-socket.sh IMAGE}

inside='
mkdir -p /tmp/sock /tmp/run/home /tmp/run/config
python -I -c "
import http.server, socketserver

class Handler(http.server.BaseHTTPRequestHandler):
    def handle_one_request(self):
        self.raw_requestline = self.rfile.readline(65537)
        if self.raw_requestline:
            with open(\"/tmp/sock/seen\", \"a\") as f:
                f.write(self.raw_requestline.decode(\"latin-1\"))
        self.close_connection = True
        if self.parse_request():
            self.send_error(500)

    def log_message(self, *args):
        pass

socketserver.UnixStreamServer(\"/tmp/sock/backend.sock\", Handler).serve_forever()
" &
for _ in $(seq 50); do [ -S /tmp/sock/backend.sock ] && break; sleep 0.1; done
cd /tmp/run
HOME=/tmp/run/home CLAUDE_CONFIG_DIR=/tmp/run/config TMPDIR=/tmp/run \
ANTHROPIC_UNIX_SOCKET=/tmp/sock/backend.sock ANTHROPIC_BASE_URL=http://127.0.0.1:9 \
ANTHROPIC_AUTH_TOKEN=smoke-dummy-token CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
python -I -m trajectree.runner.confine \
    --ro /usr /lib /lib64 /bin /sbin /etc /opt /app /proc /sys \
    --rw /dev /tmp/run \
    -- claude -p hi >/tmp/run/claude.log 2>&1 &
claude=$!
# Claude Code retries a 500 with backoff; the first request is all this test needs.
for _ in $(seq 600); do grep -q "^POST /v1/messages" /tmp/sock/seen 2>/dev/null && break; sleep 0.1; done
kill "$claude" 2>/dev/null || true
echo "--- requests"; cat /tmp/sock/seen 2>/dev/null || true
echo "--- claude"; cat /tmp/run/claude.log
'

out=$(docker run --rm \
    --read-only --tmpfs /tmp \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --user "$(id -u):$(id -g)" --pids-limit 256 --memory 4g \
    --entrypoint sh "$image" -c "$inside") || true

if [ -n "${SMOKE_VERBOSE:-}" ]; then echo "$out"; fi
fail() {
    echo "$out"
    echo "smoke: FAILED: $1"
    exit 1
}
grep -q "^POST /v1/messages" <<<"$out" || fail "no POST /v1/messages reached the socket"
echo "smoke: backend socket ok"
