#!/usr/bin/env bash
# Container smoke test for run confinement: `codex exec --sandbox danger-full-access`, started
# through trajectree.runner.confine inside the hardened container (read-only root, non-root,
# no capabilities, no-new-privileges), driven by a local mock of the Responses API.
#
# The mocked model runs one shell command that reads a project file, two fake secrets and the
# owner token through /proc/1/root; only the project file may be readable.
#
# Needs Docker. Usage: bash scripts/smoke-confined-codex.sh   (SMOKE_VERBOSE=1 prints Codex's output)
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
image="trajectree-smoke-codex:$$"
work=$(mktemp -d "${TMPDIR:-/tmp}/trajectree-smoke.XXXXXX")

cleanup() {
    docker image rm -f "$image" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT

echo "smoke: building $image (app target, INSTALL_CODEX=1)"
docker build --quiet --target app --build-arg INSTALL_CODEX=1 -t "$image" "$repo" >/dev/null

mkdir -p "$work/data" "$work/cfg/claude" "$work/cfg/codex" "$work/projects/demo"
echo "fake-owner-token-5f1c" > "$work/data/token"
echo "fake-oauth-token-9a2e" > "$work/cfg/claude/oauth_token"
echo "readable project file 7d3b" > "$work/projects/demo/readme"
cat > "$work/cfg/codex/config.toml" <<'EOF'
model_provider = "mock"
model = "mock-model"
approval_policy = "never"

[model_providers.mock]
name = "mock"
base_url = "http://127.0.0.1:60001/v1"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
EOF

# Runs as the container's PID 1, so /proc/1/root is this shell's view of the whole filesystem.
inside='
/opt/smoke/mock-responses 2>/tmp/mock.log &
python -c "
import socket, time
for _ in range(50):
    try:
        socket.create_connection((\"127.0.0.1\", 60001), 0.2).close()
        break
    except OSError:
        time.sleep(0.1)
"
python -I -m trajectree.runner.confine \
    --ro /usr /lib /lib64 /bin /sbin /etc /opt /app /proc /sys /projects \
    --rw /tmp /dev /cfg/codex \
    -- /opt/codex/bin/codex exec --sandbox danger-full-access --ephemeral --skip-git-repo-check \
       -C /projects/demo "read the readme" 2>&1
echo "codex exit: $?"
'

out=$(docker run --rm \
    --read-only --tmpfs /tmp \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --user "$(id -u):$(id -g)" --pids-limit 256 --memory 4g \
    -v "$work/data:/data" -v "$work/cfg:/cfg" -v "$work/projects:/projects:ro" \
    -v "$repo/tests/bin:/opt/smoke:ro" \
    -e CODEX_HOME=/cfg/codex -e HOME=/cfg/codex \
    -e MOCK_WORKDIR=/projects/demo \
    -e MOCK_CMD="cat /projects/demo/readme; cat /data/token; cat /cfg/claude/oauth_token; cat /proc/1/root/data/token; echo end" \
    --entrypoint sh "$image" -c "$inside") || true

if [ -n "${SMOKE_VERBOSE:-}" ]; then echo "$out"; fi
fail() {
    echo "$out"
    echo "smoke: FAILED: $1"
    exit 1
}
grep -q "readable project file 7d3b" <<<"$out" || fail "project file not readable"
grep -q "cat: /data/token: Permission denied" <<<"$out" || fail "owner token not denied"
grep -q "/cfg/claude/oauth_token: Permission denied" <<<"$out" || fail "Claude OAuth token not denied"
grep -q "/proc/1/root/data/token: Permission denied" <<<"$out" || fail "/proc/1/root escape not denied"
if grep -q -e "fake-owner-token-5f1c" -e "fake-oauth-token-9a2e" <<<"$out"; then
    fail "a secret leaked"
fi
echo "smoke: confined codex ok"
