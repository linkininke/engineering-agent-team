"""Offline fixture ONLY: explicit simulated approvals; never auto-approve real runs."""
import argparse
import json
from agent_team.orchestrator import Orchestrator
from agent_team.schemas import Stage

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--home',default='.team-demo')
    args=parser.parse_args()
    engine=Orchestrator(args.home)
    s=engine.create('calculator-demo','Create add(a, b) supporting positive and negative numbers.',allow_test_execution=True)
    history=[]
    for _ in range(40):
        history.append({'revision':s.revision,'stage':s.stage.value,'pending':s.pending})
        if s.stage in {Stage.DONE,Stage.BLOCKED}: break
        if s.stage==Stage.APPROVAL:
            engine.approve(s.run_id,s.pending,'DEMO_SIMULATED_HUMAN',note='Fixture-only simulated approval')
        s=engine.next(s.run_id)
    result={'run_id':s.run_id,'final':s.stage.value,'history':history,'status':engine.store.status(s.run_id)}
    print(json.dumps(result,indent=2))
    if s.stage!=Stage.DONE: raise SystemExit(1)

if __name__=='__main__': main()
