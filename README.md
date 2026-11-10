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
wake --agent bo list                     # another agent's clock (the owner's to see)
```

Each wake is a transient systemd user timer (`systemd-run --user`, unit
`wake-<id>`). When it fires, the clock hands the work to loom's runtime: a
body on this machine or in the agent's house, delivered through loom (see
"Delivery"). Explicit compatibility settings can select a **house** from
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
  owner allows exactly that prompt (`--approval <id>`). The card asks the
  clock's own plain question ("OK for Art to run the job below on loom's machine
  itself?") with the clock's lines for what Allow and Deny do; its details are the
  exact prompt, then the body's note on one labelled line. The body writes none of
  the headline: its Allow runs the body's words. The clock keeps the
  words it filed (`filed.json`, 8 days), so `wake in 1m --approval <id>` with
  no `-p` sets exactly them, from any of the agent's houses (or on loom's machine), and spends the
  approval, with the note it was filed with. `--approval` with other words, another
  `--note`, a lane file, a full spool, or an id
  this clock filed no wake for is refused before anything is spent (filed
  words only: the approval's key is a short hash of them). This needs the
  [approvals piece](https://github.com/sleepunit-agents/loom-approvals).
- **Standing jobs.** When the owner said **Allow every time** on a local
  wake's card (t-2013), the clock sets that job again with no card: before
  filing one it asks approvals for a standing job for exactly that agent,
  words and principal (`approvals standing`). The wake is the owner's work
  (`for: owner`, `standing: s-…`, `set_for` keeps whom the body acted for),
  and its note is the clock's own, not the body's. It fires only while the
  job is still on (`approvals standing --check`): one the owner turned off
  since is dropped and logged (`fire-refused`).
- **Parking.** A body blocked on the owner's answer (a secret request `q-…`
  from loom-secrets, an approval `a-…`) parks instead of polling: the clock
  checks the request every 5 minutes with no body running, and wakes the
  continuation, told the answer, when it comes (or after 24 hours). Parked on
  an approval, the clock reads its kind and exact target as filed
  (`approvals check <id> --json`, only when it is this agent's own) and keeps them with the parked wake; told
  yes, the continuation is handed the exact way to spend it (`approvals use
  <id> --kind … --target …`, or for a local wake `wake in 1m --approval <id>`),
  so it never asks the owner again in its own words.
- **Legacy stuck sessions.** A body the runtime runs is the runtime's to stop:
  `LOOM_RUNTIME_STUCK_AFTER`. The clock still monitors sessions recorded by
  older versions, including their configured command's program. New local work
  uses the runtime. A session the old clock started here has nobody at its
  terminal, so one that stops on a permission prompt would wait forever. The
  clock keeps a record of the sessions it started (`sessions.json`, by the id
  `claude --bg` prints) and the service looks at them every 2 minutes
  (`claude agents --json`). One that has waited on a permission prompt for
  `WAKE_STUCK_AFTER` minutes (default 10), counted from when the clock first
  saw it waiting, is stopped (`claude stop`: its conversation is kept), noted
  on the task its wake's note names (`WAKE_TASK_COMMENT`, with the tool and
  the first 120 characters of what it was waiting on, quoted as data), logged
  as `stuck` (not a failure) and said once (`WAKE_ALERT_COMMAND`). Only the
  sessions in that record are ever looked at: never an interactive session,
  never one anything else started. `wake reap` does one look now.
- **Another piece's variables aren't the clock's.** The tasks piece and loom's
  runtime run `loom-clock in 1m …` with their own `LOOM_PIECE_NAME` and
  `LOOM_PIECE_HOME`; the clock ignores a `LOOM_PIECE_HOME` whose
  `LOOM_PIECE_NAME` isn't `clock` and finds its own home from where it's
  installed, so their wakes land on the owner's clock with its settings.
- **Failures** back off (15, 30, 60, 120, 240 minutes), say so once at the
  third (`WAKE_ALERT_COMMAND`), and give up at the sixth. A house handed to
  loom-box is checked on later: one that never started is retried like a
  failed dispatch.
- **Delivery.** A wake bound for the agent's house is delivered, not
  started: the clock calls loom's `POST /api/loom/deliver` with its own piece
  token (`WAKE_DELIVER_TOKEN_FILE`), on core's venue `wake` (the piece
  declares `"venues": {"wake": {}}`), and loom's runtime records the wake in
  the agent's conversation and reaches its body. A lane's wakes share one
  conversation (place `lane:<schedule or lane file>`), so its body can
  resume; any other wake is its own (`wake:<id>`). One set from a house
  (t-2117) joins a lane's conversation only from that same conversation (its
  verified statement's, matched to the conversation the runtime last answered
  for that place, kept in `places.json`): a lane re-arming itself. From any
  other conversation it's its own, so a body never feeds words to another
  conversation's body by naming its lane. The agent's preamble
  (`WAKE_PREAMBLE_FILE`) goes in the wake's text, as it carries the wake's own
  id, note and time; the model is the runtime's choice. The answer (`fed`,
  `resumed`, `started`, `queued`, or `failed`) is in the wake log. `failed`
  is retried later as a new delivery to the same conversation (loom keeps a
  failed answer for its id a day); no answer, a 5xx or a 429 is retried with
  the same id and body, which loom answers once; any other refusal (400, 403,
  409, 413) is said loudly and never retried. A wake with a house slot still
  goes the old way: delivery keeps no slot. A wake can name its conversation,
  `--place <kind>:<name>` (like `alert:host-watch`; not `lane:` or `wake:`,
  and only when set on loom's machine, never from a house): every wake with
  that place feeds one conversation.
- **A wake that runs here is the runtime's too** (t-1921). Every wake bound
  for this machine (fire mode `local`, a lane or prompt that needs loom's
  machine, a house's wake the owner allowed here, the dead-man wake) is
  delivered with the contract's `local: true`, and the runtime runs its body
  here (its `LOOM_RUNTIME_LOCAL_BODIES`): model choice, fallback, permissions
  and stopping a body stuck on a permission prompt live there, in one place.
  Its conversation is the place it names, else `local-lane:<lane>` for a lane
  (its house runs are `lane:<lane>`: a conversation's bodies run in one place
  only), else `wake:<id>`. If the runtime refuses (400 or 403), the owner
  hears why and no other launcher runs. Temporary unavailability retries
  the same delivery, preserving its conversation and id. Legacy
  `WAKE_LOCAL_RUN=session` values use that same path, including already armed
  timers and owner-approved local wakes. `WAKE_DELIVERY=spawn` selects only
  the compatibility allocated-house task path. It cannot start a local harness.
  Configure local models, arguments and permissions in the runtime's body
  policy and `LOOM_RUNTIME_LOCAL_BODIES`; `WAKE_SESSION_COMMAND` no longer
  starts new work. If local runtime delivery is unavailable, restore it rather
  than changing launchers; the clock retains the exact delivery for retry.
- **On whose behalf, the runtime's answer.** `--verify` answers
  `{ok, agent, conversation, for, owner, people, exp}`: `owner` is true or
  false (true: every voice that conversation heard was one of the agent's
  owners, the members who own it), `for` a person's slug or null. The clock
  takes it only when `ok` is true and `agent` is the calling agent; a `for` of
  `owner` without `owner: true` is a person by that name, and unknown here.
  The hand-over names the delivery's id, so a word for a wake that's never
  delivered is never another wake's. A run verifies once and uses that answer
  for the wake, its approval and its spend.
- **Here, and set again.** On loom's machine a wake is the owner's, except
  one set by a body the runtime runs here (a local wake, in a conversation:
  `LOOM_BODY_CONVERSATION`): it fetches its own statement and is whom that
  says, unknown if the runtime can't say. A schedule set again (the page's
  edit sheet, a body's re-set) is never wider than it was: the owner's edit of
  one a body set for Loren keeps it Loren's (one from before this change that
  a body set is unknown, and stays so); remove it and set it anew to make it
  his. The local-body rule rests on the runtime's environment
  (`LOOM_BODY_CONVERSATION`, which loom-box gives every body it runs), and a
  body on loom's machine chooses its children's environment: like everything
  else on loom's machine, that's the owner's account, and not bound here.
- **Spending on loom's machine.** `wake in … --approval <id>` run on loom's
  machine itself calls the approvals bin there with no `--as`: approvals
  reads the call's principal itself (the owner's own work, or, for a local
  body, its conversation's, fetched by approvals from the runtime). So an
  approval asked from a house for someone else is refused there: it's spent
  from a house acting for them. `wake schedule run` here is narrowed the same
  way as over the API.
- **On whose behalf** (t-1943). The owner's standing permissions (his merge
  rules, say) cover work a body does for him; work it does for someone else (a
  person who messaged it in Slack or Discord) needs that person's own grant, or
  it asks. Who a body acts for is its *principal*, and loom's runtime is the
  root of trust for it. So every wake and schedule records `for`, and every
  occurrence a schedule arms copies it: set on loom's machine (the owner's own
  account, and the household's lanes), `owner`; set over the piece API, what
  the body's principal statement says once the runtime verifies it
  (`WAKE_PRINCIPAL --verify`, the statement on stdin) and it names the calling
  agent: `owner`, the person's slug, else `-` (unknown). No statement, one that
  doesn't verify, or one for another agent is `-`. The house wrapper appends
  `--principal <statement>` to every clock call from a body that has one; the
  API door takes it off before anything reads the argv. An agent running a
  schedule now gets no wider a `for` than the schedule's own. Just before a
  delivery the clock hands the wake's `for` to the runtime (`WAKE_PRINCIPAL
  --wake --agent … --place … --for …`); if that fails it's logged and the
  delivery goes ahead, and the runtime treats the wake as unknown. A wake set
  before this has no `for`: the owner's if it was set here, else unknown. A wake
  that goes the old way (a spawned house, a session here) has no runtime record
  to hand it to, so it's unknown there. The statement itself is never logged.
- **Where it was said, and the owner's word** (t-1974). A wake a statement vouched
  for keeps that statement's conversation beside its `for` (`for_in: cv-…`, never the
  statement), so an audit can say which body set it. A body with no statement is
  logged (`principal-none`): a house whose wrapper never sends one shows up in
  `wake log`, not as quietly unknown wakes. A wake that's unknown stays unknown: no
  guess makes it the owner's. The owner can say one is his with `wake vouch <id>…`,
  only at a terminal on loom's machine, typing the ids (as the runtime's `principal
  --reset`): never over the API or from a body, never piped, and never for a wake
  that's a person's (it stays theirs). It's stamped `vouched` and logged.
- **Asked on loom's page** (t-1975; Jonathan: "It needs to have the site ask me").
  The terminal is the fallback: the owner is asked on loom's Inbox instead, one card
  per agent, a `vouch-wake` approval the clock files (target `<agent>:<w-id>,…`,
  sorted, at most 12): "Count these 5 scheduled jobs as your work?", each wake listed
  with what it's for and when it fires, in the clock's words. The service files it by
  itself for unknown wakes that look like the owner's: set from a house of the agent's
  own, with no evidence of anyone else (the body sent no statement: `for_why` none).
  One whose statement didn't verify (`unverified`, which may be a person's during a
  runtime outage) isn't asked about by itself either. One the runtime called unknown (`steered`), or that named
  a person (`for_people`), is never asked about by itself. A wake from before principals
  were kept, or one a person steered, it never asks about by itself (a card it can't read
  three passes running is let go); a body that knows
  its old wakes are the owner's asks with `wake ask-vouch <id>…` (over the API it's
  kept, and the service's next pass files the card, within 10 minutes), never for one
  a person or something unplaced steered (`for_people`, `for_why` steered). One card at a time: a new candidate withdraws the
  waiting card unanswered and files one with the whole list; one that fires doesn't
  (unless none is left on it: then the card is withdrawn, nothing left to decide).
  **Allow** vouches for exactly its wakes: loom core posts `vouch-approved` here as
  the household owner right after it (fields `id`, `target`, and `agent`, which must be the
  target's), and runs `wake vouch --approval a-… <ids>` on that agent's clock, which spends the
  approval (allowed, for exactly those ids, once) and only then marks each one still
  pending and unknown `for: owner`, `vouched.approval`; one that fired meanwhile is
  said, and left. The service's next pass applies an Allow loom didn't (never a body's
  `ask-vouch` call). At most 12 wakes per card; the rest wait for the next one. The
  Deny record (`vouch.json`) goes with the clock in the household bundle. **Deny** changes
  nothing, and those wakes are never asked about again. Only the household owner's
  call applies it (an agent's owner, an agent, no caller: refused), a body never runs
  `vouch` (not over the API, and not with a body's environment here), and only the
  clock spends the approval (approvals refuses a body's spend of this kind; a parked
  body is told there's nothing to spend). Each wake is listed with its note and the start
  of what it runs (credentials taken out). State: `vouch.json` beside the spool.
- **Relays** between agents moved to loom's runtime (loom
  `docs/specs/agent-runtime.md`, "Agent-to-agent relay"): its `relay` MCP
  tool, or `loom runtime relay` from a house. Pairing is the runtime's
  permissions. `wake relay` and `wake relays` only say where they went.

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
| `WAKE_DATA` | `${LOOM_PIECE_HOME}/data` | The clocks: `<agent>/` for each, and `lanes.json`, `fire` beside them. Unmanaged: `~/.local/state/loom-clock` |
| `WAKE_AGENTS` | the local agent | Agents that may manage their own clocks over loom's piece API |
| `WAKE_FIRE` | `$WAKE_DATA/fire`, else `local` | `local`, `house` or `mixed` |
| `WAKE_MODEL` | `claude-sonnet-5` | The model a wake runs on unless it names one |
| `WAKE_LOCAL_RUN` | `runtime` | Legacy compatibility setting. All local wakes, including `session`, are delivered with `local: true` through the runtime |
| `WAKE_SESSION_COMMAND` | `["claude", "--bg", "{prompt}", "--model", "{model}"]` | Only the legacy session monitor uses the program from this setting to inspect sessions already recorded by older versions. It starts no new work |
| `WAKE_STUCK_AFTER` | `10` | Minutes a session the clock started may wait on a permission prompt before it's stopped, noted and said (see "Stuck sessions"); `0` or `off`: never |
| `WAKE_TASK_COMMENT` | `["taskstore", "comment", "{task}", "{text}"]`, when `taskstore` is on `PATH` | How a stopped session is noted on the task its note names (a `t-NNN`); `off`: never |
| `WAKE_ALERT_COMMAND` | none (the log only) | How the clock reaches the owner when a wake keeps failing or a session was stopped stuck; the message is its last argument |
| `WAKE_FIRE_GATE` | none | A command run before any wake: non-zero defers every wake an hour. For a standby that mirrors a primary and must never run the same lanes |
| `WAKE_PREAMBLE_FILE` | the piece's own | The words every wake starts with: `{name} {where} {id} {kind} {created} {note} {lanes} {body}` |
| `WAKE_DELIVERY` | `deliver` | A wake goes through loom's runtime. Missing credentials and temporary delivery failures retry the same id and body. Explicit `spawn` selects the compatibility house path |
| `WAKE_DELIVER_TOKEN_FILE` | none | A file holding a piece token of this clock's (`loom token create --piece clock/<name> --label …`), to deliver with |
| `WAKE_PRINCIPAL` | `loom runtime principal` | loom's runtime's principal command (a command line or a JSON list): `--verify` reads a body's principal statement on stdin, `--wake --agent … --place … --id … --for …` hands over whom that delivered wake is for (see "On whose behalf") |
| `WAKE_LOOM_URL` | `http://127.0.0.1:8787` | loom's address for its HTTP API, as this machine reaches it (an MCP URL's `/mcp` is dropped) |

Read from the environment only: `WAKE_HOUSEHOLD` (default
`$LOOM_HOME/household.json`, loom's household file: its `agents` and `owner`), `WAKE_LOOM` (the `loom` CLI, default `loom` on `PATH`),
`WAKE_APPROVALS` (default `loom approvals`), and `WAKE_NO_FORWARD=1` (see
"In a house").

The runtime's body policy controls local model permissions and arguments.

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
  clock verbs pass (`at in park list cancel log schedule relay relays lane ask-vouch`);
  `fire`, `rearm`, `run`, `reap`, `ui` and `bundle` are loom's machine's.
- **In a house** (`HOUSE_ID` set), `wake` forwards itself to loom's piece API
  with `LOOM_URL` and `LOOM_TOKEN_FILE` (default `/run/secrets/loom-token`),
  so a wake set there lands in the one spool, never the house's own copy.
  `WAKE_NO_FORWARD=1` keeps it local.
- **The bundle.** `bundle export` / `bundle import` is the clock's part of
  loom's household bundle: every agent's spool, schedules, pause and filed local-wake words, the
  lanes and the fire mode, encrypted with `age` to the owner's
  recipients (logs stay behind). A restored clock comes up **paused**, since
  the old machine may still be running the same lanes; resume it when this
  is the machine that should act.
- **Other pieces it uses, without needing them:** loom-box (houses),
  loom-approvals (local wakes from a house), loom-secrets
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
cp -p <old lanes.json> "$dest/"                             # if you have it
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
stand-ins for systemd and an authenticated local HTTP delivery fixture): set a
wake, list it, schedule a lane, draw the page, refuse an agent's `fire` over the
API, and fire the wake with both legacy launcher settings enabled. Check the
runtime receives its id, local placement, preamble and prompt, while the
deliberately failing legacy launcher remains unused.

## License

AGPL-3.0-or-later, like loom and its other pieces. See [LICENSE](LICENSE).

### Waiting for model quota

A delivered `failed` answer with `code: "quota"` keeps the wake until the runtime’s
ISO `retryAfter` reset (plus one minute), between 15 minutes and eight days from now.
Without a usable reset it waits an hour. This does not count as a job failure or
raise failure-streak alerts. Older runtimes saying “out of quota” get the same wait.
The `alert:runtime-quota` triage wake is not retried: the runtime already leaves an
owner notice, and running another model to investigate no available model repeats
the failure. Other alert wakes remain scheduled for their reset.

A continuation wake (`--continuation`, t-2004) stays in the atomic, fsynced spool while it fires, until its outcome
is recorded. The clock seals the complete delivery JSON there before principal handover or HTTP; recovery reuses it
even when prompt files or settings have changed. A per-firing lock prevents concurrent fire processes (locks a day
old are pruned). After a killed process, the service's rearm pass uses a fresh timer id while keeping the exact
delivery and continuation wake ids. A runtime answer of `failed` for a continuation wake is not retried: its report
authority is bound to that one firing. An unexpected error drops the firing with `fire-failed` rather than re-arming it.
Every other wake, schedule and deadman is removed before it fires (at most once), and a retry after a 5xx goes
through its standing-job, lane and mode checks again.

`--continuation` gets the immutable deadline from the local runtime, using the authenticated issuer statement,
before anything is scheduled. A wake set at or past that deadline is refused, and one that fires past it is refused
without delivery (`fire-refused`). It can't be combined with `--slot`, `--place` or `-f`. The lifetime is the
runtime's (`LOOM_RUNTIME_CONTINUATION_DAYS`); the clock never chooses or extends it.
