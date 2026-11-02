# loom-clock

loom's **clock** piece: what wakes an agent.

An agent's session can end by setting its next alarm. Recurring work is a
schedule. The household's own work on a schedule is a lane, assigned to an
agent. One agent can ask another something, and it lands as a wake on the
other's clock. When the spool would be empty, a dead-man wake fires in about a
day, so the agent always gets to decide what's next.

```
wake in 4h -p "check the soak" --note "task 12, round 2"
wake at "03:13" -f routines/triage.md --note "triage"
wake park --on a-1a2b3c4d -p "push the fix" --note "task 12, round 3"   # wake when the owner answers
wake schedule set nightly --at 22:44 -f routines/nightly.md               # recurring
wake schedule set weekly --at 17:47 --days sun -f routines/weekly.md
wake list | wake log --since 24h | wake cancel w-abc123
wake pause | wake resume                 # hold everything; each held wake fires once on resume
wake now                                 # a wake within a minute, with no lane
wake lane set verify-pages --agent ada --at 04:17 -p "…"   # household work, assigned to an agent
wake relay --to bo --say "Can you review the PR?" --why "…"  # a wake on another agent's clock
wake --agent bo list                     # another agent's clock (the owner's to see)
```

Each wake is a transient systemd user timer (`systemd-run --user`, unit
`wake-<id>`). When it fires, the clock starts the work: a **session** on this
machine (by default `claude --bg <prompt>`), or a **house** from
[loom-box](https://github.com/sleepunit-agents/loom-box) (`loom box up --agent
<id> --task -`). The prompt it hands over starts with a short preamble (who
it is, which wake, how to set the next one), then the lane file's text or the
inline prompt.

## Clocks, agents, and where wakes run

- **Every agent has its own clock**: a spool, its schedules, its log and its
  pause, in `$WAKE_DATA/<agent>/`. At most 50 one-off wakes pending, plus
  schedules: a schedule keeps exactly one occurrence pending and arms the next
  when it fires, so a lane needn't re-arm itself. A lane that still tries is
  told it's already scheduled.
- **One agent is this machine's own** (`WAKE_LOCAL_AGENT`). Its lane files
  live in `WAKE_REPO` (under `WAKE_LANE_DIRS`), and its wakes may run here as
  sessions. `WAKE_FIRE` says where: `local` (a session here), `house` (a
  loom-box house), or `mixed` (a house when the prompt says it's ready, a line
  `<!-- house: ready -->` in a lane file's first 30 lines, or any inline
  prompt that doesn't say `<!-- house: local -->`).
- **Every other agent's wakes run in houses**, and so does any wake an agent
  set for itself through loom's API. A wake set from a house that says it
  needs loom's machine (`<!-- house: local -->`) is refused, and the clock
  files an approval with the prompt as its words: it runs here only once the
  owner allows exactly that prompt (`--approval <id>`). This needs the
  [approvals piece](https://github.com/sleepunit-agents/loom-approvals).
- **Parking.** A body blocked on the owner's answer (a secret request `q-…`
  from loom-secrets, an approval `a-…`) parks instead of polling: the clock
  checks the request every 5 minutes with no body running, and wakes the
  continuation, told the answer, when it comes (or after 24 hours).
- **Failures** back off (15, 30, 60, 120, 240 minutes), say so once at the
  third (`WAKE_ALERT_COMMAND`), and give up at the sixth. A house handed to
  loom-box is checked on later: one that never started is retried like a
  failed dispatch.
- **Relays** go only between agents the household pairs (`household.json`,
  `"relay": {"ada": ["bo"]}`), or once with the owner's approval. The
  recipient's brief says plainly that the words carry none of the owner's
  authority. There are no caps: the same words again within the hour are
  refused, and a long thread is a heads-up on loom's Inbox, never a stop.

## Install

It's a loom piece: an npm package with a `loom-clock` bin and a `loom` block in
`package.json` (loom `docs/extensions.md`, `docs/specs/pieces-install.md`).

```
loom pieces install --from github:sleepunit-agents/loom-clock#<40-char commit> --yes
loom pieces set clock WAKE_LOCAL_AGENT=<agent> WAKE_REPO=/path/to/its/repo
```

It also installs as `wake`, the name bodies type. It needs Python 3.9+,
systemd's user manager (`systemd-run --user`, `systemctl --user`) and nothing
else (no npm dependencies). It runs a service, `loom-piece-clock-run`
(`loom-clock run`): at start it re-arms every clock's timers, which a reboot
loses because they're transient, and then it keeps every pending wake armed.
The wakes themselves run in their own transient units, not in the service.
Until `WAKE_LOCAL_AGENT` is set, the service arms nothing (loom starts it at
install, before its settings are set); setting it restarts the service, which
then re-arms.

Unmanaged, any `loom-clock` on `PATH` works too (link `bin/wake` as
`loom-clock` and `wake`); then run `wake rearm` at boot yourself, for example
from a `oneshot` user unit.

## Settings

| Setting | Default | What it is |
|---|---|---|
| `WAKE_LOCAL_AGENT` | `agent` (required in loom) | The agent this machine runs sessions for |
| `WAKE_REPO` | the home directory | That agent's repo: lane files, and where its sessions start |
| `WAKE_LANE_DIRS` | `routines,briefs` | Lane directories in the repo. A wake set through the API may name only a `.md` file under one of them; the page lists the first |
| `WAKE_DATA` | `${LOOM_PIECE_HOME}/data` | The clocks: `<agent>/` for each, and `lanes.json`, `relays.jsonl`, `fire` beside them. Unmanaged: `~/.local/state/loom-clock` |
| `WAKE_AGENTS` | the local agent | Agents that may manage their own clocks over loom's piece API |
| `WAKE_FIRE` | `$WAKE_DATA/fire`, else `local` | `local`, `house` or `mixed` |
| `WAKE_MODEL` | `claude-sonnet-5` | The model a wake runs on unless it names one |
| `WAKE_SESSION_COMMAND` | `["claude", "--bg", "{prompt}", "--model", "{model}"]` | The session a local wake starts. A JSON list (or a command line); `{prompt}` and `{model}` are whole arguments. The default can stop and ask for permission; an unattended agent that has decided otherwise adds its own flags here |
| `WAKE_ALERT_COMMAND` | none (the log only) | How the clock reaches the owner when a wake keeps failing; the message is its last argument |
| `WAKE_FIRE_GATE` | none | A command run before any wake: non-zero defers every wake an hour. For a standby that mirrors a primary and must never run the same lanes |
| `WAKE_PREAMBLE_FILE` | the piece's own | The words every wake starts with: `{name} {where} {id} {kind} {created} {note} {lanes} {body}` |
| `WAKE_RELAY_HEADS_UP` | `8` | A relay thread this many messages long within a day is a heads-up |

Read from the environment only: `WAKE_HOUSEHOLD` (default
`$LOOM_HOME/household.json`, loom's household file: its `agents`, `owner`
and `relay`), `WAKE_LOOM` (the `loom` CLI, default `loom` on `PATH`),
`WAKE_APPROVALS` (default `loom approvals`), and `WAKE_NO_FORWARD=1` (see
"In a house").

The default session command skips the agent harness's permission prompts,
since nobody is there to answer them. Set your own if that isn't what you want.

A managed piece gets its settings from loom. Run directly by the bin loom
linked (a timer, a body typing `wake`), it finds its piece home from where
it's installed and reads the owner's `settings.json` there. A timer also
carries the settings it was armed with (`systemd-run --setenv`), so after
changing a setting, run `wake rearm` to re-arm the pending wakes with it.

## How it composes with loom

- **The page.** `ui --json`: a Clock tile on Home (what's next, or Paused),
  each agent's card ("Next: …"), a Clock tab on each agent's pages
  (`agentTab`, with `LOOM_SCOPE`), and the household's Clock under Setup,
  which is only the household's lanes. `ui page --json` draws the hero (next
  wake, *Wake now*, *Pause*), schedules (add, run now, remove), one-off wakes
  (cancel) and recent fires. It declares `owners: 2`: a member who owns only
  some agents sees and acts on only those clocks, and never the household's
  lanes.
- **The piece API.** `api --json` (loom's `POST /api/clock/run`) takes
  `{"argv": [...]}` from the owner or an agent in `WAKE_AGENTS`. An agent
  reaches only its own clock; the owner may name any with `--agent`. Only the
  clock verbs pass (`at in park list cancel log schedule relay relays lane`);
  `fire`, `rearm`, `run`, `ui` and `bundle` are loom's machine's.
- **In a house** (`HOUSE_ID` set), `wake` forwards itself to loom's piece API
  with `LOOM_URL` and `LOOM_TOKEN_FILE` (default `/run/secrets/loom-token`),
  so a wake set there lands in the one spool, never the house's own copy.
  `WAKE_NO_FORWARD=1` keeps it local.
- **The bundle.** `bundle export` / `bundle import` is the clock's part of
  loom's household bundle: every agent's spool, schedules and pause, the
  lanes, the relay log and the fire mode, encrypted with `age` to the owner's
  recipients (logs stay behind). A restored clock comes up **paused**, since
  the old machine may still be running the same lanes; resume it when this
  is the machine that should act.
- **Other pieces it uses, without needing them:** loom-box (houses),
  loom-approvals (local wakes from a house, unpaired relays), loom-secrets
  (parking on a secret request, `loom secrets state`). Absent, the features
  that need them fail with a clear error; the rest works.

## Moving an existing clock in

The clock is plain files, one directory per agent. To bring an unmanaged one
under the piece's home, with nothing firing while it moves:

```
systemctl --user stop 'wake-*.timer'                         # no wake fires mid-move (a running one finishes)
loom pieces install --from github:sleepunit-agents/loom-clock#<commit> --yes
# the service arms nothing until WAKE_LOCAL_AGENT is set: copy first, set it last
dest="${LOOM_HOME:-$HOME/.config/loom}/pieces/clock/data"
mkdir -p "$dest/<agent>"
cp -p <old clock dir>/{spool.jsonl,schedules.json,paused,wake.log} "$dest/<agent>/" 2>/dev/null
cp -p <old lanes.json> <old relays.jsonl> "$dest/"          # if you have them
loom pieces set clock WAKE_LOCAL_AGENT=<agent> WAKE_REPO=…   # restarts the service: it re-arms every clock
loom clock list                                             # check it reads
```

Or point `WAKE_DATA` at an existing directory of clocks and leave them where
they are.

## Tests

```
npm test            # python3 test/test_wake.py && sh test/smoke.sh
```

The unit tests run in a sandbox: their clocks, repo and household file are a
throwaway directory, and `systemd-run`/`systemctl` on their `PATH` refuse, so
a test can never arm a real timer. `test/smoke.sh` runs the bin in a clean
environment (`env -i`, a throwaway `HOME`, `LOOM_PIECE_HOME` and repo, with
stand-ins for systemd and the session): set a wake, list it, schedule a lane,
draw the page, refuse an agent's `fire` over the API, fire the wake into a
session and check the session got the preamble, the prompt and the model.

## License

AGPL-3.0-or-later, like loom and its other pieces. See [LICENSE](LICENSE).
