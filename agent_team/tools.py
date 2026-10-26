import hashlib
import os
from pathlib import Path
import subprocess
import sys
from .schemas import TestReport

class Tools:
    """Application permissions, NOT an OS sandbox. Use isolated containers for untrusted tests."""
    def __init__(self,root,role):
        self.root=Path(root).resolve()
        self.role=role
    def path(self,name):
        p=Path(name)
        if p.is_absolute() or '..' in p.parts or ':' in name or '\\' in name:
            raise PermissionError('Unsafe relative path')
        if not p.parts or any(x.startswith('.') or x=='__pycache__' for x in p.parts):
            raise PermissionError('Hidden/control paths are excluded')
        result=(self.root/p).resolve()
        if not result.is_relative_to(self.root): raise PermissionError('Path escapes workspace')
        return result
    def snapshot(self):
        files={}
        for p in sorted(self.root.rglob('*')):
            if p.is_file() and p.suffix in {'.py','.md','.txt','.json','.toml'}:
                rel=p.relative_to(self.root).as_posix()
                try: safe=self.path(rel)
                except PermissionError: continue
                if safe.stat().st_size>100_000: raise ValueError('File too large for context')
                files[rel]=safe.read_text(encoding='utf-8')
                if sum(len(v) for v in files.values())>500_000: raise ValueError('Context exceeds MVP limit')
        return files
    def digest(self):
        h=hashlib.sha256()
        for p in sorted(self.root.rglob('*')):
            if p.is_file():
                rel=p.relative_to(self.root).as_posix()
                if '__pycache__' in p.parts: continue
                safe=self.path(rel)
                h.update(rel.encode()); h.update(b'\0'); h.update(safe.read_bytes()); h.update(b'\0')
        return h.hexdigest()
    def validate_changes(self,changes):
        names=set()
        for change in changes:
            p=self.path(change.path)
            if change.path in names: raise ValueError('Duplicate change path')
            names.add(change.path)
            if p.exists() and p.is_dir(): raise ValueError('Cannot overwrite directory')
            if len(change.content)>100_000: raise ValueError('Change too large')
    def write(self,changes):
        if self.role!='Builder': raise PermissionError('Only Builder may write source')
        self.validate_changes(changes)
        for change in changes:
            p=self.path(change.path)
            p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text(change.content,encoding='utf-8')
    def shell(self,argv,allow_execution=False):
        test=[sys.executable,'-B','-m','unittest','discover','-s','tests','-v']
        git_commands=[['git','status','--short'],['git','diff','--no-ext-diff','--no-textconv']]
        if argv==test:
            if self.role not in {'Builder','TestRunner'} or not allow_execution:
                raise PermissionError('Test execution requires explicit local opt-in')
        elif argv not in git_commands: raise PermissionError('Command not allowlisted')
        env={k:v for k,v in os.environ.items() if k.upper() in {'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','HOME','USERPROFILE','LANG'}}
        try:
            r=subprocess.run(argv,cwd=self.root,env=env,shell=False,capture_output=True,text=True,timeout=30)
            return r.returncode,(r.stdout+r.stderr)[-20000:]
        except subprocess.TimeoutExpired:
            return 124,'Execution timed out after 30 seconds'
    def test(self,allow_execution):
        cmd=[sys.executable,'-B','-m','unittest','discover','-s','tests','-v']
        if not list((self.root/'tests').glob('test*.py')):
            return TestReport(exit_code=2,output='No tests discovered',passed=False,command=cmd)
        code,out=self.shell(cmd,allow_execution)
        import re
        count=re.search(r'Ran (\d+) tests?',out)
        passed=code==0 and count is not None and int(count[1])>0
        return TestReport(exit_code=code,output=out,passed=passed,command=cmd)
