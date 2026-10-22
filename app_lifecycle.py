"""Tk-thread lifecycle coordination, independent of the native tray backend."""
from dataclasses import dataclass
import logging
import queue
import sys
import time
import tkinter as tk
from tkinter import messagebox

from data_compat import DataCompatibilityError
from view_mode import initial_global_geometry
from win_integration import raise_for_interaction


@dataclass
class LifecycleState:
    hidden: bool = False
    restoring: bool = False
    exiting: bool = False
    stopped: bool = False
    # Future policy hook; keep the existing application's hide-on-close behavior.
    close_hides: bool = True
    snapshot: object = None
    was_zoomed: bool = False
    view: str = "compact"
    last_error: str | None = None
    next_tray_probe: float = 0


def drain_commands(commands, limit=64):
    """Bound one event-loop turn and coalesce rapid duplicate shell requests."""
    result = []
    for _ in range(limit):
        try:
            command = commands.get_nowait()
        except queue.Empty:
            break
        if not result or command != result[-1]:
            result.append(command)
    return ["exit"] if "exit" in result else result


class ApplicationLifecycleMixin:
    def _init_lifecycle(self):
        self.lifecycle = LifecycleState()
        self._ui_actions = queue.Queue()
        self._tray_poll_job = None
        self._lifecycle_operation_depth = 0

    def _lifecycle_blocker(self):
        modal = self._active_modal_window()
        if modal is not None:
            return modal
        try:
            grab = self.grab_current()
        except (tk.TclError, KeyError):
            return self  # Native dialog with a Tcl-only grab: fail closed.
        if grab is not None:
            return grab
        if self.update_busy or self._lifecycle_operation_depth:
            return self.update_dialog or self
        overlays = self._active_overlays()
        return overlays[-1] if overlays else None

    def _focus_lifecycle_owner(self, owner):
        # A deliberate user action, not an after/lift loop. Never promote the
        # parent over a grabbed editor or replace the current grab.
        if self._lifecycle_operation_depth:
            return  # Leave native file/backup dialog foreground ownership alone.
        if owner is not None and owner.winfo_exists():
            owner.lift()
            owner.focus_set()
            raise_for_interaction(owner)

    def _on_window_close(self):
        if self.lifecycle.close_hides:
            return self.hide_to_tray()
        return self.on_close()

    def hide_to_tray(self):
        state = self.lifecycle
        if state.exiting or state.stopped:
            return False
        if state.hidden:
            return True
        blocker = self._lifecycle_blocker()
        if blocker is not None:
            self._focus_lifecycle_owner(blocker)
            return False
        if not self.tray_icon or not self.tray_icon.is_available:
            self._start_tray_icon()
        if not self.tray_icon or not self.tray_icon.probe_available():
            self._owned_data_dialog(lambda: messagebox.showwarning(
                "桌面月历", "系统通知区域图标暂时不可用，月历没有隐藏。", parent=self))
            return False
        self._cancel_schedule_drag()
        if self._global_window_move is not None:
            self._global_window_move.close()
            self._global_window_move = None
        state.view = self.view_mode
        state.was_zoomed = self.state() == "zoomed"
        state.snapshot = (self._global_normal_geometry if state.was_zoomed
                          else self._capture_window_geometry())
        for name in ("_lower_job", "_geometry_save_job", "_dpi_check_job", "_startup_foreground_restore_job"):
            job = getattr(self, name, None)
            if job:
                self.after_cancel(job)
                setattr(self, name, None)
        self._startup_foreground_active = False
        self.desktop_session_active = False
        # No Store.save, view rebuild or command-history mutation here.
        self.withdraw()
        state.hidden = True
        state.next_tray_probe = time.monotonic() + 2
        # Close a race with a failed/restarted Explorer during withdraw.
        if not self.tray_icon.is_available:
            self.restore_from_tray()
            return False
        return True

    def restore_from_tray(self, *, global_view=False):
        state = self.lifecycle
        if state.exiting or state.stopped or state.restoring:
            return False
        blocker = self._lifecycle_blocker()
        if blocker is not None:
            self._focus_lifecycle_owner(blocker)
            return False
        state.restoring = True
        was_hidden = state.hidden
        try:
            self.deiconify()
            # Reuse idempotent existing style setup; no new HWND/Toplevel.
            self.apply_window_mode()
            if (was_hidden and state.snapshot is not None and not state.was_zoomed
                    and self._capture_window_geometry() != state.snapshot):
                self._apply_saved_geometry(state.snapshot)
            if was_hidden and state.was_zoomed and self.view_mode == "global":
                self.state("zoomed")
            state.hidden = False
            if global_view and self.view_mode != "global":
                self.enter_global_view()
            self._activate_desktop_session()
            self.focus_set()
            raise_for_interaction(self)
            self.apply_window_mode()  # Restore the saved pinned/normal band last.
            return True
        except (tk.TclError, OSError, ValueError) as exc:
            state.last_error = str(exc)
            logging.getLogger(__name__).exception("Tray window restore failed; using safe geometry")
            # Recovery does not write this default into user settings.
            try:
                self.deiconify()
                self.state("normal")
                if self.view_mode == "compact":
                    self._set_initial_geometry()
                else:
                    self._apply_saved_geometry(initial_global_geometry(self.dpi.work_area(), self.dpi.dpi))
                self._activate_desktop_session()
                raise_for_interaction(self)
                self.apply_window_mode()
                state.hidden = False
                return True
            except (tk.TclError, OSError, ValueError):
                # Keep the tray service alive so a later user request can retry.
                logging.getLogger(__name__).exception("Safe window restore failed")
                return False
        finally:
            state.restoring = False

    def _handle_tray_action(self, action):
        if self.lifecycle.exiting or self.lifecycle.stopped:
            return
        if action == "exit":
            self.on_close()
        elif action == "hide":
            self.hide_to_tray()
        elif action in ("show", "global"):
            self.restore_from_tray(global_view=action == "global")
        elif action == "tray_unavailable" and self.lifecycle.hidden:
            self.restore_from_tray()

    def _poll_tray_actions(self):
        self._tray_poll_job = None
        if self.lifecycle.exiting or self.lifecycle.stopped:
            return
        try:
            for action in drain_commands(self.tray_actions):
                self._handle_tray_action(action)
                if self.lifecycle.exiting or self.lifecycle.stopped:
                    return
            for _ in range(64):
                try:
                    callback = self._ui_actions.get_nowait()
                except queue.Empty:
                    break
                try:
                    callback()
                except Exception:
                    self.report_callback_exception(*sys.exc_info())
                if self.lifecycle.exiting or self.lifecycle.stopped:
                    return
            if self.lifecycle.hidden:
                available = bool(self.tray_icon and self.tray_icon.is_available)
                if available and time.monotonic() >= self.lifecycle.next_tray_probe:
                    self.lifecycle.next_tray_probe = time.monotonic() + 2
                    available = self.tray_icon.probe_available()
                if not available:
                    self.restore_from_tray()
        finally:
            if not self.lifecycle.exiting and not self.lifecycle.stopped:
                self._tray_poll_job = self.after(250, self._poll_tray_actions)

    def _call_main(self, callback):
        # Safe even when called by update/download workers: never call Tk here.
        if not self.lifecycle.exiting and not self.lifecycle.stopped:
            self._ui_actions.put(callback)

    def _cancel_lifecycle_callbacks(self):
        def descendants(widget):
            return [widget] + [child for item in widget.winfo_children() for child in descendants(item)]
        owners = {command: widget for widget in descendants(self) for command in (widget._tclCommands or [])}
        for job in self.tk.call("after", "info"):
            command = str(self.tk.call("after", "info", job)[0]).split()[0]
            try:
                owners.get(command, self).after_cancel(job)
            except tk.TclError:
                pass  # A callback may already have been cancelled by its widget.

    def on_close(self):
        state = self.lifecycle
        if state.exiting or state.stopped:
            return False
        handoff = getattr(self, "_update_handoff", False)
        # Search is cancellable; editors/native data dialogs must finish first.
        if self.search_window and self.search_window.winfo_exists() and not handoff:
            self.search_window.close(restore_focus=False)
        if not handoff:
            blocker = self._lifecycle_blocker()
            if blocker is not None:
                self._focus_lifecycle_owner(blocker)
                return False
        state.exiting = True  # Guard reentry from native dialogs during save.
        try:
            if not handoff:
                # A hidden/iconic geometry is not a new layout; preserve the last
                # valid Compact/Global geometry while still flushing settings.
                self._save_window_settings()
        except DataCompatibilityError as exc:
            if state.hidden:
                self.deiconify()
                state.hidden = False
            self._owned_data_dialog(lambda: messagebox.showerror(
                "桌面月历", f"退出时已拒绝不兼容的数据写入。\n\n{exc}", parent=self))
        except (OSError, tk.TclError) as exc:
            state.exiting = False
            self.restore_from_tray()
            self._owned_data_dialog(lambda: messagebox.showerror(
                "桌面月历", f"保存未完成，月历保持运行：\n{exc}", parent=self))
            return False
        if self._global_window_move is not None:
            self._global_window_move.close()
            self._global_window_move = None
        self._clear_feedback()
        self._cancel_schedule_drag()
        if self.search_window and self.search_window.winfo_exists():
            self.search_window.close(restore_focus=False)
        if self.search_service is not None:
            self.search_service.close()
        if self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None
        self._cancel_lifecycle_callbacks()
        # Ignore any late worker/shell callbacks before releasing the instance.
        state.stopped = True
        while not self._ui_actions.empty():
            self._ui_actions.get_nowait()
        if self.instance_guard:
            self.instance_guard.close()
        self.destroy()
        return True
