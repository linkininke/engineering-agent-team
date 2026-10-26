import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from .schemas import EngineeringState

class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()

class Store:
    def __init__(self, home):
        self.home=Path(home).resolve()
        self.home.mkdir(parents=True,exist_ok=True)
        self.db=self.home/'team.sqlite3'
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, project TEXT NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS state(run_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, run_id TEXT, stage TEXT, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, run_id TEXT, gate TEXT, subject TEXT, status TEXT DEFAULT 'pending', actor TEXT, note TEXT, decided TEXT);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, run_id TEXT, message TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
            ''')
    def connect(self):
        return sqlite3.connect(self.db,timeout=2,factory=Connection)
    @contextmanager
    def transaction(self):
        c=self.connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()
    def load(self,c,run):
        row=c.execute('SELECT payload FROM state WHERE run_id=?',(run,)).fetchone()
        if not row: raise ValueError('Unknown run')
        return EngineeringState.model_validate_json(row[0])
    def save(self,c,s):
        c.execute('INSERT OR REPLACE INTO state VALUES(?,?)',(s.run_id,s.model_dump_json()))
    def status(self,run):
        with self.connect() as c:
            s=self.load(c,run)
            approvals=[dict(zip(['id','gate','subject','status','actor','note'],r)) for r in c.execute('SELECT id,gate,subject,status,actor,note FROM approvals WHERE run_id=?',(run,))]
            return {'state':s.model_dump(mode='json'),'approvals':approvals}
    def artifact(self,c,identifier):
        row=c.execute('SELECT payload FROM artifacts WHERE id=?',(identifier,)).fetchone()
        return json.loads(row[0])
