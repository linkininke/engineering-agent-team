import hashlib
import json
import re
import uuid
import zipfile
from pathlib import Path
from .schemas import *
from .store import Store
from .tools import Tools
from .llm import MockLLM, OpenAIAdapter, ROLES

DIRS=dict(zip(['REQUIREMENT','CONTEXT','ARCHITECTURE','IMPLEMENTATION','TEST','REVIEW','DELIVERY','OPS'],
['01_requirement','02_context','03_architecture','04_implementation','05_test','06_review','07_delivery','08_ops']))

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

class Orchestrator:
    def __init__(self,home='.team',adapter=None):
        self.store=Store(home)
        self.adapter=adapter
    def create(self,project,request,adapter='mock',target='staging',allow_test_execution=False):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,60}',project): raise ValueError('Use a simple project slug')
        if not request.strip(): raise ValueError('Requirement is empty')
        s=EngineeringState(run_id=uuid.uuid4().hex,project=project,request=request,adapter=adapter,target=target,allow_test_execution=allow_test_execution)
        root=self.root(s)
        (root/'workspace').mkdir(parents=True)
        for folder in DIRS.values(): (root/'artifacts'/folder).mkdir(parents=True)
        with self.store.transaction() as c:
            c.execute('INSERT INTO runs(id,project) VALUES(?,?)',(s.run_id,s.project))
            self.store.save(c,s)
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
    def execute(self,c,s,stage):
        root=self.root(s); workspace=root/'workspace'
        tools=Tools(workspace,ROLES[stage])
        def ask(schema):
            llm=self.adapter or (MockLLM() if s.adapter=='mock' else OpenAIAdapter())
            payload={'state':s.model_dump(mode='json'),'artifacts':{k:self.store.artifact(c,v) for k,v in s.artifacts.items()},'source':tools.snapshot()}
            return schema.model_validate(llm.generate(ROLES[stage],stage,payload,schema))
        if stage=='REQUIREMENT':
            value=ask(Requirement)
            identifier=self.record(c,s,stage,value)
            self.gate(c,s,'requirement',identifier,Stage.CONTEXT)
        elif stage=='CONTEXT':
            self.record(c,s,stage,Context(files=tools.snapshot(),constraints=['Python + stdlib unittest','Local packaging only','No arbitrary shell','No file deletion']))
            s.stage=Stage.ARCHITECTURE
        elif stage=='ARCHITECTURE':
            identifier=self.record(c,s,stage,ask(Architecture))
            self.gate(c,s,'architecture',identifier,Stage.IMPLEMENTATION)
        elif stage=='IMPLEMENTATION':
            if s.attempts>=s.max_attempts:
                s.stage=Stage.BLOCKED; return
            if s.candidate is None: s.candidate=ask(Implementation)
            tools.validate_changes(s.candidate.changes)
            # Conservative: EVERY source proposal is reviewed, covering semantic risk
            # that cannot reliably be inferred from filenames or model risk flags.
            subject=digest({'proposal':s.candidate.model_dump(),'base':tools.digest()})
            approved=c.execute("SELECT 1 FROM approvals WHERE run_id=? AND gate='high_risk_change' AND subject=? AND status='approved'",(s.run_id,subject)).fetchone()
            if not approved:
                self.gate(c,s,'high_risk_change',subject,Stage.IMPLEMENTATION); return
            tools.write(s.candidate.changes)
            self.record(c,s,stage,s.candidate)
            s.candidate=None; s.attempts+=1
            s.tested_digest=None; s.reviewed_digest=None
            s.stage=Stage.TEST
        elif stage=='TEST':
            before=tools.digest()
            report=Tools(workspace,'TestRunner').test(s.allow_test_execution)
            self.record(c,s,stage,report)
            if report.passed and before==tools.digest():
                s.tested_digest=before; s.stage=Stage.REVIEW
            else: s.stage=Stage.IMPLEMENTATION
        elif stage=='REVIEW':
            if tools.digest()!=s.tested_digest:
                s.stage=Stage.TEST; return
            review=ask(Review)
            self.record(c,s,stage,review)
            if review.verdict=='PASS':
                s.reviewed_digest=s.tested_digest; s.stage=Stage.DELIVERY
            else: s.stage=Stage.IMPLEMENTATION
        elif stage=='DELIVERY':
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
                    if p.is_file() and '__pycache__' not in p.parts:
                        z.write(tools.path(p.relative_to(workspace).as_posix()),p.relative_to(workspace))
            self.record(c,s,stage,Delivery(target=s.target,package=package.name,digest=hashlib.sha256(package.read_bytes()).hexdigest()))
            s.stage=Stage.OPS
        elif stage=='OPS':
            self.record(c,s,stage,ask(Ops)); s.stage=Stage.DONE
