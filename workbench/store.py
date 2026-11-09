import json
import os
from contextlib import contextmanager
import sqlite3
from pathlib import Path

ROOT = Path(os.environ.get('CHANLUN_WORKBENCH_ROOT',Path(__file__).resolve().parents[1])).resolve()
STATE = ROOT / 'runtime' / 'workbench'
DB = STATE / 'workbench.sqlite3'


@contextmanager
def connect(path=DB):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA busy_timeout=30000')
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def initialize(path=DB):
    with connect(path) as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS universe(symbol TEXT PRIMARY KEY, code TEXT, name TEXT, exchange TEXT);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, title TEXT, mode TEXT, state TEXT,
          created TEXT, started TEXT, updated TEXT, finished TEXT, settings TEXT,
          total INTEGER, done INTEGER DEFAULT 0, success INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
          pid INTEGER, message TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS items(job_id TEXT, symbol TEXT, state TEXT DEFAULT 'pending',
          summary TEXT, error TEXT, updated TEXT, artifact TEXT,
          PRIMARY KEY(job_id,symbol));
        CREATE INDEX IF NOT EXISTS items_state ON items(job_id,state);
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS results(symbol TEXT, mode TEXT, job_id TEXT, updated TEXT,
          summary TEXT, artifact TEXT, PRIMARY KEY(symbol,mode));
        CREATE TABLE IF NOT EXISTS monitor_quotes(symbol TEXT PRIMARY KEY,received_at TEXT,payload TEXT);
        CREATE TABLE IF NOT EXISTS monitor_structures(symbol TEXT PRIMARY KEY,updated TEXT,payload TEXT);
        CREATE TABLE IF NOT EXISTS monitor_signals(symbol TEXT,signal_id TEXT,state TEXT,signature TEXT,first_seen TEXT,last_seen TEXT,payload TEXT,
          PRIMARY KEY(symbol,signal_id));
        CREATE TABLE IF NOT EXISTS monitor_alerts(id INTEGER PRIMARY KEY AUTOINCREMENT,event_key TEXT UNIQUE,symbol TEXT,created TEXT,kind TEXT,payload TEXT,acknowledged INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS monitor_runs(id TEXT PRIMARY KEY,state TEXT,started TEXT,updated TEXT,payload TEXT);
        ''')
        if 'active' not in [row[1] for row in db.execute('PRAGMA table_info(universe)')]:
            db.execute('ALTER TABLE universe ADD COLUMN active INTEGER NOT NULL DEFAULT 1')


def decode(row):
    if row is None:
        return None
    result = dict(row)
    for key in ('settings', 'summary'):
        if result.get(key):
            result[key] = json.loads(result[key])
    return result


def publish_result(db, code, mode, job_id, updated, summary, artifact):
    """A cached rerun may be newer in wall time but older in market time."""
    value=json.loads(summary)
    previous=db.execute('SELECT summary FROM results WHERE symbol=? AND mode=?',(code,mode)).fetchone()
    if previous and json.loads(previous[0]).get('last_date','')>value.get('last_date',''):
        return False
    db.execute('INSERT OR REPLACE INTO results VALUES(?,?,?,?,?,?)',(code,mode,job_id,updated,summary,artifact))
    return True
