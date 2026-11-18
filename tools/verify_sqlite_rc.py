"""Read-only RC archive, frozen-module and manifest verification."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

def main():
    parser=argparse.ArgumentParser();parser.add_argument('candidate',type=Path);args=parser.parse_args()
    root=args.candidate;src=root/'src';app=src/'dist/DesktopCalendar';archive=src/'release/DesktopCalendar-win64.zip'
    actual=hashlib.sha256(archive.read_bytes()).hexdigest()
    assert actual==(archive.with_suffix('.zip.sha256')).read_text().split()[0]
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        names=z.namelist()
        assert not any('/tests/' in n or '__pycache__' in n or n.endswith(('.log','.png.diagnostic')) for n in names)
        for n in ('DesktopCalendar.exe','DesktopCalendarUpdater.exe','DesktopCalendarRepair.exe','storage-capabilities.json','app-version.txt'):
            assert 'DesktopCalendar/'+n in names,n
        capabilities=json.loads(z.read('DesktopCalendar/storage-capabilities.json'))
        assert capabilities['application_version']=='0.11.0' and capabilities['sqlite_schemas']==[1]
    from PyInstaller.archive.readers import CArchiveReader
    frozen=CArchiveReader(str(app/'DesktopCalendar.exe')).open_embedded_archive('PYZ.pyz')
    required=['calendar_storage_lifecycle','desktop_calendar.storage.sqlite_calendar_repository','desktop_calendar.storage.sqlite_calendar_schema','calendar_migration','calendar_migration_validation','calendar_legacy_migration','calendar_sqlite_backup','calendar_disaster_recovery','desktop_storage_startup']
    # Historical v0.11.0 RCs used top-level repository module names.
    missing = [n for n in required if n not in frozen.toc and n.removeprefix('desktop_calendar.storage.') not in frozen.toc]
    assert not missing, missing
    repository=Path(__file__).resolve().parents[1]
    compared=[]
    for staged in src.glob('*.py'):
        current=repository/staged.name
        if staged.name!='version.py' and current.exists():
            assert staged.read_bytes()==current.read_bytes(),('RC runtime source differs',staged.name)
            compared.append(staged.name)
    import pefile
    pe=pefile.PE(str(app/'DesktopCalendar.exe'))
    manifests=[]
    for entry in pe.DIRECTORY_ENTRY_RESOURCE.entries:
        if entry.id==24:
            for resource in entry.directory.entries:
                for lang in resource.directory.entries:
                    data=lang.data.struct
                    manifests.append(pe.get_data(data.OffsetToData,data.Size).decode('utf8'))
    assert any('PerMonitorV2, PerMonitor' in m for m in manifests)
    result=dict(candidate='0.11.0-rc1',internal_version='0.11.0',zip=str(archive.resolve()),sha256=actual,size=archive.stat().st_size,entries=len(names),modules=required,runtime_source_files_match=len(compared),manifest='PerMonitorV2, PerMonitor',capabilities=capabilities)
    (root/'verification.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result),flush=True)

if __name__=='__main__':main()
