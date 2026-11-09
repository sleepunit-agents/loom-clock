#!/bin/sh
# Smoke test: the clock piece on its own, with nothing of any agent's machine around it.
# A clean environment, a throwaway HOME, piece home and repo, and stand-ins for systemd-run
# and systemctl (no real timer is ever armed) and for the session a wake starts.
# Set a wake, see it on the page, fire it, and check the session got the prompt.
#
#   sh test/smoke.sh
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
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
# The session a local wake starts: write down what it was handed.
cat > "$tmp/bin/session" <<'EOF'
#!/bin/sh
printf '%s\n' "$2" > "$SMOKE_SESSION.model"
printf '%s\n' "$1" > "$SMOKE_SESSION"
echo "started"
EOF
chmod +x "$tmp/bin/"*

run() {
  env -i PATH="$tmp/bin:/usr/local/bin:/usr/bin:/bin" HOME="$tmp/home" LOOM_PIECE_HOME="$tmp/piece" \
    WAKE_LOCAL_AGENT=ada WAKE_LOCAL_RUN=session WAKE_REPO="$tmp/repo" WAKE_SESSION_COMMAND='["session", "{prompt}", "{model}"]' \
    SMOKE_TIMERS="$tmp/timers" SMOKE_SESSION="$tmp/session" ${SCOPE:+LOOM_SCOPE=$SCOPE} "$here/bin/wake" "$@"
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
[ -f "$tmp/session" ] || fail "fire started no session"
grep -q "You are Ada, waking from a self-set alarm" "$tmp/session" || fail "the session got no preamble"
grep -q "check the soak test" "$tmp/session" || fail "the session didn't get the prompt"
grep -q "claude-sonnet-5" "$tmp/session.model" || fail "the session didn't get the model"
run list | grep -q "$id" && fail "$id is still pending after it fired"
run log --grep fire | grep -q "fire | $id" || fail "the log has no fire for $id"

[ -f "$tmp/piece/data/ada/spool.jsonl" ] || fail "the spool isn't under the piece home"
[ ! -e "$tmp/home/.local/state/loom-clock" ] || fail "wrote outside the piece home"
echo "smoke: ok ($id set, armed, listed, shown, fired into a session; spool in \$LOOM_PIECE_HOME/data/ada)"
