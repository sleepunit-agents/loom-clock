"""Watch parking against the real atomic spool, with only registry/network/timers stubbed."""
import argparse
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from test_wake import wake_cli as w


class WatchParks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.paths = mock.patch.multiple(w, WAKE_DIR=root, SPOOL=root/'spool.jsonl', LOCK=root/'spool.lock', LOG=root/'wake.log')
        self.paths.start()
        self.addCleanup(self.paths.stop)
        for name in ('arm_timer', 'disarm_timer', 'hand_over_principal', 'alert'):
            patch = mock.patch.object(w, name)
            setattr(self, name, patch.start())
            self.addCleanup(patch.stop)
        self.observation = 'A'
        self.error = False
        p = mock.patch.object(w, 'read_watch_set', side_effect=self.read)
        p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(w, 'post_delivery', side_effect=self.deliver)
        p.start(); self.addCleanup(p.stop)
        self.deliveries = []
        p = mock.patch.object(w, 'paused', return_value=False)
        p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(w, 'principal_for', return_value='owner')
        p.start(); self.addCleanup(p.stop)

    def read(self, entry):
        if self.error:
            raise ValueError('not computed yet')
        return {w.watch_key(t): {'identity': json.dumps([t['kind'], w.AGENT, t['id']]), 'value': self.observation} for t in entry['targets']}, []

    def deliver(self, body):
        # Reservation must already be durable, even if this call never answers.
        self.assertEqual(self.entry()['pending']['delivery'], body)
        self.deliveries.append(body)
        return 200, {'body': 'started'}

    def park(self, targets=None):
        w.cmd_watch_park(argparse.Namespace(role='coordinate', park_on=targets or ['watch:tasks.task:t-1'],
            prompt='Check the work', note='Coordination', model='test', slot=''))

    def entry(self):
        return next(e for e in w.read_spool() if e.get('kind') == 'watch-park')

    def gate(self):
        w.cmd_fire(argparse.Namespace(id=self.entry()['id']))

    def finish(self, action='finish'):
        w.cmd_watch_finish(argparse.Namespace(action=action, token=self.entry()['pending']['token']))

    def test_quiet_change_acknowledges_only_shown_then_flip_back_triggers(self):
        self.park(); self.gate()
        self.assertEqual(len(self.deliveries), 0)
        self.observation = 'B'; self.gate()
        self.assertEqual(len(self.deliveries), 1)
        self.observation = 'A'; self.gate()
        self.assertEqual(len(self.deliveries), 1)  # reserved
        self.finish()
        self.assertEqual(next(iter(self.entry()['baseline'].values()))['value'], 'B')
        self.gate()
        self.assertEqual(len(self.deliveries), 2)

    def test_replace_mid_batch_preserves_snapshot_and_readded_baseline(self):
        self.park(); self.observation = 'B'; self.gate()
        pending = self.entry()['pending']
        self.observation = 'C'
        self.park(['watch:tasks.task:t-2'])
        self.park(['watch:tasks.task:t-1'])
        self.assertEqual(self.entry()['pending'], pending)
        self.assertEqual(next(iter(self.entry()['baseline'].values()))['value'], 'A')
        self.finish(); self.gate()
        self.assertEqual(len(self.deliveries), 2)
        self.assertEqual(next(iter(self.entry()['pending']['shown'].values()))['value'], 'C')

    def test_errors_do_not_change_baselines_and_first_success_after_failed_add_triggers(self):
        self.error = True; self.park(); self.gate()
        self.assertFalse(self.entry()['baseline']); self.assertFalse(self.deliveries)
        self.error = False; self.gate()
        self.assertEqual(len(self.deliveries), 1)
        self.finish(); before = self.entry()['baseline']
        self.error = True; self.gate()
        self.assertEqual(self.entry()['baseline'], before)
        self.assertEqual(len(self.deliveries), 1)

    def test_heartbeat_with_errors_and_release_does_not_acknowledge(self):
        self.park(); self.error = True
        with w.Spool() as spool:
            spool.entries[0]['heartbeat'] = (datetime.now()-timedelta(hours=13)).strftime(w.TIME_FMT)
        before = self.entry()['heartbeat']
        self.gate(); self.assertTrue(self.entry()['pending']['heartbeat'])
        self.finish('release'); self.assertEqual(self.entry()['heartbeat'], before)
        self.gate(); self.assertEqual(len(self.deliveries), 2)
        self.finish(); self.assertNotEqual(self.entry()['heartbeat'], before)

    def test_ambiguous_dispatch_remains_reserved_and_alerts_repeatedly(self):
        self.park(); self.observation = 'B'
        with mock.patch.object(w, 'post_delivery', side_effect=OSError('power lost')):
            with self.assertRaises(OSError): self.gate()
        self.assertIn('pending', self.entry())
        with w.Spool() as spool:
            spool.entries[0]['pending']['at'] = (datetime.now()-timedelta(hours=4)).strftime(w.TIME_FMT)
        self.gate(); self.assertEqual(self.alert.call_count, 1)
        self.gate(); self.assertEqual(self.alert.call_count, 1)
        with w.Spool() as spool:
            spool.entries[0]['pending']['alerted'] = (datetime.now()-timedelta(hours=4)).strftime(w.TIME_FMT)
        self.gate(); self.assertEqual(self.alert.call_count, 2)
        self.assertFalse(self.deliveries)

    def test_set_has_one_reservation_and_unshown_target_not_acknowledged(self):
        self.park(['watch:tasks.task:t-1', 'watch:tasks.task:t-2'])
        self.observation = 'B'; self.gate()
        self.park(['watch:tasks.task:t-3'])
        self.finish()
        self.assertEqual(self.entry()['baseline'], {})
        self.assertEqual(len([e for e in w.read_spool() if e.get('role') == 'coordinate']), 1)
        self.gate(); self.assertEqual(len(self.deliveries), 2)

    def test_other_principal_cannot_replace_or_finish_role(self):
        self.park(); self.observation = 'B'; self.gate()
        with mock.patch.object(w, 'principal_for', return_value='eve'):
            with self.assertRaises(SystemExit): self.park()
            with self.assertRaises(SystemExit): self.finish()
        self.assertIn('pending', self.entry())

    def test_pause_never_dispatches_or_consumes(self):
        self.park(); self.observation = 'B'
        with mock.patch.object(w, 'paused', return_value=True): self.gate()
        self.assertFalse(self.deliveries)
        self.gate(); self.assertEqual(len(self.deliveries), 1)


if __name__ == '__main__':
    unittest.main()
