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
        self.read_watch_set_original = w.read_watch_set
        self.observation = 'A'
        self.error = False
        self.identity_revision = ''
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
        return {w.watch_key(t): {'identity': json.dumps([t['kind'], w.AGENT, t['id'], self.identity_revision]), 'value': self.observation} for t in entry['targets']}, []

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
        self.assertEqual(next(iter(self.entry()['baseline'].values()))['value'], 'B')
        self.assertEqual(len([e for e in w.read_spool() if e.get('role') == 'coordinate']), 1)
        self.observation = 'C'; self.gate(); self.assertEqual(len(self.deliveries), 2)

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

    def test_cancel_removes_role_without_dispatch_and_obeys_ownership(self):
        self.park(); self.observation = 'B'
        wid = self.entry()['id']
        with mock.patch.object(w, 'api_may_change', return_value=False):
            with self.assertRaises(SystemExit): w.cmd_cancel(argparse.Namespace(id=wid))
        self.assertFalse(self.deliveries)
        with mock.patch.object(w, 'api_may_change', return_value=True): w.cmd_cancel(argparse.Namespace(id=wid))
        self.assertFalse(self.deliveries)
        self.assertFalse(any(e.get('role') == 'coordinate' for e in w.read_spool()))

    def test_ack_capability_is_not_delivery_identity_or_in_failed_handover_log(self):
        self.park(); self.observation = 'B'
        # Restore the production function from a fresh module, with only the principal command failing.
        import types
        fresh = types.ModuleType('fresh_clock'); fresh.__file__ = w.__file__
        exec(compile(Path(w.__file__).read_text(), w.__file__, 'exec'), fresh.__dict__)
        fresh.LOG = w.LOG; fresh.WAKE_DIR = w.WAKE_DIR
        with mock.patch.object(w, 'hand_over_principal', side_effect=fresh.hand_over_principal), \
             mock.patch.object(fresh.subprocess, 'run', side_effect=OSError('unavailable')):
            self.gate()
        token = self.entry()['pending']['token']
        body = self.deliveries[0]
        self.assertNotIn(token, body['id']); self.assertNotIn(token, body['place'])
        self.assertNotIn(token, w.LOG.read_text())
        self.assertIn(token, body['text'])

    def test_twenty_targets_fit_house_api_and_unknown_principal_local_prompt_and_caps_refused(self):
        targets = [f'watch:tasks.task:t-{n}' for n in range(20)]
        argv = ['park', '--role', 'coordinate', '-p', 'Check work', '--note', 'Coordination']
        for t in targets: argv += ['--on', t]
        run = mock.Mock(return_value=(0, '', ''))
        result = w.api_answer({'caller': {'kind': 'agent', 'agent': w.AGENT}, 'path': 'run', 'body': {'argv': argv}}, run)
        self.assertEqual(result['status'], 200); run.assert_called_once()
        self.assertEqual(w.api_answer({'caller': {'kind': 'owner'}, 'path': 'run', 'body': {'argv': ['--agent', w.AGENT] + argv}}, run)['status'], 200)
        with mock.patch.object(w, 'principal_for', return_value='-'):
            with self.assertRaises(SystemExit): self.park()
        with w.Spool() as spool:
            spool.entries = [{'id': f'w-{n:06x}', 'kind': 'wake'} for n in range(w.MAX_PENDING)]
        with self.assertRaises(SystemExit): self.park()
        self.assertFalse(any(e.get('role') for e in w.read_spool()))
        with self.assertRaises(SystemExit):
            w.cmd_watch_park(argparse.Namespace(role='coordinate', park_on=targets, prompt=w.HOUSE_LOCAL+' Do work', note='test', slot='', model='test'))

    def test_old_token_cannot_finish_new_batch_and_concurrent_firings_reserve_once(self):
        from concurrent.futures import ThreadPoolExecutor
        self.park(); self.observation = 'B'; wid = self.entry()['id']
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(w.fire_watch_set, [wid, wid]))
        self.assertEqual(len(self.deliveries), 1)
        old_token = self.entry()['pending']['token']; self.finish()
        self.observation = 'C'; self.gate()
        with self.assertRaises(SystemExit): w.cmd_watch_finish(argparse.Namespace(action='finish', token=old_token))
        self.assertNotEqual(self.entry()['pending']['token'], old_token)

    def test_new_process_load_preserves_reservation_and_rearms_without_duplicate_dispatch(self):
        import types
        self.park(); self.observation = 'B'; self.gate()
        pending = self.entry()['pending']
        fresh = types.ModuleType('rebooted_clock'); fresh.__file__ = w.__file__
        exec(compile(Path(w.__file__).read_text(), w.__file__, 'exec'), fresh.__dict__)
        fresh.WAKE_DIR, fresh.SPOOL, fresh.LOCK, fresh.LOG = w.WAKE_DIR, w.SPOOL, w.LOCK, w.LOG
        with mock.patch.object(fresh, 'arm_timer') as arm, mock.patch.object(fresh, 'post_delivery') as post, mock.patch.object(fresh, 'paused', return_value=False):
            restored = next(e for e in fresh.read_spool() if e.get('kind') == 'watch-park')
            fresh.arm_pending(restored); arm.assert_called_once()
            fresh.fire_watch_set(restored['id']); post.assert_not_called()
        self.assertEqual(self.entry()['pending'], pending)

    def test_registry_http_read_posts_once_with_clock_credential_and_checks_target_identity(self):
        import io
        self.park()
        real_read = self.read_watch_set_original
        tf = Path(self.tmp.name)/'credential'; tf.write_text('clock-test-credential')
        target = self.entry()['targets'][0]
        response = {'observations': [{**target, 'identity': 'target', 'value': 'A'}]}
        def opened(req, timeout):
            self.assertTrue(req.full_url.endswith('/api/loom/watches/observe'))
            self.assertEqual(req.get_header('Authorization'), 'Bearer clock-test-credential')
            self.assertEqual(json.loads(req.data), {'agent': w.AGENT, 'principal': 'owner', 'targets': [target]})
            return io.BytesIO(json.dumps(response).encode())
        settings = w.setting
        with mock.patch.object(w, 'setting', side_effect=lambda key, *args: str(tf) if key == 'WAKE_DELIVER_TOKEN_FILE' else settings(key, *args)), mock.patch.object(w.urllib.request, 'urlopen', side_effect=opened) as http:
            observed, errors = real_read(self.entry())
            self.assertFalse(errors); self.assertEqual(next(iter(observed.values()))['value'], 'A'); http.assert_called_once()
            response['observations'][0] = {**target, 'error': 'not computed'}
            self.assertEqual(real_read(self.entry()), ({}, [target]))
            response['observations'][0] = {**target, 'id': 'wrong', 'identity': 'x', 'value': 'y'}
            with self.assertRaises(ValueError): real_read(self.entry())


    def test_reserved_role_cannot_be_cancelled_or_cancelled_by_another_principal(self):
        self.park(); self.observation = 'B'; self.gate()
        entry = self.entry()
        with mock.patch.object(w, 'api_may_change', return_value=True):
            with self.assertRaises(SystemExit): w.cmd_cancel(argparse.Namespace(id=entry['id']))
            with mock.patch.object(w, 'principal_for', return_value='eve'):
                with self.assertRaises(SystemExit): w.cmd_cancel(argparse.Namespace(id=entry['id']))
        self.assertEqual(self.entry()['pending'], entry['pending'])
        self.gate(); self.assertEqual(len(self.deliveries), 1)

    def test_replacement_cannot_take_another_conversations_slot_past_its_cap(self):
        self.park()
        with w.Spool() as spool:
            spool.entries[0].update(origin='house', for_in='cv-old')
            spool.entries += [{'id': f'w-{n:06x}', 'kind': 'wake', 'origin': 'house', 'for_in': 'cv-new'} for n in range(w.MAX_PENDING_API)]
        with mock.patch.dict(w.os.environ, {'WAKE_ORIGIN': 'house'}), mock.patch.object(w, 'principal_in', return_value='cv-new'):
            with self.assertRaises(SystemExit): self.park()
        self.assertEqual(self.entry()['for_in'], 'cv-old')

    def test_twenty_watch_targets_park_but_twenty_one_are_refused(self):
        targets = [f'watch:tasks.task:t-{n}' for n in range(20)]
        self.park(targets); self.assertEqual(len(self.entry()['targets']), 20)
        with self.assertRaises(SystemExit): self.park(targets+['watch:tasks.task:t-20'])
        self.assertEqual(len(self.entry()['targets']), 20)

    def test_large_observations_and_escaped_targets_do_not_suppress_heartbeat(self):
        self.observation = 'x'*100000
        targets = ['watch:tasks.task:'+str(n)+'🧵'*290 for n in range(20)]
        self.park(targets)
        with w.Spool() as spool:
            spool.entries[0]['heartbeat'] = (datetime.now()-timedelta(hours=13)).strftime(w.TIME_FMT)
        self.gate()
        self.assertEqual(len(self.deliveries), 1)
        self.assertLessEqual(len(self.deliveries[0]['text'].encode('utf-16-le'))//2, w.DELIVER_TEXT_MAX)
        self.assertTrue(self.entry()['pending']['heartbeat'])
        self.assertTrue(all(v['value'].startswith('sha256:') for v in self.entry()['baseline'].values()))
        self.finish()
        self.observation = 'y'*100000
        self.gate()
        self.assertEqual(len(self.deliveries), 2)
        self.assertLessEqual(len(self.deliveries[1]['text'].encode('utf-16-le'))//2, w.DELIVER_TEXT_MAX)


    def test_large_fingerprint_is_not_equal_to_a_literal_hash_looking_value(self):
        self.observation = 'x'*100000; self.park()
        baseline = next(iter(self.entry()['baseline'].values()))
        self.observation = baseline['value']; self.gate()
        self.assertEqual(len(self.deliveries), 1)
        self.assertNotEqual(next(iter(self.entry()['pending']['shown'].values())), baseline)


    def test_replacement_initializes_new_target_quietly_and_does_not_reset_failed_old_target(self):
        self.park(); self.observation = 'B'
        self.park(['watch:tasks.task:t-2']); self.gate()
        self.assertFalse(self.deliveries)
        with w.Spool() as spool: spool.entries = []
        self.error = True; self.park(); self.error = False
        self.park(); self.gate()
        self.assertEqual(len(self.deliveries), 1)

    def test_watch_reservations_themselves_consume_spool_capacity(self):
        with w.Spool() as spool:
            spool.entries = [{'id': f'w-{n:06x}', 'kind': 'watch-park', 'role': 'other-'+str(n)} for n in range(w.MAX_PENDING)]
        with self.assertRaises(SystemExit): self.park()
        self.assertFalse(any(e.get('role') == 'coordinate' for e in w.read_spool()))


    def test_readd_of_target_first_added_during_reservation_retains_its_initial_baseline(self):
        self.park(); self.observation = 'B'; self.gate()
        self.observation = 'X'
        self.park(['watch:tasks.task:t-1', 'watch:tasks.task:t-2'])
        self.park(['watch:tasks.task:t-1'])
        self.observation = 'Y'
        self.park(['watch:tasks.task:t-2'])
        self.assertEqual(next(iter(self.entry()['baseline'].values()))['value'], 'X')
        self.finish(); self.gate()
        self.assertEqual(len(self.deliveries), 2)
        self.assertEqual(next(iter(self.entry()['pending']['shown'].values()))['value'], 'Y')

    def test_identity_only_change_is_persisted_dispatched_and_acknowledged(self):
        self.park(); before = self.entry()['baseline']
        self.identity_revision = 'retargeted'; self.gate()
        shown = self.entry()['pending']['shown']
        self.assertEqual(next(iter(shown.values()))['value'], next(iter(before.values()))['value'])
        self.assertNotEqual(next(iter(shown.values()))['identity'], next(iter(before.values()))['identity'])
        self.finish(); self.assertEqual(self.entry()['baseline'], shown)
        self.gate(); self.assertEqual(len(self.deliveries), 1)

    def test_pending_target_history_is_bounded_without_dropping_a_baseline(self):
        self.park(); self.observation = 'B'; self.gate()
        with w.Spool() as spool:
            spool.entries[0]['retained'] = {w.watch_key({'kind':'tasks.task','id':f't-{n}'}): None for n in range(100)}
        before = self.entry()
        with self.assertRaises(SystemExit): self.park(['watch:tasks.task:new'])
        self.assertEqual(self.entry(), before)


    def test_registry_response_order_is_not_authority_and_duplicates_are_errors(self):
        import io
        self.park(['watch:tasks.task:t-1', 'watch:tasks.task:t-2'])
        tf = Path(self.tmp.name)/'credential'; tf.write_text('clock-test-credential')
        targets = self.entry()['targets']
        rows = [{**t, 'identity': t['id'], 'value': t['id']} for t in reversed(targets)]
        settings = w.setting
        with mock.patch.object(w, 'setting', side_effect=lambda key, *args: str(tf) if key == 'WAKE_DELIVER_TOKEN_FILE' else settings(key, *args)), \
             mock.patch.object(w.urllib.request, 'urlopen', side_effect=lambda *a, **k: io.BytesIO(json.dumps({'observations': rows}).encode())):
            result, errors = self.read_watch_set_original(self.entry())
            self.assertFalse(errors)
            for t in targets:
                self.assertEqual(result[w.watch_key(t)], {'identity': t['id'], 'value': t['id']})
            rows[1] = rows[0]
            with self.assertRaises(ValueError): self.read_watch_set_original(self.entry())

    def test_standby_gate_suppresses_changed_watch_without_reserving_or_acknowledging(self):
        self.park(); baseline = self.entry()['baseline']; self.observation = 'B'
        with mock.patch.object(w, 'fire_gate_open', return_value=False): self.gate()
        self.assertFalse(self.deliveries); self.assertNotIn('pending', self.entry())
        self.assertEqual(self.entry()['baseline'], baseline)
        with mock.patch.object(w, 'fire_gate_open', return_value=True): self.gate()
        self.assertEqual(len(self.deliveries), 1)


if __name__ == '__main__':
    unittest.main()
