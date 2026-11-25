"""Exercise the reminder center in real Windows Tk with isolated, fixed-date fixtures."""
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import CalendarApp
from calendar_core import Event, Store
from desktop_calendar.reminders.reminder_engine import STATE_KEY, plans_for_store
from dpi_utils import dpi_for_window, enable_dpi_awareness
from tools.check_habit_schedules import capture_own_window
from tools.check_interactions import cleanup, pump


FIXED_NOW = datetime(2031, 4, 16, 8, 30)
TABS = ("rules", "brief", "channels", "plans")


def seed(app):
    engine = app.reminder_engine
    features = {"enabled": True, "windows": True, "qq": True, "ddl_days": 0,
                "events": {"fixture-0": {"mode": "custom", "days": 0, "time": "08:00"}},
                "brief": {"enabled": True, "time": "08:00"}}
    engine.configure(features)
    entries = [
        ("项目组会", FIXED_NOW.replace(hour=9, minute=0), "general", True),
        ("准备项目材料", FIXED_NOW.replace(hour=14, minute=0), "general", True),
        ("整理实验记录", FIXED_NOW, "general", False),
        ("提交阶段报告", FIXED_NOW + timedelta(days=2), "ddl", False),
        ("交付数据说明", FIXED_NOW - timedelta(days=2), "ddl", False),
    ]
    entries.extend((f"后续日程 {i:02d}", FIXED_NOW + timedelta(days=1, minutes=i*30), "general", True)
                   for i in range(23))
    for index, (title, due, kind, timed) in enumerate(entries):
        app.event_commands.upsert(Event(id=f"fixture-{index}", title=title, due=due.isoformat(),
                                        has_time=timed, reminder=0, event_type=kind))
    plans = {plan.event_id: plan for plan in plans_for_store(app.store, engine.config())}
    state = engine._state()
    state["qq_cutover"] = {"scope": "fixture-scope", "at": (FIXED_NOW - timedelta(days=5)).isoformat()}
    for index, (windows, qq) in enumerate((("submitted", "failed"), ("submitted", "sent"))):
        plan = plans[f"fixture-{index}"]
        reason = "演示接口拒绝请求" if qq == "failed" else ""
        identifier = f"fixture-batch-{index}"
        state["deliveries"][plan.key] = {"batch": identifier}
        state["batches"][identifier] = {
            "kind": "event", "when": plan.when.isoformat(), "expires": (plan.when + timedelta(days=1)).isoformat(),
            "members": [[plan.event_id, plan.key]], "scope": "fixture-scope",
            "channels": {"windows": {"status": windows, "attempts": 1, "at": FIXED_NOW.isoformat()},
                         "qq": {"status": qq, "attempts": 1, "at": FIXED_NOW.isoformat(), "reason": reason}},
        }
    engine._save(STATE_KEY, state)
    snooze = plans["fixture-2"]
    engine.snooze(snooze.event_id, snooze.key, 30, FIXED_NOW)


def verify_layout(app, window):
    dp = app.dpi.px
    width = [control.winfo_width() for control in (window.clock_field, window.general_field, window.ddl_field)]
    assert width == [dp(108)] * 3, width
    assert all(dp(32) <= control.winfo_height() <= dp(36)
               for control in (window.clock_field, window.general_field, window.ddl_field))
    right = [control.winfo_rootx() + control.winfo_width()
             for control in (window.clock_field, window.general_field, window.ddl_field)]
    assert len(set(right)) == 1, right
    assert not hasattr(window, "cancel_button")
    assert window.general.get().endswith(" 天")


def shot(window, output, filename):
    pump(window.app, .22)
    capture_own_window(window, output / (filename + ".png"))


def run_case(app, theme, dpi, native, output):
    with patch.object(app.dpi, "current_window_dpi", return_value=dpi):
        app._apply_dpi_change(dpi)
        app.set_theme(theme)
        app.open_reminder_settings()
        window = app.notification_settings_window
        assert window is not None
        try:
            pump(app, .25)
            window.title("后台与提醒 · 验收夹具")
            verify_layout(app, window)
            size = window.winfo_width(), window.winfo_height()
            if dpi == native:
                shot(window, output, f"{theme}-rules")
            window.clock.set("07:40")
            window.tabs.select(1)
            pump(app, .19)
            assert window.preview_heading.cget("text").startswith("早上好")
            labels = [str(widget.cget("text")) for widget in window.preview_body.winfo_children()
                      if isinstance(widget, window.preview_heading.__class__)]
            assert any("近期" in label for label in labels) and any("逾期" in label for label in labels), labels
            saved = app.reminder_engine.config()["brief"]
            window.brief["enabled"].set(False)
            assert "未启用" in window.preview_date.cget("text")
            assert app.reminder_engine.config()["brief"] == saved
            window.brief["enabled"].set(True)
            window.brief["upcoming"].set(False)
            assert window.brief_days.instate(("disabled",))
            window.brief["upcoming"].set(True)
            if dpi == native:
                shot(window, output, f"{theme}-brief")
            window.tabs.select(2)
            pump(app, .12)
            assert "尚未绑定" in window.qq_account.get()
            assert window.qq_test_button.cget("state") == "disabled"
            assert window.test_button.winfo_width() < window.winfo_width() // 2
            if dpi == native:
                shot(window, output, f"{theme}-channels")
            verified = SimpleNamespace(account="demo-verified-account", scope="fixture-scope")
            worker = SimpleNamespace(snapshot=lambda: SimpleNamespace(connection="live"))
            with patch.object(app.qq_reminder_backend, "binding", return_value=verified), \
                    patch.object(app.qq_reminder_backend, "get_worker", return_value=worker):
                app.notification_backend.status = "Windows 通知可用"
                app._qq_test_capability = verified.scope, "主动推送接口已通过测试"
                window.refresh_channel_status()
                assert "demo-verified-account" in window.qq_account.get()
                assert "已通过" in window.qq_availability.get()
                assert "实时在线" in window.qq_connection.get()
                if dpi == native:
                    shot(window, output, f"{theme}-channels-verified-fixture")
                app.notification_backend.status = "Windows 通知受限（状态 2）"
                app._qq_test_capability = verified.scope, "主动推送接口未提供（HTTP 404）"
                window.refresh_channel_status()
                assert "受限" in window.windows_state.get()
                assert "404" in window.qq_availability.get()
                if dpi == native:
                    shot(window, output, f"{theme}-channels-error-fixture")
            app.notification_backend.status = "尚未启用"
            window.tabs.select(0)
            pump(app, .12)
            assert window.clock.get() == "07:40"
            window.show_plans()
            pump(app, .18)
            plan = window.plan
            assert len(plan.rows) == 28
            assert not plan.tree.selection() and not plan.selection_actions.winfo_ismapped()
            assert tuple(plan.tree["columns"]) == ("when", "windows", "qq")
            recorded = tuple(plan.tree.item("event:fixture-0", "values"))[-2:]
            assert recorded == ("已提交", "发送失败"), recorded
            assert plan.rows["event:fixture-4"].group == "attention"
            assert plan.counts["snoozed"].get() == "1"
            if dpi == native:
                shot(window, output, f"{theme}-plans-unselected")
            plan.tree.selection_set("event:fixture-0")
            plan.tree.see("event:fixture-0")
            pump(app, .1)
            assert "单事项规则" in plan.detail.get() and "演示接口拒绝请求" in plan.detail.get()
            assert plan.open_button.winfo_width() < window.winfo_width() // 3
            assert plan.cancel_button.cget("state") == "disabled"
            if dpi == native:
                shot(window, output, f"{theme}-plans")
            plan.filter_choice.set("需要处理")
            plan.refresh()
            assert len(plan.rows) == int(plan.counts["attention"].get())
            plan.filter_choice.set("全部计划")
            plan.refresh()
            min_width, min_height = window.minsize()
            window.geometry(f"{min_width}x{min_height}")
            pump(app, .25)
            assert all(plan.tree.column(key, "width") > 0 for key in ("#0", "when", "windows", "qq"))
            assert sum(plan.tree.column(key, "width") for key in ("#0", "when", "windows", "qq")) <= plan.tree.winfo_width()
            if dpi == native:
                shot(window, output, f"{theme}-plans-small")
            assert plan.tree.winfo_height() >= app.dpi.px(94), (dpi, min_width, min_height, plan.tree.winfo_height())
            window.tabs.select(1)
            pump(app, .18)
            assert window._brief_layout == (False, False)
            if dpi == native:
                shot(window, output, f"{theme}-brief-small")
            return {"theme": theme, "layout_dpi": dpi, "normal_pixels": size,
                    "small_pixels": [min_width, min_height], "items": 28,
                    "draft_preserved": True, "channel_fixture_states": "passed"}
        finally:
            window.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    enable_dpi_awareness()
    store = Store(output / f"isolated-ui-{uuid4().hex}.json")
    with patch.object(CalendarApp, "_schedule_calendar_reminders"):
        app = CalendarApp(store)
    try:
        app._reminder_preview_clock = lambda: FIXED_NOW
        pump(app, .22)
        native = dpi_for_window(app)
        seed(app)
        cases = [run_case(app, theme, dpi, native, output)
                 for dpi in dict.fromkeys((native, 96, 120, 144)) for theme in ("modern", "paper")]
        app.reminder_engine.configure({"enabled": False})
        app.open_reminder_plans()
        window = app.notification_settings_window
        pump(app, .2)
        assert not window.plan.rows and window.plan.empty_label.winfo_ismapped()
        shot(window, output, "empty-plan")
        window.close()
        report = {"physical_dpi": native, "cases": cases, "empty_plan": "passed",
                  "remote_fixture": "local injected observations, no network or real account"}
        (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
    finally:
        window = app.notification_settings_window
        if window and window.winfo_exists():
            window.close()
        cleanup(app)


if __name__ == "__main__":
    main()
