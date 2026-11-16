"""Synthetic, isolated activation/backup/restore timing, no real user data."""
import json
from pathlib import Path
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.check_calendar_migration import sample
from calendar_storage_lifecycle import StorageLifecycle

if __name__=='__main__':
    for count in (100,1000,10000):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'calendar_data.json').write_text(json.dumps(sample(count)),encoding='utf8')
            stamps={};start=time.perf_counter()
            manager=StorageLifecycle(root,isolated=True,checkpoint=lambda name:stamps.update({name:time.perf_counter()-start}))
            state=manager.migrate();activated=time.perf_counter()
            manager.backup(root/'snapshot.dcalbackup');backed=time.perf_counter()
            manager.restore(root/'snapshot.dcalbackup',authorize_replacement=True);restored=time.perf_counter()
            with manager.runtime() as runtime:
                assert len(runtime.snapshot()['events'])==count
                opened=time.perf_counter()
            report=json.loads((root/state['report']/'migration-report.json').read_text())
            print(json.dumps(dict(events=count,migration_seconds=activated-start,backup_seconds=backed-activated,
                restore_seconds=restored-backed,runtime_open_seconds=opened-restored,
                validation_import_compare_ms=report['timings_ms'],source_unchanged=report['source_unchanged'])),flush=True)
