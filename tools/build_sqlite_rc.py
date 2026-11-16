"""Build a private numeric-version RC snapshot with the official build script.

Never changes tracked version.py, top-level release assets, or user data.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main():
    destination = ROOT/'build'/('v011-rc-'+uuid.uuid4().hex[:8])
    source = destination/'src'
    source.mkdir(parents=True)
    paths = subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().split('\0')
    paths += subprocess.check_output(['git','ls-files','--others','--exclude-standard','-z'],cwd=ROOT).decode().split('\0')
    for name in set(filter(None,paths)):
        item=ROOT/name
        if item.is_file():
            target=source/name
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(item,target)
    (source/'version.py').write_text('__version__ = "0.11.0"\n',encoding='utf8')
    metadata=dict(candidate='0.11.0-rc1',internal_version='0.11.0',source_head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),published=False)
    (destination/'candidate.json').write_text(json.dumps(metadata,indent=2),encoding='utf8')
    print(destination,flush=True)
    with (destination/'build.log').open('w',encoding='utf8') as log:
        result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(source/'build_release.ps1')],
            env={**os.environ,'CALENDAR_BUILD_PYTHON':sys.executable,'PYTHONIOENCODING':'utf-8',
                 'PSModulePath':str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/Modules')+os.pathsep+os.environ.get('PSModulePath','')},
            stdout=log,stderr=subprocess.STDOUT)
    print('build exit',result.returncode,'log',destination/'build.log',flush=True)
    sys.exit(result.returncode)


if __name__=='__main__':main()
