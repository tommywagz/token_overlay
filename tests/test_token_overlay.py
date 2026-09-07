import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import token_overlay as overlay


def report(results, **extra):
    return {'data': [{'start_time': 1700000000, 'end_time': 1800000000,
                      'results': results}], 'has_more': False, **extra}


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_openai_counts_only_input_and_output_across_pages(self):
        os.environ['OPENAI_ADMIN_KEY'] = 'secret-key'
        first = report([{'input_tokens': 100, 'output_tokens': 20,
                         'input_cached_tokens': 80, 'input_audio_tokens': 15,
                         'num_model_requests': 900}], has_more=True, next_page='next')
        second = report([{'input_tokens': 7, 'output_tokens': 3}])
        with patch.object(overlay, 'get_json', side_effect=[first, second]) as get:
            result = overlay.openai_reading()
        self.assertEqual(result.tokens, 130)
        self.assertEqual(result.status, 'ok')
        self.assertEqual(get.call_args.args[2]['page'], 'next')
        self.assertIsNone(result.credits)
        self.assertIn('unavailable', result.detail)

    def test_claude_cache_creation_and_reads_count_once(self):
        os.environ['ANTHROPIC_ADMIN_KEY'] = 'secret-key'
        data = report([{'uncached_input_tokens': 100, 'output_tokens': 20,
                        'cache_read_input_tokens': 50,
                        'cache_creation': {'ephemeral_1h_input_tokens': 10,
                                           'ephemeral_5m_input_tokens': 5},
                        'server_tool_use': {'web_search_requests': 999}}],
                      has_more=True, next_page='second')
        with patch.object(overlay, 'get_json', side_effect=[data, report([])]) as get:
            result = overlay.claude_reading()
        self.assertEqual(result.tokens, 185)
        self.assertEqual(get.call_args.args[2]['page'], 'second')

    def test_incomplete_pagination_and_invalid_response_are_errors(self):
        os.environ['OPENAI_ADMIN_KEY'] = 'secret-key'
        for data in ({}, report([], has_more=True),
                     report([], has_more=True, next_page='loop')):
            with self.subTest(data=data), patch.object(overlay, 'get_json', return_value=data):
                reading = overlay.openai_reading()
                self.assertEqual(reading.status, 'error')
                self.assertIsNone(reading.tokens)

    def test_google_reads_token_metric_and_pages_with_project_scope(self):
        os.environ.update(GOOGLE_CLOUD_PROJECT='my-project', GOOGLE_ACCESS_TOKEN='secret')
        def data(count, **extra):
            return {'timeSeries': [{'points': [{'value': {'int64Value': str(count)}}]}], **extra}
        with patch.object(overlay, 'get_json', side_effect=[data(90, nextPageToken='two'), data(10)]) as get:
            result = overlay.google_cloud_reading()
        self.assertEqual(result.tokens, 100)
        url, headers, params = get.call_args.args
        self.assertIn('monitoring.googleapis.com/v3/projects/my-project/timeSeries', url)
        self.assertIn('token_count', params['filter'])
        self.assertIn('resource.labels.project_id="my-project"', params['filter'])
        self.assertEqual(params['pageToken'], 'two')
        self.assertEqual(headers['X-Goog-User-Project'], 'my-project')

    def test_google_no_samples_is_not_zero_and_partial_results_error(self):
        os.environ.update(GOOGLE_CLOUD_PROJECT='project', GOOGLE_ACCESS_TOKEN='secret')
        with patch.object(overlay, 'get_json', return_value={}):
            result = overlay.google_cloud_reading()
            self.assertEqual(result.status, 'empty')
            self.assertIsNone(result.tokens)
        with patch.object(overlay, 'get_json', return_value={'executionErrors': [{'code': 5}]}):
            self.assertEqual(overlay.google_cloud_reading().status, 'error')

    def test_access_token_refresh_and_explicit_override(self):
        with patch.object(overlay.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'fresh\n')) as run:
            self.assertEqual(overlay.gcloud_token(), 'fresh')
            self.assertEqual(overlay.gcloud_token(), 'fresh')
            self.assertEqual(run.call_count, 2)
            os.environ['GOOGLE_ACCESS_TOKEN'] = 'explicit'
            self.assertEqual(overlay.gcloud_token(), 'explicit')
            self.assertEqual(run.call_count, 2)

    def test_adapters_map_all_rows_and_validate_output(self):
        for name, label in overlay.PROVIDER_NAMES.items():
            os.environ[f'{name}_COMMAND'] = '/fake/adapter'
            completed = subprocess.CompletedProcess([], 0, json.dumps({'used_tokens': 123, 'remaining_credits': 8.5}))
            with patch.object(overlay.subprocess, 'run', return_value=completed):
                reading = overlay.run_command_adapter(name)
            self.assertEqual(reading.name, label)
            self.assertEqual(reading.tokens, 123)
            self.assertEqual(reading.credits, 8.5)
        for output in ('[]', '{}', '{"used_tokens": -1}', '{"used_tokens": true}', '{"credits": "NaN"}'):
            with patch.object(overlay.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output)):
                self.assertEqual(overlay.run_command_adapter('GOOGLE_CLOUD').status, 'error')

    def test_adapter_and_http_errors_do_not_echo_secrets(self):
        secret = 'super-secret-access-key'
        os.environ['GOOGLE_CLOUD_COMMAND'] = '/fake/adapter'
        with patch.object(overlay.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, secret, secret)):
            self.assertNotIn(secret, overlay.run_command_adapter('GOOGLE_CLOUD').detail)
        error = urllib.error.HTTPError('https://example.invalid', 401, '', {}, io.BytesIO(secret.encode()))
        with patch.object(overlay.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 401') as caught:
                overlay.get_json('https://example.invalid', {})
        self.assertNotIn(secret, str(caught.exception))

    def test_manual_credit_is_labeled_and_blank_is_unavailable(self):
        os.environ.update(OPENAI_ADMIN_KEY='secret', OPENAI_REMAINING_CREDITS_USD='12.50')
        with patch.object(overlay, 'get_json', return_value=report([])):
            result = overlay.openai_reading()
        self.assertEqual(result.credits, 12.5)
        self.assertIn('manual snapshot', result.detail)
        self.assertIsNone(overlay.credit_value(''))

    def test_dotenv_export_quotes_comments_and_environment_priority(self):
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / '.env'
            env_file.write_text('export OPENAI_ADMIN_KEY="file-key" # comment\n'
                                'GOOGLE_CLOUD_COMMAND=\'/some/path --flag "two words"\'\nEMPTY=\n')
            os.environ['OPENAI_ADMIN_KEY'] = 'shell-key'
            overlay.load_dotenv(env_file)
            self.assertEqual(os.environ['OPENAI_ADMIN_KEY'], 'shell-key')
            self.assertEqual(os.environ['GOOGLE_CLOUD_COMMAND'], '/some/path --flag "two words"')
            self.assertEqual(os.environ['EMPTY'], '')
            env_file.write_text('OPENAI_ADMIN_KEY="secret-but-unclosed\n')
            with self.assertRaises(ValueError) as caught:
                overlay.load_dotenv(env_file)
            self.assertNotIn('secret-but-unclosed', str(caught.exception))

    def test_window_is_28_utc_calendar_days(self):
        start, end = overlay.usage_window()
        self.assertEqual((end.date() - start.date()).days, 27)
        self.assertEqual((start.hour, start.minute, start.second), (0, 0, 0))

    def test_missing_credentials_are_setup_not_errors(self):
        readings = overlay.collect_readings()
        self.assertEqual(len(readings), 4)
        self.assertTrue(all(item.status == 'setup' for item in readings))

    def test_reader_exception_does_not_wedge_refresh(self):
        def broken():
            raise Exception('private diagnostic')
        with patch.object(overlay, 'READERS', [broken]):
            readings = overlay.collect_readings()
        self.assertEqual(readings[0].status, 'error')

    def test_failed_notification_does_not_unlink_socket(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'control.sock'
            path.touch()
            with patch.object(overlay, 'SOCKET_PATH', path):
                self.assertFalse(overlay.notify_existing())
            self.assertTrue(path.exists())

    def test_check_cli_reads_exported_credentials_and_needs_no_display(self):
        with tempfile.TemporaryDirectory() as temp:
            adapter = Path(temp) / 'adapter.py'
            adapter.write_text('import os,json\nassert os.environ["OPENAI_ADMIN_KEY"] == "fake-key"\n'
                               'print(json.dumps({"used_tokens": 456, "remaining_credits": 1.5}))\n')
            env = dict(os.environ, PATH=os.defpath, TOKEN_OVERLAY_ENV='/nonexistent', OPENAI_ADMIN_KEY='fake-key')
            for name in overlay.PROVIDER_NAMES:
                env[f'{name}_COMMAND'] = f'{sys.executable} {adapter}'
            result = subprocess.run([str(Path(overlay.__file__).with_name('tokens')), '--check'],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(all(item['tokens'] == 456 for item in json.loads(result.stdout)))


if __name__ == '__main__':
    unittest.main()
