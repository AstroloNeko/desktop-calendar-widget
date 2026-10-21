import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from calendar_core import Event, EventCategory, Store
from event_search import EventSearchIndex, EventSearchService, capture_search_store, parse_search_date
from timeline_model import build_month_timeline


class EventSearchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / "data.json")
        self.store.categories = [EventCategory("art", "绘画 Art", "#52B788")]
        self.store.events = [
            Event("exact", "设计", "2027-02-03T10:30", category_id="art"),
            Event("prefix", "设计文稿", "2026-08-01T10:30"),
            Event("contains", "新的设计", "2025-01-01T10:30"),
            Event("notes", "交付", "2024-01-01T10:30", notes="设计稿  review", done=True),
        ]

    def query(self, text):
        return EventSearchIndex.from_store(self.store).query(text)

    def event_ids(self, text):
        return [row.key for row in self.query(text) if row.kind == "event"]

    def test_title_exact_prefix_contains_then_notes(self):
        self.assertEqual(self.event_ids("设计"), ["exact", "prefix", "contains", "notes"])

    def test_chinese_and_casefold_whitespace(self):
        self.assertEqual(self.event_ids("   REVIEW  "), ["notes"])
        self.assertEqual(self.event_ids("绘画   art"), ["exact"])

    def test_terms_can_span_title_and_notes_or_category(self):
        self.assertEqual(self.event_ids("交付 review"), ["notes"])
        self.assertEqual(self.event_ids("设计 art"), ["exact"])

    def test_history_future_and_completed_are_global(self):
        self.assertEqual(len(self.event_ids("")), 4)
        result = self.query("review")[0]
        self.assertTrue(result.document.completed)
        self.assertIn("已完成", result.detail)

    def test_same_rank_stable_date_then_identity(self):
        self.store.events += [Event("a", "x", "2026-08-02T10:30"), Event("z", "x", "2026-08-02T10:30"), Event("b", "x", "2026-08-01T10:30")]
        self.assertEqual(self.event_ids("x"), ["b", "a", "z"])

    def test_category_color_and_uncategorized_display(self):
        result = self.query("art")[0]
        self.assertEqual(result.document.color, "#52B788")
        self.assertIn("绘画 Art", result.detail)
        self.assertEqual(self.query("review")[0].document.category, "无分类")

    def test_ddl_metadata_reuses_actual_skip_day_end(self):
        self.store.events = [Event("task", "跨周", "2026-08-07T10:30", duration_days=2, skip_non_working_days=True, end_as_ddl=True)]
        self.store.set_date_status(date(2026, 8, 10), "leave")
        doc = self.query("跨周")[0].document
        self.assertEqual(doc.end, date(2026, 8, 11))
        self.assertEqual(doc.deadline, doc.end)
        self.assertIn("DDL 2026-08-11", doc.metadata)

    def test_native_ddl_and_general_metadata(self):
        self.store.events[0].event_type = "ddl"
        self.assertEqual(self.query("设计")[0].document.deadline, date(2027, 2, 3))
        self.assertIsNone(self.query("文稿")[0].document.deadline)

    def test_empty_query_exposes_all_seven_commands(self):
        self.assertEqual([row.key for row in self.query("") if row.kind == "command"],
                         ["new", "today", "date", "ddl", "flow", "timeline", "categories"])

    def test_command_aliases(self):
        for text, key in (("新增", "new"), ("today", "today"), ("deadline", "ddl"), ("flow", "flow"), ("timeline", "timeline"), ("category", "categories")):
            with self.subTest(text=text):
                self.assertIn(key, [row.key for row in self.query(text)])

    def test_explicit_date_formats(self):
        for text in ("2026-11-05", "2026/11/5", "2026年11月5日", "跳转到 2026-11-05", "goto 2026/11/5"):
            self.assertEqual(parse_search_date(text), date(2026, 11, 5))
            self.assertEqual(self.query(text)[0].target_date, date(2026, 11, 5))

    def test_invalid_or_ambiguous_dates_rejected(self):
        for text in ("11/5/26", "明天", "2026-02-30", "2026-13-01", "0000-01-01", "2026-11-5 extra"):
            self.assertIsNone(parse_search_date(text))

    def test_no_result_and_bounded_results(self):
        self.assertEqual(self.query("nonexistent"), ())
        index = EventSearchIndex.from_store(self.store)
        self.assertEqual(len([r for r in index.query("", limit=2) if r.kind == "event"]), 2)

    def test_read_only_snapshot_does_not_alias_live_events_categories_dates(self):
        snapshot = capture_search_store(self.store)
        self.store.events[0].title = "改名"
        self.store.categories[0].name = "新分类"
        self.store.date_states["2026-08-01"] = "leave"
        self.assertEqual(snapshot.events[0].title, "设计")
        self.assertEqual(snapshot.categories[0].name, "绘画 Art")
        self.assertEqual(snapshot.date_status(date(2026, 8, 1)), "normal")

    def test_temporary_reveal_preserves_filter_and_other_hidden_tasks(self):
        ids = set()
        model = build_month_timeline(self.store, 2027, 2, category_ids=ids, include_uncategorized=False, reveal_event_ids=("exact",))
        self.assertEqual([item.id for item in model.items], ["exact"])
        self.assertEqual(ids, set())
        self.assertFalse(build_month_timeline(self.store, 2027, 2, category_ids=ids, include_uncategorized=False).items)

    def test_duplicate_titles_remain_distinct_ids(self):
        self.store.events.append(replace(self.store.events[0], id="another"))
        self.assertEqual(self.event_ids("art"), ["another", "exact"])

    def test_async_requests_only_deliver_latest_ticket(self):
        service = EventSearchService()
        self.addCleanup(service.close)
        service.request(self.store, "设计")
        ticket = service.request(self.store, "review")
        service._future.result(timeout=5)
        delivery = service.poll()
        self.assertEqual(delivery[0], ticket)
        self.assertEqual([row.key for row in delivery[1]], ["notes"])
        self.assertIsNone(delivery[2])

    def test_async_cache_invalidation_after_edit_delete_undo_or_category_change(self):
        service = EventSearchService()
        self.addCleanup(service.close)
        for title in ("first", "edited", "restored"):
            self.store.events[0].title = title
            service.invalidate()
            service.request(self.store, title)
            service._future.result(timeout=5)
            self.assertEqual(service.poll()[1][0].key, "exact")
        self.store.events.pop(0)
        service.invalidate()
        service.request(self.store, "restored")
        service._future.result(timeout=5)
        self.assertEqual(service.poll()[1], ())

    def test_async_cancel_discards_already_completed_results(self):
        service = EventSearchService()
        self.addCleanup(service.close)
        service.request(self.store, "设计")
        service._future.result(timeout=5)
        service.cancel_pending()
        self.assertIsNone(service.poll())

    def test_search_never_mutates_or_saves_live_data(self):
        from unittest.mock import patch
        with patch.object(self.store, "save") as save:
            self.query("设计")
            capture_search_store(self.store)
            save.assert_not_called()

    def test_index_is_reused_until_revision_changes(self):
        from unittest.mock import patch
        service = EventSearchService()
        self.addCleanup(service.close)
        with patch.object(EventSearchIndex, "from_store", wraps=EventSearchIndex.from_store) as build:
            for query in ("设计", "review"):
                service.request(self.store, query)
                service._future.result(timeout=5)
            self.assertEqual(build.call_count, 1)
            service.invalidate()
            service.request(self.store, "art")
            service._future.result(timeout=5)
            self.assertEqual(build.call_count, 2)

    def test_worker_failure_is_delivered_not_hidden_in_future(self):
        from unittest.mock import patch
        service = EventSearchService()
        self.addCleanup(service.close)
        with patch.object(EventSearchIndex, "from_store", side_effect=ValueError("invalid date")):
            service.request(self.store, "query")
            service._future.result(timeout=5)
            delivery = service.poll()
        self.assertEqual(delivery[1], ())
        self.assertIn("invalid date", delivery[2])
