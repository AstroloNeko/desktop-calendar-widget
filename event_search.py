"""Local event search and command matching, with no Tk dependency."""

import re
import queue
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from dataclasses import dataclass, replace
from datetime import date
from typing import Optional

from calendar_core import Store


def normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def parse_search_date(value: str) -> Optional[date]:
    text = re.sub(r"^(?:跳转到?|日期|date|goto)\s*", "", value.strip(), flags=re.IGNORECASE)
    match = re.fullmatch(r"(\d{4})(?:[-/]|年)\s*(\d{1,2})(?:[-/]|月)\s*(\d{1,2})日?", text)
    if match is None:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


@dataclass(frozen=True)
class SearchDocument:
    event_id: str
    title: str
    notes: str
    category: str
    color: str
    start: date
    end: date
    deadline: Optional[date]
    completed: bool

    @property
    def metadata(self) -> str:
        span = f"{self.start:%Y-%m-%d}"
        if self.end != self.start:
            span += f" → {self.end:%Y-%m-%d}"
        if self.deadline:
            span += f" · DDL {self.deadline:%Y-%m-%d}"
        return f"{self.category} · {span} · {'已完成' if self.completed else '未完成'}"


@dataclass(frozen=True)
class SearchResult:
    kind: str
    key: str
    title: str
    detail: str
    document: Optional[SearchDocument] = None
    target_date: Optional[date] = None


COMMANDS = (
    ("new", "新建事项", "新增 创建 new add"),
    ("today", "跳转到今天", "今天 今日 today"),
    ("date", "跳转到指定日期", "日期 跳转 date goto"),
    ("ddl", "查看全部 DDL", "截止 deadline ddl"),
    ("flow", "打开月度排期", "月历 flow calendar"),
    ("timeline", "打开任务时间轴", "时间轴 timeline"),
    ("categories", "管理事项分类", "分类 category categories"),
)


def capture_search_store(store: Store) -> Store:
    """Detached read-only snapshot for workers; never reads/writes a data file.

    Reuse Store's date/color methods instead of inventing search-specific
    calendar rules. Mutable event/category objects are cloned before dispatch.
    """
    snapshot = copy(store)
    snapshot.events = [replace(event) for event in store.events]
    snapshot.categories = [replace(category) for category in store.categories]
    snapshot.date_states = dict(store.date_states)
    return snapshot


class EventSearchIndex:
    def __init__(self, documents: tuple[SearchDocument, ...]):
        self.documents = documents
        self._text = tuple((doc, normalized(doc.title), normalized(doc.notes + " " + doc.category)) for doc in documents)

    @classmethod
    def from_store(cls, store: Store) -> "EventSearchIndex":
        documents = []
        for event in store.events:
            days = store.event_dates(event)
            category = store.category_by_id(event.category_id)
            documents.append(SearchDocument(
                event.id, event.title, event.notes, category.name if category else "无分类",
                category.color if category else store.effective_event_color(event),
                days[0], days[-1], days[-1] if store.event_has_deadline(event) else None, event.done,
            ))
        return cls(tuple(documents))

    def query(self, text: str, *, limit: int = 100) -> tuple[SearchResult, ...]:
        query = normalized(text)
        tokens = query.split()
        ranked = []
        for doc, title, other in self._text:
            if query and not all(token in title + " " + other for token in tokens):
                continue
            rank = 0 if query and title == query else 1 if query and title.startswith(query) else 2 if query and query in title else 3
            ranked.append((rank, doc.start, doc.event_id, doc))
        ranked.sort(key=lambda row: row[:3])
        events = [SearchResult("event", doc.event_id, doc.title, doc.metadata, document=doc) for *_, doc in ranked[:limit]]
        commands = []
        target_date = parse_search_date(text)
        if target_date:
            commands.append(SearchResult("date", "goto", f"跳转到 {target_date:%Y-%m-%d}", "日期导航", target_date=target_date))
        for key, title, aliases in COMMANDS:
            if not query or all(token in normalized(title + " " + aliases) for token in tokens):
                commands.append(SearchResult("command", key, title, "快捷命令" if key != "date" else "输入 YYYY-MM-DD / YYYY/MM/DD / YYYY年M月D日"))
        # Event match relevance is not displaced by an alias that happens to
        # match a title. Empty input presents the small command catalog first.
        return tuple(commands + events if not query or target_date else events + commands)


class EventSearchService:
    """One worker, immutable snapshots, latest-request-only delivery.

    No worker calls Tk. The UI polls a queue. Queued stale requests are cancelled
    and their completed results are ignored by ticket, including after close.
    """

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="calendar-search")
        self._revision = 0
        self._ticket = 0
        self._snapshot = None
        self._index_revision = -1
        self._index = None
        self._future = None
        self._results = queue.SimpleQueue()

    def invalidate(self):
        self._revision += 1
        self._snapshot = None
        self.cancel_pending()

    def cancel_pending(self):
        self._ticket += 1
        if self._future:
            self._future.cancel()

    def request(self, store: Store, text: str) -> int:
        self.cancel_pending()
        ticket, revision = self._ticket, self._revision
        if self._snapshot is None:
            self._snapshot = capture_search_store(store)
        snapshot = self._snapshot

        def search():
            try:
                if self._index_revision != revision:
                    self._index = EventSearchIndex.from_store(snapshot)
                    self._index_revision = revision
                result = self._index.query(text)
                self._results.put((ticket, result, None))
            except Exception as exc:
                # Deliver a visible failure instead of leaving a worker Future
                # exception hidden behind a permanently searching dialog.
                self._results.put((ticket, (), f"{type(exc).__name__}: {exc}"))

        self._future = self._executor.submit(search)
        return ticket

    def poll(self):
        latest = None
        while True:
            try:
                ticket, results, error = self._results.get_nowait()
            except queue.Empty:
                return latest
            if ticket == self._ticket:
                latest = (ticket, results, error)

    def close(self):
        self.cancel_pending()
        self._executor.shutdown(wait=False, cancel_futures=True)
