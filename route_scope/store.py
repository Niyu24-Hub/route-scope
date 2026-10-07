import json
import os
import sqlite3
from pathlib import Path

BODY_FIELDS = {'request_body','response_body','request_headers','response_headers','output_text','events','forwarded_request_body','forwarded_request_headers'}


def compact(record):
    return {k:v for k,v in record.items() if k not in BODY_FIELDS}


class Store:
    def __init__(self, directory, retention=1000, recover=True, database_dir=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        salt_path = self.directory / ".identity-salt"
        try:
            with salt_path.open("xb") as f:
                f.write(os.urandom(32))
            salt_path.chmod(0o600)
        except FileExistsError:
            pass
        self.salt = salt_path.read_bytes()
        self.retention = retention
        self.database_directory=Path(database_dir) if database_dir is not None else self.directory
        self.database_directory.mkdir(parents=True,exist_ok=True)
        self.db = sqlite3.connect(self.database_directory / "captures.db")
        self.db.execute("pragma journal_mode=WAL")
        self.db.execute("create table if not exists captures (id text primary key, started text, data text)")
        cols={row[1] for row in self.db.execute('pragma table_info(captures)')}
        if 'summary' not in cols:
            self.db.execute('alter table captures add column summary text')
        self.db.execute("create index if not exists capture_started on captures(started)")
        self.db.commit()
        # Large prompt/response bodies are only read when opening details or exporting.
        cursor=self.db.execute('select id,data from captures where summary is null')
        while batch:=cursor.fetchmany(20):
            self.db.executemany('update captures set summary=? where id=?',[
                (json.dumps(compact(json.loads(data)),ensure_ascii=False),capture_id) for capture_id,data in batch])
        self.db.commit()
        # A previous process may have stopped before observing stream termination.
        for capture_id,data in self.db.execute("select id,summary from captures").fetchall() if recover else []:
            if json.loads(data).get("state") in ("sending", "streaming"):
                record = self.get(capture_id)
                record.update(state="interrupted", verdict="incomplete", verdict_label="进程中止，未完整捕获")
                self.save(record)

    def save(self, record):
        self.db.execute("insert into captures (id,started,data,summary) values (?, ?, ?, ?) on conflict(id) do update set data=excluded.data,summary=excluded.summary", (record["id"], record["started_at"], json.dumps(record, ensure_ascii=False),json.dumps(compact(record),ensure_ascii=False)))
        self.db.execute("delete from captures where id in (select id from captures order by started desc limit -1 offset ?)", (self.retention,))
        self.db.commit()

    def list(self, limit=1000, full=False):
        column='data' if full else 'summary'
        return [json.loads(x[0]) for x in self.db.execute(f"select {column} from captures order by started desc limit ?", (min(limit, self.retention),))]

    def get(self, capture_id):
        row = self.db.execute("select data from captures where id=?", (capture_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        self.db.close()
