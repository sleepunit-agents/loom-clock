#!/bin/sh
# Smoke test: the clock piece on its own, with nothing of any agent's machine around it.
# A clean environment, a throwaway HOME, piece home and repo, and stand-ins for systemd-run
# and systemctl (no real timer is ever armed), and a local delivery HTTP fixture.
# Set a wake, see it on the page, fire it, and inspect the runtime contract.
#
#   sh test/smoke.sh
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d)
delivery_pid=
cleanup() {
  if [ -n "$delivery_pid" ]; then kill "$delivery_pid" 2>/dev/null || true; wait "$delivery_pid" 2>/dev/null || true; fi
  rm -rf "$tmp"
}
trap cleanup EXIT
mkdir -p "$tmp/home" "$tmp/bin" "$tmp/repo/routines"
printf '# Nightly\nDo the nightly pass.\n' > "$tmp/repo/routines/nightly.md"

# systemd-run: record the timer it was asked for; systemctl: nothing is loaded or active.
cat > "$tmp/bin/systemd-run" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$SMOKE_TIMERS"
EOF
cat > "$tmp/bin/systemctl" <<'EOF'
#!/bin/sh
case "$*" in *LoadState*) echo not-found ;; *is-active*) exit 3 ;; esac
exit 0
EOF
# A legacy launcher is deliberately present: it must never be called.
cat > "$tmp/bin/session" <<'EOF'
#!/bin/sh
touch "$SMOKE_FORBIDDEN_SESSION"
exit 1
EOF
cat > "$tmp/bin/principal" <<'EOF'
#!/bin/sh
exit 0
EOF
chmod +x "$tmp/bin/"*

cat > "$tmp/delivery.py" <<'EOF'
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
root = Path(sys.argv[1])
class Delivery(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        assert self.path == '/api/loom/deliver'
        assert self.headers.get('authorization') == 'Bearer clock-smoke-token'
        body = json.loads(self.rfile.read(int(self.headers['content-length'])))
        (root / 'delivered.json').write_text(json.dumps(body))
        reply = json.dumps({'body':'queued','conversation':'cv-0123456789ab','seq':1}).encode()
        self.send_response(200); self.send_header('content-type','application/json'); self.end_headers(); self.wfile.write(reply)
server = HTTPServer(('127.0.0.1',0), Delivery)
(root / 'port').write_text(str(server.server_port))
server.serve_forever()
EOF
python3 "$tmp/delivery.py" "$tmp" &
delivery_pid=$!
checks=0
while [ ! -s "$tmp/port" ]; do
  checks=$((checks + 1))
  [ "$checks" -lt 50 ] || { echo 'smoke: delivery fixture did not start' >&2; exit 1; }
  kill -0 "$delivery_pid" 2>/dev/null || { echo 'smoke: delivery fixture exited' >&2; exit 1; }
  sleep 0.1
done
printf 'clock-smoke-token\n' > "$tmp/token"

run() {
  env -i PATH="$tmp/bin:/usr/local/bin:/usr/bin:/bin" HOME="$tmp/home" LOOM_PIECE_HOME="$tmp/piece" \
    WAKE_LOCAL_AGENT=ada WAKE_REPO="$tmp/repo" WAKE_SESSION_COMMAND='["session", "{prompt}", "{model}"]' \
    WAKE_LOCAL_RUN=session WAKE_DELIVERY=spawn WAKE_PRINCIPAL=principal \
    WAKE_DELIVER_TOKEN_FILE="$tmp/token" WAKE_LOOM_URL="http://127.0.0.1:$(cat "$tmp/port")" \
    SMOKE_TIMERS="$tmp/timers" SMOKE_FORBIDDEN_SESSION="$tmp/forbidden-session" ${SCOPE:+LOOM_SCOPE=$SCOPE} "$here/bin/wake" "$@"
}
fail() { echo "smoke: FAIL: $*" >&2; exit 1; }

run --help | grep -q "loom's clock piece" || fail "--help"

out=$(run in 2h -p "check the soak test" --note "smoke round 1")
id=$(echo "$out" | grep -Eo 'w-[0-9a-f]{6}' | head -1)
[ -n "$id" ] || fail "in: $out"
run list | grep -q "$id" || fail "list doesn't show $id"
grep -q -- "--unit wake-$id" "$tmp/timers" || fail "no timer asked for $id"
grep -q -- "--setenv LOOM_PIECE_HOME=$tmp/piece" "$tmp/timers" || fail "the timer doesn't carry the piece home"
grep -q -- "--agent ada fire $id" "$tmp/timers" || fail "the timer doesn't fire $id"

run schedule set nightly --at 03:13 -f routines/nightly.md --note "the nightly pass" | grep -q "schedule nightly: next at" || fail "schedule set"
run schedule list | grep -q "^nightly" || fail "schedule list"

run ui --json | grep -q '"label": "Clock"' || fail "manifest"
echo '{"path": ""}' | SCOPE=ada run ui page --json | grep -q 'Nightly' || fail "the page doesn't show the schedule"
echo '{"caller":{"kind":"agent","agent":"ada"},"path":"run","body":{"argv":["fire","w-x"]}}' | run api --json | grep -q '"status": 403' || fail "api lets an agent fire"

run fire "$id"
[ -f "$tmp/delivered.json" ] || fail "fire delivered no runtime message"
[ ! -e "$tmp/forbidden-session" ] || fail "fire ran the legacy session launcher"
python3 - "$tmp/delivered.json" "$id" <<'EOF'
import json,sys
b=json.load(open(sys.argv[1]))
assert b['id']==sys.argv[2] and b['local'] is True and b['agent']=='ada'
assert b['place']=='wake:'+sys.argv[2] and b['kind']=='wake'
assert 'You are Ada, waking from a self-set alarm' in b['text']
assert 'check the soak test' in b['text']
assert 'model' not in b
EOF
run list | grep -q "$id" && fail "$id is still pending after it fired"
run log --grep fire | grep -q "fire | $id" || fail "the log has no fire for $id"

[ -f "$tmp/piece/data/ada/spool.jsonl" ] || fail "the spool isn't under the piece home"
[ ! -e "$tmp/home/.local/state/loom-clock" ] || fail "wrote outside the piece home"
echo "smoke: ok ($id set, armed, listed, shown, delivered through runtime; legacy launcher unused)"
