import argparse
import json
import sys
from .orchestrator import Orchestrator

def main():
    p=argparse.ArgumentParser(description='Engineering Agent Team — local approval-driven MVP')
    p.add_argument('--home',default='.team')
    sub=p.add_subparsers(dest='command',required=True)
    create=sub.add_parser('create')
    create.add_argument('project'); create.add_argument('--requirement',required=True)
    create.add_argument('--adapter',choices=['mock','openai'],default='mock')
    create.add_argument('--target',choices=['staging','production'],default='staging')
    create.add_argument('--allow-test-execution',action='store_true',help='Trust generated Python tests to execute on the host; not sandboxed')
    create.add_argument('--repo',help='Import a local Git repository into a linked worktree')
    create.add_argument('--dirty',choices=['committed','include'],help='Required when the repo has uncommitted changes')
    create.add_argument('--executor',choices=['host','container'],default='host',help='Test runner; container never falls back to the host')
    for command in ['status','next','artifacts']:
        q=sub.add_parser(command); q.add_argument('run')
    q=sub.add_parser('approve'); q.add_argument('run'); q.add_argument('approval')
    q.add_argument('--actor',required=True); q.add_argument('--reject',action='store_true'); q.add_argument('--note',default='')
    sub.add_parser('list')
    sub.add_parser('doctor')
    args=p.parse_args()
    try:
        engine=Orchestrator(args.home)
        if args.command=='create':
            result=engine.create(args.project,args.requirement,args.adapter,args.target,args.allow_test_execution,args.repo,args.dirty,args.executor).model_dump(mode='json')
        elif args.command=='status': result=engine.store.status(args.run)
        elif args.command=='next': result=engine.next(args.run).model_dump(mode='json')
        elif args.command=='approve': result=engine.approve(args.run,args.approval,args.actor,not args.reject,args.note).model_dump(mode='json')
        elif args.command=='artifacts':
            with engine.store.connect() as c:
                s=engine.store.load(c,args.run)
                result={k:engine.store.artifact(c,v) for k,v in s.artifacts.items()}
                result['pending_proposal']=s.candidate.model_dump() if s.candidate else None
        elif args.command=='list':
            with engine.store.connect() as c: result=c.execute('SELECT id,project,created FROM runs ORDER BY created').fetchall()
        else:
            import importlib.util, pydantic, shutil
            from . import __version__
            from .container import runtime_path
            result={'version':__version__,'python':sys.version,'pydantic':pydantic.__version__,'sqlite':'ready','openai_sdk':bool(importlib.util.find_spec('agents')),'git':shutil.which('git'),'container':runtime_path(),'mock':'ready'}
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}',file=sys.stderr)
        return_code=1
        raise SystemExit(return_code)
