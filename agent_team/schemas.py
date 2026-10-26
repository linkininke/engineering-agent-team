from enum import StrEnum
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class Model(BaseModel):
    model_config = ConfigDict(extra='forbid')

class Stage(StrEnum):
    REQUIREMENT='REQUIREMENT'
    CONTEXT='CONTEXT'
    ARCHITECTURE='ARCHITECTURE'
    APPROVAL='APPROVAL'
    IMPLEMENTATION='IMPLEMENTATION'
    TEST='TEST'
    REVIEW='REVIEW'
    DELIVERY='DELIVERY'
    OPS='OPS'
    DONE='DONE'
    BLOCKED='BLOCKED'

class Requirement(Model):
    goal: str
    acceptance_criteria: list[str] = Field(min_length=1)
    non_scope: list[str]
    open_questions: list[str]

class Context(Model):
    files: dict[str,str]
    constraints: list[str]

class Architecture(Model):
    design: str
    tasks: list[str] = Field(min_length=1)
    risks: list[str]

class Change(Model):
    path: str
    content: str

class Implementation(Model):
    summary: str
    changes: list[Change] = Field(min_length=1)
    high_risk: bool

class TestReport(Model):
    exit_code: int
    output: str
    passed: bool
    command: list[str]

class Review(Model):
    verdict: Literal['PASS','BLOCK']
    findings: list[str]

class Delivery(Model):
    target: Literal['staging','production']
    mode: Literal['local-package'] = 'local-package'
    package: str
    digest: str

class Ops(Model):
    summary: str
    recommendations: list[str]

class EngineeringState(Model):
    run_id: str
    project: str
    request: str
    adapter: Literal['mock','openai']='mock'
    stage: Stage=Stage.REQUIREMENT
    target: Literal['staging','production']='staging'
    revision: int=0
    attempts: int=0
    max_attempts: int=3
    artifacts: dict[str,str]=Field(default_factory=dict)
    pending: str | None=None
    resume: Stage | None=None
    candidate: Implementation | None=None
    tested_digest: str | None=None
    reviewed_digest: str | None=None
    allow_test_execution: bool=False
