"""Real Windows drag A/B test. Uses temporary data and a solid QA backdrop.

Run with --diagnose before fixing: compare Compact, old Global, no native
frame, no DWM NC rendering, and native system move. PNG/JSON go to build/drag-qa.
Input timing and event pumping here are test machinery, not production fixes.
"""
import argparse
import ctypes
import faulthandler
from ctypes import wintypes as wt
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
import traceback
import tkinter as tk
import zlib
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CalendarApp
from calendar_core import Store
from tools.check_interactions import cleanup, pump
from global_header import header_layout
from dpi_utils import scaled_geometry
from win_integration import window_handle


BACKGROUND = (49, 83, 101)
OUTPUT = Path(__file__).resolve().parents[1] / "build" / "drag-qa"
if getattr(sys, "frozen", False):
    OUTPUT = Path(sys.executable).parent / "qa_results"
    OUTPUT.mkdir(parents=True, exist_ok=True)
    sys.stdout = (OUTPUT / "smoke.log").open("w", encoding="utf-8", buffering=1)
    sys.stderr = sys.stdout
U = ctypes.windll.user32
G = ctypes.windll.gdi32


def native_signatures():
    declarations = {
        "GetWindowRect": ([wt.HWND, ctypes.POINTER(wt.RECT)], wt.BOOL),
        "GetClientRect": ([wt.HWND, ctypes.POINTER(wt.RECT)], wt.BOOL),
        "ClientToScreen": ([wt.HWND, ctypes.POINTER(wt.POINT)], wt.BOOL),
        "WindowFromPoint": ([wt.POINT], wt.HWND),
        "GetWindowLongPtrW": ([wt.HWND, ctypes.c_int], ctypes.c_ssize_t),
        "SetWindowLongPtrW": ([wt.HWND, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t),
        "SetWindowPos": ([wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT], wt.BOOL),
        "ReleaseCapture": ([], wt.BOOL),
        "SendMessageW": ([wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t], ctypes.c_ssize_t),
        "PostMessageW": ([wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t], wt.BOOL),
        "SetCursorPos": ([ctypes.c_int, ctypes.c_int], wt.BOOL),
        "GetCursorPos": ([ctypes.POINTER(wt.POINT)], wt.BOOL),
        "mouse_event": ([wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t], None),
        "GetDC": ([wt.HWND], wt.HDC),
        "ReleaseDC": ([wt.HWND, wt.HDC], ctypes.c_int),
    }
    for name, (args, result) in declarations.items():
        function = getattr(U, name)
        function.argtypes, function.restype = args, result
    for name, args, result in (
        ("CreateCompatibleDC", [wt.HDC], wt.HDC),
        ("CreateCompatibleBitmap", [wt.HDC, ctypes.c_int, ctypes.c_int], wt.HBITMAP),
        ("SelectObject", [wt.HDC, wt.HANDLE], wt.HANDLE),
        ("BitBlt", [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD], wt.BOOL),
        ("GetDIBits", [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT], ctypes.c_int),
        ("DeleteObject", [wt.HANDLE], wt.BOOL),
        ("DeleteDC", [wt.HDC], wt.BOOL),
    ):
        function = getattr(G, name)
        function.argtypes, function.restype = args, result


def rect(hwnd, client=False):
    result = wt.RECT()
    (U.GetClientRect if client else U.GetWindowRect)(hwnd, ctypes.byref(result))
    return (result.left, result.top, result.right, result.bottom)


def screenshot(area):
    x, y, right, bottom = area
    width, height = right-x, bottom-y
    screen = U.GetDC(None)
    memory = G.CreateCompatibleDC(screen)
    bitmap = G.CreateCompatibleBitmap(screen, width, height)
    previous = G.SelectObject(memory, bitmap)
    try:
        assert G.BitBlt(memory, 0, 0, width, height, screen, x, y, 0x40CC0020)
        G.SelectObject(memory, previous)
        info = ctypes.create_string_buffer(struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0))
        data = ctypes.create_string_buffer(width*height*4)
        assert G.GetDIBits(memory, bitmap, 0, height, data, info, 0) == height
        return data.raw
    finally:
        G.DeleteObject(bitmap)
        G.DeleteDC(memory)
        U.ReleaseDC(None, screen)


def save_png(path, area, bgra):
    width, height = area[2]-area[0], area[3]-area[1]
    rgb = bytearray(width*height*3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    scanlines = b"".join(b"\0" + rgb[y*width*3:(y+1)*width*3] for y in range(height))
    def chunk(name, payload):
        return struct.pack(">I", len(payload)) + name + payload + struct.pack(">I", zlib.crc32(name+payload))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(scanlines)) + chunk(b"IEND", b""))


def background_nonuniform_samples(area, pixels, current, margin=220):
    # Exclude the current window and legitimate shadow. During movement this
    # also counts OS Snap UI / compositor latency, NOT necessarily ghosting.
    # Inspect client black pixels and settled screenshots independently.
    width = area[2]-area[0]
    expected = bytes((BACKGROUND[2], BACKGROUND[1], BACKGROUND[0]))
    count = 0
    for y in range(20, area[3]-area[1]-20, 3):
        for x in range(20, width-20, 3):
            if current[0]-margin <= x+area[0] <= current[2]+margin and current[1]-margin <= y+area[1] <= current[3]+margin:
                continue
            offset = (y*width+x)*4
            if pixels[offset:offset+3] != expected:
                count += 1
    return count


def black_client_samples(area, pixels, hwnd):
    origin = wt.POINT()
    U.ClientToScreen(hwnd, ctypes.byref(origin))
    client = rect(hwnd, True)
    width = area[2]-area[0]
    count = 0
    for y in range(max(0, origin.y-area[1]+12), min(area[3]-area[1], origin.y-area[1]+client[3]-12), 3):
        for x in range(max(0, origin.x-area[0]+12), min(width, origin.x-area[0]+client[2]-12), 3):
            offset = (y*width+x)*4
            if pixels[offset:offset+3] == b"\0\0\0":
                count += 1
    return count


def native_move(widget):
    hwnd = window_handle(widget)
    U.ReleaseCapture()
    U.PostMessageW(hwnd, 0x0112, 0xF012, 0)  # Async: never re-enter Tk via synchronous SendMessage.


def drag_header(app, dx=60, dy=30):
    """Real native input: must not fake Motion events for a system move loop."""
    native_signatures()
    header = app.global_header
    bounds = header_layout(app.dpi.logical(header.winfo_width()))["drag"]
    start = (header.winfo_rootx()+app.dpi.px(bounds[0]+bounds[2]//2),
             header.winfo_rooty()+app.dpi.px(12))
    hwnd = window_handle(app)
    errors = []
    def input_worker():
        pressed = False
        try:
            assert U.SetCursorPos(*start), "Native cursor positioning failed; interactive desktop required"
            assert U.GetAncestor(U.WindowFromPoint(wt.POINT(*start)), 2) == hwnd
            U.mouse_event(2, 0, 0, 0, 0)
            pressed = True
            deadline = time.monotonic()+2
            while not (app._global_window_move and app._global_window_move.started):
                if time.monotonic() >= deadline:
                    raise AssertionError("Native drag press was not delivered")
                threading.Event().wait(.005)
            threading.Event().wait(.1)
            assert U.SetCursorPos(start[0]+dx, start[1]+dy), "Native drag positioning failed"
            threading.Event().wait(.1)
        except BaseException as error:
            errors.append(error)
        finally:
            if pressed:
                U.mouse_event(4, 0, 0, 0, 0)
    worker = threading.Thread(target=input_worker, daemon=True)
    worker.start()
    while worker.is_alive():
        pump(app)
    worker.join()
    pump(app, .2)
    assert not errors, errors
    assert app._global_window_move is None
    assert app.grab_current() is None and app.editor_window is None and app.search_window is None


def gesture(app, area, start, name, *, native=False):
    faulthandler.dump_traceback_later(20, exit=True)
    hwnd = window_handle(app)
    positions = [(start[0]+dx, start[1]+dy) for dx, dy in
                 ((0, 0), (180, 0), (180, 100), (0, 100), (-80, 100), (-80, 0), (0, -50), (0, 0))]
    positions += [(start[0]+(200 if i % 2 == 0 else -70), start[1]+(60 if i % 2 == 0 else 0)) for i in range(40)]
    positions += [(start[0], start[1])]
    samples = []
    error = []
    def input_worker():
        try:
            assert U.SetCursorPos(*start), "Native cursor positioning failed; interactive desktop required"
            hit = U.WindowFromPoint(wt.POINT(*start))
            assert U.GetAncestor(hit, 2) == hwnd, ("drag point covered", start, hwnd, hit)
            U.mouse_event(2, 0, 0, 0, 0)
            deadline = time.monotonic()+2
            while time.monotonic() < deadline:
                session = app._global_window_move
                if native or (session and session.started) or app._drag_origin:
                    break
                threading.Event().wait(.01)
            else:
                raise AssertionError("Drag press did not start a move")
            for index, point in enumerate(positions):
                threading.Event().wait(.025)
                assert U.SetCursorPos(*point), "Native cursor positioning failed during drag"
                if index in (5, 15, 47):
                    pixels = screenshot(area)
                    current = rect(hwnd)
                    session = app._global_window_move
                    samples.append({"step": index, "background_nonuniform": background_nonuniform_samples(area, pixels, current),
                                    "rect": current, "client": rect(hwnd, True),
                                    "black_client": black_client_samples(area, pixels, hwnd),
                                    "system_move": bool(session and session.started)})
                    save_png(OUTPUT / f"{name}-{index}.png", area, pixels)
        except BaseException as exc:
            error.append(repr(exc))
        finally:
            U.mouse_event(4, 0, 0, 0, 0)
    header = app.global_header if app.view_mode == "global" else app.header
    binding = None
    if native:
        # Isolated A/B override; no source or task data mutation.
        app._drag_origin = None
        header.unbind("<B1-Motion>")
        header.unbind("<ButtonRelease-1>")
        binding = header.bind("<ButtonPress-1>", lambda _e: native_move(app))
    thread = threading.Thread(target=input_worker, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while thread.is_alive() and time.monotonic() < deadline:
        pump(app, .02)
    thread.join(1)
    assert not thread.is_alive() and not error, error
    pump(app, .25)
    if app.view_mode == "global" and not native and not getattr(app, "_drag_origin", None):
        session = app._global_window_move
        assert session is None, ("Move-end notification did not release the session", session.started, session.ended, session.hook)
    if binding:
        header.unbind("<ButtonPress-1>", binding)
    current = rect(hwnd)
    pixels = screenshot(area)
    save_png(OUTPUT / f"{name}-released.png", area, pixels)
    faulthandler.cancel_dump_traceback_later()
    return {"name": name, "dpi": app.dpi.dpi, "override": bool(app.overrideredirect()),
            "style": hex(U.GetWindowLongPtrW(hwnd, -16)), "exstyle": hex(U.GetWindowLongPtrW(hwnd, -20)),
            "samples": samples, "released_background_nonuniform": background_nonuniform_samples(area, pixels, current),
            "released_black_client": black_client_samples(area, pixels, hwnd)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--native-only", action="store_true")
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--system-only", action="store_true")
    parser.add_argument("--dpi-matrix", action="store_true")
    args = parser.parse_args()
    faulthandler.dump_traceback_later(30, exit=True)
    print("Initializing native drag QA", flush=True)
    native_signatures()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    backdrop = tk.Tk()
    backdrop.overrideredirect(True)
    backdrop.configure(bg="#315365")
    backdrop.attributes("-topmost", True)
    print("Backdrop ready", flush=True)
    results = []
    with tempfile.TemporaryDirectory() as tmp, patch.object(CalendarApp, "_start_tray_icon"), patch.object(CalendarApp, "check_reminders"):
        app = CalendarApp(Store(Path(tmp) / "data.json"))
        errors = []
        def report(*parts):
            text = "".join(traceback.format_exception(*parts))
            errors.append(text)
            print(text, flush=True)
        app.report_callback_exception = report
        print("App ready", flush=True)
        try:
            pump(app, .6)  # Finish the existing one-shot startup foreground pulse.
            app.window_mode = "pinned"
            work = app.dpi.work_area()
            area = (work.left, work.top, work.right, work.bottom)
            backdrop.geometry(f"{work.width}x{work.height}+{work.left}+{work.top}")
            backdrop.update()
            cases = [("compact", "modern", False, False, False)]
            cases += [("global", theme, False, False, False) for theme in ("modern", "aero", "paper", "frutiger")]
            if args.diagnose:
                cases += [("global", "modern", True, False, False), ("global", "modern", False, True, False),
                          ("global", "modern", False, False, True)]
            if args.native_only:
                cases = [("global", "modern", False, False, True), ("global", "modern", False, True, True)]
            if args.system_only:
                cases = [("global", "modern", False, False, False)]
            if args.dpi_matrix:
                cases = []
            for mode, theme, no_frame, no_dwm, native in cases:
                if mode == "global":
                    app.enter_global_view()
                    app.geometry(f"{app.dpi.px(720)}x{app.dpi.px(520)}+160+100")
                app.set_theme(theme)
                if mode == "global" and args.legacy:
                    app.global_header.bind("<ButtonPress-1>", app._start_drag)
                    app.global_header.bind("<B1-Motion>", app._drag_window)
                    app.global_header.bind("<ButtonRelease-1>", app._end_drag)
                app.apply_window_mode()
                backdrop.lift()
                app.lift()
                app.geometry("+160+100")
                pump(app, .25)
                assert not errors, errors
                hwnd = window_handle(app)
                if no_frame:
                    U.SetWindowLongPtrW(hwnd, -16, U.GetWindowLongPtrW(hwnd, -16) & ~0x40000)
                    U.SetWindowLongPtrW(hwnd, -20, U.GetWindowLongPtrW(hwnd, -20) & ~0x300)
                    U.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x37)
                if no_dwm:
                    policy = ctypes.c_int(1)  # DWMNCRP_DISABLED
                    ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 2, ctypes.byref(policy), 4)
                pump(app)
                if mode == "global":
                    bounds = header_layout(app.dpi.logical(app.global_header.winfo_width()))["drag"]
                    start = (app.global_header.winfo_rootx()+app.dpi.px(bounds[0]+bounds[2]//2),
                             app.global_header.winfo_rooty()+app.dpi.px(12))
                else:
                    start = app.header.winfo_rootx()+app.dpi.px(170), app.header.winfo_rooty()+app.dpi.px(12)
                name = f"{mode}-{theme}" + ("-no-frame" if no_frame else "-no-dwm" if no_dwm else "-native" if native else "-legacy" if args.legacy or mode == "compact" else "-system")
                result = gesture(app, area, start, name, native=native)
                print(json.dumps(result), flush=True)
                results.append(result)
                if no_dwm:
                    policy = ctypes.c_int(0)
                    ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 2, ctypes.byref(policy), 4)
                if mode == "global":
                    app.return_to_compact_view()
                    pump(app)
            if args.dpi_matrix:
                for theme in ("modern", "aero", "paper", "frutiger"):
                    app.enter_global_view()
                    app.set_theme(theme)
                    for dpi in (96, 120, 144, 168, 192):
                        # Bound each case, not the whole 20-case DPI matrix.
                        # Otherwise normal progress can exhaust the startup timer.
                        faulthandler.dump_traceback_later(15, exit=True)
                        with patch.object(app.dpi, "current_window_dpi", return_value=dpi):
                            app._apply_dpi_change(dpi)
                            app.geometry(scaled_geometry(720, 520, 160, 100, dpi))
                            app.apply_window_mode()
                            app.lift()
                            pump(app, .25)
                            deadline = time.monotonic()+3
                            while (app._geometry_save_job or app._dpi_check_job) and time.monotonic() < deadline:
                                pump(app)
                            assert not app._geometry_save_job
                            before = rect(window_handle(app))
                            saves = []
                            original_save = app.store.save
                            def trace_save():
                                saves.append([frame.name for frame in traceback.extract_stack(limit=9)])
                                return original_save()
                            with patch.object(app.store, "save", side_effect=trace_save) as save:
                                drag_header(app, 60, 30)
                                after = rect(window_handle(app))
                                assert after[:2] == (before[0]+60, before[1]+30), (theme, dpi, before, after)
                                assert save.call_count == 1, ("move should persist only once", saves)
                            assert not errors, errors
                            print(theme, "simulated DPI", dpi, "native move/end/one save: passed", flush=True)
                        faulthandler.cancel_dump_traceback_later()
        finally:
            cleanup(app)
            backdrop.destroy()
    (OUTPUT / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
