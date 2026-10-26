import hashlib
import json
import re
import shutil
import uuid
import zipfile
from pathlib import Path
from .schemas import *
from .store import Store
from .tools import Tools, acceptance_digest, render_patch
from .llm import MockLLM, OpenAIAdapter, ROLES
from .repo import GitError, fingerprint, inspect_repo, add_worktree, remove_worktree, import_uncommitted, commit_paths, head_commit

DIRS=dict(zip(['REQUIREMENT','CONTEXT','ARCHITECTURE','IMPLEMENTATION','TEST','REVIEW','DELIVERY','OPS'],
['01_requirement','02_context','03_architecture','04_implementation','05_test','06_review','07_delivery','08_ops']))

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

class Orchestrator:
    def __init__(self,home='.team',adapter=None):
        self.store=Store(home)
        self.adapter=adapter
    def create(self,project,request,adapter='mock',target='staging',allow_test_execution=False,repo=None,dirty=None,executor='host'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,60}',project): raise ValueError('Use a simple project slug')
        if not request.strip(): raise ValueError('Requirement is empty')
        if executor not in {'host','container'}: raise ValueError('Unknown test executor')
        if dirty not in {None,'committed','include'}: raise ValueError('Unknown dirty policy')
        s=EngineeringState(run_id=uuid.uuid4().hex,project=project,request=request,adapter=adapter,target=target,allow_test_execution=allow_test_execution,executor=executor)
        root=self.root(s)
        origin=None
        branch=None
        dest=root/'workspace'
        try:
            for folder in DIRS.values(): (root/'artifacts'/folder).mkdir(parents=True)
            if repo:
                info=inspect_repo(repo)
                origin=info['root']
                if info['dirty'] and not dirty:
                    raise ValueError(
                        '仓库存在未提交修改，原始工作区未改动。'
                        '请先自行提交或清理，或显式指定 --dirty committed（仅以 HEAD 建立任务，不复制未提交内容），'
                        '或 --dirty include（将未提交内容复制到独立 worktree 和任务分支，仍不修改原始工作区）。')
                branch=f'agent-team/{s.run_id}'
                before=fingerprint(origin)
                add_worktree(origin,dest,branch,info['commit'])
                task_head=info['commit']
                policy='clean'
                if info['dirty'] and dirty=='include':
                    task_head=import_uncommitted(origin,dest)
                    policy='include'
                elif info['dirty']:
                    policy='committed'
                if fingerprint(origin)!=before:
                    raise GitError('导入改动了原始工作区，已中止并清理任务分支')
                s.repo=str(origin)
                s.base_commit=info['commit']
                s.branch=branch
                s.task_head=task_head
                s.dirty=policy
            else:
                dest.mkdir(parents=True)
            with self.store.transaction() as c:
                c.execute('INSERT INTO runs(id,project) VALUES(?,?)',(s.run_id,s.project))
                self.store.save(c,s)
        except BaseException:
            if origin is not None and branch:
                remove_worktree(origin,dest,branch)
            shutil.rmtree(root,ignore_errors=True)
            raise
        return s
    def root(self,s): return self.store.home/'runs'/s.run_id
    def record(self,c,s,stage,value):
        identifier=uuid.uuid4().hex
        payload=value.model_dump_json(indent=2)
        c.execute('INSERT INTO artifacts VALUES(?,?,?,?)',(identifier,s.run_id,stage,payload))
        path=self.root(s)/'artifacts'/DIRS[stage]/f'{s.revision:04d}-{identifier}.json'
        path.write_text(payload,encoding='utf-8')
        s.artifacts[stage]=identifier
        return identifier
    def gate(self,c,s,gate,subject,resume):
        identifier=uuid.uuid4().hex
        c.execute('INSERT INTO approvals(id,run_id,gate,subject) VALUES(?,?,?,?)',(identifier,s.run_id,gate,subject))
        s.pending=identifier; s.resume=resume; s.stage=Stage.APPROVAL
    def approve(self,run,identifier,actor,accept=True,note=''):
        if not actor.strip(): raise ValueError('Reviewer name required')
        with self.store.transaction() as c:
            s=self.store.load(c,run)
            if s.stage!=Stage.APPROVAL or s.pending!=identifier: raise ValueError('Approval is not current')
            row=c.execute('SELECT status FROM approvals WHERE id=? AND run_id=?',(identifier,run)).fetchone()
            if not row or row[0]!='pending': raise ValueError('Approval already decided')
            c.execute('UPDATE approvals SET status=?,actor=?,note=?,decided=CURRENT_TIMESTAMP WHERE id=?',('approved' if accept else 'rejected',actor,note,identifier))
            if not accept: s.stage=Stage.BLOCKED
            self.store.save(c,s)
        return s
    def next(self,run):
        # Single writer lock prevents simultaneous progression and duplicate approvals.
        with self.store.transaction() as c:
            s=self.store.load(c,run)
            stage=s.stage.value
            if s.stage in {Stage.DONE,Stage.BLOCKED}: return s
            if s.stage==Stage.APPROVAL:
                row=c.execute('SELECT status FROM approvals WHERE id=?',(s.pending,)).fetchone()
                if row[0]!='approved': return s
                s.stage=s.resume; s.resume=None; s.pending=None
            else:
                self.execute(c,s,stage)
            s.revision+=1
            c.execute('INSERT INTO events(run_id,message) VALUES(?,?)',(run,f'{stage} -> {s.stage.value}'))
            self.store.save(c,s)
            return s
    def llm_for(self,s):
        return self.adapter or (MockLLM() if s.adapter=='mock' else OpenAIAdapter())
    def payload(self,c,s,tools):
        return {'state':s.model_dump(mode='json'),'artifacts':{k:self.store.artifact(c,v) for k,v in s.artifacts.items()},'source':tools.snapshot()}
    def bind_proposal(self,tools,workspace,proposal):
        baseline=json.dumps({'commit':head_commit(workspace),'digest':tools.digest()},sort_keys=True,ensure_ascii=False)
        return proposal.model_copy(update={'patch':render_patch(tools,proposal.changes),'baseline':baseline})
    def ensure_acceptance(self,c,s,workspace):
        if s.acceptance_digest and acceptance_digest(workspace)!=s.acceptance_digest:
            s.stage=Stage.BLOCKED
            return False
        if s.acceptance_digest:
            return True
        tools=Tools(workspace,'Verifier',s.executor)
        suite=AcceptanceSuite.model_validate(self.llm_for(s).generate('Verifier','ACCEPTANCE',self.payload(c,s,tools),AcceptanceSuite).model_dump())
        for change in suite.files:
            name=Path(change.path).name
            if not change.path.startswith('acceptance/') or not name.startswith('test') or not name.endswith('.py'):
                raise ValueError('验收测试必须放在 acceptance/ 下，且文件名以 test 开头、以 .py 结尾')
        tools.write(suite.files)
        if s.branch:
            s.task_head=commit_paths(workspace,[change.path for change in suite.files],'Add Verifier acceptance tests')
        s.acceptance_digest=acceptance_digest(workspace)
        return True
    def run_reports(self,s,workspace):
        if s.executor=='container':
            from .container import ensure_ready
            ensure_ready()
        runner=Tools(workspace,'TestRunner',s.executor)
        allow=bool(s.allow_test_execution or s.executor=='container')
        dev=runner.test(allow,'dev')
        acceptance=runner.test(allow,'acceptance')
        passed=dev.passed and acceptance.passed
        return TestReport(exit_code=0 if passed else 1,output=f'== dev ==\n{dev.output}\n== acceptance ==\n{acceptance.output}',
            passed=passed,command=dev.command,dev_passed=dev.passed,acceptance_passed=acceptance.passed,
            dev_output=dev.output,acceptance_output=acceptance.output,acceptance_command=acceptance.command)
    def guarded(self,s,workspace):
        if not s.acceptance_digest or acceptance_digest(workspace)!=s.acceptance_digest:
            s.stage=Stage.BLOCKED
            return False
        return True
    def execute(self,c,s,stage):
        root=self.root(s); workspace=root/'workspace'
        tools=Tools(workspace,ROLES[stage],s.executor)
        def ask(schema):
            return schema.model_validate(self.llm_for(s).generate(ROLES[stage],stage,self.payload(c,s,tools),schema))
        if stage=='REQUIREMENT':
            value=ask(Requirement)
            identifier=self.record(c,s,stage,value)
            self.gate(c,s,'requirement',identifier,Stage.CONTEXT)
        elif stage=='CONTEXT':
            constraints=['Python + stdlib unittest','Local packaging only','No arbitrary shell','No file deletion','Developer tests live in tests/','Acceptance tests in acceptance/ are owned by the Verifier']
            if s.repo:
                constraints.append('The task runs in a linked worktree and must not modify the original working tree')
                constraints.append('Baseline commit '+s.base_commit)
            self.record(c,s,stage,Context(files=tools.snapshot(),constraints=constraints))
            s.stage=Stage.ARCHITECTURE
        elif stage=='ARCHITECTURE':
            identifier=self.record(c,s,stage,ask(Architecture))
            self.gate(c,s,'architecture',identifier,Stage.IMPLEMENTATION)
        elif stage=='IMPLEMENTATION':
            if s.attempts>=s.max_attempts:
                s.stage=Stage.BLOCKED; return
            if s.candidate is None: s.candidate=ask(Implementation)
            proposal=self.bind_proposal(tools,workspace,s.candidate)
            tools.validate_changes(proposal.changes)
            s.candidate=proposal
            # Approval is bound to the canonical patch and the baseline commit plus tree digest.
            subject=digest({'patch':proposal.patch,'baseline':proposal.baseline})
            approved=c.execute("SELECT 1 FROM approvals WHERE run_id=? AND gate='high_risk_change' AND subject=? AND status='approved'",(s.run_id,subject)).fetchone()
            if not approved:
                self.gate(c,s,'high_risk_change',subject,Stage.IMPLEMENTATION); return
            tools.write(s.candidate.changes)
            if s.branch:
                s.task_head=commit_paths(workspace,[change.path for change in s.candidate.changes],s.candidate.summary)
            self.record(c,s,stage,s.candidate)
            s.candidate=None; s.attempts+=1
            s.tested_digest=None; s.reviewed_digest=None
            s.stage=Stage.TEST
        elif stage=='TEST':
            if not self.ensure_acceptance(c,s,workspace):
                return
            before=tools.digest()
            report=self.run_reports(s,workspace)
            self.record(c,s,stage,report)
            if acceptance_digest(workspace)!=s.acceptance_digest:
                s.stage=Stage.BLOCKED; return
            if report.passed and before==tools.digest():
                s.tested_digest=before; s.stage=Stage.REVIEW
            else: s.stage=Stage.IMPLEMENTATION
        elif stage=='REVIEW':
            if not self.guarded(s,workspace): return
            if tools.digest()!=s.tested_digest:
                s.stage=Stage.TEST; return
            review=ask(Review)
            self.record(c,s,stage,review)
            if review.verdict=='PASS':
                s.reviewed_digest=s.tested_digest; s.stage=Stage.DELIVERY
            else: s.stage=Stage.IMPLEMENTATION
        elif stage=='DELIVERY':
            if not self.guarded(s,workspace): return
            current=tools.digest()
            if current!=s.reviewed_digest or current!=s.tested_digest:
                s.stage=Stage.TEST; return
            if s.target=='production':
                approved=c.execute("SELECT 1 FROM approvals WHERE run_id=? AND gate='production' AND subject=? AND status='approved'",(s.run_id,current)).fetchone()
                if not approved:
                    self.gate(c,s,'production',current,Stage.DELIVERY); return
            package=root/f'{s.target}-{current}.zip'
            with zipfile.ZipFile(package,'w',zipfile.ZIP_DEFLATED) as z:
                for p in sorted(workspace.rglob('*')):
                    if not p.is_file() or p.is_symlink(): continue
                    rel=p.relative_to(workspace).as_posix()
                    if any(part.startswith('.') or part=='__pycache__' for part in Path(rel).parts): continue
                    z.write(tools.path(rel),rel)
            self.record(c,s,stage,Delivery(target=s.target,package=package.name,digest=hashlib.sha256(package.read_bytes()).hexdigest()))
            s.stage=Stage.OPS
        elif stage=='OPS':
            self.record(c,s,stage,ask(Ops)); s.stage=Stage.DONE
