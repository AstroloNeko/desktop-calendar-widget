from copy import deepcopy
from datetime import date,timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from desktop_calendar.storage.calendar_migration import validate_source,dry_run_migration
from calendar_core import Store
from desktop_calendar.storage.sqlite_calendar_repository import SqliteCalendarRepository

FIXTURE=json.loads((Path(__file__).parent/'fixtures'/'schema8_v0102.json').read_text(encoding='utf8'))

class Schema8Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'source.json'
        self.data=deepcopy(FIXTURE['document'])
    def validate(self):
        self.source.write_text(json.dumps(self.data),encoding='utf8')
        return validate_source(self.source)
    def migrate(self):
        self.validate();return dry_run_migration(self.source,self.root/'stage')
    def test_real_serializer_and_independent_oracle(self):
        self.assertEqual(self.data['version'],8)
        result=self.migrate();self.assertTrue(result['candidate_ready'],result)
        s=Store(Path(result['candidate_path']),repository_factory=SqliteCalendarRepository)
        self.addCleanup(s.repository.close)
        self.assertEqual([e.id for e in s.events],[e['id'] for e in self.data['events']])
        self.assertEqual([r.habit_done for r in s.routines],[r['habit_done'] for r in self.data['routines']])
        for e,old in zip(s.events,FIXTURE['oracle']['events']):
            self.assertEqual([d.isoformat() for d in s.event_dates(e)],old['dates'])
            self.assertEqual(s.event_ends_at(e).isoformat(),old['end'])
            self.assertEqual(s.effective_event_color(e),old['color'])
        days=[date(2024,12,30)+timedelta(days=i) for i in range(800)]
        for r,old in zip(s.routines,FIXTURE['oracle']['habits']):
            self.assertEqual([d.isoformat() for d in days if s.routine_planned_on(r,d)],old['planned'])
            self.assertEqual(r.schedule_history,[])
    def test_conditional_workdays(self):
        for frequency in (True,False):
            for explicit in (True,False,None):
                with self.subTest(frequency=frequency,explicit=explicit):
                    r=self.data['routines'][0];r.pop('workdays_only',None)
                    if frequency:r['frequency']='daily'
                    else:r.pop('frequency',None)
                    if explicit is not None:r['workdays_only']=explicit
                    v=self.validate();self.assertTrue(v.valid,v.report())
                    self.assertEqual(v._document['routines'][0]['workdays_only'],explicit if explicit is not None else not frequency)
    def test_missing_static_defaults(self):
        e=self.data['events'][2]
        for k in ('notes','done','color','reminder','duration_days','category_id','color_mode'):e.pop(k)
        v=self.validate();self.assertTrue(v.valid,v.report())
        self.assertEqual(v._document['events'][2]['reminder'],60)
    def test_anchor_defaults(self):
        r=self.data['routines'][0]
        for k in ('start_on','month_day','year_month','weekdays','frequency','interval'):r.pop(k)
        v=self.validate();self.assertTrue(v.valid,v.report())
        self.assertEqual(v._document['routines'][0]['start_on'],r['created_on'])
    def test_untimed_missing_reminder(self):
        self.data['events'][0].pop('reminder')
        v=self.validate();self.assertTrue(v.valid);self.assertIsNone(v._document['events'][0]['reminder'])
    def test_inherited_missing_color(self):
        self.data['events'][-1].pop('color')
        v=self.validate();self.assertTrue(v.valid,v.report())
        self.assertEqual(v._document['events'][-1]['color'],self.data['categories'][0]['color'])
    def test_alias(self):
        e=self.data['events'][2];e.pop('event_type');e['priority']='normal'
        v=self.validate();self.assertTrue(v.valid);self.assertEqual(v._document['events'][2]['event_type'],'general')
    def test_conflicting_alias_rejected(self):
        self.data['events'][2]['priority']='urgent';self.assertFalse(self.validate().valid)
    def test_unknown_field_rejected(self):
        self.data['events'][0]['future']='not lost';self.assertFalse(self.validate().valid)
    def test_duplicate_rejected(self):
        self.data['events'][1]['id']=self.data['events'][0]['id'];self.assertFalse(self.validate().valid)
    def test_no_clock_defaults(self):
        self.data['events'][0].pop('created_at');self.assertFalse(self.validate().valid)
    def test_no_created_on_guess(self):
        self.data['routines'][0].pop('created_on');self.assertFalse(self.validate().valid)
    def test_bad_date_rejected(self):
        self.data['events'][0]['due']='bad';self.assertFalse(self.migrate()['candidate_ready'])
        self.assertFalse((self.root/'stage'/'calendar.candidate.sqlite3').exists())
    def test_source_hash_and_report(self):
        self.validate();before=self.source.read_bytes();r=self.migrate()
        self.assertEqual(self.source.read_bytes(),before)
        self.assertEqual(r['source_sha256'],hashlib.sha256(before).hexdigest())
        self.assertEqual(r['source_schema_version'],8);self.assertFalse(r['switch_allowed'])
        self.assertTrue(r['field_mapping_verified']);self.assertTrue(r['legacy_defaults_applied'])
        self.assertIsNone(r['source_app_version'])
    def test_future_and_older_schemas(self):
        for version in (*range(1,8),10):
            self.data['version']=version;self.assertFalse(self.validate().valid)
    def test_schema9_not_relaxed(self):
        self.data['version']=9;self.assertFalse(self.validate().valid)
    def test_schema8_cannot_smuggle_history(self):
        self.data['routines'][0]['schedule_history']=[];self.assertFalse(self.validate().valid)
    def test_rollback(self):
        with patch.object(SqliteCalendarRepository,'_commit',side_effect=OSError('injected')):r=self.migrate()
        self.assertFalse(r['candidate_ready']);self.assertFalse((self.root/'stage'/'calendar.candidate.sqlite3').exists())
    def test_existing_target(self):
        self.assertTrue(self.migrate()['candidate_ready'])
        target=self.root/'stage'/'calendar.candidate.sqlite3';before=target.read_bytes()
        self.assertFalse(dry_run_migration(self.source,self.root/'stage')['candidate_ready'])
        self.assertEqual(target.read_bytes(),before)
    def test_future_edit_exclusive_history(self):
        from habit_schedule import preserve_schedule_history,rule_on
        from calendar_core import RoutineItem
        old=RoutineItem.from_dict(self.validate()._document['routines'][0]);new=deepcopy(old)
        new.frequency='daily';preserve_schedule_history(old,new,date(2026,1,1))
        self.assertEqual(rule_on(new,date(2025,12,31)).frequency,'weekly')
        self.assertEqual(rule_on(new,date(2026,1,1)).frequency,'daily')
        self.assertEqual(new.habit_done,old.habit_done)
    def test_defaults_match_tagged_parser(self):
        # Execute actual tagged parsing on deliberately omitted fields, not a
        # current-model simulation. The generator is entirely temporary.
        from tools.generate_schema8_fixture import generate
        generated=generate(defaults=True)
        self.data=generated['input']
        v=self.validate();self.assertTrue(v.valid,v.report())
        expected=generated['document']
        for table in ('events','routines','categories'):
            for actual,old in zip(v._document[table],expected[table]):
                self.assertEqual({k:actual[k] for k in old},old)
    def test_empty_legacy_document(self):
        self.data={'version':8};self.assertTrue(self.migrate()['candidate_ready'])
    def test_invalid_interval_not_defaulted(self):
        self.data['routines'][0]['interval']=0;self.assertFalse(self.validate().valid)
    def test_invalid_boolean_not_coerced(self):
        self.data['routines'][0]['workdays_only']='false';self.assertFalse(self.validate().valid)
    def test_history_duplicates_rejected(self):
        self.data['routines'][0]['habit_done']*=2;self.assertFalse(self.validate().valid)
    def test_bad_category_reference(self):
        self.data['events'][0]['category_id']='missing';self.assertFalse(self.validate().valid)
    def test_unknown_settings_retained(self):
        v=self.validate();self.assertEqual(v._document['settings'],self.data['settings'])
    def test_conversion_is_pure(self):
        from desktop_calendar.storage.calendar_legacy_migration import convert_schema8
        from desktop_calendar.storage.calendar_migration_validation import ValidationResult
        before=deepcopy(self.data);convert_schema8(self.data,ValidationResult());self.assertEqual(before,self.data)
    def test_source_changed(self):
        from desktop_calendar.storage.calendar_migration import _compare_candidate
        def change(*args):
            r=_compare_candidate(*args);self.source.write_bytes(b'changed by test');return r
        with patch('desktop_calendar.storage.calendar_migration._compare_candidate',side_effect=change):r=self.migrate()
        self.assertFalse(r['candidate_ready']);self.assertFalse(r['source_unchanged'])
    def test_equivalent_schema9(self):
        canonical=self.validate()._document
        legacy=self.migrate();self.data=canonical;self.validate()
        modern=dry_run_migration(self.source,self.root/'modern')
        self.assertTrue(modern['candidate_ready'])
        self.assertEqual(legacy['comparison']['semantic_sha256'],modern['comparison']['semantic_sha256'])

if __name__=='__main__':unittest.main()
