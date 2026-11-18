"""Actual product/updater EXEs in a disposable APPDATA/install directory.

Uses native owned-dialog automation and tray messages, never a test runtime hook.
No model calls. Reports only the operations actually observed.
"""
import argparse
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from contextlib import closing

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.generate_schema8_fixture import generate
from desktop_calendar.assistant.llm_provider import AIConfiguration
from calendar_storage_lifecycle import StorageLifecycle

U=ctypes.WinDLL('user32')
CB=ctypes.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
U.EnumWindows.argtypes=[CB,w.LPARAM]
U.EnumChildWindows.argtypes=[w.HWND,CB,w.LPARAM]
U.GetWindowThreadProcessId.argtypes=[w.HWND,ctypes.POINTER(w.DWORD)]
U.GetWindowTextW.argtypes=[w.HWND,w.LPWSTR,ctypes.c_int]
U.GetClassNameW.argtypes=[w.HWND,w.LPWSTR,ctypes.c_int]
U.PostMessageW.argtypes=[w.HWND,w.UINT,ctypes.c_size_t,ctypes.c_ssize_t]
U.SendMessageW.argtypes=[w.HWND,w.UINT,ctypes.c_size_t,ctypes.c_void_p]
U.GetDlgItem.argtypes=[w.HWND,ctypes.c_int];U.GetDlgItem.restype=w.HWND
U.IsWindowVisible.argtypes=[w.HWND];U.IsWindowVisible.restype=w.BOOL


def dialog_action(title, texts):
    """Allowlist only known native migration messages; never shell dialogs."""
    content='\n'.join(texts)
    if '选择备份' in title or '找不到文件' in content:
        raise RuntimeError('Shell file selection requires manual acceptance; no automated retry.')
    if title=='桌面月历 · 存储升级' and '是否开始' in content:
        return 6
    if title=='桌面月历 · 隔离存储迁移' and '迁移与完整性验证完成' in content:
        return 1
    raise RuntimeError(('Unexpected native dialog; no automatic action',title,texts))

def windows(pid):
    result=[]
    @CB
    def collect(hwnd,_):
        owner=w.DWORD();U.GetWindowThreadProcessId(hwnd,ctypes.byref(owner))
        if owner.value==pid:
            title=ctypes.create_unicode_buffer(512);kind=ctypes.create_unicode_buffer(256)
            U.GetWindowTextW(hwnd,title,512);U.GetClassNameW(hwnd,kind,256)
            result.append((hwnd,title.value,kind.value))
        return True
    U.EnumWindows(collect,0)
    return result

def pump_dialogs(process, backup=None):
    handled=getattr(process,'_qa_handled_dialogs',None)
    if handled is None:
        handled=set();process._qa_handled_dialogs=handled
    for hwnd,title,kind in windows(process.pid):
        if kind!='#32770' or not U.IsWindowVisible(hwnd):continue
        texts=[]
        @CB
        def detail(handle,_):
            text=ctypes.create_unicode_buffer(2048);U.GetWindowTextW(handle,text,2048)
            if text.value:texts.append(text.value)
            return True
        U.EnumChildWindows(hwnd,detail,0)
        action=dialog_action(title,texts)
        identity=(hwnd,title,tuple(texts))
        if identity not in handled:
            # Record BEFORE posting: a busy dialog must not receive a flood of
            # queued actions. Reused HWNDs with different content are distinct.
            handled.add(identity)
            if not U.PostMessageW(hwnd,0x111,action,0):
                raise OSError('Native dialog action was not accepted')

def wait(process,predicate,backup=None,timeout=35):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        pump_dialogs(process,backup)
        if predicate():return
        if process.poll() is not None:raise AssertionError(('EXE exited',process.returncode))
        time.sleep(.1)
    raise AssertionError(('EXE timeout',windows(process.pid)))

def close(process):
    helpers=[h for h,_,c in windows(process.pid) if c.startswith('DesktopCalendarTray_')]
    if helpers:U.PostMessageW(helpers[0],0x111,1005,0)
    process.wait(15)
    assert process.returncode==0

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--rc',type=Path,required=True);parser.add_argument('--old',type=Path,required=True);parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args();result={}
    with tempfile.TemporaryDirectory(prefix='calendar-rc-exe-') as folder:
        root=Path(folder);install=root/'install';install.mkdir()
        with zipfile.ZipFile(args.old) as z:z.extractall(install)
        install=install/'DesktopCalendar'
        profile=root/'roaming/DesktopCalendar';profile.mkdir(parents=True)
        raw=generate()['document'];(profile/'calendar_data.json').write_text(json.dumps(raw),encoding='utf8')
        env={**os.environ,'APPDATA':str(profile.parent)}
        children=[]
        try:
            old=subprocess.Popen([str(install/'DesktopCalendar.exe')],env=env,cwd=install);children.append(old)
            wait(old,lambda:any(c.startswith('DesktopCalendarTray_') for _,_,c in windows(old.pid)))
            close(old)
            assert json.loads((profile/'calendar_data.json').read_text(encoding='utf8'))['version']==8
            result['old_exe_start_save_exit']='passed (tag source generated fixture, old EXE saved settings)'
            original=(profile/'calendar_data.json').read_bytes()
            rcdir=root/'candidate'
            with zipfile.ZipFile(args.rc) as z:z.extractall(rcdir)
            rcdir=rcdir/'DesktopCalendar'
            owned_archive=root/'owned-update.zip';shutil.copy2(args.rc,owned_archive)
            updater=subprocess.Popen([str(rcdir/'DesktopCalendarUpdater.exe'),'--archive',str(owned_archive),'--install-dir',str(install),'--pid',str(old.pid),'--executable','DesktopCalendar.exe'],env=env,cwd=rcdir);children.append(updater)
            # Only the private copy may be consumed by the updater.
            updater.wait(70);assert updater.returncode==0
            result['actual_updater_install']='passed'
            print('old EXE save/exit and real updater install passed',flush=True)
            # Updater starts JSON RC. Locate only the isolated installed EXE via
            # Windows child process inventory, not any real user's process.
            import subprocess as sp
            command=f"Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -eq '{install / 'DesktopCalendar.exe'}' }} | Select-Object -ExpandProperty ProcessId"
            pid=int(sp.check_output(['powershell.exe','-NoProfile','-Command',command],text=True).strip())
            class Existing:
                def __init__(self,pid):self.pid=pid
                def poll(self):return None
                def wait(self,timeout=15):
                    end=time.monotonic()+timeout
                    while windows(self.pid) and time.monotonic()<end:time.sleep(.1)
                    assert not windows(self.pid);self.returncode=0
            launched=Existing(pid)
            wait(launched,lambda:any(c.startswith('DesktopCalendarTray_') for _,_,c in windows(pid)))
            close(launched)
            original=(profile/'calendar_data.json').read_bytes()
            config=AIConfiguration(profile/'ai-assistant')
            with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
            config.save({**config.load(),'http_enabled':True,'port':port})
            def healthy():
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/v1/health',timeout=.5) as r:return r.status==200
                except OSError:return False
            product=install/'DesktopCalendar.exe'
            child=subprocess.Popen([str(product),'--upgrade-storage'],env=env,cwd=install);children.append(child)
            wait(child,healthy)
            # Production path is only inspected through the explicitly isolated API.
            manager=StorageLifecycle(profile,isolated=True)
            state=manager._read();assert state['state']=='ACTIVE'
            assert (profile/state['protection']).read_bytes()==original
            assert (profile/'calendar_data.json').read_bytes()==original
            import sqlite3
            with closing(sqlite3.connect((profile/state['database']).as_uri()+'?mode=ro',uri=True)) as db:
                for table,key in (('events','events'),('routines','routines'),('categories','categories')):
                    assert {r[0] for r in db.execute('SELECT id FROM '+table)}=={r['id'] for r in raw[key]}
            result['migrated_counts']={key:len(raw[key]) for key in ('events','routines','categories')}
            result['packaged_migration']='ACTIVE; real dialogs auto-confirmed; health endpoint live'
            close(child)
            backup=root/'exe-data.dcalbackup';manager.backup(backup)
            result['backup']='Format 2 via lifecycle after packaged migration (not EXE backup UI)'
            child=subprocess.Popen([str(product)],env=env,cwd=install);children.append(child);wait(child,healthy);close(child)
            result['packaged_restart']='passed'
            result['packaged_offline_recovery']='not exercised by automation; use manual_sqlite_rc.py for manual acceptance'
        finally:
            for child in children:
                if child.poll() is None:
                    try:close(child)
                    except Exception:child.terminate();child.wait(5)
            args.report.write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result),flush=True)

if __name__=='__main__':main()
