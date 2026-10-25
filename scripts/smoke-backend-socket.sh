#!/usr/bin/env bash
# Container smoke test for local backends through a socket: Claude Code, started through
# trajectree.runner.confine inside the hardened container (read-only root, non-root, no
# capabilities, no-new-privileges), must send its API requests to ANTHROPIC_UNIX_SOCKET.
#
# The run env comes from run_env with a ccx profile in settings.json, as in a real run. A stdlib
# HTTP server on a Unix socket records each request line and answers 500. The socket
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

socketserver.UnixStreamServer(\"/tmp/sock/smoke.sock\", Handler).serve_forever()
" &
for _ in $(seq 50); do [ -S /tmp/sock/smoke.sock ] && break; sleep 0.1; done
cd /tmp/run
mkdir -p /tmp/run/cfg/backends/smoke
echo "{\"env\": {\"ANTHROPIC_AUTH_TOKEN\": \"smoke-dummy-token\", \"ANTHROPIC_BASE_URL\": \"http://127.0.0.1:9\",
  \"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC\": \"1\"}}" >/tmp/run/cfg/backends/smoke/settings.json
# The run env comes from run_env, as in a real ccx run: the profile sits in settings.json only.
python -I -c "
import os
from pathlib import Path
from trajectree.config import Settings
from trajectree.runner.argv import run_env
from trajectree.runner.spec import RunSpec, ToolPolicy

settings = Settings(data_dir=Path(\"/tmp/run/data\"), cfg_dir=Path(\"/tmp/run/cfg\"), sockets_dir=Path(\"/tmp/sock\"))
spec = RunSpec(run_id=\"smoke\", kind=\"ask\", runner=\"ccx:smoke\", backend=\"smoke\", model=None, prompt=\"hi\",
               system_prompt=None, cwd=Path(\"/tmp/run\"), tools=ToolPolicy(tools=[], allowed=[]), usd_cap=0.1,
               timeout_s=60, mcp_url=\"http://127.0.0.1:9/mcp\", mcp_token=\"smoke\", json_schema=None)
env = {**run_env(spec, settings, base=os.environ, oauth_token=None), \"HOME\": \"/tmp/run/home\", \"TMPDIR\": \"/tmp/run\"}
argv = [\"python\", \"-I\", \"-m\", \"trajectree.runner.confine\",
        \"--ro\", \"/usr\", \"/lib\", \"/lib64\", \"/bin\", \"/sbin\", \"/etc\", \"/opt\", \"/app\", \"/proc\", \"/sys\",
        \"--rw\", \"/dev\", \"/tmp/run\", \"--\", \"claude\", \"-p\", \"hi\"]
os.execvpe(argv[0], argv, env)
" >/tmp/run/claude.log 2>&1 &
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
