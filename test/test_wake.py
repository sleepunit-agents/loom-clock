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
import argparse
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


def json_keys(path):
    return set(wake_cli.json.loads(Path(path).read_text()))


class AgentWakes(unittest.TestCase):
    """A wake set by an agent (WAKE_ORIGIN, from the piece API): its -f is a
    lane file, read the way argparse reads it, and it only fires into a house."""

    def setUp(self):
        # These exercise the explicitly selected compatibility dispatch path.
        patch = mock.patch.dict(wake_cli.os.environ, {"WAKE_DELIVERY": "spawn"})
        patch.start()
        self.addCleanup(patch.stop)

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
        words = "push the agent <!-- house: local -->"
        filed = {a: {"prompt": words, "note": "t-1 r1", "target": "ada:" + wake_cli.hashlib.sha256(words.encode()).hexdigest()[:16]}
                 for a in ("a-12345678", "a-99999999")}
        with mock.patch.object(wake_cli, "spend_approval", refuse), mock.patch.object(wake_cli, "filed_wakes", return_value=filed):
            # A refusal says the piece's reason, not just no.
            err, _ = self.set_(["in", "1h", "-p", "push the agent <!-- house: local -->", "--note", "t-1 r1", "--approval", "a-99999999"])
            self.assertIn("not by bo in h-2", err or "")
            err, entries = self.set_(["in", "1h", "-p", "push the agent <!-- house: local -->", "--note", "t-1 r1", "--approval", "a-12345678"])
        self.assertIsNone(err, err)
        self.assertEqual(entries[0]["local_approval"], "a-12345678")

    def test_an_allowed_local_wake_is_set_again_from_the_words_the_owner_allowed(self):
        # 2026-11-06: a parked body's continuation couldn't reproduce the prompt byte for byte, so its hash missed and
        # the owner was asked twice. The clock keeps the words it filed; --approval alone sets exactly those.
        prompt = "Run bin/units-install on loom's machine  \n<!-- house: local -->"
        target = "ada:" + wake_cli.hashlib.sha256(prompt.encode()).hexdigest()[:16]
        spent = []
        spend = lambda aid, kind, target: spent.append((aid, kind, target)) or ""
        with tempfile.TemporaryDirectory() as td:
            def set_(argv):
                with mock.patch.dict(wake_cli.os.environ, {"WAKE_ORIGIN": "agent:ada"}), \
                     mock.patch.object(wake_cli, "AGENT", "ada"), mock.patch.object(wake_cli, "WAKE_DIR", Path(td)), \
                     mock.patch.object(wake_cli, "SPOOL", Path(td) / "spool.jsonl"), \
                     mock.patch.object(wake_cli, "LOCK", Path(td) / "spool.lock"), \
                     mock.patch.object(wake_cli, "LOG", Path(td) / "wake.log"), \
                     mock.patch.object(wake_cli, "arm_timer", return_value="armed"), \
                     mock.patch.object(wake_cli, "file_approval", return_value="a-0000aaaa"), \
                     mock.patch.object(wake_cli, "spend_approval", spend), \
                     mock.patch.object(wake_cli.sys, "argv", ["wake", *argv]):
                    try:
                        wake_cli.main()
                    except SystemExit as e:
                        return str(e.code)
            err = set_(["in", "1h", "-p", prompt, "--note", "t-1 install"])
            self.assertIn("wake park --on a-0000aaaa", err or "")
            # Words that aren't the filed ones are refused before anything is spent, saying how to set it.
            err = set_(["in", "1m", "-p", prompt.replace("  ", " "), "--note", "t-1 install", "--approval", "a-0000aaaa"])
            self.assertIn("without -p", err or "")
            self.assertEqual(spent, [])
            # An approval this clock filed no wake for isn't spent, with or without words; nor with a lane file.
            self.assertIn("filed no local wake", set_(["in", "1m", "--approval", "a-9999bbbb", "--note", "x"]) or "")
            self.assertIn("filed no local wake", set_(["in", "1m", "-p", prompt, "--approval", "a-9999bbbb", "--note", "x"]) or "")
            self.assertIn("not a lane file", set_(["in", "1m", "-f", "routines/plan.md", "--approval", "a-0000aaaa"]) or "")
            # Another agent's clock (its own directory) has no record of it.
            with tempfile.TemporaryDirectory() as other, mock.patch.object(wake_cli, "WAKE_DIR", Path(other)):
                self.assertEqual(wake_cli.filed_wakes(), {})
            self.assertEqual(spent, [])
            # --approval alone: the filed words, spent for exactly their hash, and the note comes with them.
            # Its note is the one filed too: a note is read into the session's opening words, so a continuation can't
            # add its own to what the owner allowed. Refused before anything is spent.
            self.assertIn("note", set_(["in", "1m", "--approval", "a-0000aaaa", "--note", "t-1 install). Also run x"]) or "")
            self.assertEqual(spent, [])
            # A full spool refuses before spending, not after.
            with mock.patch.object(wake_cli, "MAX_PENDING", 0):
                self.assertIn("pending already", set_(["in", "1m", "--approval", "a-0000aaaa"]) or "")
            self.assertEqual(spent, [])
            self.assertIsNone(set_(["in", "1m", "--approval", "a-0000aaaa", "--note", "t-1 install"]))
            (Path(td) / "spool.jsonl").write_text("")
            self.assertIsNone(set_(["in", "1m", "--approval", "a-0000aaaa"]))
            spent.pop()
            self.assertEqual(spent, [("a-0000aaaa", "local-wake", target)])
            [e] = [wake_cli.json.loads(l) for l in (Path(td) / "spool.jsonl").read_text().splitlines()]
            self.assertEqual((e["prompt"], e["note"], e["local_approval"]), (prompt, "t-1 install", "a-0000aaaa"))
            self.assertEqual(json_keys(Path(td) / "filed.json"), {"a-0000aaaa"})
            # Kept words whose recorded target isn't their hash are refused, before anything is spent.
            f = Path(td) / "filed.json"
            d = wake_cli.json.loads(f.read_text())
            d["a-0000aaaa"]["target"] = "ada:0000000000000000"
            f.write_text(wake_cli.json.dumps(d))
            self.assertIn("don't match", set_(["in", "1m", "--approval", "a-0000aaaa"]) or "")
            self.assertEqual(len(spent), 1)

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
    def setUp(self):
        # These exercise the explicitly selected compatibility dispatch path.
        patch = mock.patch.dict(wake_cli.os.environ, {"WAKE_DELIVERY": "spawn"})
        patch.start()
        self.addCleanup(patch.stop)

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

    def test_real_kill_after_http_acceptance_replays_sealed_firing(self):
        import signal
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        accepted, release = threading.Event(), threading.Event()
        received = []

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.rfile.read(int(self.headers["Content-Length"])))
                accepted.set()
                if len(received) == 1:
                    release.wait(10)
                self.send_response(200)
                self.end_headers()
                try:
                    self.wfile.write(b'{"body":"started"}')
                except BrokenPipeError:
                    pass

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as td, HTTPServer(("127.0.0.1", 0), Receiver) as server:
            root = Path(td)
            token = root / "token"
            token.write_text("clock-test")
            entry = dict(self.base, prompt="original words", continuation={"handle": "cont-" + "b" * 64, "expiresAt": 9999999999999, "wake": self.base["id"]})
            with mock.patch.multiple(wake_cli, WAKE_DIR=root, SPOOL=root / "spool.jsonl", LOCK=root / "spool.lock", LOG=root / "wake.log"), \
                 mock.patch.object(wake_cli, "arm_timer"), mock.patch.object(wake_cli, "ensure_deadman"), \
                 mock.patch.object(wake_cli, "hand_over_principal"), mock.patch.object(wake_cli, "fire_mode", return_value="house"), \
                 mock.patch.object(wake_cli, "paused", return_value=False), \
                 mock.patch.dict(os.environ, {"WAKE_DELIVER_TOKEN_FILE": str(token), "WAKE_LOOM_URL": f"http://127.0.0.1:{server.server_port}"}):
                with wake_cli.Spool() as spool:
                    spool.entries.append(entry)

                def start():
                    # A new interpreter, with no inherited Python locks or mocked HTTP client.
                    code = """
import argparse, sys, types
from pathlib import Path
from unittest import mock
m = types.ModuleType('clock_child')
m.__file__ = sys.argv[1]
exec(compile(Path(m.__file__).read_text(), m.__file__, 'exec'), m.__dict__)
r = Path(sys.argv[2])
with mock.patch.multiple(m, WAKE_DIR=r, SPOOL=r/'spool.jsonl', LOCK=r/'spool.lock', LOG=r/'wake.log'), mock.patch.object(m, 'arm_timer'), mock.patch.object(m, 'ensure_deadman'), mock.patch.object(m, 'hand_over_principal'), mock.patch.object(m, 'fire_mode', return_value='house'), mock.patch.object(m, 'paused', return_value=False):
    m.cmd_fire(argparse.Namespace(id=sys.argv[3]))
"""
                    return subprocess.Popen([sys.executable, "-c", code, str(SCRIPT), str(root), entry["id"]])

                first = start()
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    self.assertTrue(accepted.wait(10), "first real HTTP request never arrived")
                    first.kill()
                    first.wait(timeout=10)
                    pending = json.loads((root / "spool.jsonl").read_text())
                    self.assertEqual(pending["delivery"], json.loads(received[0]))
                    with wake_cli.Spool() as spool:
                        recovered = spool.entries[0]
                        recovered["prompt"] = "changed after crash"
                        self.assertEqual(wake_cli.arm_pending(recovered), wake_cli.arm_timer.return_value)
                        self.assertNotEqual(recovered["id"], entry["id"])
                        entry["id"] = recovered["id"]
                    self.assertEqual(recovered["delivery"], pending["delivery"])
                    release.set()
                    second = start()
                    self.assertEqual(second.wait(timeout=10), 0)
                    self.assertEqual(len(received), 2)
                    self.assertEqual(received[0], received[1])
                    self.assertEqual(json.loads(received[1])["continuation"], entry["continuation"])
                    self.assertEqual((root / "spool.jsonl").read_text(), "")
                finally:
                    release.set()
                    server.shutdown()
                    thread.join(5)

    def test_continuation_inspection_requires_runtime_deadline_before_any_commit(self):
        handle = "cont-" + "a" * 64
        when = wake_cli.datetime.now() + wake_cli.timedelta(hours=1)
        deadline = int(when.timestamp() * 1000) + 1
        good = {"handle": handle, "expiresAt": deadline}
        with mock.patch.dict(os.environ, {"WAKE_PRINCIPAL_STATEMENT": "signed-issuer"}), \
             mock.patch.object(wake_cli.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=json.dumps(good))) as call:
            self.assertEqual(wake_cli.inspect_continuation(handle, when), deadline)
            self.assertEqual(call.call_args.kwargs["input"], "signed-issuer")
            for bad in [{**good, "expiresAt": deadline - 1}, {**good, "expiresAt": str(deadline)},
                        {**good, "handle": "other"}, {}]:
                call.return_value.stdout = json.dumps(bad)
                with self.assertRaises(SystemExit):
                    wake_cli.inspect_continuation(handle, when)
        args = mock.Mock(prompt="continue", prompt_file=None, note="follow up", model="m", slot="", approval="", place="", continuation=handle)
        with mock.patch.object(wake_cli, "inspect_continuation", side_effect=SystemExit("refused")), \
             mock.patch.object(wake_cli, "Spool") as spool, mock.patch.object(wake_cli, "arm_timer") as timer:
            with self.assertRaises(SystemExit):
                wake_cli.cmd_set(args, when)
            spool.assert_not_called()
            timer.assert_not_called()

    def test_continuation_firing_holds_legacy_expired_and_changed_deadlines(self):
        now = int(wake_cli.datetime.now().timestamp() * 1000)
        for deadline in [None, now, now - 1, str(now + 999999)]:
            entry = {**self.base, "continuation": {"handle": "cont-" + "a" * 64, "wake": self.base["id"], "expiresAt": deadline}}
            with mock.patch.object(wake_cli, "post_delivery") as post, mock.patch.object(wake_cli, "log"):
                wake_cli.deliver_wake(entry, "continue", "art", False)
                post.assert_not_called()
        grant = {"handle": "cont-" + "a" * 64, "wake": self.base["id"], "expiresAt": now + 999999}
        entry = {**self.base, "continuation": grant, "delivery": {"continuation": {**grant, "expiresAt": now + 999998}}}
        with mock.patch.object(wake_cli, "post_delivery") as post, mock.patch.object(wake_cli, "log"):
            wake_cli.deliver_wake(entry, "continue", "art", False)
            post.assert_not_called()

    def test_setting_a_continuation_persists_opaque_handle_and_rejects_shared_places(self):
        handle = "cont-" + "a" * 64
        args = mock.Mock(prompt="continue", prompt_file=None, note="follow up", model="m", slot="", approval="", place="", park_on="", continuation=handle)
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(wake_cli, "SPOOL", Path(td) / "spool.jsonl"), \
             mock.patch.object(wake_cli, "LOCK", Path(td) / "spool.lock"), \
             mock.patch.object(wake_cli, "LOG", Path(td) / "wake.log"), \
             mock.patch.object(wake_cli, "arm_timer", return_value="armed"), \
             mock.patch.object(wake_cli, "inspect_continuation", return_value=9999999999999):
            wake_cli.cmd_set(args, wake_cli.datetime.now())
            entries = [json.loads(l) for l in (Path(td) / "spool.jsonl").read_text().splitlines()]
            entry = next(e for e in entries if e.get("continuation"))
            self.assertEqual(entry["continuation"], {"handle": handle, "wake": entry["id"], "expiresAt": 9999999999999})
            args.place = "alert:shared"
            with self.assertRaises(SystemExit):
                wake_cli.cmd_set(args, wake_cli.datetime.now())
            args.place = ""
            args.continuation = "cv-0123456789ab"
            with self.assertRaises(SystemExit):
                wake_cli.cmd_set(args, wake_cli.datetime.now())

    def test_opaque_continuation_survives_retry_without_authority_from_origin(self):
        grant = {"handle": "cont-" + "a" * 64, "expiresAt": 9999999999999, "wake": self.base["id"]}
        entry = {**self.base, "continuation": grant, "for_in": "cv-0123456789ab"}
        bodies = []
        def post(body):
            bodies.append(json.loads(json.dumps(body)))
            return (503, {"error": "busy"}) if len(bodies) == 1 else (200, {"body": "queued"})
        with mock.patch.object(wake_cli, "post_delivery", side_effect=post), \
             mock.patch.object(wake_cli, "hand_over_principal"), \
             mock.patch.object(wake_cli, "retry", side_effect=lambda e, *a: e.update(id="w-999999")), \
             mock.patch.object(wake_cli, "log"):
            wake_cli.deliver_wake(entry, "continue", "art", False)
            wake_cli.deliver_wake(entry, "changed prompt must not change retry", "art", False)
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(bodies[0]["continuation"], grant)
        entry = {**self.base, "for_in": "cv-0123456789ab"}
        with mock.patch.object(wake_cli, "post_delivery", return_value=(200, {"body": "queued"})) as post, \
             mock.patch.object(wake_cli, "hand_over_principal"), mock.patch.object(wake_cli, "log"):
            wake_cli.deliver_wake(entry, "continue", "art", False)
        self.assertNotIn("continuation", post.call_args.args[0])

    def test_delivery_hands_over_parent_conversation_only_for_one_off_continuations(self):
        for extra, expected in [({}, "cv-0123456789ab"), ({"kind": "schedule", "schedule": "nightly"}, None)]:
            entry = {**self.base, "for": "owner", "for_in": "cv-0123456789ab", **extra}
            with mock.patch.object(wake_cli, "hand_over_principal") as hand, \
                 mock.patch.object(wake_cli, "post_delivery", return_value=(200, {"conversation": "cv-111111111111", "body": "started"})), \
                 mock.patch.object(wake_cli, "log"):
                wake_cli.deliver_wake(entry, "continue", "art", False)
            self.assertEqual(hand.call_args.kwargs["origin"], expected)

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

    def test_quota_waits_for_reset_without_counting_failures_or_alerting(self):
        reset = (wake_cli.datetime.now(wake_cli.timezone.utc) + wake_cli.timedelta(hours=12)).isoformat()
        failed = (200, {"body": "failed", "code": "quota", "reason": "no room", "retryAfter": reset})
        entry = {**self.base, "prompt": "work", "fails": 3}
        posts, dispatch, spool, logged, alerts = self.fire(entry, [failed])
        dispatch.assert_not_called()
        (r,) = spool
        self.assertEqual(r["fails"], 3)
        self.assertEqual(r["deliver_place"], "wake:w-cccccc")
        self.assertNotEqual(r["id"], entry["id"])
        expected = wake_cli.datetime.fromisoformat(reset) + wake_cli.timedelta(minutes=1)
        self.assertEqual(r["at"], expected.astimezone().strftime(wake_cli.TIME_FMT))
        self.assertEqual(alerts, [])
        self.assertIn("quota-wait", logged)
        posts2, _, spool2, *_ = self.fire(r, [self.ok])
        self.assertEqual(posts2[0]["place"], posts[0]["place"])
        self.assertEqual(spool2, [])

    def test_quota_alert_does_not_retry_itself_but_other_alerts_wait(self):
        failed = (200, {"body": "failed", "reason": "every model account art may use is out of quota"})
        for place, count in [("alert:runtime-quota", 0), ("alert:disk", 1)]:
            _, _, spool, logged, alerts = self.fire({**self.base, "place": place, "prompt": "<!-- house: local -->\ntriage"}, [failed])
            self.assertEqual(len(spool), count)
            self.assertEqual(alerts, [])
            self.assertIn("quota-wait" if count else "quota-notice", logged)

    def test_bad_or_past_quota_reset_cannot_make_a_hot_loop(self):
        for reset in [None, "bad", "2000-01-01T00:00:00Z", "2030-01-01", "9999-12-31T23:59:59Z"]:
            before = wake_cli.datetime.now()
            _, _, spool, _, alerts = self.fire({**self.base, "prompt": "work"}, [(200, {"body": "failed", "code": "quota", "retryAfter": reset})])
            when = wake_cli.datetime.strptime(spool[0]["at"], wake_cli.TIME_FMT)
            self.assertGreater(when, before + wake_cli.timedelta(minutes=14))
            self.assertLess(when, before + wake_cli.timedelta(days=8, seconds=5))
            self.assertEqual(alerts, [])

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
        # Unset: missing credentials or missing runtime must not change execution paths.
        with mock.patch.dict(os.environ, {"WAKE_DELIVER_TOKEN_FILE": "", "WAKE_DELIVERY": ""}):
            self.assertEqual(wake_cli.delivery_mode(), "deliver")
        for env in ({}, {"WAKE_DELIVERY": "deliver"}, {"WAKE_DELIVER_TOKEN_FILE": ""}):
            posts, dispatch, spool, logged, _ = self.fire(
                {**self.base, "prompt": "p"},
                [(503, {"error": "unavailable: no installed piece offers delivery"})], env=env)
            dispatch.assert_not_called()
            self.assertEqual(len(spool), 1, "same runtime delivery is retried")
            self.assertEqual(spool[0]["delivery"], posts[0])
            self.assertNotIn("fire-spawn", logged)

    def test_what_delivery_cant_carry_goes_the_old_way(self):
        # A lane that keeps a house slot; and, with WAKE_LOCAL_RUN=session (the way back), a wake that needs loom's
        # machine and names no place.
        posts, dispatch, *_ = self.fire({**self.base, "prompt": "ssh buildbox <!-- house: local -->"}, [], env={"WAKE_LOCAL_RUN": "session"})
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
        self.assertIs(b["local"], True, "the contract's field says where its body runs")
        self.assertNotIn("<!-- house: local -->", b["text"], "never the words: the marker is the clock's input only")
        self.assertIn("on loom's machine (loom's runtime delivered it)", b["text"])
        self.assertEqual(spool, [])
        # A house wake with a place joins that conversation too.
        posts, *_ = self.fire({**self.base, "prompt": "p", "place": "alert:dns"}, [self.ok])
        self.assertEqual(posts[0]["place"], "alert:dns")
        self.assertNotIn("local", posts[0])
        # With WAKE_LOCAL_RUN=session, a house's wake approved for loom's machine is a session here, as before t-1921.
        approved = {**self.base, "prompt": "<!-- house: local -->\nx", "origin": "agent:ada", "local_approval": "a-12345678"}
        posts, dispatch, *_ = self.fire(approved, [], env={"WAKE_LOCAL_RUN": "session"})
        self.assertEqual((posts, dispatch.call_args.args[3]), ([], "local"))
        # Else it's the runtime's too, in its own conversation: a house never names a place on loom's machine.
        posts, dispatch, *_ = self.fire(approved, [self.ok])
        dispatch.assert_not_called()
        self.assertEqual((posts[0]["place"], posts[0]["local"], posts[0]["agent"]), ("wake:w-cccccc", True, "ada"))

    def test_every_local_wake_is_the_runtimes(self):
        # t-1921: no wake starts a harness here itself. A follow-up that needs loom's machine: its own conversation.
        posts, dispatch, spool, logged, _ = self.fire({**self.base, "prompt": "ssh buildbox <!-- house: local -->"}, [self.ok])
        dispatch.assert_not_called()
        (b,) = posts
        self.assertEqual((b["place"], b["local"], b["venue"], b["kind"], b["from"]), ("wake:w-cccccc", True, "wake", "wake", {"kind": "schedule"}))
        self.assertNotIn("<!-- house: local -->", b["text"])
        self.assertIn("on loom's machine (loom's runtime delivered it)", b["text"])
        self.assertTrue(b["text"].endswith("ssh buildbox"))
        self.assertEqual(spool, [])
        self.assertIn("fire | w-cccccc delivered: queued", logged)
        # A lane that runs here: one conversation for its runs here, never its house runs' (`lane:<name>`).
        posts, dispatch, *_ = self.fire({**self.base, "kind": "schedule", "schedule": "plan"}, [self.ok], body="# Plan\nthe lane's own words\n")
        dispatch.assert_not_called()
        self.assertEqual((posts[0]["place"], posts[0]["local"]), ("local-lane:plan", True))
        self.assertIn("the lane's own words", posts[0]["text"], "here, the lane's text, as a session had it")
        # A retry keeps its conversation, and stays local.
        failed = (200, {"conversation": "cv-0123456789ab", "seq": 1, "body": "failed", "reason": "every model account art may use is out of quota"})
        posts, _, spool, *_ = self.fire({**self.base, "kind": "schedule", "schedule": "plan"}, [failed], body="# Plan\nx\n")
        (r,) = spool
        self.assertEqual(r["deliver_place"], "local-lane:plan")
        posts2, *_ = self.fire(r, [self.ok], body="# Plan\nx\n")
        self.assertEqual((posts2[0]["place"], posts2[0]["local"]), ("local-lane:plan", True))
        # The dead-man wake too.
        posts, dispatch, *_ = self.fire({**self.base, "kind": "deadman", "prompt": "decide"}, [self.ok], env={"WAKE_FIRE": "local"})
        dispatch.assert_not_called()
        self.assertEqual((posts[0]["place"], posts[0]["local"]), ("wake:w-cccccc", True))
        # Its timer carries the setting, so a fired run reads it as the clock that armed it.
        self.assertIn("WAKE_LOCAL_RUN", wake_cli.TIMER_ENV)

    def test_local_runtime_refusal_never_starts_another_harness(self):
        for status in (400, 403, 503):
            for place in (None, "alert:host-watch"):
                entry = {**self.base, "prompt": "<!-- house: local -->\ntriage"}
                if place:
                    entry["place"] = place
                posts, dispatch, spool, logged, alerts = self.fire(entry, [(status, {
                    "error": "no installed piece offers delivery" if status == 503 else "runtime runs no local bodies"})])
                dispatch.assert_not_called()
                self.assertEqual(len(posts), 1)
                self.assertNotIn("fire-spawn", logged)
                if status == 503:
                    self.assertEqual(spool[0]["delivery"], posts[0])
                else:
                    self.assertEqual(spool, [])
                    self.assertEqual(len(alerts), 1)
                    self.assertIn("not retried", alerts[0].lower())

    def test_the_refusal_alert_is_once_a_day(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(wake_cli, "WAKE_DIR", Path(td)), \
             mock.patch.object(wake_cli, "LOG", Path(td) / "wake.log"), \
             mock.patch.object(wake_cli, "alert") as alert:
            wake_cli.alert_daily("k", "one")
            wake_cli.alert_daily("k", "two")
            wake_cli.alert_daily("other", "three")
            self.assertEqual([c.args[0] for c in alert.call_args_list], ["one", "three"])
            self.assertIn("alert-quiet | two", (Path(td) / "wake.log").read_text())

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


MANIFEST = json.loads((SCRIPT.parent.parent / "package.json").read_text())["loom"]


class Manifest(unittest.TestCase):
    def test_the_manifest_fits_looms_limits(self):
        # loom's checks (src/cli/pieces-managed.ts): `settings.length > 20`, `str(b.description, 300)`,
        # `str(s.label, 100)`, `str(s.hint, 200)` ("setting X: hint is not a string" when it's longer), and
        # `s.default.length > 500`. bcf3a7c broke the hint one, and `loom pieces update clock` refused the piece.
        self.assertLessEqual(len(MANIFEST["settings"]), 20)
        self.assertLessEqual(len(str(MANIFEST.get("description", ""))), 300)
        for s in MANIFEST["settings"]:
            name = s.get("name")
            self.assertTrue(isinstance(s.get("label"), str) and 0 < len(s["label"]) <= 100, name)
            self.assertTrue(isinstance(s.get("hint", ""), str) and len(s.get("hint", "")) <= 200, name)
            self.assertTrue(isinstance(s.get("default", ""), str) and len(s.get("default", "")) <= 500, name)

    def test_the_clock_runs_no_harness_of_its_own_choosing(self):
        # Every execution goes through loom's runtime (Jonathan, 2026-11-06): no Codex fallback here (#8-#10 reverted).
        self.assertFalse("codex" in SCRIPT.read_text().lower(), "bin/wake names codex")
        names = {s.get("name") for s in MANIFEST["settings"]}
        for gone in ("WAKE_CODEX", "WAKE_CODEX_FALLBACK", "WAKE_CLAUDE_MANAGED_SETTINGS", "WAKE_CODEX_SYSTEM_DIR"):
            self.assertNotIn(gone, names)
            self.assertNotIn(gone, wake_cli.TIMER_ENV)
        self.assertFalse(hasattr(wake_cli, "resolve_harness"))
        self.assertFalse("models resolve" in SCRIPT.read_text(), "bin/wake asks the resolver")


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
            mock.patch.dict(wake_cli.os.environ, {"WAKE_ORIGIN": "", "WAKE_DELIVERY": "spawn"}),
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

    def test_a_park_on_an_approval_hands_the_continuation_exactly_what_to_spend_it_on(self):
        # The continuation runs in a later house, often hours on; it's handed the kind and target as filed, so it
        # spends the owner's yes instead of asking again in its own words.
        merge = "github:acme/app#12@" + "a" * 40
        card = {"id": "a-1a2b3c4d", "agent": "ada", "state": "pending", "kind": "merge", "target": merge}
        calls = []
        def run(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout=wake_cli.json.dumps(card) + "\n", stderr="")
        with mock.patch.object(wake_cli.subprocess, "run", run), \
             mock.patch.dict(wake_cli.os.environ, {"WAKE_CALLER": '{"kind": "agent", "agent": "ada", "house": "h-9"}'}):
            self.assertIsNone(self.cli("park", "--on", "a-1a2b3c4d", "-p", "merge it", "--note", "t-1 merge"))
        self.assertEqual(calls[0][-5:], ["check", "a-1a2b3c4d", "--json", "--as", '{"kind": "agent", "agent": "ada", "house": "h-9"}'])
        [e] = [e for e in self.spool() if e["kind"] == "parked"]
        self.assertEqual(e["approval"], {"kind": "merge", "target": merge})
        wake_cli.unpark(e, "allowed")
        self.assertIn(f"approvals use a-1a2b3c4d --kind merge --target '{merge}'", e["prompt"])
        self.assertIn("merge it", e["prompt"])
        # Told no: nothing to spend, and no spend line.
        e = {"on": "a-1a2b3c4d", "prompt": "merge it", "note": "n", "approval": {"kind": "merge", "target": merge}}
        wake_cli.unpark(e, "denied")
        self.assertNotIn("approvals use", e["prompt"])
        # Another agent's approval: its kind and target are never kept for this agent's continuation.
        bo = {**card, "id": "a-5a2b3c4d", "agent": "bo"}
        with mock.patch.object(wake_cli.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=wake_cli.json.dumps(bo), stderr="")):
            self.assertIsNone(self.cli("park", "--on", "a-5a2b3c4d", "-p", "x", "--note", "t-9"))
        [e] = [e for e in self.spool() if e.get("on") == "a-5a2b3c4d"]
        self.assertNotIn("approval", e)
        # An approval piece that can't say (an older one, another agent's id): parked all the same, the old words.
        with mock.patch.object(wake_cli.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="", stderr="no")):
            self.assertIsNone(self.cli("park", "--on", "a-2a2b3c4d", "-p", "go", "--note", "t-2"))
        [e] = [e for e in self.spool() if e.get("on") == "a-2a2b3c4d"]
        self.assertNotIn("approval", e)
        wake_cli.unpark(e, "allowed")
        self.assertIn("spend it with --approval a-2a2b3c4d", e["prompt"])

    def test_a_park_on_a_local_wake_s_approval_hands_back_the_one_line_that_sets_it(self):
        prompt = "check the disks <!-- house: local -->"
        wake_cli.remember_filed("a-3a2b3c4d", "ada:0123456789abcdef", prompt, "t-3 disks")
        card = {"id": "a-3a2b3c4d", "agent": "ada", "state": "pending", "kind": "local-wake", "target": "ada:0123456789abcdef"}
        with mock.patch.object(wake_cli.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=wake_cli.json.dumps(card), stderr="")):
            self.cli("park", "--on", "a-3a2b3c4d", "-p", "set the disk check", "--note", "t-3 park")
        [e] = [e for e in self.spool() if e["kind"] == "parked"]
        wake_cli.unpark(e, "allowed")
        self.assertIn("`wake in 1m --approval a-3a2b3c4d`", e["prompt"])
        self.assertIn("check the disks", e["prompt"])
        # The continuation itself runs where any continuation runs: the quote carries no house marker.
        self.assertNotIn(wake_cli.HOUSE_LOCAL, e["prompt"])
        self.assertTrue(wake_cli.house_ready(e, e["prompt"]))
        # However the marker is nested, the quote carries none of it.
        wake_cli.remember_filed("a-8a2b3c4d", "ada:0", "x <!-- house: <!-- house: local -->local --> y", "n")
        self.assertNotIn(wake_cli.HOUSE_LOCAL, wake_cli.spend_words("a-8a2b3c4d", None))
        # A stale temp file left by a crash doesn't decide the store's mode.
        tmp = wake_cli.filed_path().with_suffix(f".{wake_cli.os.getpid()}.tmp")
        tmp.write_text("{}")
        tmp.chmod(0o644)
        wake_cli.remember_filed("a-9a2b3c4d", "ada:0", "z <!-- house: local -->", "n")
        self.assertEqual(wake_cli.filed_path().stat().st_mode & 0o777, 0o600)
        # A wake approval whose words this clock no longer has: no spend line (the piece wouldn't run it).
        lost = wake_cli.spend_words("a-7a2b3c4d", {"kind": "local-wake", "target": "ada:0123456789abcdef"})
        self.assertNotIn("approvals use", lost)
        self.assertNotIn("--approval a-7a2b3c4d", lost)
        # Filed words past FILED_KEEP are gone.
        f = wake_cli.filed_path()
        d = wake_cli.json.loads(f.read_text())
        d["a-3a2b3c4d"]["at"] = "2000-01-01 00:00:00"
        f.write_text(wake_cli.json.dumps(d))
        self.assertNotIn("a-3a2b3c4d", wake_cli.filed_wakes())
        # A card whose id isn't the one parked on is nobody's.
        with mock.patch.object(wake_cli.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=wake_cli.json.dumps({**card, "id": "a-6a2b3c4d"}), stderr="")):
            self.assertIsNone(wake_cli.approval_card("a-3a2b3c4d"))

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
        patch = mock.patch.dict(wake_cli.os.environ, {"WAKE_DELIVERY": "spawn"})
        patch.start()
        self.addCleanup(patch.stop)
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
            with mock.patch.dict(wake_cli.os.environ, {"WAKE_PREAMBLE_FILE": str(pre), "WAKE_FIRE": "local", "WAKE_LOCAL_RUN": "session"}), \
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


class AnotherPiecesVariables(unittest.TestCase):
    """The tasks piece and loom's runtime run `loom-clock in 1m …` with their own LOOM_PIECE_NAME and
    LOOM_PIECE_HOME. Those aren't the clock's: read as its own, they made a second clock in that piece's
    data, with an agent called "agent" and the default session command (t-1854)."""

    def install(self, d):
        pieces = Path(d) / "pieces"
        home = pieces / "clock"
        pkg = home / "versions" / ("a" * 40) / "node_modules" / "loom-clock" / "bin"
        pkg.mkdir(parents=True)
        (home / "piece.json").write_text("{}")
        (home / "settings.json").write_text(json.dumps({"WAKE_LOCAL_AGENT": "zed", "WAKE_REPO": d}))
        (pkg / "wake").write_text("")
        (pieces / "tasks").mkdir()
        (pieces / "tasks" / "settings.json").write_text(json.dumps({"TASKSTORE_DB": "/x"}))
        return pieces, home, pkg / "wake"

    def test_a_foreign_piece_home_is_ignored_and_the_clock_finds_its_own(self):
        with tempfile.TemporaryDirectory() as d:
            pieces, home, script = self.install(d)
            m = Settings.load(self, {"HOME": d, "LOOM_PIECE_NAME": "tasks", "LOOM_PIECE_HOME": str(pieces / "tasks")},
                              script=script)
            self.assertEqual((m.PIECE_HOME, m.CLOCKS, m.LOCAL_AGENT), (home, home / "data", "zed"))
            with mock.patch.dict(m.os.environ, {"LOOM_PIECE_NAME": "tasks", "LOOM_PIECE_HOME": str(pieces / "tasks")}):
                env = m.timer_env()
            self.assertNotIn("LOOM_PIECE_NAME=tasks", env)
            self.assertNotIn(f"LOOM_PIECE_HOME={pieces / 'tasks'}", env)
            self.assertIn(f"LOOM_PIECE_HOME={home}", env)

    def test_the_clocks_own_and_an_unnamed_piece_home_are_still_taken(self):
        for env in ({"LOOM_PIECE_NAME": "clock"}, {}):
            m = Settings.load(self, {"HOME": "/h", "LOOM_PIECE_HOME": "/h/pieces/clock", **env})
            self.assertEqual(m.PIECE_HOME, Path("/h/pieces/clock"))


class StuckSessions(unittest.TestCase):
    """The reaper (t-1854): a session the clock started that waits on a permission prompt past WAKE_STUCK_AFTER
    is stopped, commented on its task, logged `stuck` and said once. Nothing else is ever touched. `claude` is a
    stand-in: `agents --json` prints $FAKE_AGENTS, `stop` writes its id down."""

    COMMAND = "cd ~/Art && taskstore comment t-40 \"Round 12: killed a stale \\`rclone authorize drive\\` from $(cat <<'EOF'\nx\nEOF\n)\""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        t = Path(self.td.name)
        self.t = t
        (t / "bin").mkdir()
        claude = t / "bin" / "claude"
        claude.write_text("#!/bin/sh\n"
                          "case \"$1\" in\n"
                          "  agents) cat \"$FAKE_AGENTS\" ;;\n"
                          "  stop) echo \"$2\" >> \"$FAKE_STOPS\" ;;\n"
                          "  --bg) printf 'backgrounded \\302\\267 abc12345\\n  claude agents   list sessions\\n  claude attach abc12345\\n' ;;\n"
                          "esac\n")
        claude.chmod(0o755)
        for name, out in (("comment", "comments"), ("alert", "alerts")):
            f = t / "bin" / name
            f.write_text(f"#!/bin/sh\nprintf '%s\\t%s\\n' \"$1\" \"$2\" >> \"{t / out}\"\n")
            f.chmod(0o755)
        (t / "projects" / "-home-art-Art").mkdir(parents=True)
        self.transcript = t / "projects" / "-home-art-Art" / "11111111-2222-3333-4444-555555555555.jsonl"
        self.transcript.write_text("\n".join(json.dumps(x) for x in [
            {"type": "user", "message": {"role": "user", "content": "wake up"}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "commenting"},
                                                          {"type": "tool_use", "name": "Bash", "input": {"command": self.COMMAND}}]}},
            {"type": "cost-state"},
        ]) + "\n")
        self.agents = t / "agents.json"
        self.patches = [
            mock.patch.object(wake_cli, "WAKE_DIR", t / "clock"),
            mock.patch.object(wake_cli, "LOG", t / "clock" / "wake.log"),
            mock.patch.object(wake_cli, "REPO", t),
            mock.patch.dict(wake_cli.os.environ, {
                "FAKE_AGENTS": str(self.agents), "FAKE_STOPS": str(t / "stops"), "CLAUDE_CONFIG_DIR": str(t),
                "WAKE_SESSION_COMMAND": json.dumps([str(claude), "--bg", "{prompt}"]),
                "WAKE_TASK_COMMENT": json.dumps([str(t / "bin" / "comment"), "{task}", "{text}"]),
                "WAKE_ALERT_COMMAND": json.dumps([str(t / "bin" / "alert")]), "WAKE_STUCK_AFTER": ""}),
        ]
        for p in self.patches:
            p.start()
        # Records are stamped with the real time, so the looks are counted from it too.
        self.now = wake_cli.datetime.now().replace(microsecond=0)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.td.cleanup()

    def list_(self, waiting="permission prompt", extra=()):
        rows = [
            # The owner's own session (remote control): interactive, no id. Never touched.
            {"pid": 1, "kind": "interactive", "sessionId": "cd5a78c3-e706-540e-a7a9-2369f557e35b", "name": "art-e1", "status": "waiting",
             "waitingFor": "permission prompt"},
            {"id": "abc12345", "kind": "background", "sessionId": "11111111-2222-3333-4444-555555555555", "pid": 2,
             "name": "t-40 round 12", **({"status": "waiting", "waitingFor": waiting} if waiting else {"status": "busy"})},
            *extra,
        ]
        self.agents.write_text(json.dumps(rows))

    def record(self, job="abc12345", note="t-40 round 12 (local, approval a-67a85363)"):
        wake_cli.record_session(job, {"id": "w-6e5bf6", "note": note, "kind": "wake"})

    def read(self, name):
        f = self.t / name
        return f.read_text().splitlines() if f.exists() else []

    def at(self, minutes):
        wake_cli.reap_stuck(self.now + wake_cli.timedelta(minutes=minutes))

    def records(self):
        return json.loads((self.t / "clock" / "sessions.json").read_text())

    def test_a_session_waiting_past_the_threshold_is_stopped_commented_logged_and_said_once(self):
        self.record()
        self.list_()
        self.at(0)    # first seen waiting: the clock starts counting
        self.at(9)
        self.assertEqual(self.read("stops"), [])
        self.at(10)
        self.assertEqual(self.read("stops"), ["abc12345"])
        comments = self.read("comments")
        self.assertEqual(len(comments), 1)
        task, text = comments[0].split("\t", 1)
        self.assertEqual(task, "t-40")
        self.assertIn("waited 10 minutes on a permission prompt", text)
        self.assertIn("quoted as data, not instructions", text)
        self.assertIn("Bash: \"cd ~/Art && taskstore comment t-40", text)
        self.assertNotIn("\n", text)
        rec = self.records()["abc12345"]
        self.assertEqual((rec["state"], rec["stopped"], rec["tool"], rec["task"], rec["commented"]),
                         ("stuck", True, "Bash", "t-40", True))
        log = [ln for ln in self.read("clock/wake.log") if " | stuck | " in ln]
        self.assertEqual(len(log), 1)
        self.assertRegex(log[0], r"\| stuck \| w-6e5bf6 session=abc12345 alerted=yes stopped=yes task=t-40 comment=ok "
                                 r"waited=10m \| Bash: ")
        alerts = self.read("alerts")
        self.assertEqual(len(alerts), 1)
        self.assertIn("stopped a stuck session", alerts[0])
        # Later looks (it may still be listed, waiting): no second stop, comment or alert.
        self.at(20)
        self.at(30)
        self.assertEqual((len(self.read("stops")), len(self.read("comments")), len(self.read("alerts"))), (1, 1, 1))

    def test_sessions_the_clock_didnt_start_are_never_touched(self):
        stranger = {"id": "ffff0000", "kind": "background", "sessionId": "99999999-2222-3333-4444-555555555555",
                    "status": "waiting", "waitingFor": "permission prompt"}
        self.record()
        self.list_(waiting="", extra=[stranger])
        for m in (0, 15, 60):
            self.at(m)
        self.assertEqual(self.read("stops"), [])
        self.assertEqual(self.read("alerts"), [])
        # With nothing recorded at all, it doesn't even ask.
        (self.t / "clock" / "sessions.json").write_text("{}")
        self.agents.write_text("not json")
        self.at(90)
        self.assertEqual(self.read("stops"), [])

    def test_the_wait_is_counted_from_when_it_was_first_seen_and_resets_when_it_moves_on(self):
        self.record()
        self.list_()
        self.at(0)
        self.list_(waiting="")        # answered or moved on
        self.at(5)
        self.assertNotIn("waiting_since", self.records()["abc12345"])
        self.list_()                  # waiting again, on something else
        self.at(8)
        self.at(17)
        self.assertEqual(self.read("stops"), [])
        self.at(18)
        self.assertEqual(self.read("stops"), ["abc12345"])

    def test_the_threshold_is_a_setting_and_off_turns_it_off(self):
        with mock.patch.dict(wake_cli.os.environ, {"WAKE_STUCK_AFTER": "1"}):
            self.assertEqual(wake_cli.stuck_after(), wake_cli.timedelta(minutes=1))
            self.record()
            self.list_()
            self.at(0)
            self.at(1)
        self.assertEqual(self.read("stops"), ["abc12345"])
        for off in ("0", "off"):
            with mock.patch.dict(wake_cli.os.environ, {"WAKE_STUCK_AFTER": off}):
                self.assertIsNone(wake_cli.stuck_after())
        with mock.patch.dict(wake_cli.os.environ, {"WAKE_STUCK_AFTER": "off"}):
            self.record(job="abc12345")   # a fresh round under the same id
            self.at(0)
            self.at(600)
        self.assertEqual(self.read("stops"), ["abc12345"])

    def test_a_session_that_ended_is_let_go_and_old_records_are_dropped(self):
        self.record()
        self.agents.write_text("[]")
        self.at(1)   # just started: not listed yet isn't gone
        self.assertEqual(self.records()["abc12345"]["state"], "running")
        self.at(6)
        self.assertEqual(self.records()["abc12345"]["state"], "gone")
        wake_cli.reap_stuck(wake_cli.datetime.now() + wake_cli.timedelta(days=8))
        self.assertEqual(self.records(), {})

    def test_a_round_with_no_task_in_its_note_is_still_stopped_and_said(self):
        self.record(note="alert unit-x: something failed")
        self.list_()
        self.at(0)
        self.at(10)
        self.assertEqual(self.read("stops"), ["abc12345"])
        self.assertEqual(self.read("comments"), [])
        self.assertEqual(len(self.read("alerts")), 1)
        self.assertTrue(any("task=none comment=none" in ln for ln in self.read("clock/wake.log")))

    def test_a_failed_stop_is_said_and_still_recorded_stuck_once(self):
        claude = self.t / "bin" / "claude"
        claude.write_text(claude.read_text().replace('stop) echo "$2" >> "$FAKE_STOPS" ;;', 'stop) echo nope >&2; exit 1 ;;'))
        self.record()
        self.list_()
        self.at(0)
        self.at(10)
        self.at(20)
        self.assertEqual(self.records()["abc12345"]["state"], "stuck")
        alerts = self.read("alerts")
        self.assertEqual(len(alerts), 1)
        self.assertIn("The stop FAILED", alerts[0])
        self.assertTrue(any(" | stuck | w-6e5bf6 session=abc12345 alerted=yes stopped=no " in ln
                            for ln in self.read("clock/wake.log")))

    def test_fire_records_the_session_it_started_and_logs_one_line(self):
        entry = {"id": "w-aaa111", "kind": "wake", "at": "2026-11-04 18:42:25", "created": "2026-11-04 18:41:25",
                 "note": "t-40 round 12", "prompt": "do it"}
        spool = self.t / "clock" / "spool.jsonl"
        spool.parent.mkdir(parents=True, exist_ok=True)
        spool.write_text(json.dumps(entry) + "\n")
        with mock.patch.object(wake_cli, "SPOOL", spool), \
             mock.patch.object(wake_cli, "LOCK", self.t / "clock" / "spool.lock"), \
             mock.patch.object(wake_cli, "fire_mode", return_value="local"), \
             mock.patch.object(wake_cli, "fire_gate_open", return_value=True), \
             mock.patch.object(wake_cli, "arm_timer", return_value="armed"), \
             mock.patch.object(wake_cli, "delivery_mode", return_value="spawn"):
            wake_cli.cmd_fire(wake_cli.argparse.Namespace(id="w-aaa111"))
        rec = self.records()["abc12345"]
        self.assertEqual((rec["wake"], rec["note"], rec["state"]), ("w-aaa111", "t-40 round 12", "running"))
        fire = [ln for ln in self.read("clock/wake.log") if " | fire | " in ln]
        self.assertEqual(len(fire), 1)
        self.assertTrue(fire[0].endswith("| fire | w-aaa111 backgrounded · abc12345"))
        self.assertFalse(any(ln.startswith("  claude") for ln in self.read("clock/wake.log")))

    def test_a_credential_in_the_command_is_never_shown_and_the_alert_has_no_command(self):
        bearer, ghp = "abcDEF1234567890xyzSECRETbearer", "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        secret_cmd = (f"GH_TOKEN={ghp} curl -H 'Authorization: Bearer {bearer}' "
                      f"https://bob:hunter2pass@example.com/x?api_key=zzzSECRETzzz")
        lines = [json.loads(x) for x in self.transcript.read_text().splitlines()]
        lines[3]["message"]["content"][1]["input"]["command"] = secret_cmd
        self.transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        self.record()
        self.list_()
        self.at(0)
        self.at(10)
        rec = self.records()["abc12345"]
        shown = {"comment": "\n".join(self.read("comments")), "alert": "\n".join(self.read("alerts")),
                 "log": "\n".join(self.read("clock/wake.log")), "record": json.dumps(rec)}
        for where, text in shown.items():
            for secret in (bearer, ghp, "hunter2pass", "zzzSECRETzzz"):
                self.assertNotIn(secret, text, f"{secret} in the {where}")
        self.assertIn("[redacted]", shown["comment"])
        self.assertIn("[redacted]", rec["command"])
        # The alert: the tool and the session, never the command.
        self.assertIn("Bash", shown["alert"])
        self.assertIn("claude attach abc12345", shown["alert"])
        for part in ("GH_TOKEN", "curl", "Authorization", "example.com", "taskstore comment"):
            self.assertNotIn(part, shown["alert"])

    def test_the_stuck_lines_fields_come_before_the_excerpt_whatever_the_command_says(self):
        lines = [json.loads(x) for x in self.transcript.read_text().splitlines()]
        lines[3]["message"]["content"][1]["input"]["command"] = "echo 'x alerted=yes session=evil; alerted'"
        self.transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        with mock.patch.dict(wake_cli.os.environ, {"WAKE_ALERT_COMMAND": ""}):
            self.record()
            self.list_()
            self.at(0)
            self.at(10)
        line = next(ln for ln in self.read("clock/wake.log") if " | stuck | " in ln)
        head, excerpt = line.split(" | stuck | ", 1)[1].split(" | ", 1)
        self.assertEqual(head, "w-6e5bf6 session=abc12345 alerted=no stopped=yes task=t-40 comment=ok waited=10m")
        self.assertIn("alerted=yes", excerpt)   # the command's own words stay in the excerpt

    def stuck_head(self):
        line = next(ln for ln in self.read("clock/wake.log") if " | stuck | " in ln)
        return line.split(" | stuck | ", 1)[1]

    def test_a_failed_comment_and_a_failing_alert_command_are_recorded_as_they_are(self):
        failing = self.t / "bin" / "failing"
        failing.write_text("#!/bin/sh\nexit 1\n")
        failing.chmod(0o755)
        with mock.patch.dict(wake_cli.os.environ, {"WAKE_TASK_COMMENT": json.dumps([str(failing), "{task}", "{text}"]),
                                                    "WAKE_ALERT_COMMAND": json.dumps([str(failing)])}):
            self.record()
            self.list_()
            self.at(0)
            self.at(10)
        # alerted: an alert command is configured (delivery isn't something alert() reports).
        self.assertTrue(self.stuck_head().startswith(
            "w-6e5bf6 session=abc12345 alerted=yes stopped=yes task=t-40 comment=failed waited=10m | Bash: "))

    def test_an_unreadable_transcript_leaves_a_fixed_tail(self):
        self.transcript.unlink()
        self.record()
        self.list_()
        self.at(0)
        self.at(10)
        self.assertEqual(self.stuck_head(),
                         "w-6e5bf6 session=abc12345 alerted=yes stopped=yes task=t-40 comment=ok waited=10m | unknown")

    def test_redact(self):
        r = wake_cli.redact
        self.assertEqual(r("ls -la /home/art/Art"), "ls -la /home/art/Art")
        self.assertNotIn("s3cr3t", r("PASSWORD=s3cr3t ./run"))
        self.assertNotIn("s3cr3t", r("mysql --password s3cr3t"))
        self.assertNotIn("xoxb-12345-abcdefgh", r("SLACK=xoxb-12345-abcdefgh"))
        self.assertNotIn("sk-ant-abcdefghijklmnop", r("call sk-ant-abcdefghijklmnop"))
        self.assertNotIn("deadbeef" * 4, r("sha " + "deadbeef" * 4))
        self.assertNotIn("pw", r("git clone https://me:pw@github.com/x/y"))

    def test_a_look_that_fails_never_raises_and_is_said_once(self):
        self.record()
        for broken in ("not json", '{"a": 1}'):
            self.agents.write_text(broken)
            self.at(0)
            self.at(30)
        claude = self.t / "bin" / "claude"
        claude.unlink()   # claude isn't there at all
        self.at(40)
        with mock.patch.dict(wake_cli.os.environ, {"WAKE_SESSION_COMMAND": "[not json", "WAKE_TASK_COMMENT": "[nope"}):
            self.at(50)   # malformed settings: still no exception, no SystemExit
        self.assertEqual(self.records()["abc12345"]["state"], "running")
        blind = [ln for ln in self.read("clock/wake.log") if "| reap-blind |" in ln]
        self.assertGreaterEqual(len(blind), 1)
        self.assertEqual(len(blind), 4)   # four looks that failed four ways: each said once, not once per look (8 looks)

    def test_a_transcript_line_that_isnt_an_object_is_skipped(self):
        with self.transcript.open("a") as f:
            f.write('[1, 2]\n"text"\n{"type": "assistant", "message": "x"}\n')
        self.assertEqual(wake_cli.last_tool_use(self.transcript), ("Bash", self.COMMAND))

    def test_a_busy_session_with_a_stale_waiting_for_isnt_counted(self):
        self.assertFalse(wake_cli.waiting_on_permission({"status": "busy", "waitingFor": "permission prompt"}))
        self.assertTrue(wake_cli.waiting_on_permission({"status": "waiting", "waitingFor": "permission prompt"}))

    def test_off_doesnt_even_look(self):
        self.record()
        self.agents.write_text("not json")
        with mock.patch.dict(wake_cli.os.environ, {"WAKE_STUCK_AFTER": "off"}):
            self.at(0)
        self.assertFalse(any("reap-blind" in ln for ln in self.read("clock/wake.log")))

    def test_the_service_reaps_every_two_minutes_and_rearms_every_ten(self):
        clock = [0.0]
        sleeps = []

        def sleep(n):
            sleeps.append(n)
            clock[0] += n
            if len(sleeps) > 10:
                raise KeyboardInterrupt

        def boom():
            raise SystemExit("wake: WAKE_SESSION_COMMAND is a JSON list …")

        with mock.patch.dict(wake_cli.os.environ, {"WAKE_LOCAL_AGENT": "ada"}), \
             mock.patch.object(wake_cli, "cmd_rearm"), \
             mock.patch.object(wake_cli, "configure"), \
             mock.patch.object(wake_cli.time, "sleep", side_effect=sleep), \
             mock.patch.object(wake_cli.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(wake_cli, "keep_armed") as keep, \
             mock.patch.object(wake_cli, "reap_stuck", side_effect=boom) as reap, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                wake_cli.cmd_run(wake_cli.argparse.Namespace())
        self.assertEqual(set(sleeps), {wake_cli.REAP_CHECK})
        self.assertEqual(reap.call_count, 10)            # a SystemExit from a look never ends the service
        self.assertEqual(keep.call_count, 10 * wake_cli.REAP_CHECK // wake_cli.RUN_CHECK)

    def test_the_last_tool_call_is_read_from_the_transcript(self):
        self.assertEqual(wake_cli.transcript_of("11111111-2222-3333-4444-555555555555"), self.transcript)
        self.assertIsNone(wake_cli.transcript_of("../../etc/passwd"))
        self.assertEqual(wake_cli.last_tool_use(self.transcript), ("Bash", self.COMMAND))
        other = self.t / "other.jsonl"
        other.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "EnterWorktree", "input": {"path": "/home/art/Art/.worktrees/x"}}]}}) + "\n")
        self.assertEqual(wake_cli.last_tool_use(other), ("EnterWorktree", '{"path": "/home/art/Art/.worktrees/x"}'))


class OnWhoseBehalf(unittest.TestCase):
    """t-1943: every wake and schedule records whom it's for (`for`): the owner's on loom's machine; over the piece API,
    what the body's principal statement says once the runtime verifies it for this same agent, else "-" (unknown).
    The house wrapper's `--principal <statement>` is taken off at the API door, and just before a delivery the clock
    hands the wake's `for` to the runtime. The runtime's command (WAKE_PRINCIPAL) is a stand-in script here: it records
    its argv and stdin, and answers what PRINCIPAL_ANSWER / PRINCIPAL_EXIT say."""

    FAKE = """#!/usr/bin/env python3
import json, os, sys
stdin = sys.stdin.read() if "--verify" in sys.argv else ""
with open(os.environ["PRINCIPAL_RECORD"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin}) + "\\n")
sys.stdout.write(os.environ.get("PRINCIPAL_ANSWER", ""))
sys.exit(int(os.environ.get("PRINCIPAL_EXIT", "0")))
"""
    ADA = {"kind": "agent", "agent": "ada"}

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td, True)
        self.fake = self.td / "principal"
        self.fake.write_text(self.FAKE)
        self.fake.chmod(0o755)
        self.record = self.td / "principal.jsonl"
        env = {"WAKE_PRINCIPAL": str(self.fake), "PRINCIPAL_RECORD": str(self.record), "PRINCIPAL_ANSWER": "",
               "PRINCIPAL_EXIT": "0", "WAKE_ORIGIN": "", "WAKE_CALLER": "", "WAKE_PRINCIPAL_STATEMENT": ""}
        w = self.td / "clocks" / "ada"
        self.patches = [
            mock.patch.dict(wake_cli.os.environ, env),
            mock.patch.object(wake_cli, "WAKE_DIR", w), mock.patch.object(wake_cli, "SPOOL", w / "spool.jsonl"),
            mock.patch.object(wake_cli, "LOCK", w / "spool.lock"), mock.patch.object(wake_cli, "LOG", w / "wake.log"),
            mock.patch.object(wake_cli, "SCHEDULES", w / "schedules.json"), mock.patch.object(wake_cli, "CLOCKS", self.td / "clocks"),
            mock.patch.object(wake_cli, "PAUSED", w / "paused"), mock.patch.object(wake_cli, "AGENT", "ada"),
            mock.patch.object(wake_cli, "arm_timer", return_value="armed"), mock.patch.object(wake_cli, "disarm_timer"),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

    def calls(self):
        return [json.loads(l) for l in self.record.read_text().splitlines()] if self.record.exists() else []

    def spool(self):
        f = wake_cli.SPOOL
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def cli(self, *argv, statement=None, answer=None, exit_code=0, over_api=True):
        wake_cli._PRINCIPAL.clear()     # each call is its own run (its own process, in a real one)
        env = {"PRINCIPAL_ANSWER": json.dumps(answer) if answer is not None else "", "PRINCIPAL_EXIT": str(exit_code)}
        if over_api:
            env.update(WAKE_ORIGIN="agent:ada", WAKE_CALLER=json.dumps(self.ADA), WAKE_PRINCIPAL_STATEMENT=statement or "")
        with mock.patch.dict(wake_cli.os.environ, env), mock.patch.object(wake_cli.sys, "argv", ["wake", *argv]), \
             contextlib.redirect_stdout(io.StringIO()):
            try:
                wake_cli.main()
            except SystemExit as e:
                return str(e.code)
        return None

    def set_for(self, **kw):
        self.assertIsNone(self.cli("in", "1h", "-p", "check the soak", "--note", "t-1 r1", **kw))
        return self.spool()[-1]["for"]

    @staticmethod
    def verified(agent="ada", for_=None, owner=False):
        return {"ok": True, "agent": agent, "conversation": "cv-0123456789ab", "for": for_, "owner": owner}

    def test_an_approval_the_clock_files_or_spends_for_a_body_says_on_whose_behalf(self):
        # A house's local wake asks the owner, and its continuation spends the yes: both with the body's principal, so
        # the card names whom it's for and the spend matches it (approvals --on-behalf, t-1943).
        seen, real_run = [], subprocess.run
        ok = subprocess.CompletedProcess([], 0, "a-1a2b3c4d: asked the owner\n", "")
        with mock.patch.dict(wake_cli.os.environ, {"WAKE_ORIGIN": "agent:ada", "WAKE_CALLER": json.dumps(self.ADA),
                                                   "WAKE_PRINCIPAL_STATEMENT": "stmt-loren",
                                                   "PRINCIPAL_ANSWER": json.dumps(self.verified("ada", "loren"))}), \
                mock.patch.object(wake_cli.subprocess, "run", side_effect=lambda argv, **k: seen.append(argv) or
                                  (real_run(argv, **k) if argv[0] == str(self.fake) else ok)):
            self.assertEqual(wake_cli.file_approval("local-wake", "ada:abc", "run X", "why"), "a-1a2b3c4d")
            self.assertEqual(wake_cli.spend_approval("a-1a2b3c4d", "local-wake", "ada:abc"), "")
        filed, spent = [a for a in seen if a[0] != str(self.fake)]
        self.assertEqual(filed[-4:], ["--as", json.dumps(self.ADA), "--on-behalf", "loren"])
        self.assertEqual(spent[-4:], ["--as", json.dumps(self.ADA), "--on-behalf", "loren"])

    def test_the_owner_s_own_call_over_the_api_is_the_owner_s(self):
        # The owner's caller (WAKE_ORIGIN empty, WAKE_CALLER the owner's): his own wake, with nothing to verify.
        with mock.patch.dict(wake_cli.os.environ, {"WAKE_CALLER": json.dumps({"kind": "owner", "who": "jonathan-barket"})}):
            self.assertEqual(self.set_for(over_api=False), "owner")
        self.assertEqual(self.calls(), [])

    def test_a_body_the_runtime_runs_here_is_its_conversation_s_not_the_owner_s(self):
        agent = wake_cli.AGENT
        cv = {"LOOM_BODY_CONVERSATION": "cv-0123456789ab"}
        with mock.patch.dict(wake_cli.os.environ, cv):
            # Its own statement, fetched from the runtime and verified for this agent.
            self.assertEqual(self.set_for(over_api=False, answer={**self.verified(agent, "loren"), "statement": "st-local"}), "loren")
            argvs = [c["argv"] for c in self.calls()]
            self.assertIn(["--conversation", "cv-0123456789ab"], argvs)
            self.assertIn(["--verify"], argvs)
            self.assertEqual(self.set_for(over_api=False, answer={**self.verified(agent, "jonathan-barket", True), "statement": "st-local"}), "owner")
            # The runtime can't say: unknown, never the owner for being here.
            self.assertEqual(self.set_for(over_api=False, answer={"ok": False}, exit_code=1), "-")
        # A conversation no one can read is a body all the same: unknown, not the owner.
        with mock.patch.dict(wake_cli.os.environ, {"LOOM_BODY_CONVERSATION": "conversation-7"}):
            self.assertEqual(self.set_for(over_api=False), "-")
        # A local body running the owner's schedule now: for whom it works, not the owner.
        self.assertIsNone(self.cli("schedule", "set", "lane1", "--at", "22:44", "-f", "routines/nightly.md", over_api=False))
        with mock.patch.dict(wake_cli.os.environ, {**cv, "PRINCIPAL_ANSWER": json.dumps({**self.verified(agent, "loren"), "statement": "st"})}):
            wake_cli._PRINCIPAL.clear()
            with mock.patch.object(wake_cli.sys, "argv", ["wake", "schedule", "run", "lane1"]), contextlib.redirect_stdout(io.StringIO()):
                try:
                    wake_cli.main()
                except SystemExit:
                    pass
        self.assertEqual(self.spool()[-1]["for"], "loren")
        # No conversation: the owner's own account.
        with mock.patch.dict(wake_cli.os.environ, {"LOOM_BODY_CONVERSATION": ""}):
            self.assertEqual(self.set_for(over_api=False), "owner")
        # A long statement is the runtime's to judge, not cut here.
        long = "a" * 3000 + "." + "b" * 43
        with mock.patch.dict(wake_cli.os.environ, {**cv, "PRINCIPAL_ANSWER": json.dumps({**self.verified(agent, "loren"), "statement": long})}):
            self.assertEqual(wake_cli.fetch_principal("cv-0123456789ab"), long)

    def test_setting_a_schedule_again_never_widens_whom_it_is_for(self):
        self.assertIsNone(self.cli("schedule", "set", "nightly", "--at", "22:44", "-f", "routines/nightly.md",
                                   statement="s", answer=self.verified("ada", "loren")))
        # The owner edits its time on the page (loom's machine): still Loren's.
        self.assertIsNone(self.cli("schedule", "set", "nightly", "--at", "23:10", "-f", "routines/nightly.md", over_api=False))
        self.assertEqual(wake_cli.load_schedules()["nightly"]["for"], "loren")
        # One from before this change, set from a house (no `for`): the owner's edit keeps it unknown.
        scheds = wake_cli.load_schedules()
        scheds["old"] = {**scheds["nightly"], "origin": "agent:ada"}
        scheds["old"].pop("for")
        wake_cli.save_schedules(scheds)
        self.assertIsNone(self.cli("schedule", "set", "old", "--at", "23:10", "-f", "routines/nightly.md", over_api=False))
        self.assertEqual(wake_cli.load_schedules()["old"]["for"], "-")
        # Removed and set anew by the owner: his.
        self.assertIsNone(self.cli("schedule", "rm", "nightly", over_api=False))
        self.assertIsNone(self.cli("schedule", "set", "nightly", "--at", "23:10", "-f", "routines/nightly.md", over_api=False))
        self.assertEqual(wake_cli.load_schedules()["nightly"]["for"], "owner")

    def test_a_person_whose_slug_is_owner_is_not_the_owner(self):
        self.assertEqual(self.set_for(statement="stmt-o", answer=self.verified("ada", "owner", False)), "-")
        self.assertEqual(self.set_for(statement="stmt-j", answer=self.verified("ada", "jonathan-barket", True)), "owner")

    def test_two_different_people_narrow_to_unknown(self):
        self.assertEqual(wake_cli.narrower("loren", "dan"), "-")
        self.assertEqual(wake_cli.narrower("loren", "owner"), "loren")
        self.assertEqual(wake_cli.narrower("owner", "loren"), "loren")
        self.assertEqual(wake_cli.narrower("owner", "owner"), "owner")

    def test_an_empty_last_principal_is_none_not_the_one_before(self):
        self.assertEqual(wake_cli.split_principal(["in", "1h", "--principal", "FORGED", "--principal", ""]), (["in", "1h"], ""))
        self.assertEqual(wake_cli.split_principal(["in", "1h", "--principal", "FORGED", "--principal=real"]), (["in", "1h"], "real"))

    def test_a_wake_set_on_loom_s_machine_is_the_owner_s(self):
        self.assertEqual(self.set_for(over_api=False), "owner")
        self.assertEqual(self.calls(), [], "nothing to verify on loom's machine")

    def test_a_wake_set_over_the_api_is_for_whom_its_verified_statement_says(self):
        self.assertEqual(self.set_for(statement="stmt-owner", answer=self.verified("ada", "jonathan-barket", True)), "owner")
        (call,) = self.calls()
        self.assertEqual((call["argv"], call["stdin"]), (["--verify"], "stmt-owner"), "the statement goes on stdin")
        self.assertEqual(self.set_for(statement="stmt-loren", answer=self.verified("ada", "loren")), "loren")
        # Another agent's statement, a missing one, a verifier that fails or says no, or a slug that isn't one: unknown.
        self.assertEqual(self.set_for(statement="stmt-bo", answer=self.verified("bo", "loren", True)), "-")
        self.assertEqual(self.set_for(statement="stmt-null", answer=self.verified("ada", None)), "-")
        before = len(self.calls())
        self.assertEqual(self.set_for(), "-")
        self.assertEqual(len(self.calls()), before, "no statement: nothing to verify")
        self.assertEqual(self.set_for(statement="stmt-bad", answer={"ok": False}, exit_code=1), "-")
        self.assertEqual(self.set_for(statement="stmt-no", answer={**self.verified("ada", "loren"), "ok": False}), "-")
        self.assertEqual(self.set_for(statement="stmt-weird", answer=self.verified("ada", "../owner")), "-")
        with mock.patch.dict(wake_cli.os.environ, {"WAKE_PRINCIPAL": str(self.td / "missing")}):
            self.assertEqual(self.set_for(statement="stmt-x", answer=self.verified("ada", "loren", True)), "-")
        # The statement itself is never written down: only what it verified to.
        logged = wake_cli.LOG.read_text() + wake_cli.SPOOL.read_text()
        self.assertNotIn("stmt-", logged)

    def test_a_park_is_for_whom_its_body_works_too(self):
        with mock.patch.object(wake_cli, "approval_card", return_value=None):
            self.assertIsNone(self.cli("park", "--on", "a-0000aaaa", "-p", "next", "--note", "t-1 r2",
                                       statement="s", answer=self.verified("ada", "loren")))
        self.assertEqual(self.spool()[-1]["for"], "loren")

    def test_the_principal_is_taken_off_at_the_api_door(self):
        self.assertEqual(wake_cli.split_principal(["list", "--principal", "S"]), (["list"], "S"))
        self.assertEqual(wake_cli.split_principal(["list", "--principal=S"]), (["list"], "S"))
        self.assertEqual(wake_cli.split_principal(["list", "--principal", "MINE", "--principal", "S"]), (["list"], "S"))
        self.assertEqual(wake_cli.split_principal(["in", "1h", "-p", "--principal", "--note", "n"]),
                         (["in", "1h", "-p", "--principal", "--note", "n"], ""), "only a trailing pair")
        seen = []

        def child(argv, **kw):
            seen.append((argv, kw["env"]))
            return _completed(0, "ok\n")
        req = {"caller": self.ADA, "path": "run", "body": {"argv": ["list", "--principal", "stmt-1"]}}
        out = io.StringIO()
        with mock.patch.object(wake_cli.sys, "stdin", io.StringIO(json.dumps(req))), \
             mock.patch.object(wake_cli.subprocess, "run", side_effect=child), contextlib.redirect_stdout(out):
            wake_cli.cmd_api(None)
        ((argv, env),) = seen
        self.assertEqual(argv[-3:], ["--agent", "ada", "list"])
        self.assertEqual((env["WAKE_PRINCIPAL_STATEMENT"], env["WAKE_ORIGIN"]), ("stmt-1", "agent:ada"))
        self.assertEqual(json.loads(out.getvalue())["status"], 200)

    def test_wake_list_over_the_api_with_a_principal_appended_still_works(self):
        req = {"caller": self.ADA, "path": "run", "body": {"argv": ["list", "--principal", "stmt-1"]}}
        out = io.StringIO()
        with mock.patch.object(wake_cli.sys, "stdin", io.StringIO(json.dumps(req))), contextlib.redirect_stdout(out):
            wake_cli.cmd_api(None)
        ans = json.loads(out.getvalue())
        self.assertEqual((ans["status"], ans["body"]["exit"]), (200, 0), ans)
        self.assertNotIn("unrecognized", ans["body"]["stderr"])
        # And typed on loom's machine, it's taken off too.
        self.assertIsNone(self.cli("list", "--principal", "stmt-1", over_api=False))

    def test_a_schedule_set_over_the_api_carries_its_principal_into_every_occurrence(self):
        self.assertIsNone(self.cli("schedule", "set", "nightly", "--at", "22:44", "-f", "routines/nightly.md",
                                   statement="s", answer=self.verified("ada", "loren")))
        sc = wake_cli.load_schedules()["nightly"]
        self.assertEqual(sc["for"], "loren")
        self.assertEqual(self.spool()[-1]["for"], "loren")
        self.assertEqual(wake_cli.schedule_entry("nightly", sc, wake_cli.datetime.now())["for"], "loren", "the next one too")
        # Run now by the owner's body: no wider than the schedule's.
        self.assertIsNone(self.cli("schedule", "run", "nightly", statement="s", answer=self.verified("ada", owner=True)))
        self.assertEqual(self.spool()[-1]["for"], "loren")
        # The owner's schedule (set here), run now by a body working for someone else: theirs, not the owner's.
        self.assertIsNone(self.cli("schedule", "set", "weekly", "--at", "17:47", "-f", "routines/x.md", over_api=False))
        self.assertEqual(wake_cli.load_schedules()["weekly"]["for"], "owner")
        self.assertIsNone(self.cli("schedule", "run", "weekly", statement="s", answer=self.verified("ada", "loren")))
        self.assertEqual(self.spool()[-1]["for"], "loren")
        self.assertIsNone(self.cli("schedule", "run", "weekly", statement="s", answer=self.verified("ada", owner=True)))
        self.assertEqual(self.spool()[-1]["for"], "owner")
        self.assertIsNone(self.cli("schedule", "run", "weekly", over_api=False))
        self.assertEqual(self.spool()[-1]["for"], "owner")

    def deliver(self, entry, exit_code=0, principal=None):
        """Fire `entry` into a delivery: (hand-over calls seen when the POST went, the posts, the log)."""
        posts, at_post = [], []
        tok = self.td / "token"
        tok.write_text("clock-token\n")
        env = {"WAKE_FIRE": "house", "WAKE_DELIVERY": "deliver", "WAKE_DELIVER_TOKEN_FILE": str(tok),
               "PRINCIPAL_EXIT": str(exit_code), **({"WAKE_PRINCIPAL": principal} if principal else {})}
        wake_cli.SPOOL.parent.mkdir(parents=True, exist_ok=True)
        wake_cli.SPOOL.write_text(json.dumps(entry) + "\n")
        self.record.unlink(missing_ok=True)

        def post(b):
            at_post.append(self.calls())
            posts.append(b)
            return 200, {"conversation": "cv-0123456789ab", "seq": 1, "body": "started"}
        with mock.patch.dict(wake_cli.os.environ, env), mock.patch.object(wake_cli, "ensure_deadman"), \
             mock.patch.object(wake_cli, "post_delivery", post), mock.patch.object(wake_cli, "dispatch") as dispatch:
            wake_cli.cmd_fire(mock.Mock(id=entry["id"]))
        dispatch.assert_not_called()
        return at_post, posts, wake_cli.LOG.read_text() if wake_cli.LOG.exists() else ""

    base = {"id": "w-dddddd", "kind": "wake", "at": "2026-01-01 00:00:00", "note": "t-9 round 2", "model": "m",
            "created": "x", "prompt": "check PR 12"}

    def test_a_delivery_hands_over_whom_the_wake_is_for_first(self):
        at_post, posts, _ = self.deliver({**self.base, "origin": "agent:ada", "for": "loren"})
        (seen,) = at_post
        self.assertEqual([c["argv"] for c in seen],
                         [["--wake", "--agent", "ada", "--place", "wake:w-dddddd", "--id", "w-dddddd", "--for", "loren"]], "before the POST, for that delivery")
        self.assertEqual(posts[0]["place"], "wake:w-dddddd")
        at_post, *_ = self.deliver({**self.base, "schedule": "nightly", "kind": "schedule", "for": "owner"})
        self.assertEqual(at_post[0][0]["argv"], ["--wake", "--agent", "ada", "--place", "lane:nightly", "--id", "w-dddddd", "--for", "owner"])

    def test_a_retried_delivery_hands_over_for_the_id_loom_is_given(self):
        # A retry re-ids the entry but resends the delivery it kept: the hand-over names the delivery's id.
        body = {"id": "w-dddddd", "agent": "ada", "venue": "wake", "place": "wake:w-dddddd", "label": "x",
                "from": {"kind": "schedule"}, "kind": "wake", "at": "2026-01-01T00:00:00Z", "text": "check PR 12"}
        at_post, posts, _ = self.deliver({**self.base, "id": "w-eeeeee", "origin": "agent:ada", "for": "loren", "delivery": body})
        self.assertEqual(at_post[0][0]["argv"][:8], ["--wake", "--agent", "ada", "--place", "wake:w-dddddd", "--id", "w-dddddd", "--for"])
        self.assertEqual(posts[0]["id"], "w-dddddd")

    def test_a_hand_over_that_fails_doesnt_stop_the_delivery(self):
        _, posts, logged = self.deliver({**self.base, "for": "owner"}, exit_code=1)
        self.assertEqual(len(posts), 1)
        self.assertIn("principal-fail | wake:w-dddddd for=owner: exit 1", logged)
        _, posts, logged = self.deliver({**self.base, "for": "owner"}, principal=str(self.td / "missing"))
        self.assertEqual(len(posts), 1)
        self.assertIn("didn't run", logged)

    def test_a_wake_from_before_hands_over_owner_or_unknown_by_where_it_was_set(self):
        at_post, *_ = self.deliver(dict(self.base))
        self.assertEqual(at_post[0][0]["argv"][-2:], ["--for", "owner"])
        at_post, *_ = self.deliver({**self.base, "origin": "agent:ada"})
        self.assertEqual(at_post[0][0]["argv"][-2:], ["--for", "-"])
        self.assertEqual(wake_cli.entry_for({"for": "../x"}), "-")

    # ─── t-1974: the household's own wakes keep the owner's trust ────────────────

    def fire_for(self, entry):
        """The `--for` the clock hands the runtime as `entry` fires."""
        at_post, *_ = self.deliver(entry)
        return at_post[0][0]["argv"][-1]

    def test_a_lane_wake_fires_with_owner(self):
        # A household lane (a schedule set on loom's machine): every occurrence is the owner's, and fires as his.
        self.assertIsNone(self.cli("schedule", "set", "plan-night", "--at", "23:41", "-f", "routines/nightly.md", over_api=False))
        occ = self.spool()[-1]
        self.assertEqual((occ["kind"], occ["for"]), ("schedule", "owner"))
        self.assertEqual(self.fire_for(occ), "owner")
        # And a wake that lane's own body sets from its owner house, with its statement: the owner's, said where.
        self.assertEqual(self.set_for(statement="stmt-lane", answer=self.verified("ada", "jonathan-barket", True)), "owner")
        self.assertEqual(self.spool()[-1]["for_in"], "cv-0123456789ab")
        self.assertEqual(self.fire_for(self.spool()[-1]), "owner")

    def test_a_wake_set_from_a_loren_conversation_fires_for_loren(self):
        self.assertEqual(self.set_for(statement="stmt-loren", answer=self.verified("ada", "loren")), "loren")
        w = self.spool()[-1]
        self.assertEqual(w["for_in"], "cv-0123456789ab")
        # The owner can't vouch it into his: it's Loren's work.
        with mock.patch.object(wake_cli, "owner_at_terminal", return_value=True):
            self.assertIn("stays theirs", self.cli("vouch", w["id"], over_api=False))
        self.assertEqual(self.spool()[-1]["for"], "loren")
        self.assertEqual(self.fire_for(w), "loren")

    def test_an_unsigned_wake_stays_unknown(self):
        # No statement (a stale wrapper): unknown, said so in the log, and fired as unknown.
        self.assertEqual(self.set_for(), "-")
        w = self.spool()[-1]
        self.assertNotIn("for_in", w)
        self.assertIn("principal-none | ada called with no principal statement", wake_cli.LOG.read_text())
        self.assertEqual(self.fire_for(w), "-")
        # A statement that doesn't verify: the same.
        self.assertEqual(self.set_for(statement="stmt-forged", answer={"ok": False}, exit_code=1), "-")
        self.assertNotIn("for_in", self.spool()[-1])
        # One from before, set from a house: unknown, never promoted by where it fires.
        self.assertEqual(self.fire_for({**self.base, "origin": "agent:ada"}), "-")

    def test_verified_unknown_work_keeps_display_origin_without_owner_authority(self):
        answer = {**self.verified("ada", None), "people": ["loren"]}
        self.assertEqual(self.set_for(statement="stmt-unknown", answer=answer), "-")
        entry = self.spool()[-1]
        self.assertEqual(entry["for_in"], "cv-0123456789ab")
        self.assertEqual((entry["for_why"], entry["for_people"]), ("steered", ["loren"]))
        self.assertEqual(self.fire_for(entry), "-", "a display link never promotes unknown work to owner work")
        self.assertEqual(self.set_for(statement="stmt-other", answer=self.verified("bo", None)), "-")
        self.assertNotIn("for_in", self.spool()[-1], "another agent's verified conversation cannot supply this origin")

    def test_only_the_owner_at_a_terminal_vouches_for_an_unknown_wake(self):
        self.assertEqual(self.set_for(), "-")
        wid = self.spool()[-1]["id"]
        # Over the API, from a house, or from a body the runtime runs here: refused, whatever a terminal says.
        self.assertNotIn("vouch", wake_cli.REMOTE_VERBS)
        with mock.patch.object(wake_cli, "owner_at_terminal", return_value=True):
            self.assertIn("never a body's", self.cli("vouch", wid))
            for k, v in (("LOOM_BODY_CONVERSATION", "cv-0123456789ab"), ("LOOM_BODY_KEY", "k")):
                with mock.patch.dict(wake_cli.os.environ, {k: v}):
                    self.assertIn("never a body's", self.cli("vouch", wid, over_api=False))
            with mock.patch.dict(wake_cli.os.environ, {"HOUSE_ID": "h-1"}):     # a house's goes to loom's API, refused there
                self.assertIsNotNone(self.cli("vouch", wid, over_api=False))
                wake_cli._PRINCIPAL.clear()
                self.assertIn("never a body's", self._vouch_error(wid))
        # No terminal (piped, a background session): refused, and nothing changes.
        with mock.patch.object(wake_cli.os, "open", side_effect=OSError("no tty")):
            self.assertIn("at a terminal", self.cli("vouch", wid, over_api=False))
        self.assertEqual(self.spool()[-1]["for"], "-")
        # The wrong ids typed: refused.
        with mock.patch.object(wake_cli, "owner_at_terminal", side_effect=lambda prompt, want: False):
            self.assertIn("at a terminal", self.cli("vouch", wid, over_api=False))
        # The owner, typing its id: the owner's from here, stamped as vouched, and it fires as his.
        typed = []
        with mock.patch.object(wake_cli, "owner_at_terminal", side_effect=lambda prompt, want: typed.append(want) or True):
            self.assertIsNone(self.cli("vouch", wid, over_api=False))
        self.assertEqual(typed, [wid])
        w = self.spool()[-1]
        self.assertEqual((w["for"], w["vouched"]["by"], w["vouched"]["was"]), ("owner", "owner", "-"))
        self.assertIn(f"vouch | {wid}", wake_cli.LOG.read_text())
        # Already the owner's, a schedule's occurrence, or one that isn't there: nothing to vouch for.
        with mock.patch.object(wake_cli, "owner_at_terminal", return_value=True):
            self.assertIn("already for owner", self.cli("vouch", wid, over_api=False))
            self.assertIn("no pending wake", self.cli("vouch", "w-ffffff", over_api=False))
        self.assertEqual(self.fire_for(w), "owner")

    def _vouch_error(self, wid):
        try:
            wake_cli.cmd_vouch(mock.Mock(ids=[wid]))
        except SystemExit as e:
            return str(e.code)
        return ""

    def test_the_terminal_word_is_read_from_dev_tty_exactly(self):
        r, w = os.pipe()
        os.write(w, b"w-aaaaaa w-bbbbbb\n")
        written = []
        with mock.patch.object(wake_cli.os, "open", return_value=r), \
             mock.patch.object(wake_cli.os, "write", side_effect=lambda fd, b: written.append(b) or len(b)):
            self.assertTrue(wake_cli.owner_at_terminal("? ", "w-aaaaaa w-bbbbbb"))
        os.close(w)
        r, w = os.pipe()
        os.write(w, b"w-aaaaaa\n")
        with mock.patch.object(wake_cli.os, "open", return_value=r), mock.patch.object(wake_cli.os, "write", return_value=2):
            self.assertFalse(wake_cli.owner_at_terminal("? ", "w-aaaaaa w-bbbbbb"))
        os.close(w)


class OwnerAsksOnThePage(OnWhoseBehalf):
    """t-1975: unknown wakes that look like the owner's reach him as one card on loom's page (a vouch-wake approval the
    clock files); his Allow vouches for exactly its wakes, his Deny changes nothing, and nobody else's call applies it.
    The approvals piece is a stand-in here: its store is a JSON file the test answers in, as the owner would."""

    APPROVALS = """#!/usr/bin/env python3
import json, os, sys
path = os.environ["FAKE_APPROVALS"]
d = json.load(open(path)) if os.path.exists(path) else {"n": 0, "a": {}, "calls": []}
argv = sys.argv[1:]
d["calls"].append(argv)
def opt(k):
    return argv[argv.index(k) + 1] if k in argv else None
out, code = "", 0
NEED = {"request": ("--kind", "--target", "--ask", "--why", "--if-yes", "--if-no", "--details", "--as", "--on-behalf"),
        "use": ("--kind", "--target", "--as", "--on-behalf"), "withdraw": ("--as",)}
if any(k not in argv for k in NEED.get(argv[0], ())):     # the real piece's flags, every one (loom-approvals' run)
    sys.stderr.write("usage\\n")
    sys.exit(2)
if argv[0] == "request":
    hit = [i for i, a in d["a"].items() if a["kind"] == opt("--kind") and a["target"] == opt("--target") and a["state"] == "pending"]
    if hit:
        aid = hit[0]
    else:
        d["n"] += 1
        aid = "a-%08x" % d["n"]
        d["a"][aid] = {"kind": opt("--kind"), "target": opt("--target"), "state": "pending", "details": opt("--details"),
                       "ask": opt("--ask"), "as": opt("--as"), "on_behalf": opt("--on-behalf")}
    out = aid + ": asked the owner\\n"
elif argv[0] == "check":
    out = d["a"][argv[1]]["state"] + "\\n"
elif argv[0] == "use":
    a = d["a"].get(argv[1])
    if not a or a["state"] != "allowed" or a["kind"] != opt("--kind") or a["target"] != opt("--target"):
        out, code = "not allowed for that\\n", 1
    else:
        a["state"] = "used"
        out = argv[1] + ": used\\n"
elif argv[0] == "withdraw":
    a = d["a"][argv[1]]
    if a["state"] != "pending":
        out, code = "an answer stands\\n", 1
    else:
        a["state"] = "withdrawn"
        out = argv[1] + ": withdrawn\\n"
json.dump(d, open(path, "w"))
(sys.stdout if code == 0 else sys.stderr).write(out)
sys.exit(code)
"""
    OWNER = {"kind": "owner", "who": "jonathan-barket", "agents": "*"}

    def setUp(self):
        super().setUp()
        fake = self.td / "approvals"
        fake.write_text(self.APPROVALS)
        fake.chmod(0o755)
        self.store = self.td / "approvals.json"
        p = mock.patch.dict(wake_cli.os.environ, {"WAKE_APPROVALS": str(fake), "FAKE_APPROVALS": str(self.store)})
        p.start()
        self.addCleanup(p.stop)

    def approvals(self):
        return json.loads(self.store.read_text()) if self.store.exists() else {"a": {}, "calls": []}

    def answer(self, aid, state):
        d = self.approvals()
        d["a"][aid]["state"] = state
        self.store.write_text(json.dumps(d))

    def unsigned(self, n):
        """n unknown wakes set from ada's house with no word from the runtime: they look like the owner's."""
        for _ in range(n):
            self.assertEqual(self.set_for(), "-")
        return [e["id"] for e in self.spool()[-n:]]

    def act(self, aid, caller=OWNER, target=None):
        d = self.approvals()["a"][aid]
        req = {"action": "vouch-approved", "fields": {"id": aid, "agent": "ada", "target": target or d["target"]}}
        if caller:
            req["caller"] = caller
        def run(agent, argv):
            out = io.StringIO()
            with mock.patch.dict(wake_cli.os.environ, {"WAKE_ORIGIN": "", "WAKE_CALLER": ""}), contextlib.redirect_stdout(out):
                try:
                    wake_cli.cmd_vouch(argparse.Namespace(ids=[a for a in argv if a.startswith("w-")], approval=argv[argv.index("--approval") + 1]))
                except SystemExit as e:
                    return 1, out.getvalue(), str(e.code)
            return 0, out.getvalue(), ""
        return wake_cli.ui_act(req, run)

    def test_unknown_wakes_that_look_like_the_owner_s_become_one_card(self):
        ids = self.unsigned(3)
        self.assertEqual({self.spool()[-1]["for_why"]}, {"none"})
        card = wake_cli.vouch_pass()
        self.assertEqual(card["ids"], sorted(ids))
        a = self.approvals()["a"][card["aid"]]
        self.assertEqual((a["kind"], a["target"], a["on_behalf"]), ("vouch-wake", "ada:" + ",".join(sorted(ids)), "owner"))
        self.assertEqual(a["ask"], "Count these 3 scheduled jobs as your work?")
        self.assertEqual(len(a["details"].splitlines()), 3)
        self.assertIn("fires", a["details"])
        self.assertLessEqual(len(a["details"]), 1000)
        # Never repeated: another look files nothing new.
        self.assertEqual(wake_cli.vouch_pass(), card)
        self.assertEqual(len(self.approvals()["a"]), 1)

    def test_allow_vouches_for_exactly_its_wakes_and_they_fire_as_the_owner_s(self):
        ids = self.unsigned(2)
        card = wake_cli.vouch_pass()
        extra = self.unsigned(1)[0]     # set after the card: not on it
        self.answer(card["aid"], "allowed")
        r = self.act(card["aid"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["message"], "2 of Ada's scheduled jobs count as your work now.")
        by = {e["id"]: e for e in self.spool()}
        for i in ids:
            self.assertEqual((by[i]["for"], by[i]["vouched"]["approval"]), ("owner", card["aid"]))
            self.assertEqual(self.fire_for(by[i]), "owner")
        self.assertEqual(by[extra]["for"], "-", "the card covers exactly its listed wakes")
        self.assertEqual(self.approvals()["a"][card["aid"]]["state"], "used")
        # Spent once: with the same wakes unknown again, the same Allow is refused by the approvals piece, not by the spool.
        with wake_cli.Spool() as sp:
            sp.entries = [{**self.base, "id": i, "origin": "agent:ada", "for": "-", "for_why": "none"} for i in ids]
        r = self.act(card["aid"])
        self.assertFalse(r["ok"])
        self.assertIn("couldn't be spent", r["message"])
        # Every spend the clock made: as itself for ada, on the owner's behalf, for exactly the card's target.
        spends = [c for c in self.approvals()["calls"] if c[0] == "use"]
        for c in spends:
            self.assertEqual(c[c.index("--as") + 1], wake_cli.clock_as())
            self.assertEqual(c[c.index("--on-behalf") + 1], "owner")
            self.assertEqual(c[c.index("--target") + 1], self.approvals()["a"][card["aid"]]["target"])
        filed = [c for c in self.approvals()["calls"] if c[0] == "request"]
        self.assertTrue(all(c[c.index("--as") + 1] == wake_cli.clock_as() for c in filed))

    def test_a_target_can_t_be_widened(self):
        ids = self.unsigned(2)
        card = wake_cli.vouch_pass()
        more = self.unsigned(1)[0]
        self.answer(card["aid"], "allowed")
        wider = "ada:" + ",".join(sorted([*ids, more]))
        r = self.act(card["aid"], target=wider)
        self.assertFalse(r["ok"])
        self.assertIn("couldn't be spent", r["message"])
        self.assertEqual({e["for"] for e in self.spool()}, {"-"})
        self.assertEqual(self.approvals()["a"][card["aid"]]["state"], "allowed", "the owner's yes is still there for its own wakes")

    def test_deny_changes_nothing_and_is_never_asked_again(self):
        ids = self.unsigned(2)
        card = wake_cli.vouch_pass()
        self.answer(card["aid"], "denied")
        self.assertFalse(self.act(card["aid"])["ok"])
        self.assertIsNone(wake_cli.vouch_pass())
        self.assertEqual({e["for"] for e in self.spool()}, {"-"})
        self.assertEqual(len(self.approvals()["a"]), 1)
        self.assertIn("already", self.cli("ask-vouch", ids[0]))
        # A new one later gets a card of its own, without the denied ones.
        new = self.unsigned(1)[0]
        self.assertEqual(wake_cli.vouch_pass()["ids"], [new])

    def test_only_the_household_owner_s_call_applies_it(self):
        ids = self.unsigned(1)
        card = wake_cli.vouch_pass()
        self.answer(card["aid"], "allowed")
        for caller in (None, {"kind": "owner", "who": "loren", "agents": ["ada"]}, {"kind": "agent", "agent": "ada"}):
            r = self.act(card["aid"], caller=caller)
            self.assertFalse(r["ok"], caller)
        self.assertEqual(self.spool()[-1]["for"], "-")
        # Nor a body: over the API vouch isn't a verb, and here a body's environment is refused before anything is spent.
        self.assertIn("runs only on loom's machine", json.dumps(wake_cli.api_answer(
            {"caller": {"kind": "agent", "agent": "ada"}, "path": "run", "body": {"argv": ["vouch", "--approval", card["aid"], *ids]}},
            lambda *a: (0, "", ""))))
        with mock.patch.dict(wake_cli.os.environ, {"LOOM_BODY_CONVERSATION": "cv-0123456789ab"}):
            self.assertIn("never a body's", self.cli("vouch", "--approval", card["aid"], *ids, over_api=False))
        self.assertEqual(self.approvals()["a"][card["aid"]]["state"], "allowed")
        self.assertTrue(self.act(card["aid"])["ok"])

    def test_a_new_one_refreshes_the_card_and_one_that_fires_doesn_t(self):
        first = self.unsigned(2)
        card = wake_cli.vouch_pass()
        new = self.unsigned(1)[0]
        card2 = wake_cli.vouch_pass()
        self.assertNotEqual(card2["aid"], card["aid"])
        self.assertEqual(card2["ids"], sorted([*first, new]))
        self.assertEqual(self.approvals()["a"][card["aid"]]["state"], "withdrawn")
        # One fires (it leaves the spool): the card stays as it is.
        with wake_cli.Spool() as sp:
            sp.entries = [e for e in sp.entries if e["id"] != first[0]]
        self.assertEqual(wake_cli.vouch_pass(), card2)
        # Allowed now: the one that fired is said, the rest vouched.
        self.answer(card2["aid"], "allowed")
        r = self.act(card2["aid"])
        self.assertTrue(r["ok"], r)
        self.assertIn("1 had fired already", r["message"])
        # All of a card's wakes gone: it's withdrawn, nothing left to decide.
        more = self.unsigned(1)[0]
        c3 = wake_cli.vouch_pass()
        with wake_cli.Spool() as sp:
            sp.entries = [e for e in sp.entries if e["id"] != more]
        self.assertIsNone(wake_cli.vouch_pass())
        self.assertEqual(self.approvals()["a"][c3["aid"]]["state"], "withdrawn")

    def test_the_clock_applies_an_allow_loom_didn_t(self):
        ids = self.unsigned(1)
        card = wake_cli.vouch_pass()
        self.answer(card["aid"], "allowed")
        self.assertIsNone(wake_cli.vouch_pass())
        self.assertEqual(self.spool()[-1]["for"], "owner")
        self.assertIn("vouch-applied", wake_cli.LOG.read_text())

    def test_what_the_clock_won_t_ask_about_by_itself(self):
        # From before principals were kept, one a person steered, one set on loom's machine: no card.
        with wake_cli.Spool() as sp:
            sp.entries.append({**self.base, "id": "w-aaaaa1", "origin": "agent:ada"})
        self.assertEqual(self.set_for(statement="stmt", answer={**self.verified("ada"), "people": ["loren", "dan"]}), "-")
        self.assertEqual(self.spool()[-1]["for_people"], ["loren", "dan"])
        self.assertIsNone(wake_cli.vouch_pass())
        # A body that knows asks about the old one; a person's stays theirs.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(self.cli("ask-vouch", "w-aaaaa1", over_api=False))
        self.assertIn("stays theirs", self.cli("ask-vouch", self.spool()[-1]["id"]))
        (card,) = [a for a in self.approvals()["a"].values() if a["state"] == "pending"]
        self.assertEqual(card["target"], "ada:w-aaaaa1")
        self.assertEqual(card["ask"], "Count this scheduled job as your work?")
        # Not about a wake already the owner's, a schedule's occurrence, or one that isn't there.
        self.assertIn("no pending wake", self.cli("ask-vouch", "w-ffffff"))

    def test_another_agent_s_wake_or_a_malformed_id_is_refused_before_anything_is_spent(self):
        ids = self.unsigned(1)
        card = wake_cli.vouch_pass()
        self.answer(card["aid"], "allowed")
        for target, agent in (("bo:" + ids[0], "bo"), ("ada:" + ids[0], "bo"), ("ada:w-ZZZZZZ", "ada"), ("ada:" + ids[0] + ",*", "ada"),
                              ("ada:w-ffffff," + ids[0], "ada")):
            req = {"action": "vouch-approved", "caller": self.OWNER, "fields": {"id": card["aid"], "agent": agent, "target": target}}
            ran = []
            r = wake_cli.ui_act(req, lambda agent, argv: ran.append(argv) or (1, "", "no"))
            self.assertFalse(r["ok"], target)
            self.assertEqual(ran, [], f"refused before the clock ran anything: {target} for {agent}")
        self.assertEqual(self.approvals()["a"][card["aid"]]["state"], "allowed")
        self.assertEqual(self.spool()[-1]["for"], "-")
        # The card lists every wake on it by id, with when it fires.
        self.assertIn(ids[0] + ", fires", self.approvals()["a"][card["aid"]]["details"])

    def test_only_a_body_with_no_word_from_the_runtime_is_asked_about_by_itself(self):
        # none and unverified: no evidence anyone else was behind it. steered (the runtime said unknown), other-agent,
        # a person named, or from before principals were kept: never by itself.
        for why, people, asked in (("none", [], True), ("unverified", [], False), ("steered", [], False), ("other-agent", [], False),
                                   ("none", ["loren"], False), (None, [], False), ("none", [], "local")):
            with wake_cli.Spool() as sp:
                # "local": set by a body the runtime runs on loom's machine (no house origin): never by itself.
                origin = {} if asked == "local" else {"origin": "agent:ada"}
                asked = False if asked == "local" else asked
                sp.entries = [{**self.base, "id": "w-aaaaa1", **origin, "for": "-",
                               **({"for_why": why} if why else {}), **({"for_people": people} if people else {})}]
            self.assertEqual(wake_cli.vouch_candidates(self.spool()) == ["w-aaaaa1"], asked, (why, people))
        # A statement that verifies as a person whose slug is "owner": unknown, steered, never asked about.
        self.assertEqual(self.set_for(statement="stmt", answer={**self.verified("ada", "owner"), "people": []}), "-")
        self.assertEqual(self.spool()[-1]["for_why"], "steered")
        self.assertNotIn(self.spool()[-1]["id"], wake_cli.vouch_candidates(self.spool()))

    def test_a_schedule_s_occurrences_keep_why_it_s_unknown(self):
        e = wake_cli.schedule_entry("nightly", {"at": "23:00", "prompt": "x", "origin": "agent:ada", "for": "-",
                                                "for_why": "steered", "for_people": ["loren"]}, wake_cli.datetime.now())
        self.assertEqual((e["for_why"], e["for_people"]), ("steered", ["loren"]))

    def test_a_full_card_of_long_notes_fits_approvals_cap_as_approvals_counts_it(self):
        import unicodedata
        with wake_cli.Spool() as sp:
            sp.entries = [{**self.base, "id": f"w-00000{i:x}", "origin": "agent:ada", "note": "t-1076 wake-chain-density round 8 " + "x " * 200,
                           "prompt": "check everything " * 50} for i in range(12)]
        d = wake_cli.vouch_listing(self.spool(), [f"w-00000{i:x}" for i in range(12)])
        self.assertLessEqual(len(unicodedata.normalize("NFKC", d)), 1000)
        for i in range(12):
            self.assertIn(f"- w-00000{i:x}, fires", d)


    def test_the_target_is_sorted_and_each_wake_once(self):
        self.assertEqual(wake_cli.vouch_target(["w-cccccc", "w-aaaaaa", "w-cccccc", "w-bbbbbb"]), "ada:w-aaaaaa,w-bbbbbb,w-cccccc")

    def test_a_body_s_ask_never_spends_an_allowed_card(self):
        ids = self.unsigned(2)
        card = wake_cli.vouch_pass()
        self.answer(card["aid"], "allowed")
        self.cli("ask-vouch", ids[0])
        with mock.patch.dict(wake_cli.os.environ, {"LOOM_BODY_CONVERSATION": "cv-0123456789ab"}):
            self.cli("ask-vouch", ids[0], over_api=False)     # a body the runtime runs here: kept, never a pass
        self.assertEqual(self.approvals()["a"][card["aid"]]["state"], "allowed")
        self.assertEqual({e["for"] for e in self.spool()}, {"-"})
        wake_cli.vouch_pass()       # the service's own pass applies it
        self.assertEqual({e["for"] for e in self.spool()}, {"owner"})

    def test_the_card_never_carries_a_wake_s_prompt_and_its_words_are_plain(self):
        with wake_cli.Spool() as sp:
            sp.entries.append({**self.base, "id": "w-aaaaa2", "note": "", "prompt": "token=SECRETVALUE and sk-SECRETSECRETSECRET do x", "origin": "agent:ada"})
            sp.entries.append({**self.base, "id": "w-aaaaa3", "note": "check \u202eevil\u202c [link](x)", "origin": "agent:ada"})
        d = wake_cli.vouch_listing(self.spool(), ["w-aaaaa2", "w-aaaaa3"])
        self.assertNotIn("SECRET", d)
        self.assertIn("runs: token=[redacted]", d, "what it runs, with the credential taken out")
        self.assertNotIn("\u202e", d)

    def test_the_deny_record_goes_with_the_clock_in_the_bundle(self):
        self.assertIn("vouch.json", wake_cli.CLOCK_FILES)
        self.assertEqual(wake_cli.vouch_path(), wake_cli.WAKE_DIR / "vouch.json", "the agent's own clock, which the bundle reads")

    def test_a_card_that_can_t_be_read_is_let_go_after_three_passes(self):
        self.unsigned(1)
        card = wake_cli.vouch_pass()
        d = self.approvals()
        del d["a"][card["aid"]]
        self.store.write_text(json.dumps(d))
        with mock.patch.object(wake_cli, "request_state", return_value="unknown"):
            self.assertIsNotNone(wake_cli.vouch_pass())
            self.assertIsNotNone(wake_cli.vouch_pass())
            third = wake_cli.vouch_pass()
        self.assertNotEqual(third["aid"], card["aid"], "let go, and asked again on a card that can be read")

    def test_a_body_never_asks_about_one_the_runtime_said_was_steered_or_another_agent_s(self):
        for why in ("steered", "other-agent"):
            with wake_cli.Spool() as sp:
                sp.entries = [{**self.base, "id": "w-aaaaa4", "origin": "agent:ada", "for": "-", "for_why": why}]
            self.assertIn("isn't asked about", self.cli("ask-vouch", "w-aaaaa4"))
        # A local body's unknown wake keeps why too, so its ask is refused the same.
        with mock.patch.dict(wake_cli.os.environ, {"LOOM_BODY_CONVERSATION": "cv-0123456789ab", "WAKE_PRINCIPAL_STATEMENT": "stmt"}):
            self.assertEqual(self.set_for(over_api=False, answer={**self.verified("ada"), "people": ["loren"]}), "-")
        self.assertEqual(self.spool()[-1]["for_people"], ["loren"])

    def test_over_the_api_a_body_s_ask_is_kept_for_the_service_s_pass(self):
        # One the clock would never ask about by itself (from before principals were kept): only the kept ask files it.
        with wake_cli.Spool() as sp:
            sp.entries.append({**self.base, "id": "w-aaaaa7", "origin": "agent:ada"})
        ids = ["w-aaaaa7"]
        self.assertEqual(wake_cli.vouch_candidates(self.spool()), [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(self.cli("ask-vouch", ids[0]))
        self.assertEqual(self.approvals()["a"], {})
        self.assertEqual(wake_cli.vouch_state()["asked"], ids)
        self.assertEqual(wake_cli.vouch_pass()["ids"], ids)

    def test_the_page_action_runs_the_vouch_on_the_target_s_clock(self):
        seen = []
        run = lambda agent, argv: seen.append((agent, argv)) or (0, "vouched: w-0000b1; fired: ", "")
        with mock.patch.object(wake_cli, "clock_agents", return_value=["ada", "bo"]):
            r = wake_cli.ui_act({"action": "vouch-approved", "caller": self.OWNER,
                                 "fields": {"id": "a-1a2b3c4d", "target": "bo:w-0000b1"}}, run)
        self.assertTrue(r["ok"], r)
        self.assertEqual(seen, [("bo", ["vouch", "--approval", "a-1a2b3c4d", "w-0000b1"])])
        self.assertEqual(r["message"], "Bo's scheduled job counts as your work now.")
        # An agent field that says otherwise: refused before anything runs.
        seen.clear()
        with mock.patch.object(wake_cli, "clock_agents", return_value=["ada", "bo"]):
            self.assertFalse(wake_cli.ui_act({"action": "vouch-approved", "caller": self.OWNER,
                                              "fields": {"id": "a-1a2b3c4d", "agent": "ada", "target": "bo:w-0000b1"}}, run)["ok"])
        self.assertEqual(seen, [])

    def test_a_parked_body_is_never_told_to_spend_an_owner_applied_card(self):
        words = wake_cli.spend_words("a-1a2b3c4d", {"kind": "vouch-wake", "target": "ada:w-000001"})
        self.assertNotIn("approvals use", words)
        self.assertIn("nothing for you to spend", words)


class FindsLoomWithoutPath(unittest.TestCase):
    """A wake set from a systemd user unit has no ~/.local/bin on PATH, so a bare `loom` didn't run and the wake was
    delivered as unknown (t-1976). The clock finds loom itself: WAKE_LOOM, else PATH, else ~/.local/bin/loom."""

    def _home_loom(self, td):
        b = Path(td) / ".local" / "bin"
        b.mkdir(parents=True)
        (b / "loom").write_text("#!/bin/sh\nexit 0\n")
        (b / "loom").chmod(0o755)
        return b / "loom"

    def test_without_path_it_finds_loom_in_home_local_bin(self):
        with tempfile.TemporaryDirectory() as td:
            loom = self._home_loom(td)
            with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "WAKE_LOOM": ""}):
                self.assertEqual(wake_cli.find_loom(home=Path(td)), str(loom))

    def test_loom_on_path_wins_over_home(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as pd:
            self._home_loom(td)
            (Path(pd) / "loom").write_text("#!/bin/sh\n")
            (Path(pd) / "loom").chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": f"{pd}:/usr/bin:/bin", "WAKE_LOOM": ""}):
                self.assertEqual(wake_cli.find_loom(home=Path(td)), str(Path(pd) / "loom"))

    def test_wake_loom_still_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            self._home_loom(td)
            with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "WAKE_LOOM": "/opt/loom"}):
                self.assertEqual(wake_cli.find_loom(home=Path(td)), "/opt/loom")

    def test_nowhere_it_stays_bare_loom(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "WAKE_LOOM": ""}):
                self.assertEqual(wake_cli.find_loom(home=Path(td)), "loom")

    def test_the_principal_command_runs_from_a_bare_path(self):
        """End to end: a fresh module load with PATH=/usr/bin:/bin and loom only in ~/.local/bin."""
        with tempfile.TemporaryDirectory() as td:
            loom = self._home_loom(td)
            env = {**os.environ, "PATH": "/usr/bin:/bin", "HOME": td, "WAKE_LOOM": ""}
            code = ("import sys; from pathlib import Path; ns={'__file__': sys.argv[1], '__name__': 'wake_t'}; "
                    "exec(compile(Path(sys.argv[1]).read_text(), sys.argv[1], 'exec'), ns); print(ns['principal_argv']()[0])")
            r = subprocess.run([sys.executable, "-c", code, str(SCRIPT)], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), str(loom))


if __name__ == "__main__":
    unittest.main()
