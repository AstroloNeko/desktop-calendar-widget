"""Evaluation harness tests. Network use and credential access are mocked."""
from collections import Counter
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from tools import calendar_agent_eval as eval_tool


class CalendarAgentEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {case['case_id']:case for case in eval_tool.validate_cases(
            json.loads(eval_tool.DATASET.read_text(encoding='utf-8')))}

    def case(self, identifier):
        return deepcopy(self.cases[identifier])

    def run_mock(self, identifier, *, budget=30):
        return eval_tool.run_case(self.case(identifier),eval_tool.MockProvider(),[budget])

    def test_dataset_has_five_cases_per_category_and_rejects_schema_errors(self):
        self.assertEqual(Counter(row['category'] for row in self.cases.values()),
                         dict.fromkeys(('create','patch','followup','non_action','ambiguity','query','delete','recovery'),5))
        case = self.case('a01-create-tomorrow')
        with self.assertRaises(ValueError):
            eval_tool.validate_cases([case,case])
        case['expected_final_state'].pop('writes')
        with self.assertRaises(ValueError):
            eval_tool.validate_cases([case])
        case = self.case('c01-select-second')
        case['turns'][1]['index'] = 0
        with self.assertRaises(ValueError):
            eval_tool.validate_cases([case])

    def memo_case(self, identifier):
        path = eval_tool.DATASET.with_name('calendar_agent_eval_memo_cases.json')
        cases = eval_tool.validate_cases(json.loads(path.read_text(encoding='utf-8')))
        return next(case for case in cases if case['case_id'] == identifier)

    def test_memo_suite_has_twenty_independent_cases_and_three_retests(self):
        path = eval_tool.DATASET.with_name('calendar_agent_eval_memo_cases.json')
        cases = eval_tool.validate_cases(json.loads(path.read_text(encoding='utf-8')))
        self.assertEqual(Counter(case['category'] for case in cases),
                         {'memo_create': 5, 'memo_notice': 5, 'memo_interference': 5,
                          'memo_followup': 5, 'memo_retest': 3})
        self.assertFalse(self.cases.keys() & {case['case_id'] for case in cases})
        self.assertTrue(all(case['reference_datetime'] == '2026-11-19T10:00:00+08:00'
                            and case['timezone'] == 'Asia/Shanghai' for case in cases))

    def test_safe_read_only_alternative_stays_separate_from_reply_review(self):
        case = self.memo_case('memo-r23-complaint-review')
        result = eval_tool.run_case(case, eval_tool.MockProvider(), [10])
        self.assertEqual(result['verdict'], 'needs_review')
        self.assertEqual(result['dimensions']['state'], 'pass')
        self.assertEqual({issue['code'] for issue in result['issues']}, {'reply_review'})
        self.assertEqual(eval_tool.summarize([result])['verdicts_by_support']['supported']['needs_review'], 1)
        case['expected_interaction'][0]['writes'] = 1
        with self.assertRaises(ValueError):
            eval_tool.validate_cases([case])

    def test_memo_revision_preserves_the_earlier_event_id(self):
        case = self.memo_case('memo-d17-correct-date')
        result = eval_tool.run_case(case, eval_tool.MockProvider(), [10])
        self.assertEqual(result['verdict'], 'pass')
        turns = result['turns']
        original_id = turns[0]['delta']['created'][0]['id']
        self.assertIn(original_id, turns[1]['delta']['updated'])
        turns[1]['plans'] = [{'action': 'update', 'items': [{'event_id': original_id,
                                                           'fields': {'date': '2026-11-29'}}]}]
        verdict, _, _ = eval_tool.judge(case, turns, result['final_delta'])
        self.assertEqual(verdict, 'pass')
        turns[1]['plans'][0]['items'][0]['event_id'] = 'wrong-id'
        _, _, issues = eval_tool.judge(case, turns, result['final_delta'])
        self.assertEqual({issue['code'] for issue in issues}, {'plan_incomplete'})
        turns[1]['delta']['updated'] = {'wrong-id': {}}
        verdict, _, issues = eval_tool.judge(case, turns, result['final_delta'])
        self.assertEqual(verdict, 'fail')
        self.assertIn('earlier Event ID was not preserved', {issue['detail'] for issue in issues})

    def test_memo_wrong_member_count_and_date_are_distinguished(self):
        case = self.memo_case('memo-a03-two-compact')
        result = eval_tool.run_case(case, eval_tool.MockProvider(), [10])
        self.assertEqual(result['verdict'], 'pass')
        items = result['turns'][0]['plans'][0]['items']
        items[0]['fields']['date'] = '2026-11-27'
        _, _, issues = eval_tool.judge(case, result['turns'], result['final_delta'])
        self.assertIn('date_time_mismatch', {issue['code'] for issue in issues})
        items.pop()
        _, _, issues = eval_tool.judge(case, result['turns'], result['final_delta'])
        self.assertIn('member_count_mismatch', {issue['code'] for issue in issues})
        self.assertNotIn('date_time_mismatch', {issue['code'] for issue in issues})

    def test_date_reference_is_fixed_in_prompt_host_and_sqlite(self):
        case = self.case('a02-wb-two-days')
        provider = eval_tool.MockProvider()
        complete = provider.complete_bounded

        def check_prompt(messages, tools, remaining):
            state = json.loads(messages[0]['content'].split('\n',1)[1])
            self.assertEqual(state['context']['now'],'2026-11-18T10:00:00+08:00')
            self.assertEqual(state['context']['tomorrow'],'2026-11-19')
            return complete(messages, tools, remaining)

        with patch.object(provider,'complete_bounded',side_effect=check_prompt):
            result = eval_tool.run_case(case,provider,[20])
        self.assertEqual(result['verdict'],'pass',result['issues'])
        self.assertEqual(sorted(row['date'] for row in result['final_delta']['created']),
                         ['2026-11-19','2026-11-20'])

    def test_explicit_title_variants_keep_date_and_quantity_checks(self):
        case = self.case('a02-wb-two-days')
        members = case['turns'][0]['mock'][0][0]['arguments']['items']
        for member in members:
            member['fields']['title'] = 'WB 实验'
        result = eval_tool.run_case(case, eval_tool.MockProvider(), [20])
        self.assertEqual(result['verdict'], 'pass', result['issues'])
        expected = case['expected_final_state']['create']
        rows = result['final_delta']['created']
        self.assertFalse(eval_tool.match_members(rows[:1], expected))
        self.assertFalse(eval_tool.match_members([{**rows[0], 'date': '2026-11-21'}, rows[1]], expected))
        self.assertFalse(eval_tool.match_fields({'titles': ['WB 实验']}, {'titles': ['WB实验']}))

    def test_no_cross_attempt_or_case_state_and_temporary_directory_cleanup(self):
        paths = []
        original = eval_tool.EvaluationCalendar.start

        def start(calendar):
            paths.append(calendar.root)
            return original(calendar)

        provider, budget = eval_tool.MockProvider(), [20]
        with patch.object(eval_tool.EvaluationCalendar,'start',start):
            first = eval_tool.run_case(self.case('a01-create-tomorrow'),provider,budget)
            second = eval_tool.run_case(self.case('a01-create-tomorrow'),provider,budget,2)
            unrelated = eval_tool.run_case(self.case('d05-non-action'),provider,budget)
        self.assertEqual([r['verdict'] for r in (first,second,unrelated)],['pass']*3)
        self.assertEqual(len(set(paths)),3)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertNotEqual(first['final_delta']['created'][0]['id'],second['final_delta']['created'][0]['id'])
        self.assertFalse(unrelated['final_delta']['receipts'])

    def test_local_selection_keeps_original_patch_without_second_model_call(self):
        result = self.run_mock('c01-select-second')
        self.assertEqual(result['verdict'],'pass',result['issues'])
        self.assertEqual(result['model_calls'],1)
        self.assertEqual(set(result['final_delta']['updated']),{'slot-b'})
        self.assertEqual(result['final_delta']['updated']['slot-b']['reminder']['after'],30)

    def test_revision_patch_and_affirmation_both_use_saved_plan(self):
        for identifier in ('c04-affirm-draft','c05-revise-target'):
            with self.subTest(identifier=identifier):
                result=self.run_mock(identifier)
                self.assertEqual(result['verdict'],'pass',result['issues'])
                self.assertTrue(result['turns'][0]['pending'])
                self.assertEqual(len(result['final_delta']['receipts']),1)

    def test_pending_round_trip_restart_and_two_stage_confirmation(self):
        for identifier in ('h04-pending-across-restart','g05-repeat-final-confirm','g04-stale-delete-card'):
            with self.subTest(identifier=identifier):
                result = self.run_mock(identifier)
                self.assertEqual(result['verdict'],'pass',result['issues'])
                self.assertEqual(len(result['final_delta']['receipts']),1)
                self.assertFalse(result['turns'][0]['delta']['receipts'])
                self.assertFalse(result['turns'][1]['delta']['receipts'])

    def test_complaint_with_spurious_create_identifies_model_and_state_errors(self):
        case = self.case('d04-non-action')
        case['turns'][0]['mock'] = self.case('a01-create-tomorrow')['turns'][0]['mock']
        result = eval_tool.run_case(case,eval_tool.MockProvider(),[20])
        self.assertEqual(result['verdict'],'fail')
        self.assertEqual(result['dimensions']['model'],'fail')
        self.assertEqual(result['dimensions']['state'],'fail')
        self.assertIn('unexpected_write',{issue['code'] for issue in result['issues']})

    def test_incomplete_plan_and_wrong_final_state_are_separately_reported(self):
        case = self.case('a02-wb-two-days')
        case['turns'][0]['mock'][0][0]['arguments']['items'].pop()
        case['turns'][0]['mock'].extend([[],[]])
        result = eval_tool.run_case(case,eval_tool.MockProvider(),[20])
        self.assertEqual(result['verdict'],'fail')
        self.assertIn('member_count_mismatch',{issue['code'] for issue in result['issues']})
        self.assertIn('verification_error',{issue['code'] for issue in result['issues']})
        self.assertFalse(result['final_delta']['created'])

    def test_different_clarification_stops_before_sending_an_unrelated_answer(self):
        case = self.case('c04-affirm-draft')
        case['turns'][0]['mock'][0][0]['arguments'] = {'question': '要不要先指定一个时间？'}
        result = eval_tool.run_case(case,eval_tool.MockProvider(),[20])
        self.assertEqual(result['verdict'],'needs_review',result['issues'])
        self.assertEqual(len(result['turns']),1)
        self.assertFalse(result['final_delta']['receipts'])

    def test_ddl_can_move_from_title_to_type_but_cannot_be_lost(self):
        case = self.memo_case('memo-a01-compact-ddl')
        fields = case['turns'][0]['mock'][0][0]['arguments']['items'][0]['fields']
        fields['title'] = '新药研发'
        typed = eval_tool.run_case(case,eval_tool.MockProvider(),[10])
        self.assertEqual(typed['verdict'],'pass',typed['issues'])
        self.assertEqual(typed['final_delta']['created'][0]['event_type'],'ddl')
        fields.pop('event_type')
        untyped = eval_tool.run_case(case,eval_tool.MockProvider(),[10])
        self.assertEqual(untyped['verdict'],'fail',untyped['issues'])
        self.assertEqual(untyped['final_delta']['created'][0]['event_type'],'general')

    def test_text_quality_is_independent_of_transaction_success(self):
        case = self.case('a01-create-tomorrow')
        case['expected_interaction'][0]['message_contains'] = ['不可能出现在正常回执的文本']
        result = eval_tool.run_case(case,eval_tool.MockProvider(),[20])
        self.assertEqual(result['verdict'],'needs_review',result['issues'])
        self.assertEqual(result['dimensions']['response'],'needs_review')
        self.assertEqual(result['dimensions']['state'],'pass')

    def test_one_turn_safe_clarification_is_review_not_false_verification_failure(self):
        case = self.case('a01-create-tomorrow')
        case['turns'][0]['mock'] = [[dict(name='clarify', arguments=dict(question='要指定具体几点吗？'))]]
        result = eval_tool.run_case(case,eval_tool.MockProvider(),[20])
        self.assertEqual(result['verdict'],'needs_review',result['issues'])
        self.assertEqual(result['dimensions']['state'],'not_checked')

    def test_malformed_model_plan_is_a_model_failure(self):
        provider = Mock()
        provider.complete_bounded.side_effect = [
            dict(tool_calls=[dict(id='invalid', function=dict(name='propose_events',
                 arguments=json.dumps(dict(action='create',items=[None]))))]),
            dict(content=''),
        ]
        result = eval_tool.run_case(self.case('a01-create-tomorrow'),provider,[10])
        self.assertEqual(result['verdict'],'fail',result['issues'])
        self.assertIn('plan_incomplete',{issue['code'] for issue in result['issues']})

    def test_provider_failure_and_call_limit_are_infrastructure_errors(self):
        case = self.case('a01-create-tomorrow')
        provider = Mock()
        provider.complete_bounded.side_effect = eval_tool.ChatError('model_unavailable','secret body')
        result = eval_tool.run_case(case,provider,[1])
        self.assertEqual(result['verdict'],'infrastructure_error',result['issues'])
        self.assertEqual(result['issues'][0]['code'],'model_unavailable')
        provider.complete_bounded.side_effect = TimeoutError()
        timeout = eval_tool.run_case(case,provider,[1])
        self.assertEqual(timeout['verdict'],'infrastructure_error')
        self.assertEqual(timeout['issues'][0]['code'],'timeout')
        provider.reset_mock()
        result = eval_tool.run_case(case,provider,[0])
        self.assertEqual(result['verdict'],'infrastructure_error')
        provider.complete_bounded.assert_not_called()

    def test_initialization_and_judge_errors_cannot_pass_or_fall_back(self):
        with (
            patch.object(eval_tool.EvaluationCalendar,'start',side_effect=OSError('unavailable')),
            patch.object(eval_tool,'HeadlessRuntime') as runtime,
        ):
            result = self.run_mock('d05-non-action')
        runtime.assert_not_called()
        self.assertEqual(result['verdict'],'infrastructure_error')
        self.assertIsNone(result['final_delta'])
        with patch.object(eval_tool,'judge',side_effect=RuntimeError('broken judge')):
            result = self.run_mock('d05-non-action')
        self.assertEqual(result['verdict'],'infrastructure_error')

    def test_profile_and_out_of_tree_paths_are_rejected(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            profile = root/'real-profile'
            trial = root/'trial'
            profile.mkdir()
            trial.mkdir()
            data = profile/'calendar.sqlite3'
            data.write_bytes(b'must not open')
            with patch.object(eval_tool,'DATA_DIR',profile), patch.object(eval_tool,'Store') as store:
                with self.assertRaises(ValueError):
                    eval_tool.read_calendar(data,trial)
                with self.assertRaises(ValueError):
                    eval_tool.EvaluationCalendar(profile,Mock(),eval_tool.reference_time(self.case('a01-create-tomorrow')))
                with self.assertRaises(ValueError):
                    eval_tool.checked_path(root/'other.sqlite3',trial)
            store.assert_not_called()
            self.assertEqual(data.read_bytes(),b'must not open')

    def test_recording_does_not_export_provider_prompts_or_credentials(self):
        secret, token = 'private-api-value', 'private-http-value'
        case = self.case('d05-non-action')
        provider = Mock()
        provider.complete_bounded.return_value = dict(reasoning_content='DO NOT RECORD THIS', tool_calls=[
            dict(id='call-1', function=dict(name='clarify', arguments=json.dumps(
                 dict(question=f'Bearer {secret} {token}'))))])
        result = eval_tool.run_case(case,provider,[3],secrets=(secret,token))
        self.assertEqual(result['verdict'],'pass',result['issues'])
        serialized = json.dumps(result)
        for text in (secret,token,'DO NOT RECORD THIS','category_catalog','我的密钥'):
            self.assertNotIn(text,serialized)
        self.assertIn('[REDACTED]',serialized)
        self.assertEqual(eval_tool.redact({'api_key':'abcd','Authorization':'anything'}),
                         {'api_key':'[REDACTED]','Authorization':'[REDACTED]'})

    def test_live_is_opt_in_and_uses_only_provider_fields(self):
        with (
            TemporaryDirectory() as temp,
            redirect_stdout(StringIO()),
            patch.object(eval_tool,'AIConfiguration',side_effect=AssertionError('credential access')),
            patch.object(eval_tool,'DeepSeekProvider',side_effect=AssertionError('live provider used')),
        ):
            self.assertEqual(eval_tool.main(['--cases','a01-create-tomorrow','--output',temp]),0)
            run, = Path(temp).iterdir()
            self.assertEqual(json.loads((run/'summary.json').read_text(encoding='utf-8'))['mode'],'mock')
            self.assertTrue((run/'report.md').exists())
            self.assertTrue((run/'cases.jsonl').exists())
        config = dict(endpoint='https://api.deepseek.com',model='test',api_key='private',
                      timeout=30,temperature=0.1,consent=True,token='local-http',http_enabled=True)
        with TemporaryDirectory() as temp:
            path = Path(temp)/'configuration.dpapi'
            path.touch()
            with patch.object(eval_tool,'AIConfiguration',return_value=Mock(load=Mock(return_value=config))):
                provider, secrets, model = eval_tool.live_provider(path)
                self.assertEqual(model,'test')
                self.assertNotIn('token',provider.configuration.load())
                self.assertNotIn('http_enabled',provider.configuration.load())
                self.assertEqual(secrets,('private','local-http'))
                config['consent'] = False
                with self.assertRaises(ValueError):
                    eval_tool.live_provider(path)

    def test_repeat_summary_keeps_review_in_denominator_and_detects_variation(self):
        result = self.run_mock('a01-create-tomorrow')
        reviewed = {**result,'verdict':'needs_review','attempt':2}
        summary = eval_tool.summarize([result,reviewed])
        self.assertEqual(summary['supported_pass_rate'],0.5)
        self.assertTrue(summary['per_case'][result['case_id']]['varied'])

    def test_live_cli_calls_injected_provider_only_when_explicit(self):
        fixture = self.case('a01-create-tomorrow')
        provider = eval_tool.MockProvider()
        provider.set_turn(fixture['turns'][0]['mock'])
        injected = Mock(complete_bounded=provider.complete_bounded)
        with (
            TemporaryDirectory() as temp,
            redirect_stdout(StringIO()),
            patch.object(eval_tool,'live_provider',return_value=(injected,('credential-sample',),'fake-live')) as loader,
        ):
            path = str(Path(temp)/'configuration.dpapi')
            result = eval_tool.main(['--mode','live','--config',path,'--cases',fixture['case_id'],
                                     '--max-calls','1','--output',temp])
            loader.assert_called_once_with(Path(path))
            self.assertEqual(result,0)
            run, = Path(temp).iterdir()
            summary = json.loads((run/'summary.json').read_text(encoding='utf-8'))
            self.assertEqual((summary['mode'],summary['model_calls']),('live',1))
            self.assertNotIn('credential-sample',(run/'cases.jsonl').read_text(encoding='utf-8'))
