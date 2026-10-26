import tempfile
import unittest
from pathlib import Path
from agent_team.orchestrator import Orchestrator
from agent_team.llm import MockLLM
from agent_team.schemas import Stage, Review, Change
from agent_team.tools import Tools

class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.e=Orchestrator(self.temp.name)
        self.s=self.e.create('demo','calculator',allow_test_execution=True)
    def advance(self,until):
        for _ in range(45):
            if self.s.stage==until: return
            if self.s.stage==Stage.APPROVAL:
                self.e.approve(self.s.run_id,self.s.pending,'test-human')
            self.s=self.e.next(self.s.run_id)
        self.fail('Did not reach '+str(until))
    def test_complete_and_persisted(self):
        self.advance(Stage.DONE)
        self.assertEqual(len(self.s.artifacts),8)
        fresh=Orchestrator(self.temp.name)
        self.assertEqual(fresh.store.status(self.s.run_id)['state']['stage'],'DONE')
    def test_gate_cannot_skip_and_reject(self):
        self.s=self.e.next(self.s.run_id)
        self.assertEqual(self.e.next(self.s.run_id).stage,Stage.APPROVAL)
        with self.assertRaises(ValueError): self.e.approve(self.s.run_id,'wrong','human')
        self.e.approve(self.s.run_id,self.s.pending,'human',False)
        self.assertEqual(self.e.next(self.s.run_id).stage,Stage.BLOCKED)
    def test_production_gate(self):
        self.s=self.e.create('prod','calculator',target='production',allow_test_execution=True)
        self.advance(Stage.DELIVERY)
        self.s=self.e.next(self.s.run_id)
        self.assertEqual(self.s.stage,Stage.APPROVAL)
        self.assertFalse(list(self.e.root(self.s).glob('production-*.zip')))
        self.assertEqual(self.e.store.status(self.s.run_id)['approvals'][-1]['gate'],'production')
        self.advance(Stage.DONE)
    def test_test_fail_returns_to_builder(self):
        self.advance(Stage.TEST)
        (self.e.root(self.s)/'workspace/calculator.py').write_text('def add(a,b): return 999\n')
        self.s=self.e.next(self.s.run_id)
        self.assertEqual(self.s.stage,Stage.IMPLEMENTATION)
        self.advance(Stage.DONE)
        self.assertEqual(self.s.attempts,2)
    def test_review_block_and_attempt_limit(self):
        class Block(MockLLM):
            def generate(self,role,stage,payload,schema):
                if stage=='REVIEW': return Review(verdict='BLOCK',findings=['Needs improvement'])
                return super().generate(role,stage,payload,schema)
        self.e.adapter=Block()
        self.advance(Stage.REVIEW)
        self.s=self.e.next(self.s.run_id)
        self.assertEqual(self.s.stage,Stage.IMPLEMENTATION)
        self.advance(Stage.BLOCKED)
        self.assertEqual(self.s.attempts,3)
    def test_change_invalidates_test_and_review(self):
        self.advance(Stage.DELIVERY)
        (self.e.root(self.s)/'workspace/calculator.py').write_text('def add(a,b): return 0\n')
        self.assertEqual(self.e.next(self.s.run_id).stage,Stage.TEST)
    def test_permissions_and_path_escape(self):
        root=self.e.root(self.s)/'workspace'
        for role in ['Planner','Verifier','Operator']:
            with self.assertRaises(PermissionError): Tools(root,role).write([Change(path='x.py',content='')])
        t=Tools(root,'Builder')
        for path in ['../escape','/absolute','.git/config','x/../../oops','C:/file','x\\y']:
            with self.assertRaises(PermissionError): t.path(path)
        with self.assertRaises(PermissionError): t.shell(['python','-c','print(1)'],True)
        with self.assertRaises(PermissionError): t.test(False) if (root/'tests').exists() else t.shell(['rm','-rf','.'])
    def test_no_tests_is_failure(self):
        report=Tools(self.e.root(self.s)/'workspace','TestRunner').test(True)
        self.assertFalse(report.passed)
    def test_requires_execution_opt_in(self):
        self.s=self.e.create('noexecute','calculator')
        self.advance(Stage.TEST)
        with self.assertRaises(PermissionError): self.e.next(self.s.run_id)
        self.assertEqual(self.e.store.status(self.s.run_id)['state']['stage'],'TEST')
    def test_source_change_invalidates_change_approval(self):
        self.advance(Stage.IMPLEMENTATION)
        self.s=self.e.next(self.s.run_id)
        old=self.s.pending
        self.e.approve(self.s.run_id,old,'human')
        self.s=self.e.next(self.s.run_id)
        (self.e.root(self.s)/'workspace/new.txt').write_text('changed')
        self.s=self.e.next(self.s.run_id)
        self.assertEqual(self.s.stage,Stage.APPROVAL)
        self.assertNotEqual(self.s.pending,old)

if __name__=='__main__': unittest.main()
