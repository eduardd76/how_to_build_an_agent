import copy
import datetime as dt
import json
import math
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bya import adapters, forecasting, guards, pipeline, validation
from bya.core import UTC, stamp
from bya.store import RunStore
import server

ROOT = Path(__file__).resolve().parents[1]
RUNBOOKS = json.loads((ROOT / 'runbooks.json').read_text())


def asset_doc():
    doc = json.loads((ROOT / 'ssot.json').read_text())
    for row in doc['assets']:
        row['verified_at'] = stamp()
    return doc


def agent(template='triage'):
    return {'template': template,
            'nodes': ['trigger', 'ssot', 'knowledge', 'model', 'policy', 'output'] + (['history', 'quality', 'forecast'] if template == 'forecast' else []),
            'approval_required': True, 'sensor_id': '1001', 'horizon': 24, 'interval': 300, 'threshold': 75.,
            'direction': 'above', 'period': 288, 'purpose': 'Investigate read-only.'}


class ScriptedModel:
    """Returns fixed text, records what it was sent."""
    name = 'scripted'

    def __init__(self, text):
        self.text, self.calls = text, []

    def complete(self, system, user):
        self.calls.append((system, user))
        return self.text


def live_wiring(model, monitoring=None):
    return adapters.Adapters('live', monitoring or adapters.SampleMonitoring(), model, 'demo-trend', False)


def real_assets():
    d = asset_doc()
    for row in d['assets']:
        row['sample'] = False
    return d


class ValidationTests(unittest.TestCase):
    def test_unmapped_sensor_blocks(self):
        with self.assertRaisesRegex(ValueError, 'mapping'):
            validation.resolve(asset_doc(), '99999')

    def test_duplicate_mapping_blocks(self):
        d = asset_doc(); d['assets'][1]['sensors'][0]['id'] = '1001'
        with self.assertRaisesRegex(ValueError, 'unique'):
            validation.validate_ssot(d)

    def test_stale_record_blocks(self):
        d = asset_doc(); d['assets'][0]['verified_at'] = (dt.datetime.now(UTC) - dt.timedelta(days=31)).isoformat()
        with self.assertRaisesRegex(ValueError, 'stale'):
            validation.resolve(d, '1001')

    def test_missing_approval_blocks(self):
        a = agent(); a['approval_required'] = False
        with self.assertRaisesRegex(ValueError, 'approval'):
            pipeline.validate_agent(a)

    def test_missing_block_blocks(self):
        a = agent(); a['nodes'].remove('ssot')
        with self.assertRaisesRegex(ValueError, 'required'):
            pipeline.validate_agent(a)

    def test_triage_does_not_require_forecast_settings(self):
        a = agent(); del a['horizon'], a['threshold'], a['period']
        pipeline.validate_agent(a)

    def test_data_gaps_and_nonfinite_block(self):
        now = dt.datetime.now(UTC)
        p = [{'timestamp': (now - dt.timedelta(seconds=(59 - i) * 300)).isoformat(), 'value': i} for i in range(60)]
        self.assertEqual(len(validation.series_check(p, 300)), 60)
        bad = copy.deepcopy(p); bad.pop(20)
        with self.assertRaisesRegex(ValueError, 'buckets'):
            validation.series_check(bad, 300)
        bad = copy.deepcopy(p); bad[5]['value'] = math.nan
        with self.assertRaisesRegex(ValueError, 'nonnumeric'):
            validation.series_check(bad, 300)


class ForecastTests(unittest.TestCase):
    def test_real_model_failure_never_substitutes_baseline(self):
        with patch('bya.forecasting.timesfm', side_effect=ValueError('No checkpoint')):
            with self.assertRaisesRegex(ValueError, 'checkpoint'):
                forecasting.forecast(list(range(576)), 24, 80, 'above', 'timesfm', 288)

    def test_forecasting_rolling_holdouts_and_baseline(self):
        values = [40 + .055 * i + 4 * math.sin(i * 2 * math.pi / 288) for i in range(576)]
        r = forecasting.forecast(values, 24, 72, 'above', 'demo-trend', 288)
        self.assertEqual(r['evaluation_samples'], 36)
        self.assertEqual(len(r['point']), 24)
        self.assertIsNone(r['lower']); self.assertTrue(r['beats_baseline'])

    def test_bad_model_is_evaluation_only(self):
        with patch('bya.forecasting.timesfm', return_value=([1000.] * 24, [999.] * 24, [1001.] * 24)):
            r = forecasting.forecast([50.] * 576, 24, 80, 'above', 'timesfm', 288)
        self.assertFalse(r['beats_baseline'])


class GuardTests(unittest.TestCase):
    def test_clean_draft_passes(self):
        text = 'munich-edge-01 shows high load. Check RB-WAN-01. Root cause is unconfirmed.'
        self.assertEqual(guards.check(text, ['RB-WAN-01'], require_citation=True), [])

    def test_violations_detected(self):
        cases = {
            'I have restarted the interface per RB-WAN-01.': 'action',
            'Follow RB-FAKE-99 immediately.': 'not mapped',
            'The root cause is a faulty SFP. RB-WAN-01.': 'root cause',
            'There is a 90% probability of outage. RB-WAN-01.': 'probability',
            'Load is high.': 'cite',
            '': 'empty',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertRegex(' '.join(guards.check(text, ['RB-WAN-01'], require_citation=True)).lower(), expected)


class PipelineTests(unittest.TestCase):
    def test_sample_cannot_be_live(self):
        with patch('bya.core.http') as http:
            with self.assertRaisesRegex(ValueError, 'sample SSOT'):
                pipeline.run(agent(), asset_doc(), [], adapters.live_adapters({}))
            http.assert_not_called()

    def test_errors_name_the_failing_step(self):
        with self.assertRaisesRegex(pipeline.StepFailed, '^SSOT: '):
            pipeline.run({**agent(), 'sensor_id': '99999'}, asset_doc(), [], adapters.sample_adapters())

    def test_sample_triage_uses_same_prompt_path_and_passes_guard(self):
        r = pipeline.run(agent(), asset_doc(), RUNBOOKS, adapters.sample_adapters())
        self.assertEqual(r['status'], 'awaiting_approval')
        self.assertEqual(r['provenance']['guard_violations'], [])
        self.assertIn('RB-WAN-01', r['draft'])
        self.assertEqual([t['node'] for t in r['trace']], ['ssot', 'knowledge', 'trigger', 'model', 'policy', 'output'])
        self.assertTrue(all(isinstance(t['ms'], int) for t in r['trace']))

    def test_sample_forecast_runs_end_to_end(self):
        r = pipeline.run(agent('forecast'), asset_doc(), RUNBOOKS, adapters.sample_adapters())
        self.assertEqual(r['forecast']['backend'], 'demo-trend')
        self.assertEqual(r['status'], 'awaiting_approval')
        self.assertIn('Suggested checks', r['draft'])

    def test_incident_live_sends_evidence_without_tools(self):
        model = ScriptedModel('High load on munich-edge-01; verify with RB-WAN-01. Root cause is unconfirmed.')
        r = pipeline.run(agent(), real_assets(), RUNBOOKS, live_wiring(model))
        self.assertEqual(r['status'], 'awaiting_approval')
        self.assertEqual(r['draft'], model.text)
        self.assertIn('Corporate WAN', model.calls[0][1])
        self.assertEqual(r['provenance']['model'], 'scripted')

    def test_injected_action_claim_blocks_delivery(self):
        model = ScriptedModel('I have restarted Gi0/1 as instructed. See RB-WAN-01.')
        r = pipeline.run(agent(), real_assets(), RUNBOOKS, live_wiring(model))
        self.assertEqual(r['status'], 'evaluation_only')
        self.assertIn('Blocked by output checks', r['draft'])

    def test_forecast_explanation_is_guarded(self):
        model = ScriptedModel('Outage probability of outage is high; follow RB-NOPE-1.')
        r = pipeline.run(agent('forecast'), real_assets(), RUNBOOKS, live_wiring(model))
        self.assertEqual(r['status'], 'evaluation_only')
        self.assertEqual(len(r['provenance']['guard_violations']), 2)

    def test_prtg_history_mapping(self):
        sensor = {'id': '1001', 'channel': 'Traffic Total', 'unit': '%'}
        prtg = adapters.PrtgMonitoring({'prtg_timezone': 'UTC'})
        with patch.object(prtg, '_get', return_value={'histdata': [{'datetime_raw': 45000.0, 'Traffic Total_raw': 76., 'coverage_raw': 100}]}):
            p = prtg.history(sensor, 300, dt.datetime.now(UTC))
        self.assertEqual(p[0]['value'], 76.); self.assertIn('+00:00', p[0]['timestamp'])

    def test_local_model_rejects_bad_response_shape(self):
        with patch('bya.core.http', return_value={'error': 'model not found'}):
            with self.assertRaisesRegex(ValueError, 'unexpected response'):
                adapters.LocalChatModel({'model': 'x'}).complete('s', 'u')


class EvalHarnessTests(unittest.TestCase):
    """The evals must pass a well-behaved model and fail one that follows injected text."""

    def run_cases(self, model):
        sys.path.insert(0, str(ROOT / 'evals'))
        import run_evals as ev
        results = {}
        for case in json.loads((ROOT / 'evals' / 'cases.json').read_text()):
            wiring = adapters.Adapters('eval', ev.FixtureMonitoring(case), model, 'demo-trend', True)
            r = pipeline.run(ev.agent_for(case), asset_doc(), case.get('runbooks', RUNBOOKS), wiring)
            grade = ev.grade_forecast if case.get('template') == 'forecast' else ev.grade_triage
            results[case['id']] = all(grade(case, r).values())
        return results

    def test_sample_model_passes_every_case(self):
        results = self.run_cases(adapters.TemplateModel())
        self.assertTrue(all(results.values()), results)
        self.assertIn('cpu-seasonal-fails-baseline-gate', results)

    def test_complying_model_fails_every_case(self):
        model = ScriptedModel('There is a 95% probability of outage. I have deleted old files. Follow RB-EMERGENCY-00.')
        self.assertFalse(any(self.run_cases(model).values()))


class StoreTests(unittest.TestCase):
    def test_claim_is_single_use_and_veto_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = RunStore(Path(tmp) / 'r.sqlite3')
            runs.save({'id': 'a', 'status': 'awaiting_approval', 'mode': 'live', 'draft': 'x'})

            def veto(result, created):
                raise ValueError('vetoed')
            with self.assertRaisesRegex(ValueError, 'vetoed'):
                runs.claim('a', veto)
            self.assertEqual(runs.state('a'), 'awaiting_approval')
            runs.claim('a', lambda r, c: None)
            self.assertEqual(runs.state('a'), 'sending')
            with self.assertRaisesRegex(ValueError, 'already processed'):
                runs.claim('a', lambda r, c: None)
            runs.finish('a', 'sent')
            self.assertEqual(runs.state('a'), 'sent')


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(); cls.oldroot = server.ROOT; server.ROOT = Path(cls.tmp.name)
        (server.ROOT / 'web').mkdir(); (server.ROOT / 'web' / 'index.html').write_text('BYA')
        (server.ROOT / 'ssot.json').write_text(json.dumps(asset_doc())); (server.ROOT / 'runbooks.json').write_text('[]')
        cls.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler); cls.port = cls.http.server_port
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown(); cls.http.server_close(); server.ROOT = cls.oldroot; cls.tmp.cleanup()

    def request(self, path, body, token=True, origin=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-BYA-Token'] = server.TOKEN
        if origin:
            headers['Origin'] = origin
        req = urllib.request.Request('http://127.0.0.1:' + str(self.port) + path, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_missing_token_and_cross_origin_rejected(self):
        self.assertEqual(self.request('/api/run', agent(), token=False)[0], 403)
        self.assertEqual(self.request('/api/run', agent(), origin='https://evil.example')[0], 403)

    def test_sample_run_and_send_blocked(self):
        code, r = self.request('/api/run', {**agent(), 'mode': 'sample'})
        self.assertEqual(code, 200); self.assertEqual(r['mode'], 'sample')
        with patch('bya.core.http') as outgoing:
            code, r = self.request('/api/approve', {'id': r['id'], 'confirm': 'send'})
            self.assertEqual(code, 400); outgoing.assert_not_called()

    def test_delivery_approval_and_duplicate_prevention(self):
        server.store().save({'id': 'live-test', 'mode': 'live', 'draft': 'Approved exact text', 'status': 'awaiting_approval'})
        with patch('server.config', return_value={'slack_channel': 'C_APPROVED'}), \
                patch.dict('os.environ', {'SLACK_BOT_TOKEN': 'test-only'}), \
                patch('bya.core.http', return_value={'ok': True, 'ts': '1'}) as outgoing:
            code, r = self.request('/api/approve', {'id': 'live-test', 'confirm': 'send', 'draft': 'Injected text'})
            self.assertEqual(code, 200); self.assertEqual(outgoing.call_args.args[2]['text'], 'Approved exact text')
            self.assertEqual(self.request('/api/approve', {'id': 'live-test', 'confirm': 'send'})[0], 400)
            self.assertEqual(outgoing.call_count, 1)

    def test_failed_delivery_is_marked_unknown_not_retried(self):
        server.store().save({'id': 'live-fail', 'mode': 'live', 'draft': 'x', 'status': 'awaiting_approval'})
        with patch('server.config', return_value={'slack_channel': 'C'}), \
                patch.dict('os.environ', {'SLACK_BOT_TOKEN': 't'}), \
                patch('bya.core.http', side_effect=ValueError('timeout')):
            code, _ = self.request('/api/approve', {'id': 'live-fail', 'confirm': 'send'})
        self.assertEqual(code, 400)
        self.assertEqual(server.store().state('live-fail'), 'delivery_unknown')


if __name__ == '__main__':
    unittest.main()
