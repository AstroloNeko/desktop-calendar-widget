"""Opt-in, synthetic-only native notification gate; never opens Calendar Store.

One unique test shortcut/AUMID is created and removed. Default timeout 120s.
Run source or build this harness as a separate QA EXE; click the Windows toast
to establish activation. Output explicitly distinguishes acceptance and click.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import uuid
import winreg

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from native_notifications import WinRTTransport, toast_xml


def main():
    # No cold activation UI is supported by this gate. A late COM launch must
    # exit harmlessly, never open a calendar or launch another notification.
    if "--activation-only" in sys.argv:
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--hidden", action="store_true", help="Hide QA window only after its tray icon is available")
    args = parser.parse_args()
    nonce = uuid.uuid4().hex[:12]
    identity = "AstroloNeko.DesktopCalendar.NotificationQA." + nonce
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.SetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.c_wchar_p]
    shell.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
    shell.IsUserAnAdmin.argtypes = []
    shell.IsUserAnAdmin.restype = ctypes.c_int
    identity_result = shell.SetCurrentProcessExplicitAppUserModelID(identity)
    shortcut = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs" / f"桌面月历通知测试-{nonce}.lnk"
    registry_path = "Software\\Classes\\AppUserModelId\\" + identity
    report = {"identity": identity, "frozen": bool(getattr(sys, "frozen", False)),
              "accepted": False, "activated": False, "events": [], "errors": [],
              "process_identity_result": identity_result,
              "elevated": bool(shell.IsUserAnAdmin())}
    result_queue = queue.Queue()
    tray_queue = queue.Queue()
    stop = threading.Event()
    com_registry_path = None

    def worker():
        nonlocal com_registry_path
        transport = None
        try:
            dll = ROOT / ("DesktopCalendarNotifications.dll" if report["frozen"] else "build/native-notifications/DesktopCalendarNotifications.dll")
            report["stage"] = "load"
            transport = WinRTTransport(dll, identity)
            command = "" if report["frozen"] else f'"{Path(__file__).resolve()}"'
            report["stage"] = "shortcut"
            transport.shortcut(shortcut, Path(sys.executable), command, ROOT / "assets/calendar.ico", identity)
            report["stage"] = "identity"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, registry_path) as key:
                winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "桌面月历通知测试")
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(ROOT / "assets/calendar.ico"))
                winreg.SetValueEx(key, "CustomActivator", 0, winreg.REG_SZ, transport.activator)
            # A live class factory alone is not the complete unpackaged-app
            # registration contract. Register ONLY this random test CLSID.
            com_registry_path = "Software\\Classes\\CLSID\\" + transport.activator
            launch = f'"{sys.executable}" {command} --activation-only'
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, com_registry_path + "\\LocalServer32") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, launch)
            report["stage"] = "open"
            transport.open()
            report["stage"] = "setting"
            try:
                report["setting"] = transport.setting()
            except Exception as error:
                report["setting_error"] = str(error)
            report["stage"] = "show"
            transport.show(nonce, toast_xml("桌面月历通知测试", f"隔离测试 {nonce}：请点击此 Windows 通知验证回调。不读取用户日历。", "qa:" + nonce, ROOT / "assets/calendar.png"))
            report["accepted"] = True
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            deadline = time.monotonic() + args.timeout
            while not stop.wait(.1) and time.monotonic() < deadline:
                try:
                    report["history_count"] = transport.history_count()
                except Exception as error:
                    report["history_error"] = str(error)
                try:
                    kind, value = transport.events.get_nowait()
                except queue.Empty:
                    continue
                report["events"].append({"kind": kind, "value": value})
                result_queue.put((kind, value))
                if kind == 1 and value == "qa:" + nonce:
                    report["activated"] = True
                    break
        except Exception as error:
            report["errors"].append(str(error))
        finally:
            if transport:
                try:
                    transport.close()
                except Exception as error:
                    report["errors"].append(str(error))
            if shortcut.exists():
                shortcut.unlink()  # This run's unique shortcut only.
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_path)
            except FileNotFoundError:
                pass
            if com_registry_path:
                for key_path in (com_registry_path + "\\LocalServer32", com_registry_path):
                    try:
                        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
                    except FileNotFoundError:
                        pass
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            result_queue.put((0, "done"))

    import tkinter as tk
    root = tk.Tk()
    root.title("桌面月历 · 隔离原生通知验证")
    root.geometry("580x190")
    main_thread = threading.get_ident()
    status = tk.StringVar(value="即将发送一条合成测试通知。请点击 Windows 通知本身，不是本窗口。")
    tk.Label(root, textvariable=status, wraplength=540).pack(padx=15, pady=25)
    tk.Button(root, text="结束测试", command=stop.set).pack()
    thread = threading.Thread(target=worker, daemon=False)
    tray = None
    if args.hidden:
        from tray_icon import TrayIcon
        tray = TrayIcon("桌面月历 · 原生通知隔离测试", ROOT / "assets/calendar.ico", tray_queue.put)
        report["tray_available"] = bool(tray.start() and tray.probe_available())
        if report["tray_available"]:
            root.withdraw()
            report["hidden_before_show"] = True
        else:
            report["errors"].append("QA tray unavailable; window kept visible")

    def close():
        stop.set()
        if not thread.is_alive():
            root.destroy()

    def poll():
        try:
            while not tray_queue.empty():
                action = tray_queue.get_nowait()
                if action in ("show", "global"):
                    root.deiconify()
                    root.lift()
                elif action == "exit":
                    close()
            while True:
                kind, value = result_queue.get_nowait()
                if kind == 0:
                    if report["activated"]:
                        report["restored_after_click"] = root.state() == "normal"
                        root.after(1500, root.destroy)
                    else:
                        root.destroy()
                    return
                if kind == 1:
                    root.deiconify()
                    root.lift()
                    root.focus_set()
                    report["callback_consumed_on_tk_thread"] = threading.get_ident() == main_thread
                status.set(f"收到通知事件 {kind}: {value}")
        except queue.Empty:
            pass
        root.after(100, poll)
    root.protocol("WM_DELETE_WINDOW", close)
    root.after(100, thread.start)
    root.after(200, poll)
    root.mainloop()
    stop.set()
    thread.join(5)
    if tray:
        tray.stop()
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["activated"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
