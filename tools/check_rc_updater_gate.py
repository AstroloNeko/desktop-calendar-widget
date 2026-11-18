"""Actual frozen updater rejects an old payload; one owned error acknowledgment.

Never runs old/new calendar, never copies onto a real installation, never loops
clicks. Only one exact updater-owned error dialog may be acknowledged once.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.check_sqlite_rc_exe import windows,U
from tools.generate_schema8_fixture import generate
from desktop_calendar.storage.calendar_storage_lifecycle import StorageLifecycle


def main():
    parser=argparse.ArgumentParser();parser.add_argument('updater',type=Path);parser.add_argument('old',type=Path);parser.add_argument('report',type=Path);args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='calendar-updater-gate-') as folder:
        root=Path(folder);profile=root/'roaming/DesktopCalendar';profile.mkdir(parents=True)
        (profile/'calendar_data.json').write_text(json.dumps(generate()['document']),encoding='utf8')
        manager=StorageLifecycle(profile,isolated=True);manager.migrate()
        before=manager.state_path.read_bytes()
        install=root/'install';install.mkdir();sentinel=install/'DesktopCalendar.exe';sentinel.write_bytes(b'MZ isolated original sentinel')
        gone=subprocess.Popen([sys.executable,'-c','pass']);gone.wait()
        exe=args.updater.resolve()
        process=subprocess.Popen([str(exe),'--archive',str(args.old.resolve()),'--install-dir',str(install),'--pid',str(gone.pid),'--executable',sentinel.name],
            cwd=exe.parent,env={**os.environ,'APPDATA':str(profile.parent)})
        seen=set();pids={process.pid};error_text='';deadline=time.monotonic()+35
        try:
            while process.poll() is None and time.monotonic()<deadline:
                # Onefile bootloader owns a child process with the error dialog.
                if len(pids)==1:
                    command=f'Get-CimInstance Win32_Process | Where-Object {{ $_.ParentProcessId -eq {process.pid} }} | Select-Object -ExpandProperty ProcessId'
                    children=subprocess.check_output(['powershell.exe','-NoProfile','-Command',command],text=True).split()
                    pids.update(map(int,children))
                for pid in pids:
                    for hwnd,title,kind in windows(pid):
                        if title=='桌面月历更新失败' and kind=='#32770' and hwnd not in seen:
                            from tools.check_sqlite_rc_exe import CB
                            import ctypes
                            texts=[]
                            @CB
                            def collect(h,_):
                                text=ctypes.create_unicode_buffer(2048);U.GetWindowTextW(h,text,2048)
                                if text.value:texts.append(text.value)
                                return True
                            U.EnumChildWindows(hwnd,collect,0)
                            error_text='\n'.join(texts)
                            seen.add(hwnd)
                            U.PostMessageW(hwnd,0x111,1,0)
                time.sleep(.1)
            process.wait(2)
            assert process.returncode==1
            assert 'SQLite' in error_text and '兼容' in error_text,error_text
            assert manager.state_path.read_bytes()==before
            assert sentinel.read_bytes()==b'MZ isolated original sentinel'
            result=dict(frozen_updater=True,old_payload='rejected before copy/restart',active_state_unchanged=True,installation_unchanged=True,one_owned_error_acknowledgment=len(seen))
            args.report.write_text(json.dumps(result,indent=2),encoding='utf8');print(json.dumps(result),flush=True)
        finally:
            if process.poll() is None:process.terminate();process.wait(5)

if __name__=='__main__':main()
