#!/usr/bin/env python3
"""Tests for the clock piece (bin/wake): wakes, schedules, every agent's clock, relays (moved: only the pointer),
loom lanes, the page, the piece API and the bundle part.

    python3 test/test_wake.py

Everything runs in a throwaway sandbox (see below): no real clock, repo or
systemd timer is touched. `looks_like_path` is tested directly; its rejection
path is also exercised end-to-end via subprocess, since `cmd_set` exits on it
before ever opening the spool or calling systemd-run.

The arm_timer/cmd_rearm tests mock subprocess.run, using the exact stderr text
systemd 257 gives for a unit-name collision: "Unit <name> was already loaded or
has a fragment file." — never the older "already exists" wording.
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "wake"

# Nothing a test does reaches a real clock, a real repo, or systemd. Before the module loads,
# its whole world is a throwaway directory: the clocks (WAKE_DATA), the local agent's repo with
# a few lane files (WAKE_REPO), the household file, and a PATH whose systemd-run and systemctl
# refuse, so a test that forgets to stub arm_timer fails instead of arming a real timer.
# HOUSE_ID would make main() forward every call to loom's clock piece: never a test's.
_SANDBOX = tempfile.TemporaryDirectory()
SANDBOX = Path(_SANDBOX.name)
REPO = SANDBOX / "repo"
for f in ("routines/plan.md", "routines/boot.md", "routines/nightly.md", "routines/x.md", "notes/decisions/README.md"):
    (REPO / f).parent.mkdir(parents=True, exist_ok=True)
    (REPO / f).write_text(f"# {f}\n")
(SANDBOX / "bin").mkdir()
for tool in ("systemd-run", "systemctl"):
    (SANDBOX / "bin" / tool).write_text("#!/bin/sh\necho 'test sandbox: no systemd here' >&2\nexit 1\n")
    (SANDBOX / "bin" / tool).chmod(0o755)
for k in [k for k in os.environ if k.startswith(("WAKE_", "LOOM_"))] + ["HOUSE_ID"]:
    os.environ.pop(k, None)
os.environ.update({"WAKE_DATA": str(SANDBOX / "data"), "WAKE_REPO": str(REPO), "WAKE_LOCAL_AGENT": "ada",
                   "WAKE_HOUSEHOLD": str(SANDBOX / "household.json"), "HOME": str(SANDBOX / "home"),
                   "PATH": f"{SANDBOX / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}"})

spec = importlib.util.spec_from_loader("wake_cli", loader=None, origin=str(SCRIPT))
wake_cli = importlib.util.module_from_spec(spec)
wake_cli.__file__ = str(SCRIPT)
exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), wake_cli.__dict__)

REAL_PATHS = ("WAKE_DIR", "SPOOL", "LOCK", "LOG", "SCHEDULES", "PAUSED", "CLOCKS",
              "HOUSEHOLD", "LANES", "FIRE_CONF", "REPO")


def tearDownModule():
    wake_cli.configure("ada")
    _SANDBOX.cleanup()


class RealClocksOutOfReach(unittest.TestCase):
    def test_no_path_the_module_writes_is_a_real_one(self):
        wake_cli.configure("ada")
        for name in REAL_PATHS:
            self.assertTrue(str(getattr(wake_cli, name)).startswith(_SANDBOX.name), name)
        self.assertEqual(wake_cli.WAKE_DIR, SANDBOX / "data" / "ada")


class LooksLikePath(unittest.TestCase):
    def test_bare_routines_md_path_is_path_like(self):
        self.assertTrue(wake_cli.looks_like_path("routines/plan.md"))

    def test_bare_md_filename_is_path_like(self):
        self.assertTrue(wake_cli.looks_like_path("plan.md"))

    def test_any_slash_bearing_single_token_is_path_like(self):
        self.assertTrue(wake_cli.looks_like_path("notes/decisions/foo"))

    def test_absolute_path_is_path_like(self):
        self.assertTrue(wake_cli.looks_like_path(str(SCRIPT.parent.parent / "routines" / "plan.md")))

    def test_existing_filename_under_routines_with_no_slash_is_path_like(self):
        # boot.md exists under ~/Ada/routines/ with no slash and no leading path.
        self.assertTrue(wake_cli.looks_like_path("boot.md"))

    def test_inline_prose_prompt_is_not_path_like(self):
        self.assertFalse(wake_cli.looks_like_path("t-19 round-2 follow-up: check PR9/PR11"))

    def test_short_inline_prompt_is_not_path_like(self):
        self.assertFalse(wake_cli.looks_like_path("check the soak"))

    def test_single_word_prompt_with_no_path_markers_is_not_path_like(self):
        self.assertFalse(wake_cli.looks_like_path("triage"))

    def test_empty_prompt_is_not_path_like(self):
        self.assertFalse(wake_cli.looks_like_path("   "))


class CmdSetRejectsPathLikePrompt(unittest.TestCase):
    """End-to-end: the guard must fire before Spool()/systemd-run are touched."""

    def _run(self, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "in", "5m", "-p", *extra],
            capture_output=True, text=True, timeout=30,
        )

    def test_bare_md_path_is_refused(self):
        r = self._run("routines/plan.md", "--note", "path guard test")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("looks like a file path", r.stderr)
        self.assertIn("--prompt-file/-f", r.stderr)

    def test_existing_routines_filename_with_no_slash_is_refused(self):
        r = self._run("boot.md", "--note", "path guard test")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("looks like a file path", r.stderr)

    def test_slash_bearing_token_without_md_is_refused(self):
        r = self._run("notes/some-file", "--note", "path guard test")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("looks like a file path", r.stderr)

    def test_prose_prompt_passes_the_path_guard(self):
        # Fails later for lack of --note, never on the path check — proves
        # a real inline prompt doesn't trip this guard.
        r = self._run("check the soak")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("looks like a file path", r.stderr)
        self.assertIn("--note", r.stderr)


def _completed(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# The real failure text for a unit-name collision on systemd 257, captured by
# running `systemd-run --user --collect --unit <name> ...` twice back to
# back for the same unit name. Never "already exists".
REAL_COLLISION_STDERR = (
    "Failed to start transient timer unit: Unit wake-xyz.timer was "
    "already loaded or has a fragment file.\n"
)


class ArmTimerCollisionDetection(unittest.TestCase):
    """arm_timer must classify a real collision as 'exists', not exit."""

    def _entry(self, id_="w-xyz"):
        return {
            "id": id_,
            "at": wake_cli.datetime.now().strftime(wake_cli.TIME_FMT),
        }

    def test_real_collision_stderr_is_detected_via_load_state_not_wording(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[0] == "systemd-run":
                return _completed(1, stderr=REAL_COLLISION_STDERR)
            if cmd[0] == "systemctl":
                return _completed(0, stdout="loaded\n")
            raise AssertionError(f"unexpected command: {cmd}")

        with mock.patch.object(wake_cli.subprocess, "run", side_effect=fake_run):
            result = wake_cli.arm_timer(self._entry("w-xyz"))

        self.assertEqual(result, "exists")
        # Detection went through systemctl's LoadState, not stderr parsing.
        self.assertTrue(any(c[0] == "systemctl" for c in calls))

    def test_old_already_exists_wording_is_not_what_detection_relies_on(self):
        # Guard against regressing back to string-matching: even stderr
        # that *does* contain the old "already exists" phrase must still
        # be classified via LoadState, and a not-loaded unit is a real exit.
        def fake_run(cmd, **kwargs):
            if cmd[0] == "systemd-run":
                return _completed(1, stderr="some unrelated already exists noise")
            if cmd[0] == "systemctl":
                return _completed(0, stdout="not-found\n")
            raise AssertionError(f"unexpected command: {cmd}")

        with mock.patch.object(wake_cli.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(SystemExit):
                wake_cli.arm_timer(self._entry("w-abc"))

    def test_genuine_non_collision_failure_still_exits(self):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "systemd-run":
                return _completed(1, stderr="Failed to connect to bus: no such file")
            if cmd[0] == "systemctl":
                return _completed(0, stdout="not-found\n")
            raise AssertionError(f"unexpected command: {cmd}")

        with mock.patch.object(wake_cli.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(SystemExit):
                wake_cli.arm_timer(self._entry("w-def"))


class CmdRearmSurvivesAnAlreadyArmedEntry(unittest.TestCase):
    """cmd_rearm's loop must not abort on one already-armed spool entry."""

    def test_loop_processes_every_entry_even_when_one_collides(self):
        entries = [
            {"id": "w-first", "kind": "wake", "at": "2099-01-01 00:00:00",
             "note": "collision test", "prompt": "x", "model": "m",
             "created": "2026-01-01 00:00:00"},
            {"id": "w-second", "kind": "wake", "at": "2099-01-01 00:00:00",
             "note": "collision test", "prompt": "x", "model": "m",
             "created": "2026-01-01 00:00:00"},
        ]
        calls = []

        def fake_arm_timer(entry):
            calls.append(entry["id"])
            # First entry simulates the already-armed collision case.
            return "exists" if entry["id"] == "w-first" else "armed"

        with tempfile.TemporaryDirectory() as td:
            wake_dir = Path(td)
            spool_path = wake_dir / "spool.jsonl"
            spool_path.write_text(
                "".join(wake_cli.json.dumps(e) + "\n" for e in entries))
            with mock.patch.object(wake_cli, "WAKE_DIR", wake_dir), \
                 mock.patch.object(wake_cli, "SPOOL", spool_path), \
                 mock.patch.object(wake_cli, "LOCK", wake_dir / "spool.lock"), \
                 mock.patch.object(wake_cli, "LOG", wake_dir / "wake.log"), \
                 mock.patch.object(wake_cli, "arm_timer", side_effect=fake_arm_timer):
                wake_cli.cmd_rearm(None)

        # Both entries were armed — the "exists" collision on the first
        # entry did not abort the loop before the second entry was reached.
        self.assertEqual(calls, ["w-first", "w-second"])


class SpoolCommitsOnException(unittest.TestCase):
    """Spool.__exit__ must write what's in self.entries even when the body
    raises partway through: any irreversible side
    effect (arm_timer, disarm_timer) that already ran before the raise is
    reflected in self.entries by then, and discarding the write instead of
    committing it silently drops the mutation instead of surfacing or
    recovering it."""

    def test_write_lands_even_when_body_raises(self):
        with tempfile.TemporaryDirectory() as td:
            wake_dir = Path(td)
            spool_path = wake_dir / "spool.jsonl"
            lock_path = wake_dir / "spool.lock"
            with mock.patch.object(wake_cli, "WAKE_DIR", wake_dir), \
                 mock.patch.object(wake_cli, "SPOOL", spool_path), \
                 mock.patch.object(wake_cli, "LOCK", lock_path):
                with self.assertRaises(SystemExit):
                    with wake_cli.Spool() as spool:
                        spool.entries.append({"id": "w-new", "kind": "wake"})
                        sys.exit("simulated arm_timer failure")

                self.assertTrue(spool_path.exists())
                written = [wake_cli.json.loads(l)
                           for l in spool_path.read_text().splitlines() if l.strip()]
                self.assertEqual([e["id"] for e in written], ["w-new"])

    def test_lock_is_released_even_when_body_raises(self):
        with tempfile.TemporaryDirectory() as td:
            wake_dir = Path(td)
            spool_path = wake_dir / "spool.jsonl"
            lock_path = wake_dir / "spool.lock"
            with mock.patch.object(wake_cli, "WAKE_DIR", wake_dir), \
                 mock.patch.object(wake_cli, "SPOOL", spool_path), \
                 mock.patch.object(wake_cli, "LOCK", lock_path):
                with self.assertRaises(RuntimeError):
                    with wake_cli.Spool():
                        raise RuntimeError("boom")
                # A second open must not deadlock on the first's lock.
                with wake_cli.Spool():
                    pass


class PieceApi(unittest.TestCase):
    """wake as loom's clock piece (POST /api/clock/run): who may set wakes,
    and what a house's -f may name. The command itself is a stub here."""

    def setUp(self):
        self.ran = []

    def ask(self, argv, caller=None, **env):
        caller = caller if caller is not None else {"kind": "agent", "agent": "ada"}
        def run(a, origin, agent):
            self.ran.append(a)
            return 0, "ok\n", ""
        with mock.patch.dict(wake_cli.os.environ, env):
            return wake_cli.api_answer({"caller": caller, "path": "run", "body": {"argv": argv}}, run)

    def test_an_agents_wake_is_marked_as_its_own(self):
        seen = []
        def run(a, origin, agent):
            seen.append(origin)
            return 0, "", ""
        for caller, want in [({"kind": "agent", "agent": "ada"}, "agent:ada"), ({"kind": "owner"}, "")]:
            r = wake_cli.api_answer({"caller": caller, "path": "run", "body": {"argv": ["list"]}}, run)
            self.assertEqual(r["status"], 200)
        self.assertEqual(seen, ["agent:ada", ""])

    def test_an_agent_owner_reaches_only_their_agents_clocks_and_no_lanes(self):
        # loom docs/specs/owners.md: Sam owns bo.
        sam = {"kind": "owner", "who": "sam", "agents": ["bo"]}
        agents = []
        def run(a, origin, agent):
            agents.append(agent)
            return 0, "", ""
        ask = lambda argv: wake_cli.api_answer({"caller": sam, "path": "run", "body": {"argv": argv}}, run)
        self.assertEqual(ask(["list"])["status"], 200)                       # her default is her agent
        self.assertEqual(ask(["--agent", "bo", "list"])["status"], 200)
        self.assertEqual(ask(["--agent", "ada", "list"])["status"], 403)
        self.assertEqual(ask(["--agent", "bo", "lane", "list"])["status"], 403)
        self.assertEqual(agents, ["bo", "bo"])
        # A household owner still names any clock.
        pat = {"kind": "owner", "who": "pat-owner", "agents": "*"}
        self.assertEqual(wake_cli.api_answer({"caller": pat, "path": "run", "body": {"argv": ["--agent", "ada", "list"]}}, run)["status"], 200)

    def test_the_page_acts_only_on_an_agent_owners_clocks(self):
        sam = {"kind": "owner", "who": "sam", "agents": ["bo"]}
        ran = []
        run = lambda agent, argv: ran.append((agent, argv)) or (0, "", "")
        self.assertFalse(wake_cli.ui_act({"action": "pause", "fields": {"agent": "ada"}, "caller": sam}, run)["ok"])
        self.assertFalse(wake_cli.ui_act({"action": "lane-new", "fields": {"agent": "bo", "name": "x"}, "caller": sam}, run)["ok"])
        self.assertEqual(ran, [])
        self.assertEqual(wake_cli.owned_by({"kind": "owner", "agents": "*"}), None)
        self.assertEqual(wake_cli.owned_by({"kind": "owner", "agents": ["bo", "Bad!"]}), ["bo"])

    def test_only_the_clock_verbs_reach_it(self):
        for v in ["fire", "rearm", "api"]:
            self.assertEqual(self.ask([v, "w-x"])["status"], 403)
        self.assertEqual(self.ran, [])

    def test_strangers_and_boxes_are_refused(self):
        for c in [{"kind": "agent", "agent": "bo"}, {"kind": "piece", "subject": "box/thor"}, {"kind": "x"}]:
            self.assertEqual(self.ask(["list"], c)["status"], 403)
        self.assertEqual(self.ask(["list"], WAKE_AGENTS="bo")["status"], 403)
        self.assertEqual(self.ran, [])

    def test_malformed_requests(self):
        self.assertEqual(wake_cli.api_answer(None, None)["status"], 400)
        self.assertEqual(self.ask([])["status"], 400)
        self.assertEqual(self.ask([1])["status"], 400)


class FireMode(unittest.TestCase):
    def test_house_mode_asks_loom_box_with_the_task_as_json(self):
        with mock.patch.object(wake_cli.subprocess, "run") as run:
            wake_cli.dispatch("the prompt", "claude-x", {}, "house")
        cmd, kw = run.call_args.args[0], run.call_args.kwargs
        self.assertEqual(cmd[1:], ["box", "up", "--agent", "ada", "--task", "-"])
        self.assertEqual(wake_cli.json.loads(kw["input"]), {"prompt": "the prompt", "model": "claude-x"})

    def test_local_is_the_default(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(wake_cli.os.environ, {"WAKE_FIRE": ""}), \
             mock.patch.object(wake_cli, "FIRE_CONF", Path(td) / "none"), \
             mock.patch.object(wake_cli.subprocess, "run") as run:
            wake_cli.dispatch("p", "m", {}, wake_cli.fire_mode())
        self.assertEqual(run.call_args.args[0][:3], ["claude", "--bg", "p"])

    def test_the_session_command_is_a_setting(self):
        cmd = json.dumps(["my-runner", "--model={model}", "{prompt}"])
        with mock.patch.dict(wake_cli.os.environ, {"WAKE_SESSION_COMMAND": cmd}), \
             mock.patch.object(wake_cli.subprocess, "run") as run:
            wake_cli.dispatch("the prompt", "m1", {}, "local")
        # {prompt} and {model} are whole arguments; one inside another word stays as written.
        self.assertEqual(run.call_args.args[0], ["my-runner", "--model={model}", "the prompt"])
        with mock.patch.dict(wake_cli.os.environ, {"WAKE_SESSION_COMMAND": "my-runner {prompt} --model {model}"}), \
             mock.patch.object(wake_cli.subprocess, "run") as run:
            wake_cli.dispatch("p q", "m1", {}, "local")
        self.assertEqual(run.call_args.args[0], ["my-runner", "p q", "--model", "m1"])

    def test_a_session_command_that_isnt_there_is_a_failed_dispatch(self):
        with mock.patch.dict(wake_cli.os.environ, {"WAKE_SESSION_COMMAND": '["/nonexistent/runner", "{prompt}"]'}):
            r = wake_cli.dispatch("p", "m", {}, "local")
        self.assertEqual(r.returncode, 127)


class AgentWakes(unittest.TestCase):
    """A wake set by an agent (WAKE_ORIGIN, from the piece API): its -f is a
    lane file, read the way argparse reads it, and it only fires into a house."""

    def set_(self, argv, origin="agent:ada"):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(wake_cli.os.environ, {"WAKE_ORIGIN": origin}), \
             mock.patch.object(wake_cli, "WAKE_DIR", Path(td)), \
             mock.patch.object(wake_cli, "SPOOL", Path(td) / "spool.jsonl"), \
             mock.patch.object(wake_cli, "LOCK", Path(td) / "spool.lock"), \
             mock.patch.object(wake_cli, "LOG", Path(td) / "wake.log"), \
             mock.patch.object(wake_cli, "arm_timer", return_value="armed"), \
             mock.patch.object(wake_cli.sys, "argv", ["wake", *argv]):
            try:
                wake_cli.main()
            except SystemExit as e:
                return str(e.code), []
            return None, [wake_cli.json.loads(l) for l in (Path(td) / "spool.jsonl").read_text().splitlines()]

    def test_lane_files_only_however_the_flag_is_spelled(self):
        for argv in (["in", "1h", "-f", "/etc/passwd"], ["in", "1h", "-f/etc/passwd"],
                     ["in", "1h", "--prompt-file=/etc/passwd"], ["in", "1h", "-f", "routines/../../.ssh/id_ed25519"],
                     ["in", "1h", "-f", "notes/decisions/README.md"]):
            err, entries = self.set_(argv)
            self.assertIn("takes a lane file", err or "", argv)
            self.assertEqual(entries, [])
        # Abbreviations aren't flags at all now.
        err, _ = self.set_(["in", "1h", "--prompt-fi", "/etc/passwd"])
        self.assertIsNotNone(err)

    def test_a_lane_file_is_taken_and_marked(self):
        err, entries = self.set_(["in", "1h", "-f", "routines/plan.md"])
        self.assertIsNone(err, err)
        self.assertEqual(entries[0]["origin"], "agent:ada")
        self.assertEqual(entries[0]["prompt_file"], str(wake_cli.REPO / "routines" / "plan.md"))

    def test_the_owners_and_local_wakes_take_any_file(self):
        err, entries = self.set_(["in", "1h", "-f", "notes/decisions/README.md"], origin="")
        self.assertIsNone(err, err)
        self.assertNotIn("origin", entries[0])

    def test_a_houses_wake_that_says_local_is_refused_unless_the_owner_allowed_it(self):
        # A house body that sets `<!-- house: local -->` isn't sent to a house silently: it's told at
        # once, and the owner can allow it (approvals).
        filed = []
        with mock.patch.object(wake_cli, "file_approval", lambda kind, target, what, why: filed.append((kind, target, what, why)) or "a-0000aaaa"):
            err, entries = self.set_(["in", "1h", "-p", "push the agent <!-- house: local -->", "--note", "t-1 r1"])
        self.assertIn("--approval a-0000aaaa", err or "")
        self.assertEqual(entries, [])
        # The card shows the prompt itself (wake files it), not the body's own description of it.
        (kind, target, what, why), = filed
        self.assertEqual(kind, "local-wake")
        self.assertIn("push the agent", what)
        self.assertIn("t-1 r1", why)
        # Too long to show whole: refused, never cut.
        with mock.patch.object(wake_cli, "file_approval", lambda *a: self.fail("filed a truncated ask")):
            err, _ = self.set_(["in", "1h", "-p", "x" * 300 + " <!-- house: local -->", "--note", "t-1 r2"])
        self.assertIn("characters at most", err or "")
        refuse = lambda aid, kind, target: "" if aid == "a-12345678" and kind == "local-wake" else f"{aid} was asked by ada in h-1, not by bo in h-2"
        with mock.patch.object(wake_cli, "spend_approval", refuse):
            # A refusal says the piece's reason, not just no.
            err, _ = self.set_(["in", "1h", "-p", "push the agent <!-- house: local -->", "--note", "t-1 r1", "--approval", "a-99999999"])
            self.assertIn("not by bo in h-2", err or "")
            err, entries = self.set_(["in", "1h", "-p", "push the agent <!-- house: local -->", "--note", "t-1 r1", "--approval", "a-12345678"])
        self.assertIsNone(err, err)
        self.assertEqual(entries[0]["local_approval"], "a-12345678")

    def test_an_allowed_local_wake_runs_on_loom_s_machine(self):
        entry = {"id": "w-aaaaaa", "kind": "wake", "at": "2026-01-01 00:00:00", "note": "n",
                 "prompt": "do it <!-- house: local -->", "model": "m", "created": "x", "origin": "agent:ada", "local_approval": "a-12345678"}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(wake_cli.os.environ, {"WAKE_FIRE": "mixed"}), \
             mock.patch.object(wake_cli, "WAKE_DIR", Path(td)), \
             mock.patch.object(wake_cli, "SPOOL", Path(td) / "spool.jsonl"), \
             mock.patch.object(wake_cli, "LOCK", Path(td) / "spool.lock"), \
             mock.patch.object(wake_cli, "LOG", Path(td) / "wake.log"), \
             mock.patch.object(wake_cli, "ensure_deadman"), \
             mock.patch.object(wake_cli, "dispatch") as dispatch, \
             mock.patch.object(wake_cli.subprocess, "run") as prime:
            prime.return_value.returncode = 0
            dispatch.return_value.returncode = 0
            dispatch.return_value.stdout = ""
            (Path(td) / "spool.jsonl").write_text(wake_cli.json.dumps(entry) + "\n")
            wake_cli.cmd_fire(mock.Mock(id="w-aaaaaa"))
        self.assertEqual(dispatch.call_args.args[3], "local")

    def test_an_agents_wake_fires_into_a_house_even_in_local_mode(self):
        entry = {"id": "w-aaaaaa", "kind": "wake", "at": "2026-01-01 00:00:00", "note": "n",
                 "prompt": "do it", "model": "m", "created": "x", "origin": "agent:cy"}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(wake_cli.os.environ, {"WAKE_FIRE": "local"}), \
             mock.patch.object(wake_cli, "WAKE_DIR", Path(td)), \
             mock.patch.object(wake_cli, "SPOOL", Path(td) / "spool.jsonl"), \
             mock.patch.object(wake_cli, "LOCK", Path(td) / "spool.lock"), \
             mock.patch.object(wake_cli, "LOG", Path(td) / "wake.log"), \
             mock.patch.object(wake_cli, "ensure_deadman"), \
             mock.patch.object(wake_cli, "dispatch") as dispatch, \
             mock.patch.object(wake_cli.subprocess, "run") as prime:
            prime.return_value.returncode = 0
            dispatch.return_value.returncode = 0
            dispatch.return_value.stdout = ""
            (Path(td) / "spool.jsonl").write_text(wake_cli.json.dumps(entry) + "\n")
            wake_cli.cmd_fire(mock.Mock(id="w-aaaaaa"))
        self.assertEqual(dispatch.call_args.args[3:5], ("house", "cy"))


class MixedMode(unittest.TestCase):
    def fire(self, entry, body=None, mode="mixed"):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(wake_cli.os.environ, {"WAKE_FIRE": mode}), \
             mock.patch.object(wake_cli, "WAKE_DIR", Path(td)), \
             mock.patch.object(wake_cli, "SPOOL", Path(td) / "spool.jsonl"), \
             mock.patch.object(wake_cli, "LOCK", Path(td) / "spool.lock"), \
             mock.patch.object(wake_cli, "LOG", Path(td) / "wake.log"), \
             mock.patch.object(wake_cli, "ensure_deadman"), \
             mock.patch.object(wake_cli, "dispatch") as dispatch, \
             mock.patch.object(wake_cli.subprocess, "run") as prime:
            prime.return_value.returncode = 0
            dispatch.return_value.returncode = 0
            dispatch.return_value.stdout = ""
            if body is not None:
                lane = Path(td) / "lane.md"
                lane.write_text(body)
                entry = {**entry, "prompt_file": str(lane)}
            (Path(td) / "spool.jsonl").write_text(wake_cli.json.dumps(entry) + "\n")
            wake_cli.cmd_fire(mock.Mock(id=entry["id"]))
        self.call = dispatch.call_args
        return dispatch.call_args.args[3]

    base = {"id": "w-bbbbbb", "kind": "wake", "at": "2026-01-01 00:00:00", "note": "n", "model": "m", "created": "x"}

    def test_a_lane_that_says_ready_goes_to_a_house_and_the_rest_stay(self):
        self.assertEqual(self.fire(self.base, "# Lane\n<!-- house: ready -->\nwork"), "house")
        self.assertEqual(self.fire(self.base, "# Lane\nwork"), "local")
        self.assertEqual(self.fire(self.base, "# Lane\n" + "x\n" * 40 + "<!-- house: ready -->\n"), "local")
        # An inline prompt (a nudge-watch, a follow-up) goes to a house unless it says it needs
        # loom's machine.
        self.assertEqual(self.fire({**self.base, "prompt": "nudge <!-- house: ready -->"}), "house")
        self.assertEqual(self.fire({**self.base, "prompt": "nudge"}), "house")
        self.assertEqual(self.fire({**self.base, "prompt": "ssh buildbox <!-- house: local -->"}), "local")

    def test_a_lane_file_goes_to_a_house_as_a_pointer_not_its_text(self):
        # loom-box takes a task of 8000 characters at most, and a lane can be longer:
        # handed its text, every run and retry would fail.
        self.fire(self.base, "# Lane\n<!-- house: ready -->\n" + "long lane text\n" * 1000)
        prompt = self.call.args[0]
        self.assertLess(len(prompt), 8000)
        self.assertNotIn("long lane text", prompt)
        self.assertIn("lane.md", prompt)
        # Fired on this machine, the lane's text is still the prompt.
        self.fire(self.base, "# Lane\nlocal lane text\n", mode="local")
        self.assertIn("local lane text", self.call.args[0])

    def test_a_lanes_slot_goes_with_its_house(self):
        self.assertEqual(wake_cli.house_slot({"prompt_file": "x"}, "# L\n<!-- house: ready -->\n<!-- house-slot: nightly -->\n"), "nightly")
        self.assertIsNone(wake_cli.house_slot({"prompt_file": "x"}, "# L\n<!-- house-slot: Bad Name -->\n"))
        self.assertIsNone(wake_cli.house_slot({"prompt": "<!-- house-slot: nightly -->"}, "<!-- house-slot: nightly -->"))
        with mock.patch.object(wake_cli.subprocess, "run") as run:
            wake_cli.dispatch("p", "m", {}, "house", "ada", "nightly")
        self.assertEqual(run.call_args.args[0][1:], ["box", "up", "--agent", "ada", "--slot", "nightly", "--task", "-"])

    def test_local_mode_ignores_the_marker(self):
        self.assertEqual(self.fire(self.base, "<!-- house: ready -->\n", mode="local"), "local")
        self.assertEqual(self.fire({**self.base, "prompt": "p"}, mode="house"), "house")


class Delivery(unittest.TestCase):
    """A wake for the agent's house is delivered through loom's `POST /api/loom/deliver` (agent-runtime.md,
    "Delivery"), with the clock's own token; WAKE_DELIVERY=spawn keeps the old path."""

    base = {"id": "w-cccccc", "kind": "wake", "at": "2026-01-01 00:00:00", "note": "t-9 round 2", "model": "m", "created": "x"}

    def fire(self, entry, answers, env=None, body=None, entries=None):
        """Fire `entry` with post_delivery answering `answers` in turn: (posts, dispatch mock, spool, log, alerts)."""
        posts, alerts = [], []
        answers = list(answers)
        with tempfile.TemporaryDirectory() as td:
            tok = Path(td) / "token"
            tok.write_text("clock-token\n")
            e = {"WAKE_FIRE": "mixed", "WAKE_DELIVER_TOKEN_FILE": str(tok), "WAKE_DELIVERY": "", **(env or {})}
            with mock.patch.dict(wake_cli.os.environ, e), \
                 mock.patch.object(wake_cli, "WAKE_DIR", Path(td)), \
                 mock.patch.object(wake_cli, "SPOOL", Path(td) / "spool.jsonl"), \
                 mock.patch.object(wake_cli, "LOCK", Path(td) / "spool.lock"), \
                 mock.patch.object(wake_cli, "LOG", Path(td) / "wake.log"), \
                 mock.patch.object(wake_cli, "SCHEDULES", Path(td) / "schedules.json"), \
                 mock.patch.object(wake_cli, "ensure_deadman"), \
                 mock.patch.object(wake_cli, "arm_timer", return_value="armed"), \
                 mock.patch.object(wake_cli, "alert", lambda m: alerts.append(m)), \
                 mock.patch.object(wake_cli, "post_delivery", lambda b: posts.append(json.loads(json.dumps(b))) or answers.pop(0)), \
                 mock.patch.object(wake_cli, "dispatch") as dispatch, \
                 mock.patch.object(wake_cli.subprocess, "run") as prime:
                prime.return_value.returncode = 0
                dispatch.return_value.returncode = 0
                dispatch.return_value.stdout = ""
                if body is not None:
                    lane = Path(td) / "nightly.md"
                    lane.write_text(body)
                    entry = {**entry, "prompt_file": str(lane)}
                (Path(td) / "spool.jsonl").write_text("".join(json.dumps(x) + "\n" for x in [entry, *(entries or [])]))
                try:
                    wake_cli.cmd_fire(mock.Mock(id=entry["id"]))
                except SystemExit:
                    pass
                spool = [json.loads(l) for l in (Path(td) / "spool.jsonl").read_text().splitlines()]
                logged = (Path(td) / "wake.log").read_text() if (Path(td) / "wake.log").exists() else ""
        return posts, dispatch, spool, logged, alerts

    ok = (200, {"conversation": "cv-0123456789ab", "seq": 1, "body": "queued", "reason": "its body is still starting"})

    def test_a_house_wake_is_delivered_not_started(self):
        posts, dispatch, spool, logged, _ = self.fire({**self.base, "prompt": "check PR 12"}, [self.ok])
        dispatch.assert_not_called()
        (b,) = posts
        self.assertEqual({k: b[k] for k in ("id", "agent", "venue", "place", "label", "from", "kind")},
                         {"id": "w-cccccc", "agent": "ada", "venue": "wake", "place": "wake:w-cccccc", "label": "t-9 round 2",
                          "from": {"kind": "schedule"}, "kind": "wake"})
        self.assertRegex(b["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.000Z$")
        # The preamble (the agent's own words, with this wake's id) leads the text; the prompt follows it.
        self.assertIn("(wake id w-cccccc, kind wake", b["text"])
        self.assertIn("in your house (loom's runtime delivered it)", b["text"])
        self.assertTrue(b["text"].endswith("check PR 12"))
        self.assertNotIn("model", b, "the runtime chooses the model")
        self.assertIn("fire | w-cccccc delivered: queued (its body is still starting), cv-0123456789ab seq 1", logged)
        self.assertEqual(spool, [], "delivered: nothing left to retry or watch")

    def test_a_lanes_wakes_share_one_conversation(self):
        posts, *_ = self.fire(self.base, [self.ok], body="# Lane\n<!-- house: ready -->\nwork")
        self.assertEqual(posts[0]["place"], "lane:nightly")
        self.assertIn("nightly.md", posts[0]["text"], "a lane file goes as a pointer, as to a house")
        posts, *_ = self.fire({**self.base, "kind": "schedule", "schedule": "knowledge-triage", "prompt": "the lane"}, [self.ok])
        self.assertEqual(posts[0]["place"], "lane:knowledge-triage")

    def test_failed_is_retried_later_as_a_new_delivery_in_the_same_conversation(self):
        failed = (200, {"conversation": "cv-0123456789ab", "seq": 1, "body": "failed", "reason": "no box may host ada"})
        posts, _, spool, logged, _ = self.fire({**self.base, "kind": "schedule", "schedule": "nightly", "prompt": "p"}, [failed])
        (r,) = spool
        self.assertNotEqual(r["id"], "w-cccccc")
        self.assertEqual((r["fails"], r["kind"], r["deliver_place"]), (1, "wake", "lane:nightly"))
        self.assertNotIn("delivery", r, "loom keeps a failed answer for the id: the retry is a new delivery")
        self.assertIn("no body took it: no box may host ada", logged)
        posts2, *_ = self.fire(r, [self.ok])
        self.assertEqual((posts2[0]["id"], posts2[0]["place"]), (r["id"], "lane:nightly"))

    def test_no_answer_or_a_5xx_is_retried_with_the_same_id_and_body(self):
        for answer in [(503, {"error": "unavailable: clock has too many deliveries in flight"}), (0, {"error": "loom didn't answer: refused"}), (429, {"error": "too-many"})]:
            posts, _, spool, logged, alerts = self.fire({**self.base, "prompt": "p"}, [answer])
            (r,) = spool
            self.assertEqual(r["delivery"], posts[0], answer)
            posts2, _, spool2, logged2, _ = self.fire(r, [self.ok])
            self.assertEqual(posts2[0], posts[0], "the same (source, id), with the same body: loom answers it once")
            self.assertIn(f"fire | {r['id']} delivered as w-cccccc: queued", logged2)
            self.assertEqual(spool2, [])

    def test_a_refusal_is_said_loudly_and_never_retried(self):
        for status in (400, 403, 409, 413):
            posts, _, spool, logged, alerts = self.fire({**self.base, "prompt": "p"}, [(status, {"error": "forbidden: clock doesn't declare that it delivers on core's wake"})])
            self.assertEqual(spool, [], status)
            self.assertIn(f"fire-refused | w-cccccc deliver REFUSED {status}", logged)
            self.assertEqual(len(alerts), 1)
            self.assertIn("not retried", alerts[0].lower())

    def test_spawn_is_one_setting_away(self):
        posts, dispatch, *_ = self.fire({**self.base, "prompt": "p"}, [], env={"WAKE_DELIVERY": "spawn"})
        self.assertEqual(posts, [])
        self.assertEqual(dispatch.call_args.args[3], "house")
        # Unset, with no token to deliver with: the old way.
        posts, dispatch, *_ = self.fire({**self.base, "prompt": "p"}, [], env={"WAKE_DELIVER_TOKEN_FILE": ""})
        self.assertEqual((posts, dispatch.call_args.args[3]), ([], "house"))
        # Unset, and loom has no piece offering delivery: this wake goes the old way.
        posts, dispatch, _, logged, _ = self.fire({**self.base, "prompt": "p"}, [(503, {"error": "unavailable: no installed piece offers delivery (the runtime), so nothing can reach a body"})])
        self.assertEqual((len(posts), dispatch.call_args.args[3]), (1, "house"))
        self.assertIn("in a fresh house (loom-box)", dispatch.call_args.args[0], "its words say where it really runs")
        self.assertIn("fire-spawn", logged)
        # Set to deliver, it never falls back.
        posts, dispatch, spool, *_ = self.fire({**self.base, "prompt": "p"}, [(503, {"error": "unavailable: no installed piece offers delivery"})], env={"WAKE_DELIVERY": "deliver"})
        dispatch.assert_not_called()
        self.assertEqual(len(spool), 1, "retried")

    def test_what_delivery_cant_carry_goes_the_old_way(self):
        # A wake that needs loom's machine, and a lane that keeps a house slot.
        posts, dispatch, *_ = self.fire({**self.base, "prompt": "ssh buildbox <!-- house: local -->"}, [])
        self.assertEqual((posts, dispatch.call_args.args[3]), ([], "local"))
        posts, dispatch, *_ = self.fire(self.base, [], body="# L\n<!-- house: ready -->\n<!-- house-slot: nightly -->\n")
        self.assertEqual((posts, dispatch.call_args.args[3], dispatch.call_args.args[5]), ([], "house", "nightly"))

    def test_a_local_wake_that_names_its_place_is_delivered_there(self):
        # t-1830: an alert's triage needs loom's machine, and its repeats one conversation (alert:<source>).
        e = {**self.base, "prompt": "<!-- house: local -->\ntriage the alert", "place": "alert:host-watch"}
        posts, dispatch, spool, logged, _ = self.fire(e, [self.ok])
        dispatch.assert_not_called()
        (b,) = posts
        self.assertEqual((b["place"], b["venue"], b["kind"]), ("alert:host-watch", "wake", "wake"))
        self.assertIn("<!-- house: local -->", b["text"], "the runtime reads the marker: this body runs on loom's machine")
        self.assertIn("on loom's machine (loom's runtime delivered it)", b["text"])
        self.assertEqual(spool, [])
        # A house wake with a place joins that conversation too.
        posts, *_ = self.fire({**self.base, "prompt": "p", "place": "alert:dns"}, [self.ok])
        self.assertEqual(posts[0]["place"], "alert:dns")

    def test_a_runtime_that_wont_run_it_here_leaves_a_session_here(self):
        e = {**self.base, "prompt": "<!-- house: local -->\ntriage", "place": "alert:host-watch"}
        posts, dispatch, spool, logged, alerts = self.fire(e, [(403, {"error": "forbidden: the runtime runs no local bodies for ada"})])
        self.assertEqual(len(posts), 1)
        self.assertEqual(dispatch.call_args.args[3], "local")
        self.assertIn("on loom's machine", dispatch.call_args.args[0])
        self.assertIn("fire-spawn", logged)
        self.assertEqual(alerts, [], "not a refusal to shout about: it ran, the old way")
        # Without a place, a local wake is never delivered.
        posts, dispatch, *_ = self.fire({**self.base, "prompt": "<!-- house: local -->\nx"}, [])
        self.assertEqual((posts, dispatch.call_args.args[3]), ([], "local"))

    def test_place_is_set_on_loom_machine_only_and_checked(self):
        def setp(place, env=None, slot=""):
            args = mock.Mock(prompt="p", prompt_file=None, note="n", model="m", slot=slot, approval="", place=place, park_on="")
            with tempfile.TemporaryDirectory() as td, \
                 mock.patch.dict(wake_cli.os.environ, env or {}), \
                 mock.patch.object(wake_cli, "SPOOL", Path(td) / "spool.jsonl"), \
                 mock.patch.object(wake_cli, "LOCK", Path(td) / "spool.lock"), \
                 mock.patch.object(wake_cli, "LOG", Path(td) / "wake.log"), \
                 mock.patch.object(wake_cli, "arm_timer", return_value="armed"):
                wake_cli.cmd_set(args, wake_cli.datetime.now())
                return [json.loads(l) for l in (Path(td) / "spool.jsonl").read_text().splitlines()]
        (e,) = setp("alert:host-watch")
        self.assertEqual(e["place"], "alert:host-watch")
        for bad in ("lane:nightly", "wake:w-1", "Alert:x", "alert:", "alert x", "alert:" + "x" * 200):
            with self.assertRaises(SystemExit, msg=bad):
                setp(bad)
        with self.assertRaises(SystemExit):
            setp("alert:x", env={"WAKE_ORIGIN": "house:art"})
        with self.assertRaises(SystemExit):
            setp("alert:x", slot="nightly")

    def test_the_post_itself(self):
        import http.server
        import threading
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers.get("authorization"), json.loads(self.rfile.read(int(self.headers["content-length"])))))
                code, out = (403, {"error": "forbidden: no"}) if seen[-1][2].get("id") == "deny" else (200, {"conversation": "cv-0123456789ab", "seq": 3, "body": "started"})
                data = json.dumps(out).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as td:
                (Path(td) / "t").write_text("tok-1\n")
                env = {"WAKE_DELIVER_TOKEN_FILE": str(Path(td) / "t"), "WAKE_LOOM_URL": f"http://127.0.0.1:{srv.server_port}/mcp"}
                with mock.patch.dict(wake_cli.os.environ, env):
                    self.assertEqual(wake_cli.post_delivery({"id": "w-1"}), (200, {"conversation": "cv-0123456789ab", "seq": 3, "body": "started"}))
                    self.assertEqual(wake_cli.post_delivery({"id": "deny"}), (403, {"error": "forbidden: no"}))
                self.assertEqual(seen[0], ("/api/loom/deliver", "Bearer tok-1", {"id": "w-1"}))
                with mock.patch.dict(wake_cli.os.environ, {**env, "WAKE_LOOM_URL": "http://127.0.0.1:1"}):
                    self.assertEqual(wake_cli.post_delivery({"id": "w-1"})[0], 0)
                with mock.patch.dict(wake_cli.os.environ, {**env, "WAKE_DELIVER_TOKEN_FILE": str(Path(td) / "none")}):
                    status, ans = wake_cli.post_delivery({"id": "w-1"})
                    self.assertEqual(status, 0)
                    self.assertNotIn("tok-1", ans["error"])
        finally:
            srv.shutdown()

    def test_the_piece_says_it_delivers_on_cores_wake(self):
        loom = json.loads((SCRIPT.parent.parent / "package.json").read_text())["loom"]
        self.assertEqual(loom["venues"], {"wake": {}})
        names = {x["name"] for x in loom["settings"]}
        self.assertTrue({"WAKE_DELIVERY", "WAKE_DELIVER_TOKEN_FILE", "WAKE_LOOM_URL"} <= names)
        self.assertTrue({"WAKE_DELIVERY", "WAKE_DELIVER_TOKEN_FILE", "WAKE_LOOM_URL"} <= set(wake_cli.TIMER_ENV))


class Clocks(unittest.TestCase):
    """Schedules, the cap and per-agent clocks. Everything in a temp dir:
    arm_timer/disarm_timer are stubs, and no path here is loom's machine's."""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        w = self.td / "clocks" / "ada"
        self.patches = [
            mock.patch.object(wake_cli, "WAKE_DIR", w), mock.patch.object(wake_cli, "SPOOL", w / "spool.jsonl"),
            mock.patch.object(wake_cli, "LOCK", w / "spool.lock"), mock.patch.object(wake_cli, "LOG", w / "wake.log"),
            mock.patch.object(wake_cli, "SCHEDULES", w / "schedules.json"), mock.patch.object(wake_cli, "CLOCKS", self.td / "clocks"),
            mock.patch.object(wake_cli, "PAUSED", w / "paused"),
            mock.patch.object(wake_cli, "AGENT", "ada"),
            mock.patch.object(wake_cli, "arm_timer", return_value="armed"), mock.patch.object(wake_cli, "disarm_timer"),
            mock.patch.dict(wake_cli.os.environ, {"WAKE_ORIGIN": ""}),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])
        self.lane = Path(wake_cli.REPO / "routines" / "nightly.md")

    def cli(self, *argv):
        with mock.patch.object(wake_cli.sys, "argv", ["wake", *argv]):
            try:
                wake_cli.main()
            except SystemExit as e:
                return str(e.code)
        return None

    def spool(self):
        f = self.td / "clocks" / "ada" / "spool.jsonl"
        return [wake_cli.json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def wake_log(self):
        f = self.td / "clocks" / "ada" / "wake.log"
        return f.read_text() if f.exists() else ""

    def test_a_schedule_keeps_one_occurrence_and_answers_a_lane_that_re_arms(self):
        self.assertIsNone(self.cli("schedule", "set", "nightly", "--at", "22:44", "-f", str(self.lane), "--note", "nightly"))
        sp = self.spool()
        self.assertEqual([(e["kind"], e["schedule"]) for e in sp], [("schedule", "nightly")])
        self.assertEqual(sp[0]["at"][11:16], "22:44")
        # The lane's own re-arm at its end: answered, nothing armed.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(self.cli("at", "22:44", "-f", str(self.lane), "--note", "recurring nightly"))
        self.assertIn("already scheduled: nightly", out.getvalue())
        self.assertEqual(len(self.spool()), 1)
        # One far from the next occurrence is a real extra run, and goes in.
        self.assertIsNone(self.cli("in", "2d", "-f", str(self.lane), "--note", "an extra pass"))
        self.assertEqual(sorted(e["kind"] for e in self.spool()), ["schedule", "wake"])
        self.cli("cancel", [e for e in self.spool() if e["kind"] == "wake"][0]["id"])
        # Editing is setting again: still one occurrence, at the new time.
        self.cli("schedule", "set", "nightly", "--at", "21:30", "--days", "mon-fri", "-f", str(self.lane))
        sp = self.spool()
        self.assertEqual(len(sp), 1)
        self.assertEqual(sp[0]["at"][11:16], "21:30")
        self.assertIn(wake_cli.datetime.strptime(sp[0]["at"], wake_cli.TIME_FMT).weekday(), range(5))
        # Removing it leaves the dead-man, so there is always a next wake.
        self.cli("schedule", "rm", "nightly")
        self.assertEqual([e["kind"] for e in self.spool()], ["deadman"])

    def test_setting_a_schedule_adopts_the_lanes_pending_one_off(self):
        self.cli("at", "22:44", "-f", str(self.lane), "--note", "recurring nightly ~22:44 nightly")
        self.assertEqual([e["kind"] for e in self.spool()], ["wake"])
        self.cli("schedule", "set", "nightly", "--at", "22:44", "-f", str(self.lane))
        self.assertEqual([e["kind"] for e in self.spool()], ["schedule"])

    def test_firing_an_occurrence_arms_the_next_first_and_a_retry_is_a_one_off(self):
        self.cli("schedule", "set", "nightly", "--at", "22:44", "-f", str(self.lane))
        first = self.spool()[0]
        with mock.patch.object(wake_cli.subprocess, "run") as prime, mock.patch.object(wake_cli, "dispatch") as dispatch:
            prime.return_value.returncode = 0
            dispatch.return_value.returncode = 1
            dispatch.return_value.stderr = "boom"
            with self.assertRaises(SystemExit):
                wake_cli.cmd_fire(mock.Mock(id=first["id"]))
        kinds = sorted((e["kind"], e.get("schedule")) for e in self.spool())
        self.assertEqual(kinds, [("schedule", "nightly"), ("wake", None)], "the next occurrence, and the failed one's retry")

    def test_an_occurrence_whose_slot_is_busy_is_skipped_not_retried(self):
        # 2026-10-28: Bo's heartbeat (every 15 minutes in its window) found its own first
        # run still holding the slot, and each occurrence started a retry chain: 7 alerts.
        self.cli("schedule", "set", "nightly", "--at", "22:44", "-f", str(self.lane))
        first = self.spool()[0]
        alerts = []
        with mock.patch.object(wake_cli.subprocess, "run") as prime, mock.patch.object(wake_cli, "dispatch") as dispatch, \
             mock.patch.object(wake_cli, "alert", side_effect=alerts.append):
            prime.return_value.returncode = 0
            dispatch.return_value.returncode = 1
            dispatch.return_value.stderr = "loom box: h-161 holds bo's lanes slot; one house at a time"
            wake_cli.cmd_fire(mock.Mock(id=first["id"]))
        self.assertEqual([(e["kind"], e.get("schedule")) for e in self.spool() if e["kind"] != "deadman"],
                         [("schedule", "nightly")], "the next occurrence only")
        self.assertEqual(alerts, [])
        self.assertIn("fire-busy", self.wake_log())

    def test_a_one_off_whose_slot_is_busy_still_waits_its_turn(self):
        self.cli("in", "1h", "-p", "nudge", "--slot", "lanes", "--note", "t-1 round 1")
        [e] = [e for e in self.spool() if e["kind"] == "wake"]
        with mock.patch.object(wake_cli.subprocess, "run") as prime, mock.patch.object(wake_cli, "dispatch") as dispatch:
            prime.return_value.returncode = 0
            dispatch.return_value.returncode = 1
            dispatch.return_value.stderr = "loom box: h-9 holds ada's lanes slot; one house at a time"
            with self.assertRaises(SystemExit):
                wake_cli.cmd_fire(mock.Mock(id=e["id"]))
        [retry] = [e for e in self.spool() if e["kind"] == "wake"]
        self.assertEqual(retry["fails"], 1)

    def test_a_body_parks_on_a_request_and_its_answer_wakes_the_continuation(self):
        # Parked rather than polling for a grant: no body runs until the answer, and the continuation picks up.
        self.assertIn("park on", self.cli("park", "--on", "nope", "-p", "go on", "--note", "t-1 r2") or "")
        self.assertIsNone(self.cli("park", "--on", "q-1a2b3c4d", "-p", "push the fix", "--note", "t-1 r2"))
        [e] = [e for e in self.spool() if e["kind"] == "parked"]
        self.assertEqual(e["on"], "q-1a2b3c4d")
        fired = []
        def fire(state):
            [e] = [e for e in self.spool() if e["kind"] == "parked"]
            with mock.patch.object(wake_cli.subprocess, "run") as prime, mock.patch.object(wake_cli, "dispatch") as dispatch, \
                 mock.patch.object(wake_cli, "request_state", return_value=state):
                prime.return_value.returncode = 0
                dispatch.return_value.returncode = 0
                dispatch.return_value.stdout = ""
                wake_cli.cmd_fire(mock.Mock(id=e["id"]))
                fired.extend(c.args[0] for c in dispatch.call_args_list)
            return e["id"]
        # Waiting: checked again later, and no body runs.
        first = fire("pending")
        [again] = [e for e in self.spool() if e["kind"] == "parked"]
        self.assertNotEqual(again["id"], first)
        self.assertEqual(fired, [])
        # Answered: the continuation runs, told the answer, and nothing stays parked.
        fire("granted")
        self.assertEqual([e for e in self.spool() if e["kind"] == "parked"], [])
        self.assertEqual(len(fired), 1)
        self.assertIn("The owner allowed q-1a2b3c4d", fired[0])
        self.assertIn("push the fix", fired[0])
        # A no says so; a day without an answer says that.
        self.cli("park", "--on", "a-1a2b3c4d", "-p", "teardown", "--note", "t-2 r1")
        fire("denied")
        self.assertIn("a-1a2b3c4d: denied", fired[-1])
        self.cli("park", "--on", "a-2a2b3c4d", "-p", "teardown", "--note", "t-2 r2")
        [p] = [e for e in self.spool() if e["kind"] == "parked"]
        lines = [l for l in (self.td / "clocks" / "ada" / "spool.jsonl").read_text().splitlines()]
        (self.td / "clocks" / "ada" / "spool.jsonl").write_text("\n".join(
            l.replace(p["until"], "2000-01-01 00:00:00") for l in lines) + "\n")
        fire("pending")
        self.assertIn("No answer to a-2a2b3c4d", fired[-1])

    def test_a_resumed_wakes_note_says_the_answer_in_words_once(self):
        # Was "unparked (a-24e194de allowed): DNS health check" on loom's pages.
        def resumed(state, note="DNS health check"):
            e = {"on": "a-24e194de", "prompt": "go", "note": note}
            wake_cli.unpark(e, state)
            return e
        e = resumed("allowed")
        self.assertEqual(e["note"], "Resumed after your OK: DNS health check")
        self.assertEqual((e["on"], e["answer"]), ("a-24e194de", "allowed"))
        self.assertIn("The owner allowed a-24e194de", e["prompt"])  # the body still gets the id
        self.assertEqual(resumed("granted")["note"], "Resumed after your OK: DNS health check")
        self.assertEqual(resumed("denied")["note"], "Resumed after you said no: DNS health check")
        self.assertEqual(resumed("pending")["note"], "Resumed, no answer in 24h: DNS health check")
        # Resumed twice (or once under the old wording): the answer is said once, the latest.
        self.assertEqual(resumed("denied", e["note"])["note"], "Resumed after you said no: DNS health check")
        self.assertEqual(resumed("allowed", "unparked (a-1 denied): DNS health check")["note"],
                         "Resumed after your OK: DNS health check")
        self.assertEqual(resumed("weird", "Resumed after your answer (odd): x")["note"], "Resumed after your answer (weird): x")
        for note in ("DNS health check", "t-2 r1", ""):
            self.assertNotIn("a-24e194de", resumed("allowed", note)["note"])

    def test_a_wake_that_keeps_failing_backs_off_says_so_and_gives_up(self):
        # Not failing every 15 minutes forever with nobody hearing of it.
        self.cli("in", "1h", "-p", "nudge", "--note", "t-1 round 1")
        alerts, gaps = [], []
        with mock.patch.object(wake_cli.subprocess, "run") as prime, mock.patch.object(wake_cli, "dispatch") as dispatch, \
             mock.patch.object(wake_cli, "alert", side_effect=alerts.append):
            prime.return_value.returncode = 0
            dispatch.return_value.returncode = 1
            dispatch.return_value.stderr = "boom"
            for _ in range(wake_cli.RETRY_GIVE_UP):
                [e] = [e for e in self.spool() if e["kind"] == "wake"]
                before = wake_cli.datetime.now()
                with self.assertRaises(SystemExit):
                    wake_cli.cmd_fire(mock.Mock(id=e["id"]))
                left = [e for e in self.spool() if e["kind"] == "wake"]
                if left:
                    gaps.append(round((wake_cli.datetime.strptime(left[0]["at"], wake_cli.TIME_FMT) - before).total_seconds() / 60))
        self.assertEqual(gaps, [15, 30, 60, 120, 240])
        self.assertEqual([e["kind"] for e in self.spool()], ["deadman"], "given up: no retry left")
        self.assertEqual(len(alerts), 2, alerts)
        self.assertIn("3 times", alerts[0])
        self.assertIn("t-1 round 1", alerts[0])
        self.assertIn("boom", alerts[0])
        self.assertIn("gave up", alerts[1])

    def test_cancelling_an_occurrence_skips_just_that_one(self):
        self.cli("schedule", "set", "nightly", "--at", "22:44", "-f", str(self.lane))
        first = self.spool()[0]
        self.cli("cancel", first["id"])
        sp = self.spool()
        self.assertEqual(len(sp), 1)
        self.assertGreater(sp[0]["at"], first["at"])

    def test_at_most_fifty_one_off_wakes(self):
        for i in range(wake_cli.MAX_PENDING):
            self.assertIsNone(self.cli("in", f"{i + 1}h", "-p", f"nudge {i}", "--note", f"t-1 round {i}"))
        self.assertIn("50 one-off wakes pending", self.cli("in", "99h", "-p", "one more", "--note", "t-1 round 99") or "")
        # A schedule isn't a one-off: still allowed.
        self.assertIsNone(self.cli("schedule", "set", "nightly", "--at", "22:44", "-f", str(self.lane)))

    def test_another_agents_clock_is_its_own_and_its_lanes_are_in_its_repo(self):
        self.assertIsNone(self.cli("--agent", "bo", "in", "2h", "-f", "routines/x.md", "--note", "bo t-9"))
        self.assertEqual(self.spool(), [], "nothing on ada's clock")
        bo = self.td / "clocks" / "bo" / "spool.jsonl"
        e = [wake_cli.json.loads(l) for l in bo.read_text().splitlines()]
        self.assertEqual((e[0]["lane"], "prompt_file" in e[0]), ("routines/x.md", False))
        self.assertIn("bo", wake_cli.clock_agents())

    def test_a_slot_rides_a_wake_and_a_schedule_into_its_house(self):
        self.cli("--agent", "bo", "schedule", "set", "heartbeat", "--at", "03:07,03:22", "-p", "run: /home/bo/bin/heartbeat", "--slot", "lanes")
        bo = self.td / "clocks" / "bo" / "spool.jsonl"
        [e] = [wake_cli.json.loads(l) for l in bo.read_text().splitlines()]
        self.assertEqual((e["slot"], e["kind"]), ("lanes", "schedule"))
        self.assertIn("slot", wake_cli.load_schedules()["heartbeat"])
        self.assertIn("--slot", self.cli("in", "1h", "-p", "x", "--note", "t-1", "--slot", "Bad Slot") or "--slot")
        wake_cli.configure("ada")

    def test_the_api_scopes_an_agent_to_its_own_clock(self):
        seen = []

        def run(argv, origin, agent):
            seen.append((agent, argv))
            return 0, "", ""
        ask = lambda caller, argv: wake_cli.api_answer({"caller": caller, "path": "run", "body": {"argv": argv}}, run)
        self.assertEqual(ask({"kind": "agent", "agent": "ada"}, ["schedule", "list"])["status"], 200)
        self.assertEqual(ask({"kind": "agent", "agent": "ada"}, ["--agent", "bo", "list"])["status"], 403)
        self.assertEqual(ask({"kind": "agent", "agent": "ada"}, ["list", "--agent=bo"])["status"], 403)
        self.assertEqual(ask({"kind": "owner"}, ["--agent", "bo", "list"])["status"], 200)
        self.assertEqual(seen, [("ada", ["schedule", "list"]), ("bo", ["list"])])


class PauseAndNow(Clocks):
    """Pause holds what comes due (each fires once on resume); Wake now runs even while paused."""

    def fire(self, wid):
        with mock.patch.object(wake_cli.subprocess, "run") as prime, mock.patch.object(wake_cli, "dispatch") as dispatch:
            prime.return_value.returncode = 0
            dispatch.return_value.returncode = 0
            dispatch.return_value.stdout = "backgrounded"
            wake_cli.cmd_fire(mock.Mock(id=wid))
        return dispatch.call_count

    def test_a_wake_that_comes_due_while_paused_is_held_then_fires_once_on_resume(self):
        self.cli("in", "1h", "-p", "nudge", "--note", "t-1 round 1")
        [e] = self.spool()
        self.cli("pause")
        self.assertEqual(self.fire(e["id"]), 0, "nothing dispatched while paused")
        [held] = self.spool()
        self.assertTrue(held["held"])
        self.cli("resume")
        [back] = self.spool()
        self.assertNotIn("held", back)
        self.assertNotEqual(back["id"], e["id"], "a fresh id: the old timer fired")
        self.assertEqual(self.fire(back["id"]), 1)

    def test_wake_now_runs_even_while_paused(self):
        self.cli("pause")
        self.cli("now")
        [e] = [e for e in self.spool() if e["kind"] == "wake"]
        self.assertIn("Woken now", e["prompt"])
        self.assertEqual(self.fire(e["id"]), 1)

    def test_the_page_and_its_actions(self):
        self.cli("schedule", "set", "nightly", "--at", "22:44", "-f", str(self.lane), "--note", "nightly")
        self.cli("pause")
        with mock.patch.dict(wake_cli.os.environ, {"LOOM_SCOPE": "ada"}):
            view = wake_cli.ui_view()
        page = wake_cli.ui_page(view, "", scope="ada")
        hero = page["sections"][0]["blocks"]
        self.assertEqual((hero[0]["type"], hero[0]["tone"], hero[0]["text"]), ("health", "warn", "Ada's clock is paused"))
        # The clock's switch says it's off, and Wake now is the panel's one action.
        [switch] = hero[1]["rows"]
        self.assertEqual((switch["value"], switch["toggle"]), ("Paused", {"action": "clock", "on": False, "fields": {"agent": "ada"}}))
        self.assertEqual(hero[1]["action"]["label"], "Wake now")
        sched = page["sections"][1]["blocks"][0]["rows"][0]
        self.assertEqual((sched["label"], sched["value"], sched["page"]), ("Nightly", "10:44 PM, every day", "schedule/ada/nightly"))
        self.assertEqual(wake_cli.ui_page(view, "schedule/ada/nightly")["title"], "Nightly")
        # The household's Clock is loom's own: its lanes, and a link to each agent's clock, no agent's schedule.
        home = wake_cli.ui_page(view, "")
        self.assertEqual([x["id"] for x in home["sections"]], ["loom-lanes", "agents"])
        [link] = home["sections"][1]["blocks"][1]["rows"]
        self.assertEqual((link["page"], link["value"]), ("agent:ada", "Paused"))
        m = wake_cli.ui_manifest(view)
        self.assertEqual((m["tab"], m["agentTab"]), ({"label": "Clock", "kind": "setting"}, {"label": "Clock"}))
        self.assertEqual(m["tile"]["page"], "agent:ada")
        self.assertNotIn("its-lanes", [x["id"] for x in wake_cli.ui_page(view, "", lanes=False, scope="ada")["sections"]],
                         "an agent owner never sees the household's lanes")
        # Home's card: paused is unavailable, and says why.
        self.assertEqual(wake_cli.ui_manifest(view)["agents"]["ada"]["state"], "unavailable")
        self.assertTrue(wake_cli.ui_manifest(view)["agents"]["ada"]["reason"].startswith("its clock is paused"))
        self.assertEqual(wake_cli.ui_page(view, "../etc")["title"], "Not found")
        calls = []
        run = lambda agent, argv: (calls.append((agent, argv)), (0, "", ""))[1]
        out = wake_cli.ui_act({"action": "schedule-rm", "fields": {"agent": "ada", "name": "nightly"}}, run)
        self.assertEqual(out["undo"]["fields"]["lane"], "routines/nightly.md")
        out = wake_cli.ui_act({"action": "schedule-set", "fields": {"agent": "ada", "name": "x", "at": "09:30", "lane": "/etc/passwd"}}, run)
        self.assertFalse(out["ok"], "a lane is a file in routines/ or briefs/")
        self.assertFalse(wake_cli.ui_act({"action": "now", "fields": {"agent": "../x"}}, run)["ok"])
        self.assertEqual(calls, [("ada", ["schedule", "rm", "nightly"])])

    def test_home_card_shows_the_next_wake(self):
        self.cli("schedule", "set", "nightly", "--at", "22:44", "-f", str(self.lane), "--note", "nightly")
        with mock.patch.dict(wake_cli.os.environ, {"LOOM_SCOPE": "ada"}):
            view = wake_cli.ui_view()
        [r] = wake_cli.ui_manifest(view)["agents"]["ada"]["readings"]
        self.assertEqual(r["label"], "Next")
        self.assertRegex(r["value"], r", (Tomorrow )?10:44 PM$")
        self.assertTrue(r["detail"].startswith("in "))
        self.assertNotIn("state", wake_cli.ui_manifest(view)["agents"]["ada"])

    def test_run_now_keeps_the_schedules_origin(self):
        with mock.patch.dict(wake_cli.os.environ, {"WAKE_ORIGIN": "agent:ada"}):
            self.cli("schedule", "set", "nightly", "--at", "22:44", "-f", str(self.lane))
        self.cli("schedule", "run", "nightly")
        [now] = [e for e in self.spool() if e["kind"] == "wake"]
        self.assertEqual((now["origin"], now["prompt_file"], "schedule" in now), ("agent:ada", str(self.lane), False))
        self.assertIn("unknown", self.cli("schedule", "run", "unknown") or "")

    def test_an_agent_running_the_owners_schedule_is_marked_as_its_own(self):
        self.cli("schedule", "set", "nightly", "--at", "22:44", "-f", str(self.lane))
        with mock.patch.dict(wake_cli.os.environ, {"WAKE_ORIGIN": "agent:ada"}):
            self.cli("schedule", "run", "nightly")
        [now] = [e for e in self.spool() if e["kind"] == "wake"]
        self.assertEqual(now["origin"], "agent:ada")

    def test_times_read_like_the_board(self):
        now = wake_cli.datetime(2026, 10, 26, 9, 16)
        self.assertEqual(wake_cli.when_text({"at": "09:30", "days": "mon-fri"}), "Weekdays 9:30 AM")
        self.assertEqual(wake_cli.when_text({"at": "18:00", "days": "sun"}), "Sun 6:00 PM")
        self.assertEqual(wake_cli.day_time(wake_cli.datetime(2026, 10, 25, 21, 0), now), "Yesterday 9:00 PM")
        self.assertEqual(wake_cli.until_text(wake_cli.datetime(2026, 10, 26, 9, 30), now), "in 14 minutes")

    def test_recent_fires_carry_what_they_were(self):
        lines = ["2026-10-26 08:00:00 | set | w-aaaaaa at 2026-10-26 08:12:00 note='t-1 round 2'",
                 "2026-10-26 08:12:01 | fire | w-aaaaaa backgrounded · 1234",
                 "2026-10-26 09:00:00 | fire-fail | w-bbbbbb rc=1 boom — retry 1 as w-cccccc 2026-10-26 09:15:00"]
        got = [(w, ev, what) for _, w, ev, what, _ in wake_cli.recent_fires(lines)]
        self.assertEqual(got, [("w-aaaaaa", "fire", "t-1 round 2"), ("w-bbbbbb", "fire-fail", "")])


class Relay(unittest.TestCase):
    """Relays moved to loom's runtime (loom docs/specs/agent-runtime.md, "Agent-to-agent relay"): `wake relay`
    and `wake relays` set nothing and say where they went, and the clock keeps no relay log."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.hh = self.root / "household.json"
        self.hh.write_text(wake_cli.json.dumps({"agents": ["ada", "bo"], "owner": "pat", "relay": {"ada": ["bo"]}}))

    def tearDown(self):
        self.td.cleanup()

    def run_verb(self, argv, agent="ada"):
        clocks = self.root / "clocks"
        with mock.patch.object(wake_cli, "HOUSEHOLD", self.hh), \
             mock.patch.object(wake_cli, "CLOCKS", clocks), \
             mock.patch.object(wake_cli, "arm_timer", return_value="armed"), \
             mock.patch.object(wake_cli.sys, "argv", ["wake", "--agent", agent, *argv]), \
             contextlib.redirect_stdout(io.StringIO()):
            try:
                wake_cli.main()
                err = None
            except SystemExit as e:
                err = str(e.code) if e.code not in (None, 0) else None
        return err, clocks

    def test_relay_and_relays_set_nothing_and_point_at_the_runtime(self):
        for argv in (["relay", "--to", "bo", "--say", "Can you review loom#250?", "--why", "x"], ["relay"], ["relays"], ["relays", "--thread", "r-12345678"]):
            err, clocks = self.run_verb(argv)
            self.assertIn("relays moved to loom's runtime", err or "", argv)
            self.assertIn("loom runtime relay", err)
            self.assertFalse((clocks / "bo" / "spool.jsonl").exists(), argv)
        self.assertFalse(hasattr(wake_cli, "RELAYS"))
        self.assertFalse(hasattr(wake_cli, "relay_notices"))

    def test_the_clock_api_lets_an_agent_relay_as_itself(self):
        seen = []
        def run(a, origin, agent):
            seen.append((a, agent))
            return 0, "", ""
        r = wake_cli.api_answer({"caller": {"kind": "agent", "agent": "bo"}, "path": "run",
                                 "body": {"argv": ["relay", "--to", "ada", "--say", "hi"]}}, run)
        with mock.patch.dict(wake_cli.os.environ, {"WAKE_AGENTS": "ada,bo"}):
            r = wake_cli.api_answer({"caller": {"kind": "agent", "agent": "bo"}, "path": "run",
                                     "body": {"argv": ["relay", "--to", "ada", "--say", "hi"]}}, run)
        self.assertEqual(r["status"], 200)
        self.assertEqual(seen[-1], (["relay", "--to", "ada", "--say", "hi"], "bo"))


class HouseStartWatch(unittest.TestCase):
    """A wake handed to loom-box is checked on later. A house that
    failed before it ever started (a credential it couldn't get, a state it
    couldn't restore) is the wake's own failure: retried with back-off and
    alerted like a dispatch failure. One that started is the lane's business."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.d = Path(self.td.name)
        self.alerts = []

    def tearDown(self):
        self.td.cleanup()

    def spool(self):
        p = self.d / "spool.jsonl"
        return [wake_cli.json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []

    def fire(self, entry, house=None, dispatch_out="h-55: ada on loom (wanted)\n"):
        (self.d / "spool.jsonl").write_text(wake_cli.json.dumps(entry) + "\n")
        with mock.patch.dict(wake_cli.os.environ, {"WAKE_FIRE": "house"}), \
             mock.patch.object(wake_cli, "WAKE_DIR", self.d), \
             mock.patch.object(wake_cli, "SPOOL", self.d / "spool.jsonl"), \
             mock.patch.object(wake_cli, "LOCK", self.d / "spool.lock"), \
             mock.patch.object(wake_cli, "LOG", self.d / "wake.log"), \
             mock.patch.object(wake_cli, "arm_timer", return_value="armed"), \
             mock.patch.object(wake_cli, "disarm_timer"), \
             mock.patch.object(wake_cli, "alert", side_effect=self.alerts.append), \
             mock.patch.object(wake_cli, "house_record", return_value=house), \
             mock.patch.object(wake_cli, "dispatch") as dispatch, \
             mock.patch.object(wake_cli.subprocess, "run") as prime:
            prime.return_value.returncode = 0
            dispatch.return_value.returncode = 0
            dispatch.return_value.stdout = dispatch_out
            dispatch.return_value.stderr = ""
            try:
                wake_cli.cmd_fire(mock.Mock(id=entry["id"]))
            except SystemExit:
                pass
        return self.spool()

    WAKE = {"id": "w-aaaaaa", "kind": "wake", "at": "2026-01-01 00:00:00", "note": "t-1 round 1",
            "prompt": "do it", "model": "m", "created": "x"}

    def test_a_house_wake_leaves_a_watch_on_its_house(self):
        (w,) = [e for e in self.fire(dict(self.WAKE)) if e["kind"] == "watch"]
        self.assertEqual(w["house"], "h-55")
        self.assertEqual(w["wake"]["note"], "t-1 round 1")
        # Was "watch h-55 for t-1 round 1": the house is in its own field, not the words.
        self.assertEqual(w["note"], "Checking that its house started: t-1 round 1")

    def test_a_watch_on_a_schedule_occurrence_names_the_schedule_in_words(self):
        # Was "watch h-423 for schedule plan-post: post the plan" on the Clock.
        wake = dict(self.WAKE, kind="schedule", schedule="plan-post", note="schedule plan-post: post the plan")
        with mock.patch.object(wake_cli, "SPOOL", self.d / "spool.jsonl"), \
             mock.patch.object(wake_cli, "LOCK", self.d / "spool.lock"), \
             mock.patch.object(wake_cli, "arm_timer"):
            wake_cli.arm_watch("h-423", wake, 0)
        [w] = [e for e in self.spool() if e["kind"] == "watch"]
        self.assertEqual((w["house"], w["note"]), ("h-423", "Checking that its house started: Plan post (schedule): post the plan"))
        self.assertGreater(wake_cli.datetime.strptime(w["at"], wake_cli.TIME_FMT), wake_cli.datetime.now())

    def watch(self, house, checks=0):
        return {"id": "w-bbbbbb", "kind": "watch", "at": "2026-01-01 00:10:00", "house": "h-55", "checks": checks,
                "note": "Checking that its house started: t-1 round 1", "created": "x", "wake": dict(self.WAKE)}

    def test_a_house_that_never_started_is_retried_like_a_dispatch_failure(self):
        house = {"id": "h-55", "state": "failed", "log": "the box couldn't start it: can't issue anthropic for ada: HTTPError"}
        (w,) = self.fire(self.watch(house), house)
        self.assertEqual((w["kind"], w["note"], w["fails"]), ("wake", "t-1 round 1", 1))
        self.assertIn("start-fail", (self.d / "wake.log").read_text())

    def test_a_house_that_started_is_left_to_its_lane(self):
        for house in ({"id": "h-55", "state": "done", "started": "t", "exit": 0},
                      {"id": "h-55", "state": "failed", "started": "t", "exit": 1},
                      {"id": "h-55", "state": "running", "started": "t"}):
            self.assertEqual([e for e in self.fire(self.watch(house), house) if e["kind"] != "deadman"], [], house)
        self.assertEqual(self.alerts, [])

    def test_a_house_still_waiting_is_checked_again_then_said_once(self):
        house = {"id": "h-55", "state": "wanted"}
        (w,) = [e for e in self.fire(self.watch(house), house) if e["kind"] != "deadman"]
        self.assertEqual((w["kind"], w["checks"]), ("watch", 1))
        left = [e for e in self.fire(self.watch(house, checks=wake_cli.WATCH_CHECKS - 1), house) if e["kind"] != "deadman"]
        self.assertEqual(left, [])
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("h-55", self.alerts[0])

    def test_a_house_loom_box_no_longer_knows_is_let_go(self):
        self.assertEqual([e for e in self.fire(self.watch(None), None) if e["kind"] != "deadman"], [])


class EveryClock(unittest.TestCase):
    """Every agent's clock, not just Ada's: re-armed at boot (wake-rearm.service
    runs a plain `wake rearm`), and carried in loom's household bundle as the
    clock piece's part (`loom-clock bundle export|import`)."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        for d in ("clocks/ada", "clocks/bo"):
            (self.root / d).mkdir(parents=True)
        self.ada, self.bo = self.root / "clocks" / "ada", self.root / "clocks" / "bo"
        w = lambda d, i: {"id": i, "kind": "wake", "at": "2030-01-01 00:00:00", "note": "n", "prompt": "p", "model": "m", "created": "x"}
        (self.ada / "spool.jsonl").write_text(wake_cli.json.dumps(w(self.ada, "w-aaaaaa")) + "\n")
        (self.bo / "spool.jsonl").write_text(wake_cli.json.dumps(w(self.bo, "w-bbbbbb")) + "\n")
        (self.bo / "schedules.json").write_text(wake_cli.json.dumps({"heartbeat": {"at": "03:07", "prompt": "run: x"}}))
        (self.bo / "paused").write_text("{}")

    def tearDown(self):
        self.td.cleanup()
        wake_cli.configure("ada")  # the clock functions switch the module's clock; put Ada's back

    def main(self, argv, env=None, stdin=""):
        armed = []
        with mock.patch.object(wake_cli, "CLOCKS", self.root / "clocks"), \
             mock.patch.object(wake_cli, "FIRE_CONF", self.root / "fire"), \
             mock.patch.object(wake_cli, "arm_timer", side_effect=lambda e: armed.append((wake_cli.AGENT, e["id"])) or "armed"), \
             mock.patch.dict(wake_cli.os.environ, env or {}), \
             mock.patch.object(wake_cli.sys, "stdin", io.StringIO(stdin)), \
             mock.patch.object(wake_cli.sys, "argv", ["wake", *argv]), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            try:
                wake_cli.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), armed

    def test_a_plain_rearm_rearms_every_clock(self):
        code, _, armed = self.main(["rearm"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(a for a in armed if a[1] in ("w-aaaaaa", "w-bbbbbb")), [("ada", "w-aaaaaa"), ("bo", "w-bbbbbb")])
        _, _, armed = self.main(["--agent", "bo", "rearm"])
        self.assertEqual([a for a in armed if a[1] == "w-aaaaaa"], [], "an --agent rearm is that clock's only")

    @unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen"), "needs age")
    def test_the_bundle_part_round_trips_every_clock_and_rearms_them(self):
        key = self.root / "owner.key"
        r = subprocess.run(["age-keygen", "-o", str(key)], capture_output=True, text=True)
        recipient = re.search(r"age1[0-9a-z]+", r.stderr + r.stdout).group(0)
        (self.root / "fire").write_text("mixed\n")
        code, part, _ = self.main(["bundle", "export"], {"LOOM_EXPORT_RECIPIENTS": recipient})
        self.assertEqual(code, 0)
        self.assertTrue(part.startswith("-----BEGIN AGE ENCRYPTED FILE-----"))
        self.assertNotIn("heartbeat", part)
        # A clock that's already here is never written over without --force.
        code, _, _ = self.main(["bundle", "import"], {"LOOM_IMPORT_IDENTITY": str(key)}, part)
        self.assertNotEqual(code, 0)
        # Onto a fresh machine: nothing there yet.
        shutil.rmtree(self.root / "clocks"); (self.root / "fire").unlink()
        code, _, armed = self.main(["bundle", "import"], {"LOOM_IMPORT_IDENTITY": str(key)}, part)
        self.assertEqual(code, 0)
        self.assertIn(("bo", "w-bbbbbb"), armed)
        self.assertIn(("ada", "w-aaaaaa"), armed)
        self.assertEqual(wake_cli.json.loads((self.bo / "schedules.json").read_text())["heartbeat"]["at"], "03:07")
        self.assertTrue((self.bo / "paused").exists())
        # Relays are the runtime's: the clock's part carries no relay log, and writes none.
        self.assertFalse((self.root / "relays.jsonl").exists())
        # A restore comes up quiet: every clock paused (the old machine may still be up, and the
        # restored one holds live credentials) until the owner resumes it.
        held = wake_cli.json.loads((self.ada / "paused").read_text())
        self.assertEqual(held["by"], "restore")
        # The clock's mode comes along (without it, a restored clock would fall back to local).
        self.assertEqual((self.root / "fire").read_text().strip(), "mixed")

    def test_export_never_writes_in_the_clear(self):
        code, out, _ = self.main(["bundle", "export"], {"LOOM_EXPORT_RECIPIENTS": ""})
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")


class LoomLanes(EveryClock):
    """Household work on a schedule, assigned to an agent; its schedule lives in the assignee's clock."""

    def lane_main(self, argv, env=None):
        hh = self.root / "household.json"
        hh.write_text(wake_cli.json.dumps({"agents": ["ada", "bo"]}))
        with mock.patch.object(wake_cli, "LANES", self.root / "clocks" / "lanes.json"), mock.patch.object(wake_cli, "HOUSEHOLD", hh), \
             mock.patch.object(wake_cli, "disarm_timer"):
            return self.main(argv, env)

    def scheds(self, who):
        f = (self.ada if who == "ada" else self.bo) / "schedules.json"
        return wake_cli.json.loads(f.read_text()) if f.exists() else {}

    def test_set_assign_text_rm(self):
        code, out, _ = self.lane_main(["lane", "set", "verify-pages", "--agent", "ada", "--at", "04:17", "-p", "Verify the commons' pages."])
        self.assertEqual(code, 0, out)
        sc = self.scheds("ada")["verify-pages"]
        self.assertTrue(sc["loom_lane"])
        self.assertTrue(sc["prompt"].startswith("This is a loom lane: household work on a schedule (\"verify-pages\"), assigned to you"))
        self.assertTrue(sc["prompt"].endswith("Verify the commons' pages."))
        # Reassigned: off Ada's clock, onto Bo's, same text and time.
        code, out, armed = self.lane_main(["lane", "assign", "verify-pages", "--agent", "bo"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("verify-pages", self.scheds("ada"))
        self.assertEqual(self.scheds("bo")["verify-pages"]["at"], "04:17")
        self.assertIn(("bo", [e for e in armed if e[0] == "bo"][-1][1]), armed)
        # The text: the assignee may read it through the API; another agent may not.
        code, out, _ = self.lane_main(["lane", "text", "verify-pages"], env={"WAKE_ORIGIN": "agent:bo"})
        self.assertEqual((code, out), (0, "Verify the commons' pages.\n"))
        code, _, _ = self.lane_main(["lane", "text", "verify-pages"], env={"WAKE_ORIGIN": "agent:ada"})
        self.assertIn("isn't assigned to you", str(code))
        code, _, _ = self.lane_main(["lane", "assign", "verify-pages", "--agent", "ada"], env={"WAKE_ORIGIN": "agent:ada"})
        self.assertIn("the owner's to set", str(code))
        code, out, _ = self.lane_main(["lane", "rm", "verify-pages"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("verify-pages", self.scheds("bo"))

    def test_a_lane_from_a_file_answers_its_own_re_arm_and_replaces_a_pending_one(self):
        lane = self.root / "verify.md"
        lane.write_text("Verify.")
        # The lane, before it was a loom lane, armed its own next run.
        code, _, _ = self.lane_main(["at", "2030-01-02 04:17", "-f", str(lane), "--note", "re-arm yourself"])
        self.assertEqual(code, 0)
        code, out, _ = self.lane_main(["lane", "set", "verify-pages", "--agent", "ada", "--at", "04:17", "-f", str(lane)])
        self.assertEqual(code, 0, out)
        sp = [wake_cli.json.loads(l) for l in (self.ada / "spool.jsonl").read_text().splitlines()]
        self.assertEqual([e for e in sp if e.get("prompt_file") == str(lane)], [], "the one-off is gone")
        self.assertEqual(self.scheds("ada")["verify-pages"]["lane_file"], str(lane.resolve()))
        code, out, _ = self.lane_main(["at", "04:17", "-f", str(lane)])
        self.assertIn("already scheduled: verify-pages", out)

    def test_a_long_lane_points_at_its_text_and_a_local_one_runs_on_loom_s_machine(self):
        code, out, _ = self.lane_main(["lane", "set", "expand-pages", "--agent", "ada", "--at", "05:17", "--local", "-p", "x" * 9000])
        self.assertEqual(code, 0, out)
        p = self.scheds("ada")["expand-pages"]["prompt"]
        self.assertTrue(p.startswith("<!-- house: local -->\n"))
        self.assertIn("`wake lane text expand-pages`", p)
        self.assertLess(len(p), 1000)
        code, out, _ = self.lane_main(["lane", "set", "verify-pages", "--agent", "ada", "--at", "04:17", "--slot", "verify-pages", "-p", "v"])
        self.assertEqual(self.scheds("ada")["verify-pages"]["slot"], "verify-pages", "its house slot comes with it")
        code, out, _ = self.lane_main(["lane", "set", "nope", "--agent", "zed", "--at", "05:17", "-p", "x"])
        self.assertIn("assign a loom lane to an agent of this household", str(code))


class LoomLanesOnThePage(LoomLanes):
    """The lanes are managed on loom's Clock page: new, settings, text, whose, remove."""

    def run_act(self, action, fields):
        calls = []
        def run(agent, argv):
            calls.append(argv)
            code, out, _ = self.lane_main(argv)
            return (0 if code in (0, None) else 1), out, "" if code in (0, None) else str(code)
        with mock.patch.object(wake_cli, "LANES", self.root / "clocks" / "lanes.json"), \
             mock.patch.object(wake_cli, "HOUSEHOLD", self.root / "household.json"):
            r = wake_cli.ui_act({"action": action, "fields": fields}, run)
        return r, calls

    def test_new_edit_text_remove(self):
        r, calls = self.run_act("lane-new", {"name": "directory-upkeep", "to": "bo", "at": "06:05", "days": "mon-fri", "where": "house", "note": "people cards", "text": "Keep the directory tidy."})
        self.assertTrue(r["ok"], r)
        self.assertEqual(calls[0][:4], ["lane", "set", "directory-upkeep", "--agent"])
        self.assertIn("--no-local", calls[0])
        self.assertEqual(self.scheds("bo")["directory-upkeep"]["days"], "mon-fri")
        r, _ = self.run_act("lane-new", {"name": "directory-upkeep", "to": "bo", "at": "06:05", "text": "x"})
        self.assertFalse(r["ok"], "a name that's taken")
        r, _ = self.run_act("lane-edit", {"name": "directory-upkeep", "agent": "bo", "to": "ada", "at": "07:15", "days": "", "where": "local", "note": "people cards"})
        self.assertTrue(r["ok"], r)
        self.assertNotIn("directory-upkeep", self.scheds("bo"))
        sc = self.scheds("ada")["directory-upkeep"]
        self.assertEqual((sc["at"], sc.get("days")), ("07:15", None))
        self.assertTrue(sc["prompt"].startswith("<!-- house: local -->"))
        r, _ = self.run_act("lane-text", {"name": "directory-upkeep", "agent": "ada", "text": "Keep it tidier."})
        self.assertTrue(r["ok"], r)
        self.assertTrue(self.scheds("ada")["directory-upkeep"]["prompt"].endswith("Keep it tidier."))
        with mock.patch.object(wake_cli, "LANES", self.root / "clocks" / "lanes.json"), mock.patch.object(wake_cli, "HOUSEHOLD", self.root / "household.json"):
            page = wake_cli.ui_page([], "lane/directory-upkeep")
        self.assertEqual([x["id"] for x in page["sections"]], ["settings", "text", "remove"])
        rows = {r["label"]: r for r in page["sections"][0]["blocks"][0]["rows"]}
        self.assertEqual(list(rows), ["When", "Where", "Whose", "What it's for"])
        self.assertEqual(page["sections"][1]["blocks"][0]["rows"][1]["edit"]["action"], "lane-text")
        self.assertEqual(page["sections"][1]["blocks"][0]["rows"][0]["sheet"]["blocks"][0]["text"], "Keep it tidier.")
        r, _ = self.run_act("lane-rm", {"name": "directory-upkeep", "agent": "ada"})
        self.assertTrue(r["ok"], r)
        self.assertNotIn("directory-upkeep", self.scheds("ada"))


class LanePanel(LoomLanesOnThePage):
    """A lane's page is a panel (Jonathan, 2026-11-03: "settings become rows that state the current value in a
    sentence; tapping a row opens a sheet with one control"): each row's sheet or switch posts only what it sets."""

    def setUp(self):
        super().setUp()
        r, _ = self.run_act("lane-new", {"name": "upkeep", "to": "bo", "at": "06:05", "days": "mon,tue,wed,thu,fri",
                                         "days_chips": "1", "where": "house", "note": "people cards", "text": "Keep it tidy."})
        self.assertTrue(r["ok"], r)

    def test_days_come_as_chips_and_the_old_form_still_works(self):
        # The day chips post the ticked days; the clock keeps the short form.
        self.assertEqual(self.scheds("bo")["upkeep"]["days"], "mon-fri")
        r, calls = self.run_act("lane-edit", {"name": "upkeep", "agent": "bo", "at": "07:15", "days": "mon,wed,fri", "days_chips": "1"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["message"], "Upkeep is Bo's, 7:15 AM, Mon, Wed and Fri, in a house.")
        self.assertNotIn("--agent", calls[0], "a When sheet sets the time and days, nothing else")
        self.assertNotIn("--no-local", calls[0])
        sc = self.scheds("bo")["upkeep"]
        self.assertEqual((sc["at"], sc["days"]), ("07:15", "mon,wed,fri"))
        self.assertTrue(sc["prompt"].endswith("Keep it tidy."), "the text stays")
        # Every day ticked is every day; none ticked is refused, not read as every day.
        r, _ = self.run_act("lane-edit", {"name": "upkeep", "agent": "bo", "at": "07:15", "days": ",".join(wake_cli.DAYS), "days_chips": "1"})
        self.assertNotIn("days", self.scheds("bo")["upkeep"])
        r, calls = self.run_act("lane-edit", {"name": "upkeep", "agent": "bo", "at": "07:15", "days": "", "days_chips": "1"})
        self.assertEqual((r["ok"], r["message"], calls), (False, "Pick at least one day.", []))
        # The old free-text form: mon-fri, sat,sun, empty for daily.
        r, _ = self.run_act("lane-edit", {"name": "upkeep", "agent": "bo", "at": "07:15", "days": "sat,sun"})
        self.assertEqual(self.scheds("bo")["upkeep"]["days"], "sat,sun")
        r, calls = self.run_act("lane-edit", {"name": "upkeep", "agent": "bo", "days": "someday"})
        self.assertFalse(r["ok"])
        self.assertEqual(calls, [])

    def test_where_and_whose_switch_at_once(self):
        r, calls = self.run_act("lane-edit", {"name": "upkeep", "agent": "bo", "where": "local"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(calls[0], ["lane", "set", "upkeep", "--local"])
        self.assertTrue(self.scheds("bo")["upkeep"]["prompt"].startswith("<!-- house: local -->"))
        self.assertEqual(self.scheds("bo")["upkeep"]["at"], "06:05", "the time stays")
        r, calls = self.run_act("lane-edit", {"name": "upkeep", "agent": "bo", "to": "ada"})
        self.assertTrue(r["ok"], r)
        self.assertNotIn("upkeep", self.scheds("bo"))
        self.assertEqual(self.scheds("ada")["upkeep"]["days"], "mon-fri")
        r, calls = self.run_act("lane-edit", {"name": "upkeep", "agent": "ada", "note": "the directory"})
        self.assertEqual((r["ok"], r["message"]), (True, "Upkeep: what it's for is saved."))
        r, _ = self.run_act("lane-edit", {"name": "nope", "agent": "ada", "where": "local"})
        self.assertFalse(r["ok"])

    def test_run_now_runs_it_on_its_agents_clock(self):
        calls = []
        run = lambda agent, argv: (calls.append((agent, argv)), (0, "", ""))[1]
        with mock.patch.object(wake_cli, "LANES", self.root / "clocks" / "lanes.json"):
            r = wake_cli.ui_act({"action": "lane-run", "fields": {"name": "upkeep", "agent": "ada"}}, run)
        self.assertEqual(calls, [("bo", ["schedule", "run", "upkeep", "--note", "from loom"])])
        self.assertEqual(r["message"], "Upkeep runs within a minute, in a house, as Bo.")

    def test_the_page_reads_as_rows(self):
        with mock.patch.object(wake_cli, "LANES", self.root / "clocks" / "lanes.json"), \
             mock.patch.object(wake_cli, "HOUSEHOLD", self.root / "household.json"):
            page = wake_cli.ui_page([], "lane/upkeep")
            home = wake_cli.ui_page([], "")
        panel = page["sections"][0]["blocks"][0]
        rows = {r["label"]: r for r in panel["rows"]}
        self.assertEqual(rows["When"]["value"], "6:05 AM, weekdays")
        days = rows["When"]["edit"]["fields"][1]
        self.assertEqual((days["kind"], days["value"], len(days["options"])), ("checks", "mon,tue,wed,thu,fri", 7))
        self.assertEqual(rows["Where"]["choice"]["value"], "house")
        self.assertEqual([o["label"] for o in rows["Where"]["choice"]["options"]], ["In a house", "On loom's machine"])
        self.assertEqual((rows["Whose"]["value"], rows["Whose"]["choice"]["name"]), ("Bo", "to"))
        self.assertEqual(rows["What it's for"]["value"], "people cards")
        self.assertEqual(panel["action"]["action"], "lane-run")
        for r in panel["rows"]:
            self.assertLessEqual(len({"toggle", "choice", "edit", "sheet", "act", "page"} & set(r)), 1, r)
        [lane, new] = home["sections"][0]["blocks"]
        self.assertEqual(lane["rows"][0]["value"], "Bo · 6:05 AM, weekdays")
        self.assertEqual(new["rows"][0]["edit"]["action"], "lane-new")


class AppWords(unittest.TestCase):
    """What a clock says, in words: days, times, and what a wake is for without its raw ids."""

    def test_days_both_ways(self):
        for given, kept in (("", ""), ("mon-fri", "mon-fri"), ("mon,tue,wed,thu,fri", "mon-fri"), ("sun,sat", "sat,sun"),
                            ("mon,tue,wed,thu,fri,sat,sun", ""), ("fri,mon,wed", "mon,wed,fri"), ("Sat-Sun", "sat,sun")):
            self.assertEqual(wake_cli.days_spec(given), kept, given)
        with self.assertRaises(ValueError):
            wake_cli.days_spec("", chips=True)
        with self.assertRaises(ValueError):
            wake_cli.days_spec("weekdays")
        self.assertEqual(wake_cli.days_chips(""), "mon,tue,wed,thu,fri,sat,sun")
        self.assertEqual(wake_cli.days_chips("mon-fri"), "mon,tue,wed,thu,fri")

    def test_when_in_a_sentence(self):
        w = wake_cli.when_words
        self.assertEqual(w({"at": "05:17"}), "5:17 AM, every day")
        self.assertEqual(w({"at": "05:17", "days": "mon-fri"}), "5:17 AM, weekdays")
        self.assertEqual(w({"at": "18:00", "days": "sun"}), "6:00 PM, Sundays")
        self.assertEqual(w({"at": ["07:17", "19:17"], "days": "mon,wed,fri"}), "7:17 AM and 7:17 PM, Mon, Wed and Fri")

    def test_a_wake_says_what_its_for_and_keeps_its_ids_aside(self):
        w = wake_cli.wake_words
        # before: "t-1652 PR38 nudge-watch round 22"
        self.assertEqual(w("t-1652 PR38 nudge-watch round 22"), ("Checking a pull request is moving, round 22", ["t-1652", "PR 38"]))
        self.assertEqual(w("t-1340/t-1673 PR57 nudge-watch round 5")[1], ["t-1340", "t-1673", "PR 57"])
        self.assertEqual(w("t-1674 PR385 review-watch round 21")[0], "Watching for a review, round 21")
        self.assertEqual(w("t-1765: wunder swap 86% recurrence investigation"), ("Wunder swap 86% recurrence investigation", ["t-1765"]))
        self.assertEqual(w("t-40 routine 24h dead-man after t-40 reply, round 7")[0], "Safety wake, in case nothing else wakes it, round 7")
        self.assertEqual(w("making: discontinuity-mud session 27"), ("Making: discontinuity-mud session 27", []))
        self.assertEqual(w("Plan (schedule)"), ("Plan (schedule)", []))
        self.assertEqual(w(""), ("", []))

    def test_a_wakes_page_keeps_its_ids_in_its_details(self):
        now = wake_cli.datetime(2026, 10, 30, 8, 0)
        e = {"id": "w-abcdef", "kind": "wake", "at": "2026-10-30 08:39:00", "created": "2026-10-30 07:00:00",
             "note": "t-1652 PR38 nudge-watch round 22", "prompt": "Check PR 38."}
        view = [{"agent": "ada", "schedules": {}, "entries": [e], "recent": [], "paused": None}]
        page = wake_cli.ui_page(view, "wake/ada/w-abcdef", now=now)
        self.assertEqual(page["title"], "One-off wake")
        rows = {r["label"]: r for r in page["sections"][0]["blocks"][0]["rows"]}
        self.assertEqual(rows["What it's for"]["value"], "Checking a pull request is moving, round 22")
        self.assertEqual(rows["When"]["value"], "8:39 AM, in 39 minutes")
        self.assertNotIn("w-abcdef", wake_cli.json.dumps({k: v for k, v in rows.items() if k != "Details"}))
        self.assertIn("w-abcdef", wake_cli.json.dumps(rows["Details"]["sheet"]))
        row = wake_cli.oneoff_blocks(view[0], now)[1]["items"][0]
        self.assertEqual((row["text"], row["meta"]), ("Checking a pull request is moving, round 22", "8:39 AM · t-1652"))

    def test_the_clock_switch_pauses_and_resumes(self):
        calls = []
        run = lambda agent, argv: (calls.append(argv), (0, "", ""))[1]
        off = wake_cli.ui_act({"action": "clock", "fields": {"agent": "ada", "on": ""}}, run)
        on = wake_cli.ui_act({"action": "clock", "fields": {"agent": "ada", "on": "on"}}, run)
        self.assertEqual(calls, [["pause"], ["resume"]])
        self.assertTrue(off["ok"] and on["ok"])

    def test_a_schedules_when_sheet_keeps_its_lane_note_and_model(self):
        calls = []
        run = lambda agent, argv: (calls.append(argv), (0, "", ""))[1]
        with mock.patch.object(wake_cli, "lane_file", return_value=Path("/x")):
            r = wake_cli.ui_act({"action": "schedule-set", "fields": {
                "agent": "ada", "name": "groom", "at": "17:47", "days": "sun", "days_chips": "1", "lane": "routines/groom.md",
                "note": "weekly", "model": "claude-opus-5"}}, run)
        self.assertEqual(calls, [["schedule", "set", "groom", "--at", "17:47", "-f", "routines/groom.md", "--days", "sun",
                                  "--note", "weekly", "--model", "claude-opus-5"]])
        self.assertEqual(r["message"], "Groom is scheduled: Sun 5:47 PM.")


class PlainWords(unittest.TestCase):
    """The labels the Clock piece writes around a wake read as words (2026-10-30 plain-words sweep);
    a note, a lane's prompt and a schedule's own text are shown as written."""

    def setUp(self):
        self.now = wake_cli.datetime(2026, 10, 30, 8, 0)

    def entry(self, **kw):
        return {"id": "w-abcdef", "kind": "wake", "at": "2026-10-30 08:50:00", "created": "2026-10-30 07:00:00", **kw}

    def test_a_schedule_occurrence_is_named_in_words_and_its_note_stays(self):
        e = wake_cli.schedule_entry("plan-prepare", {"at": "08:50", "prompt": "x", "note": "task 7 from Pat"},
                                    wake_cli.datetime(2026, 10, 30, 8, 50))
        # before: "schedule plan-prepare: task 7 from Pat"
        self.assertEqual(wake_cli.what_of(e), "Plan prepare (schedule): task 7 from Pat")
        e2 = wake_cli.schedule_entry("expand-pages", {"at": "05:17", "prompt": "x"}, wake_cli.datetime(2026, 10, 31, 5, 17))
        self.assertEqual(wake_cli.what_of(e2), "Expand pages (schedule)")
        # an agent's own note is untouched, ids and all
        self.assertEqual(wake_cli.what_of(self.entry(note="routine dead-man rearm, round after w-ea33ba")),
                         "routine dead-man rearm, round after w-ea33ba")
        # before: "monthly-review" (a lane file's stem)
        self.assertEqual(wake_cli.what_of(self.entry(prompt_file="/srv/agent/routines/monthly-review.md")),
                         "Monthly review")

    def test_next_reads_next_at_a_12_hour_time(self):
        at = wake_cli.datetime(2026, 10, 30, 8, 50)
        self.assertEqual(wake_cli.next_at(at, self.now), "Next at 8:50 AM")
        self.assertEqual(wake_cli.next_at(wake_cli.datetime(2026, 10, 31, 3, 13), self.now), "Next tomorrow at 3:13 AM")
        e = wake_cli.schedule_entry("plan-prepare", {"at": "08:50", "prompt": "x"}, at)
        view = [{"agent": "ada", "schedules": {}, "entries": [e], "recent": [], "paused": None}]
        with mock.patch.object(wake_cli, "load_lanes", return_value={}):
            home = wake_cli.ui_page(view, "", now=self.now)
        [link] = home["sections"][-1]["blocks"][1]["rows"]
        # before: "next: 8:50 AM, schedule plan-prepare"
        self.assertEqual((link["value"], link["detail"]), ("Next at 8:50 AM", "Plan prepare"))
        # before: tile "Ada next: schedule plan-prepare"
        tile = wake_cli.ui_manifest(view, now=self.now)["tile"]
        self.assertEqual(tile["detail"], "Ada: Plan prepare")
        self.assertEqual(tile["value"], "8:50 AM")

    def test_the_tile_carries_a_wakes_words_to_three_lines_not_forty_characters(self):
        words = "Checking a pull request is moving, round 6 of the nudge-watch on the bench repro"
        view = [{"agent": "ada", "schedules": {}, "entries": [self.entry(note=words)], "recent": [], "paused": None}]
        detail = wake_cli.ui_manifest(view, now=self.now)["tile"]["detail"]
        # before: "Ada: Checking a pull request is moving, roun…" (cut at 40)
        self.assertEqual(detail, f"Ada: {wake_cli.brief_of(view[0]['entries'][0])}")
        self.assertNotIn("…", detail)
        long = view[0]["entries"][0] | {"note": "x " * 200}
        self.assertLessEqual(len(wake_cli.ui_manifest([view[0] | {"entries": [long]}], now=self.now)["tile"]["detail"]), len("Ada: ") + wake_cli.TILE_BRIEF)

    def test_the_tile_and_card_skip_house_checks_and_name_a_schedule_alone(self):
        sched = wake_cli.schedule_entry("plan-retro", {"at": "23:17", "prompt": "x", "note": "task 7 from Pat"},
                                        wake_cli.datetime(2026, 10, 30, 23, 17))
        # A house check is the clock's own bookkeeping ("Checking that its house started: …"), not what the agent does next.
        watch = self.entry(kind="watch", at="2026-10-30 09:00:00", note="Checking that its house started: t-20 …")
        view = [{"agent": "ada", "schedules": {}, "entries": [watch, sched], "recent": [], "paused": None}]
        m = wake_cli.ui_manifest(view, now=self.now)
        # before: value "9:00 AM", detail "Ada's next: Checking that its house started: t-20…"
        self.assertEqual((m["tile"]["value"], m["tile"]["detail"]), ("11:17 PM", "Ada: Plan retro"))
        [r] = m["agents"]["ada"]["readings"]
        # before: "Plan retro (schedule): task 7 from …, 11:17 PM"
        self.assertRegex(r["value"], r"^Plan retro, ")
        # Another day says which.
        tomorrow = dict(sched, at="2026-10-31 03:13:00")
        view[0]["entries"] = [tomorrow]
        self.assertEqual(wake_cli.ui_manifest(view, now=self.now)["tile"]["value"], "Tomorrow 3:13 AM")
        # Only house checks pending: the agent has nothing next.
        view[0]["entries"] = [watch]
        m = wake_cli.ui_manifest(view)
        self.assertEqual(m["tile"]["value"], "Nothing")
        self.assertEqual(m["agents"]["ada"]["readings"][0]["value"], "nothing pending")
        # The Clock page's top and the household's link to it, too.
        with mock.patch.object(wake_cli, "load_lanes", return_value={}):
            [link] = wake_cli.ui_page(view, "", now=self.now)["sections"][-1]["blocks"][1]["rows"]
        self.assertEqual(link["value"], "Nothing pending")
        self.assertEqual(wake_cli.hero(view[0], self.now)[0]["text"], "Nothing pending")

    def test_times_are_12_hour_everywhere(self):
        # before: "03:13 daily", "04:17 daily" (lane_when built from the raw 24-hour text)
        self.assertEqual(wake_cli.lane_when({"at": "03:13"}), "3:13 AM daily")
        self.assertEqual(wake_cli.lane_when({"at": ["07:17", "19:17"], "days": "mon-fri"}), "Weekdays 7:17 AM, 7:17 PM")
        self.assertEqual(wake_cli.when_text({"at": "07:17,19:17"}), "7:17 AM, 7:17 PM daily")
        self.assertEqual(wake_cli.when_text({"at": "soon"}), "soon daily", "a time it can't read is shown as written")
        with mock.patch.object(wake_cli, "load_lanes", return_value={"expand-pages": {"agent": "ada", "at": "05:17", "text": "x"}}), \
             mock.patch.object(wake_cli, "household_agents", return_value=["ada"]):
            page = wake_cli.ui_page([], "lane/expand-pages")
            rows = wake_cli.lane_blocks()[0]["rows"]
        self.assertEqual(page["intro"], "A loom lane: the household's work, run by Ada.")
        self.assertEqual(page["title"], "Expand pages")
        # before: "Ada · 05:17 daily"; then "Ada · 5:17 AM daily" in a list's meta
        self.assertEqual((rows[0]["label"], rows[0]["value"]), ("Expand pages", "Ada · 5:17 AM, every day"))

    def test_the_schedule_form_hint_and_lane_picker(self):
        c = {"agent": "ada", "schedules": {}, "entries": [], "recent": [], "paused": None}
        with mock.patch.object(wake_cli, "lanes", return_value=["routines/expand-pages.md"]):
            form = wake_cli.schedule_blocks(c, self.now)[-1]["rows"][-1]["edit"]
        fields = {f["name"]: f for f in form["fields"]}
        # A time picker now, not a text box with a format hint (TimePicker below).
        self.assertEqual((fields["at"]["kind"], fields["at"]["multiple"]), ("time", True))
        # before: label "expand-pages"; the value stays the file path
        self.assertEqual(fields["lane"]["options"], [{"value": "routines/expand-pages.md", "label": "Expand pages"}])
        lf = {f["name"]: f for f in wake_cli.lane_fields({}, ["ada"])}
        self.assertEqual((lf["at"]["kind"], lf["at"]["multiple"]), ("time", True))

    def test_a_house_id_never_shows_and_an_id_never_leads(self):
        when = wake_cli.datetime(2026, 10, 30, 8, 1)
        c = {"recent": [(when, "w-2bd487", "fire", "", "w-2bd487 h-400: ada on loom (wanted)"),
                        (when, "w-111111", "fire", "routine dead-man rearm, round after w-ea33ba", "w-111111 backgrounded · 1")]}
        a, b = wake_cli.recent_blocks(c, self.now)[0]["items"][::-1]
        # before: text "w-2bd487", meta "8:01 AM · house h-400"
        self.assertEqual((a["text"], a["meta"]), ("A wake", "8:01 AM · in a house"))
        # a note reads in words; its ids go to its detail (Jonathan, 2026-11-03: "raw ids never show unless asked")
        self.assertEqual(b["text"], "Safety wake, in case nothing else wakes it")
        oneoff = wake_cli.oneoff_blocks({"agent": "ada", "entries": [self.entry(prompt="")]}, self.now)[1]["items"][0]
        self.assertEqual(oneoff["text"], "A wake")

    def test_the_actions_say_the_time_in_words(self):
        run = lambda agent, argv: (0, "", "")
        with mock.patch.object(wake_cli, "lane_file", return_value=Path("/x")):
            out = wake_cli.ui_act({"action": "schedule-set", "fields": {"agent": "ada", "name": "nightly", "at": "22:44", "lane": "routines/x.md"}}, run)
        # before: "Nightly is scheduled: 22:44."
        self.assertEqual(out["message"], "Nightly is scheduled: 10:44 PM daily.")


class TimePicker(unittest.TestCase):
    """A schedule's and a lane's time are loom's `time` field: the device's own picker (2026-10-30)."""

    def test_times_go_as_hh_mm_several_allowed(self):
        f = wake_cli.time_field("at", "When", "5:17,19:17")
        self.assertEqual((f["kind"], f["multiple"], f["value"]), ("time", True, "05:17,19:17"))
        self.assertNotIn("value", wake_cli.time_field("at", "Time", ""))

    def test_a_time_it_cannot_read_stays_a_text_box_so_the_page_still_draws(self):
        f = wake_cli.time_field("at", "When", "noonish")
        self.assertEqual((f["kind"], f["value"]), ("text", "noonish"))
        self.assertEqual(wake_cli.time_field("at", "When", "24:00")["kind"], "text")


class HouseForwarding(unittest.TestCase):
    """In a house, the clock is loom's piece: main() hands the call over (and nothing here runs)."""

    def test_main_forwards_to_the_clock_piece_in_a_house(self):
        with tempfile.TemporaryDirectory() as d:
            tok = Path(d) / "token"
            tok.write_text("tok-123\n")
            sent = []

            class Answer(io.BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            def urlopen(req, timeout):
                sent.append((req.full_url, req.headers.get("Authorization"), json.loads(req.data)))
                return Answer(json.dumps({"exit": 3, "stdout": "w-abcdef  …\n", "stderr": ""}).encode())
            env = {"HOUSE_ID": "h-1", "LOOM_URL": "http://loom.test:8787/mcp", "LOOM_TOKEN_FILE": str(tok)}
            out = io.StringIO()
            with mock.patch.dict(wake_cli.os.environ, env), mock.patch.object(wake_cli.urllib.request, "urlopen", urlopen), \
                 mock.patch.object(wake_cli.sys, "argv", ["wake", "list"]), mock.patch.object(wake_cli, "Spool", side_effect=AssertionError("ran here")), \
                 contextlib.redirect_stdout(out):
                with self.assertRaises(SystemExit) as e:
                    wake_cli.main()
        self.assertEqual(e.exception.code, 3, "the piece's own exit code")
        self.assertEqual(sent, [("http://loom.test:8787/api/clock/run", "Bearer tok-123", {"argv": ["list"]})])
        self.assertEqual(out.getvalue(), "w-abcdef  …\n")

    def test_without_loom_s_address_it_says_so(self):
        with mock.patch.dict(wake_cli.os.environ, {"HOUSE_ID": "h-1"}), mock.patch.object(wake_cli.sys, "argv", ["wake", "list"]):
            for k in ("LOOM_URL", "LOOM_TOKEN_FILE"):
                wake_cli.os.environ.pop(k, None)
            with self.assertRaises(SystemExit) as e:
                wake_cli.main()
        self.assertIn("LOOM_URL", str(e.exception.code))


class Settings(unittest.TestCase):
    """What an instance sets, and what a timer carries so the fired run reads the same clocks."""

    def load(self, env, script=SCRIPT):
        keep = {k: os.environ[k] for k in ("PATH",) if k in os.environ}
        with mock.patch.dict(os.environ, {**keep, **env}, clear=True):
            m = importlib.util.module_from_spec(importlib.util.spec_from_loader("x", loader=None))
            m.__file__ = str(script)
            exec(compile(SCRIPT.read_text(), str(script), "exec"), m.__dict__)
            return m

    def test_defaults_without_loom(self):
        m = self.load({"HOME": "/home/someone"})
        self.assertEqual((m.LOCAL_AGENT, m.CLOCKS, m.REPO), ("agent", Path("/home/someone/.local/state/loom-clock"), Path("/home/someone")))
        self.assertEqual(m.HOUSEHOLD, Path("/home/someone/.config/loom/household.json"))

    def test_the_piece_home_loom_gives_it(self):
        m = self.load({"HOME": "/h", "LOOM_PIECE_HOME": "/h/.config/loom/pieces/clock", "WAKE_LOCAL_AGENT": "zed"})
        self.assertEqual((m.CLOCKS, m.WAKE_DIR, m.LANES), (Path("/h/.config/loom/pieces/clock/data"),
                                                           Path("/h/.config/loom/pieces/clock/data/zed"),
                                                           Path("/h/.config/loom/pieces/clock/data/lanes.json")))

    def test_a_direct_run_of_a_managed_install_finds_its_home_settings_and_stable_link(self):
        with tempfile.TemporaryDirectory() as d:
            pieces = Path(d) / "pieces"
            home = pieces / "clock"
            pkg = home / "versions" / ("a" * 40) / "node_modules" / "loom-clock" / "bin"
            pkg.mkdir(parents=True)
            (home / "piece.json").write_text("{}")
            (home / "settings.json").write_text(json.dumps({"WAKE_LOCAL_AGENT": "zed", "WAKE_REPO": d}))
            (pkg / "wake").write_text("")
            (pieces / "bin").mkdir()
            (pieces / "bin" / "loom-clock").symlink_to(pkg / "wake")
            m = self.load({"HOME": d}, script=pkg / "wake")
            self.assertEqual((m.PIECE_HOME, m.CLOCKS, m.LOCAL_AGENT, m.REPO), (home, home / "data", "zed", Path(d)))
            # A timer runs loom's link, which outlives this version.
            self.assertEqual(m.stable_self(), pieces / "bin" / "loom-clock")

    def test_a_timer_carries_the_settings_it_was_armed_with(self):
        calls = []
        with mock.patch.dict(wake_cli.os.environ, {"WAKE_FIRE": "mixed", "WAKE_ORIGIN": "agent:ada"}), \
             mock.patch.object(wake_cli.subprocess, "run", side_effect=lambda cmd, **kw: calls.append(cmd) or _completed(0)):
            self.assertEqual(wake_cli.arm_timer({"id": "w-aaaaaa", "at": "2099-01-01 00:00:00"}), "armed")
        cmd = calls[0]
        envs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--setenv"]
        self.assertIn(f"WAKE_DATA={wake_cli.os.environ['WAKE_DATA']}", envs)
        self.assertIn("WAKE_FIRE=mixed", envs)
        self.assertFalse(any(e.startswith("WAKE_ORIGIN=") for e in envs), "who asked is the entry's, not the timer's")
        self.assertEqual(cmd[-4:], ["--agent", wake_cli.AGENT, "fire", "w-aaaaaa"])
        self.assertIn(f"WorkingDirectory={wake_cli.REPO}", cmd)

    def test_an_alert_goes_to_the_alert_command_and_the_log(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(wake_cli, "LOG", Path(d) / "wake.log"), \
             mock.patch.object(wake_cli, "WAKE_DIR", Path(d)), \
             mock.patch.dict(wake_cli.os.environ, {"WAKE_ALERT_COMMAND": '["notify", "--to", "owner"]'}), \
             mock.patch.object(wake_cli.subprocess, "run") as run:
            wake_cli.alert("wake: it failed")
            self.assertIn("alert | wake: it failed", (Path(d) / "wake.log").read_text())
        self.assertEqual(run.call_args.args[0], ["notify", "--to", "owner", "wake: it failed"])
        with mock.patch.object(wake_cli, "log"), mock.patch.object(wake_cli.subprocess, "run") as run:
            wake_cli.alert("no command")
        run.assert_not_called()

    def test_the_fire_gate_defers_a_standby(self):
        entry = {"id": "w-aaaaaa", "kind": "wake", "at": "2026-01-01 00:00:00", "note": "n", "prompt": "p", "model": "m", "created": "x"}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(wake_cli.os.environ, {"WAKE_FIRE_GATE": "false"}), \
             mock.patch.object(wake_cli, "WAKE_DIR", Path(td)), mock.patch.object(wake_cli, "SPOOL", Path(td) / "spool.jsonl"), \
             mock.patch.object(wake_cli, "LOCK", Path(td) / "spool.lock"), mock.patch.object(wake_cli, "LOG", Path(td) / "wake.log"), \
             mock.patch.object(wake_cli, "arm_timer", return_value="armed"), mock.patch.object(wake_cli, "dispatch") as dispatch:
            (Path(td) / "spool.jsonl").write_text(json.dumps(entry) + "\n")
            wake_cli.cmd_fire(mock.Mock(id="w-aaaaaa"))
            [e] = [json.loads(l) for l in (Path(td) / "spool.jsonl").read_text().splitlines()]
        dispatch.assert_not_called()
        self.assertNotEqual(e["id"], "w-aaaaaa")
        self.assertGreater(wake_cli.datetime.strptime(e["at"], wake_cli.TIME_FMT), wake_cli.datetime.now() + wake_cli.timedelta(minutes=50))

    def test_the_preamble_is_the_instances_if_it_has_one(self):
        entry = {"id": "w-aaaaaa", "kind": "wake", "at": "2026-01-01 00:00:00", "note": "n", "prompt": "the work", "model": "m", "created": "x"}
        with tempfile.TemporaryDirectory() as td:
            pre = Path(td) / "preamble.txt"
            pre.write_text("Hello {name} ({id}).\n\n{body}")
            with mock.patch.dict(wake_cli.os.environ, {"WAKE_PREAMBLE_FILE": str(pre), "WAKE_FIRE": "local"}), \
                 mock.patch.object(wake_cli, "WAKE_DIR", Path(td)), mock.patch.object(wake_cli, "SPOOL", Path(td) / "spool.jsonl"), \
                 mock.patch.object(wake_cli, "LOCK", Path(td) / "spool.lock"), mock.patch.object(wake_cli, "LOG", Path(td) / "wake.log"), \
                 mock.patch.object(wake_cli, "ensure_deadman"), mock.patch.object(wake_cli, "dispatch") as dispatch:
                dispatch.return_value.returncode = 0
                dispatch.return_value.stdout = ""
                (Path(td) / "spool.jsonl").write_text(json.dumps(entry) + "\n")
                wake_cli.cmd_fire(mock.Mock(id="w-aaaaaa"))
        self.assertEqual(dispatch.call_args.args[0], "Hello Ada (w-aaaaaa).\n\nthe work")


class Service(unittest.TestCase):
    """The piece's `run` service: a pending wake that lost its timer gets one; a held one waits for resume."""

    def test_keep_armed(self):
        with tempfile.TemporaryDirectory() as td:
            clocks = Path(td)
            for agent, entries in (("ada", [{"id": "w-aaaaaa", "kind": "wake", "at": "2099-01-01 00:00:00"},
                                            {"id": "w-bbbbbb", "kind": "wake", "at": "2099-01-01 00:00:00", "held": True}]),
                                   ("bo", [{"id": "w-cccccc", "kind": "wake", "at": "2099-01-01 00:00:00"}])):
                (clocks / agent).mkdir()
                (clocks / agent / "spool.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
            armed = []
            with mock.patch.object(wake_cli, "CLOCKS", clocks), \
                 mock.patch.object(wake_cli, "timer_active", side_effect=lambda wid: wid == "w-cccccc"), \
                 mock.patch.object(wake_cli, "arm_timer", side_effect=lambda e: armed.append(e["id"]) or "armed"):
                wake_cli.keep_armed()
        self.assertEqual(armed, ["w-aaaaaa"])

    def test_an_unconfigured_service_arms_nothing_and_restarts_once_configured(self):
        # loom starts a piece's services at install, before the owner has named the local agent.
        looks = []

        def sleep(_s):
            looks.append(1)
            if len(looks) == 2:
                wake_cli.os.environ["WAKE_LOCAL_AGENT"] = "ada"   # the owner sets it
        with mock.patch.dict(wake_cli.os.environ, {}), mock.patch.object(wake_cli, "_owner_settings", return_value={}), \
             mock.patch.object(wake_cli.time, "sleep", side_effect=sleep), \
             mock.patch.object(wake_cli, "cmd_rearm", side_effect=AssertionError("armed while unconfigured")), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            wake_cli.os.environ.pop("WAKE_LOCAL_AGENT", None)
            with self.assertRaises(SystemExit) as e:
                wake_cli.cmd_run(None)
        self.assertEqual((e.exception.code, len(looks)), (1, 2))
        self.assertIn("WAKE_LOCAL_AGENT isn't set", err.getvalue())


if __name__ == "__main__":
    unittest.main()
