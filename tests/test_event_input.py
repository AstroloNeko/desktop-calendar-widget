"""Shared explicit input parity, isolated store, no live model/user data."""
from dataclasses import asdict
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from app import EventEditor, parse_event_due
from calendar_core import Event, EventCategory, Store, COLORS
from calendar_action_gateway import CalendarActionGateway
from calendar_chat_tools import ChatError
from event_commands import EventCommandHistory
from event_input import build_event_candidate, EventInputError


class EventInputTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/'calendar.json')
        self.store.categories = [EventCategory('lab', '实验', COLORS['薄荷绿'])]
        self.store.save()
        self.history = EventCommandHistory(self.store)
        self.gateway = CalendarActionGateway(self.store, self.history)

    def build(self, values, current=None):
        return build_event_candidate(values, current=current, identifier='one', context=self.store)

    def gui(self, values, current=None):
        """Execute the actual editor save callback with explicit widget values."""
        defaults = dict(title='WB 实验', date='2026-11-23', time=None, duration_days=1,
            skip_non_working_days=False, end_as_ddl=False, color=COLORS['海盐蓝'],
            category_id=None, color_mode='override', event_type='general', reminder=None, notes='')
        defaults.update(values)
        app = SimpleNamespace(store=self.store, upsert_event=Mock(return_value=True))
        editor = SimpleNamespace(event=current, master_app=app, close=Mock(), title_entry=Mock())
        mapping = {'title':'title_var', 'date':'date_var', 'time':'time_var', 'duration_days':'duration_var',
            'skip_non_working_days':'skip_non_working_var', 'end_as_ddl':'end_as_ddl_var',
            'color':'color_var', 'color_mode':'color_mode_var', 'event_type':'event_type_var'}
        for key, attribute in mapping.items():
            value = defaults[key]
            if key in ('time', 'duration_days'):
                value = str(value) if value is not None else ''
            setattr(editor, attribute, SimpleNamespace(get=lambda value=value: value))
        from calendar_core import REMINDERS
        label = next(label for label, minutes in REMINDERS.items() if minutes == defaults['reminder'])
        editor.reminder_var = SimpleNamespace(get=lambda: label)
        editor.notes = SimpleNamespace(get=lambda *a: defaults['notes'])
        editor._selected_category = lambda: self.store.category_by_id(defaults['category_id'])
        with patch('app.owned_messagebox') as message:
            EventEditor.save(editor)
        self.assertFalse(message.called)
        return app.upsert_event.call_args.args[0]

    def test_gui_ai_create_parity(self):
        for extra in ({}, dict(category_id='lab', color=COLORS['鸢尾紫'], color_mode='override'),
                      dict(category_id='lab', color_mode='inherit'),
                      dict(time='09:30', reminder=30, event_type='urgent')):
            with self.subTest(extra=extra):
                values = dict(title='WB 实验', date='2026-11-23', **extra)
                gui = asdict(self.gui(values))
                ai = self.gateway.preview('preview_create', values, 'one')['after']
                for key in ('id', 'created_at'):
                    gui.pop(key); ai.pop(key)
                self.assertEqual(gui, ai)

    def test_title_patch_preserves_every_other_field(self):
        item = Event('one', '原名', '2026-11-23T09:30:15+08:00', category_id='lab',
            color=COLORS['鸢尾紫'], color_mode='override', duration_days=5, end_as_ddl=True,
            reminder=30, notes='完整备注', done=True, snooze_until='2026-11-24T10:00')
        before = asdict(item)
        candidate = self.build({'title':'动物实验'}, item)
        self.assertEqual(asdict(candidate), {**before, 'title':'动物实验'})
        self.assertEqual(asdict(item), before)

    def test_null_missing_and_explicit_values(self):
        item = self.build(dict(title='a', date='2026-11-23', time='10:00', reminder=30, category_id='lab'))
        self.assertEqual(self.build({'notes':''}, item).reminder, 30)
        self.assertIsNone(self.build({'reminder':None}, item).reminder)
        cleared = self.build({'category_id':None}, item)
        self.assertEqual((cleared.category_id, cleared.color_mode, cleared.color), (None, 'override', item.color))
        for name in ('title','date','color','duration_days','notes','done','event_type'):
            with self.subTest(name=name), self.assertRaises(EventInputError):
                self.build({name:None}, item)

    def test_date_only_preserves_exact_old_clock(self):
        item = Event('one','a','2026-11-23T09:30:15+08:00')
        self.assertEqual(self.build({'date':'2026-11-24'}, item).due, '2026-11-24T09:30:15+08:00')

    def test_no_time_ddl_preserves_boundary(self):
        item = self.build(dict(title='a', date='2026-11-23', event_type='ddl', duration_days=4))
        for values in ({'title':'b'}, {'color':COLORS['鸢尾紫']}, {'category_id':'lab'}):
            candidate = self.build(values,item)
            self.assertFalse(candidate.has_time)
            self.assertIsNone(candidate.reminder)
            self.assertEqual(self.store.event_ends_at(candidate), self.store.event_ends_at(item))
            self.assertTrue(candidate.due.endswith('23:59'))

    def test_invalid_explicit_inputs(self):
        base = dict(title='a', date='2026-11-23')
        invalid = ({'title':' '}, {'date':'2026-02-30'}, {'time':'25:00'}, {'event_type':'low'},
            {'duration_days':0}, {'duration_days':366}, {'duration_days':True}, {'category_id':'missing'},
            {'color':'purple'}, {'color_mode':'inherit'}, {'reminder':-1}, {'reminder':True},
            {'skip_non_working_days':1}, {'end_as_ddl':True}, {'id':'overwrite'}, {'notes':[]})
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(EventInputError):
                self.build({**base, **values})

    def test_ai_create_inherit_tolerance_stays_in_adapter(self):
        values = dict(title='a', date='2026-11-23', color_mode='inherit')
        self.assertEqual(self.gateway.preview('preview_create', values, 'one')['after']['color_mode'], 'override')
        with self.assertRaises(EventInputError):
            self.build(values)
        item = self.build(dict(title='a', date='2026-11-23'))
        with self.assertRaises(ChatError):
            self.gateway._patch(item, {'color_mode':'inherit'})

    def test_inherit_with_swatch_retains_current_gui_semantics(self):
        # Existing GUI submits its swatch even in inherit mode: category wins.
        item = self.build(dict(title='a', date='2026-11-23', category_id='lab',
            color_mode='inherit', color=COLORS['鸢尾紫']))
        self.assertEqual(item.color, self.store.categories[0].color)
        override = self.build({'color':COLORS['鸢尾紫']}, item)
        self.store.categories[0].color = COLORS['珊瑚红']
        self.assertEqual(self.store.effective_event_color(override), COLORS['鸢尾紫'])

    def test_skip_days_and_endpoint_use_existing_rules(self):
        self.store.set_date_status(date(2026,10,9), 'leave')
        values = dict(title='a', date='2026-10-01', duration_days=4, skip_non_working_days=True, end_as_ddl=True)
        candidate = self.build(values)
        old = Event('old','a','2026-10-01T23:59', has_time=False, duration_days=4,
            skip_non_working_days=True, end_as_ddl=True)
        self.assertEqual(self.store.event_dates(candidate), self.store.event_dates(old))
        end = self.store.event_end_date(old)
        endpoint = self.build({**values, 'end_date':end.isoformat()})
        self.assertEqual(endpoint.duration_days, 4)
        self.assertEqual(self.store.event_end_date(endpoint), end)

    def test_preview_has_no_side_effects(self):
        disk = self.store.data_file.read_bytes()
        with patch.object(self.store, 'save') as save:
            self.build(dict(title='a', date='2026-12-31', duration_days=3))
            self.gateway.preview('preview_create', dict(title='a', date='2026-12-31'), 'one')
        save.assert_not_called()
        self.assertEqual(self.store.events, [])
        self.assertEqual(self.history.undo_count, 0)
        self.assertEqual(self.store.data_file.read_bytes(), disk)

    def test_quick_and_parser_compatibility(self):
        item = Store.make_quick_event('  a  ', date(2026,11,23))
        self.assertEqual((item.title, item.event_type, item.has_time, item.reminder), ('a','general',False,None))
        self.assertEqual(parse_event_due('2026-11-23','')[0].isoformat(), '2026-11-23T23:59:00')
        self.assertTrue(parse_event_due('2026-11-23','9:30')[1])

    def test_commit_undo_redo_still_use_existing_history(self):
        candidate = self.build(dict(title='a',date='2026-11-23'))
        self.history.upsert(candidate)
        self.history.upsert(self.build({'title':'b'}, candidate))
        self.history.undo()
        self.assertEqual(self.store.events[0].title,'a')
        self.history.redo()
        self.assertEqual(Store(self.store.data_file).events[0].title,'b')
