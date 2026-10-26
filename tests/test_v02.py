import hashlib
import json
import os
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from agent_team.container import ContainerUnavailable, client_env, describe_command, ensure_ready
from agent_team.llm import MockLLM
from agent_team.orchestrator import Orchestrator
from agent_team.repo import GitError, fingerprint
from agent_team.schemas import AcceptanceSuite, Architecture, Change, Implementation, Ops, Requirement, Review, Stage
from agent_team.tools import Tools

def git(cwd,*args,check=True):
    result=subprocess.run(['git','-c','user.name=Agent Team','-c','user.email=agent-team@localhost',*args],cwd=cwd,capture_output=True,text=True,encoding='utf-8',errors='replace')
    if check and result.returncode!=0:
        raise AssertionError(result.stderr or result.stdout)
    return result

def detach(origin):
    if not origin.exists():
        return
    listed=subprocess.run(['git','worktree','list','--porcelain'],cwd=origin,capture_output=True,text=True,encoding='utf-8',errors='replace')
    for line in listed.stdout.splitlines():
        if not line.startswith('worktree '):
            continue
        path=Path(line.split(' ',1)[1])
        if path.resolve()==origin.resolve():
            continue
        subprocess.run(['git','worktree','remove','--force',str(path)],cwd=origin,capture_output=True)
    subprocess.run(['git','worktree','prune'],cwd=origin,capture_output=True)

def files_outside_git(root):
    found={}
    for path in root.rglob('*'):
        if path.is_file() and '.git' not in path.parts:
            found[path.relative_to(root).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    return found

class FixAddLLM(MockLLM):
    """Fixture for one local repository. Not used for real approvals."""
    def generate(self,role,stage,payload,schema):
        if stage=='REQUIREMENT':
            result=Requirement(goal=payload['state']['request'],acceptance_criteria=['add(2, 3) == 5','add(-2, 2) == 0'],non_scope=['UI','Production deploy'],open_questions=[])
        elif stage=='ARCHITECTURE':
            result=Architecture(design='Fix app.add in the linked worktree.',tasks=['Replace subtraction with addition','Leave acceptance tests to the Verifier'],risks=['Do not edit the original worktree'])
        elif stage=='IMPLEMENTATION':
            result=Implementation(summary='Fix add to use addition',high_risk=True,changes=[
                Change(path='app.py',content='def add(a, b):\n    return a + b\n'),
                Change(path='tests/test_app.py',content='import unittest\nfrom app import add\n\nclass DevTests(unittest.TestCase):\n    def test_positive(self):\n        self.assertEqual(add(2, 3), 5)\n')])
        elif stage=='ACCEPTANCE':
            result=AcceptanceSuite(summary='Acceptance owned by the Verifier.',files=[
                Change(path='acceptance/test_add.py',content='import unittest\nfrom app import add\n\nclass AddAcceptance(unittest.TestCase):\n    def test_positive(self):\n        self.assertEqual(add(2, 3), 5)\n    def test_negative(self):\n        self.assertEqual(add(-2, 2), 0)\n')])
        elif stage=='REVIEW':
            result=Review(verdict='PASS',findings=[])
        elif stage=='OPS':
            result=Ops(summary='Task branch is ready for human review. Nothing was deployed.',recommendations=['Inspect the task branch diff'])
        else:
            raise ValueError(stage)
        return schema.model_validate(result.model_dump())

class BadAcceptance(MockLLM):
    def generate(self,role,stage,payload,schema):
        if stage=='ACCEPTANCE':
            result=AcceptanceSuite(summary='Intentional failing acceptance.',files=[
                Change(path='acceptance/test_bad.py',content='import unittest\n\nclass Bad(unittest.TestCase):\n    def test_fail(self):\n        self.fail("forced")\n')])
            return schema.model_validate(result.model_dump())
        return super().generate(role,stage,payload,schema)

class BuilderWritesAcceptance(MockLLM):
    def generate(self,role,stage,payload,schema):
        if stage=='IMPLEMENTATION':
            result=Implementation(summary='Illegal acceptance edit',high_risk=True,changes=[
                Change(path='acceptance/test_add.py',content='import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n')])
            return schema.model_validate(result.model_dump())
        return super().generate(role,stage,payload,schema)

class V02Tests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.e=Orchestrator(Path(self.temp.name)/'home')
        self.s=self.e.create('demo','calculator',allow_test_execution=True)
    def advance(self,until):
        for _ in range(45):
            if self.s.stage==until: return
            if self.s.stage==Stage.APPROVAL:
                self.e.approve(self.s.run_id,self.s.pending,'test-human')
            self.s=self.e.next(self.s.run_id)
        self.fail('Did not reach '+str(until))
    def workspace(self,state=None):
        return self.e.root(state or self.s)/'workspace'
    def origin(self,name='origin'):
        path=Path(self.temp.name)/name
        path.mkdir()
        self.addCleanup(lambda: detach(path))
        git(path,'init','-b','main')
        git(path,'config','core.autocrlf','false')
        (path/'app.py').write_text('def add(a, b):\n    return a - b\n',encoding='utf-8')
        git(path,'add','app.py')
        git(path,'commit','-m','initial add')
        return path
    def test_patch_is_bound_to_baseline(self):
        self.advance(Stage.IMPLEMENTATION)
        self.s=self.e.next(self.s.run_id)
        self.assertIn('+++ b/calculator.py',self.s.candidate.patch)
        self.assertIn('b/tests/test_calculator.py',self.s.candidate.patch)
        baseline=json.loads(self.s.candidate.baseline)
        self.assertIsNone(baseline['commit'])
        self.assertEqual(len(baseline['digest']),64)
        old=self.s.pending
        self.e.approve(self.s.run_id,old,'test-human')
        self.s=self.e.next(self.s.run_id)
        (self.workspace()/'marker.txt').write_text('changed',encoding='utf-8')
        self.s=self.e.next(self.s.run_id)
        self.assertEqual(self.s.stage,Stage.APPROVAL)
        self.assertNotEqual(self.s.pending,old)
        subjects=[item['subject'] for item in self.e.store.status(self.s.run_id)['approvals'] if item['gate']=='high_risk_change']
        self.assertEqual(len(set(subjects)),2)
    def test_builder_cannot_touch_acceptance_tests(self):
        root=self.workspace()
        with self.assertRaises(PermissionError):
            Tools(root,'Builder').write([Change(path='acceptance/test_x.py',content='x')])
        Tools(root,'Verifier').write([Change(path='acceptance/test_x.py',content='import unittest\n')])
        with self.assertRaises(PermissionError):
            Tools(root,'Verifier').write([Change(path='app.py',content='x')])
        command=[sys.executable,'-B','-m','unittest','discover','-s','acceptance','-v']
        with self.assertRaises(PermissionError):
            Tools(root,'Builder').shell(command,True)
        self.e.adapter=BuilderWritesAcceptance()
        self.advance(Stage.IMPLEMENTATION)
        with self.assertRaises(PermissionError):
            self.e.next(self.s.run_id)
        self.assertEqual(self.e.store.status(self.s.run_id)['state']['stage'],'IMPLEMENTATION')
    def test_both_suites_pass_and_failing_acceptance_blocks_delivery(self):
        self.advance(Stage.DONE)
        with self.e.store.connect() as connection:
            report=self.e.store.artifact(connection,self.s.artifacts['TEST'])
        self.assertTrue(report['dev_passed'])
        self.assertTrue(report['acceptance_passed'])
        self.assertTrue((self.workspace()/'acceptance'/'test_add.py').is_file())
        blocked=Orchestrator(Path(self.temp.name)/'home',BadAcceptance())
        state=blocked.create('bad','calculator',allow_test_execution=True)
        for _ in range(45):
            if state.stage in {Stage.DONE,Stage.BLOCKED}: break
            if state.stage==Stage.APPROVAL:
                blocked.approve(state.run_id,state.pending,'test-human')
            state=blocked.next(state.run_id)
        self.assertEqual(state.stage,Stage.BLOCKED)
        self.assertEqual(state.attempts,3)
        self.assertFalse(list(blocked.root(state).glob('staging-*.zip')))
        self.assertIn('forced',(blocked.root(state)/'workspace'/'acceptance'/'test_bad.py').read_text(encoding='utf-8'))
    def test_dirty_repository_requires_an_explicit_policy(self):
        origin=self.origin()
        (origin/'scratch.txt').write_text('wip\n',encoding='utf-8')
        before=files_outside_git(origin)
        porcelain=fingerprint(origin)
        with self.assertRaises(ValueError) as caught:
            self.e.create('job','Fix add',repo=origin)
        self.assertIn('未提交',str(caught.exception))
        self.assertIn('committed',str(caught.exception))
        self.assertEqual(files_outside_git(origin),before)
        self.assertEqual(fingerprint(origin),porcelain)
        self.assertEqual(git(origin,'worktree','list','--porcelain').stdout.count('worktree '),1)
    def test_committed_policy_leaves_uncommitted_files_behind(self):
        origin=self.origin()
        (origin/'scratch.txt').write_text('wip\n',encoding='utf-8')
        before=files_outside_git(origin)
        state=self.e.create('job','Fix add',repo=origin,dirty='committed')
        self.assertEqual(state.dirty,'committed')
        self.assertEqual(files_outside_git(origin),before)
        self.assertFalse((self.workspace(state)/'scratch.txt').exists())
        self.assertEqual(git(origin,'branch','--show-current').stdout.strip(),'main')
        self.assertEqual(git(self.workspace(state),'branch','--show-current').stdout.strip(),state.branch)
        self.assertNotEqual(state.branch,'main')
    def test_include_policy_copies_into_the_task_branch_only(self):
        origin=self.origin()
        (origin/'app.py').write_text('def add(a, b):\n    return 0\n',encoding='utf-8')
        (origin/'scratch.txt').write_text('wip\n',encoding='utf-8')
        before=files_outside_git(origin)
        state=self.e.create('job','Fix add',repo=origin,dirty='include')
        self.assertEqual(files_outside_git(origin),before)
        workspace=self.workspace(state)
        self.assertEqual((workspace/'scratch.txt').read_text(encoding='utf-8'),'wip\n')
        self.assertIn('return 0',(workspace/'app.py').read_text(encoding='utf-8'))
        self.assertEqual(git(workspace,'status','--porcelain').stdout.strip(),'')
        self.assertNotEqual(state.task_head,state.base_commit)
        self.assertEqual(git(origin,'branch','--show-current').stdout.strip(),'main')
    def test_include_keeps_tracked_dotfiles_and_rejects_secret_files(self):
        origin=self.origin()
        (origin/'.gitignore').write_text('*.log\n',encoding='utf-8')
        git(origin,'add','.gitignore')
        git(origin,'commit','-m','ignore logs')
        (origin/'.gitignore').write_text('*.log\n*.tmp\n',encoding='utf-8')
        before=files_outside_git(origin)
        state=self.e.create('job','Fix add',repo=origin,dirty='include')
        self.assertEqual(files_outside_git(origin),before)
        self.assertIn('*.tmp',(self.workspace(state)/'.gitignore').read_text(encoding='utf-8'))
        secret=self.origin('secret')
        (secret/'.env').write_text('API_KEY=secret\n',encoding='utf-8')
        snapshot=files_outside_git(secret)
        with self.assertRaises(GitError) as caught:
            self.e.create('job2','Fix add',repo=secret,dirty='include')
        self.assertIn('隐藏',str(caught.exception))
        self.assertEqual(files_outside_git(secret),snapshot)
    def test_real_repo_fix_stays_on_the_task_branch(self):
        origin=self.origin('repo')
        before=files_outside_git(origin)
        head=git(origin,'rev-parse','HEAD').stdout.strip()
        self.e.adapter=FixAddLLM()
        self.s=self.e.create('fix-add','Fix add so positive and negative numbers sum.',repo=origin,allow_test_execution=True)
        self.advance(Stage.DONE)
        self.assertEqual(files_outside_git(origin),before)
        self.assertEqual(git(origin,'rev-parse','HEAD').stdout.strip(),head)
        self.assertEqual(git(origin,'branch','--show-current').stdout.strip(),'main')
        workspace=self.workspace()
        self.assertIn('return a + b',(workspace/'app.py').read_text(encoding='utf-8'))
        self.assertIn('return a - b',(origin/'app.py').read_text(encoding='utf-8'))
        self.assertEqual(git(workspace,'branch','--show-current').stdout.strip(),self.s.branch)
        self.assertNotEqual(git(workspace,'rev-parse','HEAD').stdout.strip(),head)
        log=git(workspace,'log','--oneline').stdout
        self.assertIn('Fix add to use addition',log)
        self.assertIn('Add Verifier acceptance tests',log)
        with self.e.store.connect() as connection:
            implementation=self.e.store.artifact(connection,self.s.artifacts['IMPLEMENTATION'])
            report=self.e.store.artifact(connection,self.s.artifacts['TEST'])
        self.assertIn('return a + b',implementation['patch'])
        self.assertIn(head,implementation['baseline'])
        self.assertTrue(report['dev_passed'] and report['acceptance_passed'])
        package=next(self.e.root(self.s).glob('staging-*.zip'))
        with zipfile.ZipFile(package) as bundle:
            names=bundle.namelist()
        self.assertTrue(any(name.endswith('app.py') for name in names))
        self.assertTrue(any(name.startswith('acceptance/') for name in names))
        self.assertFalse(any('.git' in name for name in names))
    def test_container_command_excludes_host_secrets(self):
        sentinel='sk-agent-team-test-secret'
        previous=os.environ.get('OPENAI_API_KEY')
        os.environ['OPENAI_API_KEY']=sentinel
        try:
            argv=describe_command(self.workspace(),'tests',runtime='docker',image='python:3.11-slim')
            env=client_env()
        finally:
            if previous is None: os.environ.pop('OPENAI_API_KEY',None)
            else: os.environ['OPENAI_API_KEY']=previous
        self.assertEqual(argv[argv.index('--network')+1],'none')
        self.assertIn('--read-only',argv)
        self.assertIn('--cap-drop',argv)
        self.assertIn('512m',argv)
        self.assertIn('128',argv)
        blob=' '.join(argv)+json.dumps(env)
        self.assertNotIn(sentinel,blob)
        self.assertNotIn('OPENAI_API_KEY',env)
        with self.assertRaises(PermissionError):
            Tools(self.workspace(),'Builder',executor='container').shell(Tools(self.workspace(),'Builder').test_argv('dev'),True)
    def test_missing_container_runtime_does_not_use_the_host(self):
        self.s=self.e.create('box','calculator',allow_test_execution=True,executor='container')
        self.advance(Stage.TEST)
        with patch('agent_team.container.runtime_path',return_value=None):
            with self.assertRaises(ContainerUnavailable) as caught:
                self.e.next(self.s.run_id)
        self.assertIn('拒绝回退',str(caught.exception))
        self.assertIn('宿主机',str(caught.exception))
        self.assertEqual(self.e.store.status(self.s.run_id)['state']['stage'],'TEST')
    def test_stopped_daemon_is_not_reported_as_a_missing_image(self):
        class Result:
            returncode=1
            stderr='failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine'
            stdout=''
        with patch('agent_team.container.runtime_path',return_value='docker'),patch('agent_team.container.subprocess.run',return_value=Result()):
            with self.assertRaises(ContainerUnavailable) as caught:
                ensure_ready()
        self.assertIn('容器运行环境不可用',str(caught.exception))
        self.assertIn('已拒绝回退',str(caught.exception))
        self.assertNotIn('没有容器镜像',str(caught.exception))

if __name__=='__main__': unittest.main()
