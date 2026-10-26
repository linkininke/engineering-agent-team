import difflib
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from .schemas import TestReport

def _skipped(parts):
    return any(part.startswith('.') or part=='__pycache__' for part in parts)

def tree_digest(root,only=None):
    digest=hashlib.sha256()
    base=Path(root)
    for path in sorted(base.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        rel=path.relative_to(base).as_posix()
        if _skipped(Path(rel).parts):
            continue
        if only and not (rel==only or rel.startswith(only+'/')):
            continue
        digest.update(rel.encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()

def acceptance_digest(root):
    return tree_digest(root,'acceptance')

def render_patch(tools,changes):
    chunks=[]
    for change in changes:
        path=tools.path(change.path)
        old=path.read_text(encoding='utf-8') if path.is_file() else ''
        chunks.append(''.join(difflib.unified_diff(
            old.splitlines(keepends=True),
            change.content.splitlines(keepends=True),
            fromfile='a/'+change.path,
            tofile='b/'+change.path,
        )))
    return ''.join(chunks)

class Tools:
    """Application permissions, NOT an OS sandbox. Use isolated containers for untrusted tests."""
    def __init__(self,root,role,executor='host'):
        self.root=Path(root).resolve()
        self.role=role
        self.executor=executor
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
                if safe.is_symlink(): continue
                if safe.stat().st_size>100_000: raise ValueError('File too large for context')
                files[rel]=safe.read_text(encoding='utf-8')
                if sum(len(v) for v in files.values())>500_000: raise ValueError('Context exceeds MVP limit')
        return files
    def digest(self):
        return tree_digest(self.root)
    def _check_role(self,changes):
        for change in changes:
            acceptance=change.path=='acceptance' or change.path.startswith('acceptance/')
            if self.role=='Builder' and acceptance:
                raise PermissionError('Builder cannot modify acceptance tests')
            if self.role=='Verifier' and not acceptance:
                raise PermissionError('Verifier may only write acceptance tests')
            if self.role not in {'Builder','Verifier'}:
                raise PermissionError('Only Builder may write source')
    def validate_changes(self,changes):
        self._check_role(changes)
        names=set()
        for change in changes:
            p=self.path(change.path)
            if change.path in names: raise ValueError('Duplicate change path')
            names.add(change.path)
            if p.exists() and p.is_dir(): raise ValueError('Cannot overwrite directory')
            if len(change.content)>100_000: raise ValueError('Change too large')
    def write(self,changes):
        self.validate_changes(changes)
        for change in changes:
            p=self.path(change.path)
            p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text(change.content,encoding='utf-8')
    def test_argv(self,suite):
        folder='tests' if suite=='dev' else 'acceptance'
        return [sys.executable,'-B','-m','unittest','discover','-s',folder,'-v']
    def shell(self,argv,allow_execution=False):
        dev=self.test_argv('dev')
        acceptance=self.test_argv('acceptance')
        git_commands=[['git','status','--short'],['git','diff','--no-ext-diff','--no-textconv']]
        if argv in (dev,acceptance):
            if argv==acceptance and self.role!='TestRunner':
                raise PermissionError('Only the Verifier may run acceptance tests')
            if self.role not in {'Builder','TestRunner'}:
                raise PermissionError('Test execution requires explicit local opt-in')
            if self.executor=='container':
                if self.role!='TestRunner':
                    raise PermissionError('Container tests are run by the Verifier')
                from .container import run_suite
                return run_suite(self.root,'acceptance' if argv==acceptance else 'tests')
            if not allow_execution:
                raise PermissionError('Test execution requires explicit local opt-in')
        elif argv not in git_commands: raise PermissionError('Command not allowlisted')
        env={k:v for k,v in os.environ.items() if k.upper() in {'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','HOME','USERPROFILE','LANG','PATHEXT','COMSPEC','SYSTEMDRIVE'}}
        try:
            r=subprocess.run(argv,cwd=self.root,env=env,shell=False,capture_output=True,text=True,timeout=30)
            return r.returncode,(r.stdout+r.stderr)[-20000:]
        except subprocess.TimeoutExpired:
            return 124,'Execution timed out after 30 seconds'
    def _has_tests(self,folder):
        root=self.root/folder
        return any(p.is_file() and p.name.startswith('test') and p.suffix=='.py' for p in root.rglob('*.py')) if root.exists() else False
    def test(self,allow_execution,suite='dev'):
        host=self.test_argv(suite)
        folder='tests' if suite=='dev' else 'acceptance'
        if self.executor=='container':
            from .container import describe_command
            command=describe_command(self.root,folder)
        else:
            command=host
        if not self._has_tests(folder):
            return TestReport(exit_code=2,output=f'No {suite} tests discovered',passed=False,command=command)
        code,out=self.shell(host,allow_execution)
        import re
        count=re.search(r'Ran (\d+) tests?',out)
        passed=code==0 and count is not None and int(count[1])>0
        return TestReport(exit_code=code,output=out,passed=passed,command=command)
