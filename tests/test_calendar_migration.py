from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from calendar_core import Event,EventCategory,RoutineItem,Store
from calendar_migration import validate_source,dry_run_migration
from desktop_calendar.storage.calendar_repository import RepositoryConflictError
from habit_schedule import SCHEDULE_FIELDS
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository


def fixture(count=2):
    category=EventCategory('legacy-category','Synthetic','#52B788',created_at='2025-01-01T10:00')
    routine=RoutineItem('legacy-habit','Synthetic habit',created_on='2025-01-01',workdays_only=False,
        habit_done=['2025-01-02','2025-02-01'],frequency='weekly',weekdays=[0,2,4],reminder_enabled=True,reminder_time='09:00')
    rule={k:deepcopy(getattr(routine,k)) for k in SCHEDULE_FIELDS}
    routine.schedule_history=[{'until':'2025-11-15','rule':rule}]
    return dict(version=9,categories=[asdict(category)],routines=[asdict(routine)],
        events=[asdict(Event('legacy-'+str(i),'Same title','2025-12-30T23:59',has_time=False,
            created_at='2025-01-01T10:00',duration_days=5,skip_non_working_days=True,end_as_ddl=True,
            category_id=category.id,color_mode='inherit',color=category.color,notes='PRIVATE_NOTE_SENTINEL')) for i in range(count)],
        date_states={'2026-01-01':'leave'},settings={'theme':'paper','unknown_old':{'secret':'PRIVATE_SETTING_SENTINEL'},
        'native_reminders':{'version':1,'enabled':True,'time':'09:00'},'native_reminder_state':{'entries':{}}},notified=['z:old','a:old'])


class MigrationTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.root=Path(folder.name);self.source=self.root/'source.json';self.stage=self.root/'rehearsal'
        self.data=fixture();self.write()

    def write(self):self.source.write_text(json.dumps(self.data,ensure_ascii=False),encoding='utf8')

    def test_managed_user_directory_staging_requires_explicit_confined_parent(self):
        stage=self.root/'stage-explicit-test'
        with patch('calendar_migration.DATA_DIR',self.root):
            rejected=dry_run_migration(self.source,stage)
            self.assertFalse(rejected['candidate_ready'])
            rejected=dry_run_migration(self.source,stage,managed_directory=self.root.parent)
            self.assertFalse(rejected['candidate_ready'])
            accepted=dry_run_migration(self.source,stage,managed_directory=self.root)
            self.assertTrue(accepted['candidate_ready'],accepted['issues'])
    def result(self):self.write();return validate_source(self.source)
    def codes(self):return {i.code for i in self.result().issues}
    def run_dry(self):self.write();return dry_run_migration(self.source,self.stage)
    def assert_success(self,result):
        self.assertTrue(result['candidate_ready'],result)
        self.assertFalse(result['switch_allowed'])
        self.assertTrue(result['source_unchanged'])
        self.assertTrue(result['comparison']['equal'])
    def assert_failed_clean(self,result):
        self.assertFalse(result['candidate_ready'],result)
        self.assertFalse((self.stage/'calendar.candidate.sqlite3').exists())

    def test_full_roundtrip(self):
        before=self.source.read_bytes();result=self.run_dry();self.assert_success(result)
        self.assertEqual(before,self.source.read_bytes())
        self.assertEqual(result['imported_counts']['events'],2)
        self.assertEqual(result['imported_counts']['habit_completions'],2)
        self.assertEqual(result['imported_counts']['habit_schedule_history'],1)

    def test_empty_calendar(self):
        self.data=dict(version=9,events=[],routines=[],categories=[],date_states={},settings={},notified=[])
        result=self.run_dry();self.assert_success(result)
        self.assertEqual(result['import_revision']['generation'],1)

    def test_single_event(self):
        self.data=fixture(1);self.assert_success(self.run_dry())

    def test_large_calendar(self):
        self.data=fixture(100);self.assert_success(self.run_dry())

    def test_same_title_date_distinct_ids(self):
        result=self.run_dry();self.assert_success(result)
        store=Store(Path(result['candidate_path']),repository_factory=SqliteCalendarRepository)
        self.assertEqual([e.id for e in store.events],['legacy-0','legacy-1'])
        store.repository.close()

    def test_untimed_ddl_skip_dates(self):
        result=self.run_dry();self.assert_success(result)
        store=Store(Path(result['candidate_path']),repository_factory=SqliteCalendarRepository)
        self.assertFalse(store.events[0].has_time)
        self.assertEqual(store.events[0].due,'2025-12-30T23:59')
        self.assertNotIn(date(2026,1,1),store.event_dates(store.events[0]))
        self.assertTrue(store.event_has_deadline(store.events[0]))
        store.repository.close()

    def test_native_ddl_and_override(self):
        e=self.data['events'][0];e.update(event_type='ddl',end_as_ddl=False,color_mode='override',color='#6687F2')
        self.assert_success(self.run_dry())

    def test_monthly_yearly_custom_rules(self):
        for i,frequency in enumerate(('daily','weekly','monthly','yearly')):
            r=RoutineItem(str(i),'Habit',created_on='2024-02-29',frequency=frequency,interval=i+2,
                month_day=29,year_month=2,weekdays=[0,4],workdays_only=False)
            self.data['routines'].append(asdict(r))
        self.assert_success(self.run_dry())

    def test_todo_compatibility(self):
        self.data['routines'].append(asdict(RoutineItem('todo','Todo',kind='todo',created_on='2025-01-01',completed_on='2025-02-01')))
        self.assert_success(self.run_dry())

    def test_category_raw_order_preserved(self):
        self.data['categories'].insert(0,asdict(EventCategory('first','First',sort_order=99,created_at='2025-01-01T10:00')))
        result=self.run_dry();self.assert_success(result)
        store=Store(Path(result['candidate_path']),repository_factory=SqliteCalendarRepository)
        self.assertEqual([c.id for c in store.categories],['first','legacy-category'])
        store.settings['theme']='modern';store.save()
        store.repository.close()

    def test_normal_date_state_explicit_normalization(self):
        self.data['date_states']['2026-01-02']='normal'
        result=self.run_dry();self.assert_success(result)
        self.assertEqual(result['validation']['counts']['date_states'],2)
        self.assertEqual(result['imported_counts']['date_states'],1)
        self.assertIn('normal_means_no_override',[n['code'] for n in result['validation']['normalizations']])

    def test_all_notification_keys_not_pruned(self):
        self.data['notified']=[str(i) for i in range(700)]
        result=self.run_dry();self.assert_success(result)
        self.assertEqual(result['imported_counts']['reminder_delivery'],700)

    def test_report_redacts_values(self):
        result=self.run_dry();self.assert_success(result)
        report=(self.stage/'migration-report.json').read_text(encoding='utf8')
        self.assertNotIn('PRIVATE_',report)
        self.assertNotIn('Same title',report)
        self.assertEqual(json.loads(report),result)
        self.assertTrue((self.stage/'migration-report.txt').exists())

    def test_invalid_json(self):
        self.source.write_bytes(b'{broken')
        self.assertEqual(validate_source(self.source).issues[0].code,'malformed_json')

    def test_duplicate_json_key(self):
        self.source.write_bytes(b'{"version":9,"version":9}')
        self.assertIn('duplicate_json_key',{i.code for i in validate_source(self.source).issues})

    def test_old_schema_explicitly_not_imported(self):
        for schema in range(1,8):
            self.data['version']=schema
            self.assertIn('unsupported_schema',self.codes())

    def test_future_schema_rejected(self):
        self.data['version']=10;self.assertIn('unsupported_schema',self.codes())

    def test_wrong_schema_type(self):
        self.data['version']=True;self.assertIn('unsupported_schema',self.codes())

    def test_duplicate_event_id(self):
        self.data['events'][1]['id']=self.data['events'][0]['id'];self.assertIn('duplicate_event_id',self.codes())

    def test_duplicate_habit_id(self):
        self.data['routines']*=2;self.assertIn('duplicate_habit_id',self.codes())

    def test_duplicate_category_id(self):
        self.data['categories']*=2;self.assertIn('duplicate_category_id',self.codes())

    def test_empty_id(self):
        self.data['events'][0]['id']=' ';self.assertIn('empty_id',self.codes())

    def test_invalid_date(self):
        self.data['events'][0]['due']='2026-02-30T10:00';self.assertIn('invalid_due',self.codes())

    def test_timezone_not_guessed(self):
        self.data['events'][0]['due']='2026-02-20T10:00+08:00';self.assertIn('invalid_due',self.codes())

    def test_invalid_duration(self):
        self.data['events'][0]['duration_days']=366;self.assertIn('invalid_duration',self.codes())

    def test_integer_not_boolean(self):
        self.data['events'][0]['done']=1;self.assertIn('invalid_field_type',self.codes())

    def test_invalid_rule(self):
        self.data['routines'][0]['interval']=0;self.assertIn('invalid_habit_rule',self.codes())

    def test_invalid_history(self):
        self.data['routines'][0]['schedule_history'][0]['until']='bad';self.assertIn('invalid_habit_history',self.codes())

    def test_same_day_history_duplicate_boundary(self):
        self.data['routines'][0]['schedule_history']*=2;self.assertIn('invalid_habit_history',self.codes())

    def test_completion_history_never_deduplicated(self):
        self.data['routines'][0]['habit_done']*=2;self.assertIn('invalid_habit_completion',self.codes())

    def test_completion_history_reordering_rejected(self):
        self.data['routines'][0]['habit_done'].reverse();self.assertIn('noncanonical_record',self.codes())

    def test_missing_category(self):
        self.data['categories']=[];self.assertIn('invalid_category_reference',self.codes())

    def test_fallback_color_conflict_rejected(self):
        self.data['events'][0]['color']='#6687F2';self.assertIn('inherited_color_mismatch',self.codes())

    def test_missing_field_no_invented_timestamp(self):
        del self.data['events'][0]['created_at'];self.assertIn('missing_required_field',self.codes())

    def test_unknown_business_field_not_discarded(self):
        self.data['events'][0]['unmapped']={'x':1};self.assertIn('unmapped_business_field',self.codes())

    def test_invalid_date_override(self):
        self.data['date_states']['2026-01-02']='workday';self.assertIn('invalid_date_state',self.codes())

    def test_invalid_record_not_lost(self):
        self.data['events'].append({'bad':'record'})
        result=self.run_dry();self.assert_failed_clean(result)
        self.assertEqual(result['validation']['counts']['events'],3)

    def test_existing_target_not_overwritten(self):
        self.stage.mkdir();target=self.stage/'calendar.candidate.sqlite3';target.write_bytes(b'keep')
        result=self.run_dry();self.assertFalse(result['candidate_ready']);self.assertEqual(target.read_bytes(),b'keep')

    def test_import_sql_failure_rollback_and_cleanup(self):
        original=SqliteCalendarRepository._sync_records
        def fail(repo,db,table,*args,**kwargs):
            result=original(repo,db,table,*args,**kwargs)
            if table=='events':raise sqlite3.OperationalError('injected')
            return result
        with patch.object(SqliteCalendarRepository,'_sync_records',fail):result=self.run_dry()
        self.assert_failed_clean(result)
        self.assertTrue(result['source_unchanged'])

    def test_integrity_failure(self):
        with patch('calendar_migration._integrity',side_effect=ValueError('injected')):result=self.run_dry()
        self.assert_failed_clean(result)
        self.assertEqual(result['issues'][0]['code'],'sqlite_integrity_failure')

    def test_source_changed_during_dry_run(self):
        from calendar_migration import _compare_candidate
        def change(*args):
            result=_compare_candidate(*args);self.source.write_bytes(b'changed externally');return result
        with patch('calendar_migration._compare_candidate',side_effect=change):result=self.run_dry()
        self.assert_failed_clean(result)
        self.assertFalse(result['source_unchanged'])
        self.assertEqual(self.source.read_bytes(),b'changed externally')

    def test_unwritable_staging(self):
        with patch.object(Path,'mkdir',side_effect=PermissionError('no')):result=self.run_dry()
        self.assert_failed_clean(result)

    def test_reopen_failed(self):
        with patch('calendar_migration._reopen_target',side_effect=ValueError('injected')):result=self.run_dry()
        self.assert_failed_clean(result)
        self.assertEqual(result['issues'][0]['code'],'target_reopen_failed')

    def test_corrupted_candidate(self):
        def corrupt(repo):
            repo.path.write_bytes(b'corrupted');raise sqlite3.DatabaseError('injected')
        with patch('calendar_migration._integrity',side_effect=corrupt):result=self.run_dry()
        self.assert_failed_clean(result)

    def test_comparison_detects_dropped_record(self):
        from calendar_migration import _reopen_target
        def drop(path):
            store=_reopen_target(path);store.events.pop();return store
        with patch('calendar_migration._reopen_target',side_effect=drop):result=self.run_dry()
        self.assert_failed_clean(result)
        self.assertEqual(result['issues'][0]['code'],'inconsistent_snapshot')

    def test_user_data_output_forbidden(self):
        with patch('calendar_migration.DATA_DIR',self.stage):result=self.run_dry()
        self.assertFalse(result['candidate_ready']);self.assertFalse(self.stage.exists())

    def test_import_rejects_nonempty_repository(self):
        result=self.run_dry();self.assert_success(result)
        store=Store(Path(result['candidate_path']),repository_factory=SqliteCalendarRepository)
        with self.assertRaises(RepositoryConflictError):
            store.repository.import_snapshot(dict(events=[],routines=[],categories=[],date_states={},notified=set()),{},source_hash='a'*64)
        self.assertEqual(len(store.events),2)
        store.repository.close()

    def test_transaction_failure_does_not_leave_partial_rows(self):
        path=self.root/'isolated.db';store=Store(path,repository_factory=SqliteCalendarRepository)
        with patch.object(store.repository,'_commit',side_effect=OSError('injected')):
            with self.assertRaises(OSError):
                store.repository.import_snapshot(dict(events=[Event.from_dict(self.data['events'][0])],
                    routines=[],categories=[EventCategory.from_dict(self.data['categories'][0])],date_states={},notified=set()),
                    self.data['settings'],source_hash='a'*64)
        with closing(sqlite3.connect(path)) as db:
            for table in ('events','categories','preferences','operation_receipts'):
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0],0)
        store.repository.close()

    def test_nan_rejected(self):
        self.source.write_text('{"version":9,"settings":{"x":NaN}}',encoding='utf8')
        self.assertFalse(validate_source(self.source).valid)

    def test_report_write_failure_removes_candidate(self):
        from calendar_migration import _write_reports
        calls=0
        def fail(stage,report):
            nonlocal calls
            calls+=1
            if calls==2:raise OSError('injected')
            return _write_reports(stage,report)
        with patch('calendar_migration._write_reports',side_effect=fail):result=self.run_dry()
        self.assert_failed_clean(result)

    def test_default_store_remains_json(self):
        from desktop_calendar.storage.json_calendar_repository import JsonCalendarRepository
        self.assertIs(type(Store(self.source).repository),JsonCalendarRepository)


if __name__=='__main__':unittest.main()
