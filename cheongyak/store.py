"""SQLite 저장소: 공고·조건(리비전)·검토 이력·추출 실행·판정 스냅샷."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS notices(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT NOT NULL,
  text TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, notice_id TEXT NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
  agent TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
  summary TEXT, created_at TEXT NOT NULL, finished_at TEXT);
CREATE TABLE IF NOT EXISTS conditions(
  id TEXT NOT NULL, revision INTEGER NOT NULL,
  notice_id TEXT NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
  run_id TEXT, payload TEXT NOT NULL, status TEXT NOT NULL,
  current INTEGER NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(id, revision));
CREATE TABLE IF NOT EXISTS supplies(
  notice_id TEXT NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
  supply TEXT NOT NULL, quote TEXT NOT NULL, run_id TEXT,
  PRIMARY KEY(notice_id, supply));
CREATE TABLE IF NOT EXISTS notes(
  id TEXT PRIMARY KEY, notice_id TEXT NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
  run_id TEXT, text TEXT NOT NULL, quote TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reviews(
  id INTEGER PRIMARY KEY AUTOINCREMENT, condition_id TEXT NOT NULL, revision INTEGER NOT NULL,
  action TEXT NOT NULL, reviewer TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS judgments(
  id TEXT PRIMARY KEY, notice_id TEXT NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
  result TEXT NOT NULL, created_at TEXT NOT NULL);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def new_id(prefix):
    return f'{prefix}-{uuid.uuid4().hex[:10]}'


class Store:
    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.dir / 'cheongyak.sqlite3', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys = ON')
        self.db.executescript(SCHEMA)

    # --- 공고 ---------------------------------------------------------------
    def add_notice(self, title, filename, text):
        nid = new_id('notice')
        with self.lock, self.db:
            self.db.execute('INSERT INTO notices VALUES (?,?,?,?,?,?)',
                            (nid, title, filename, text, hashlib.sha256(text.encode()).hexdigest(), now()))
        return nid

    def notices(self):
        rows = self.db.execute("""
          SELECT n.id, n.title, n.filename, n.sha256, n.created_at, length(n.text) AS chars,
            (SELECT count(*) FROM conditions c WHERE c.notice_id=n.id AND c.current=1 AND c.status='approved') AS approved,
            (SELECT count(*) FROM conditions c WHERE c.notice_id=n.id AND c.current=1 AND c.status='pending') AS pending
          FROM notices n ORDER BY n.created_at DESC, n.id""").fetchall()
        return [dict(r) for r in rows]

    def notice(self, nid):
        row = self.db.execute('SELECT * FROM notices WHERE id=?', (nid,)).fetchone()
        if not row:
            raise KeyError('공고를 찾을 수 없습니다.')
        return dict(row)

    def delete_notice(self, nid):
        self.notice(nid)
        with self.lock, self.db:
            ids = [r[0] for r in self.db.execute('SELECT DISTINCT id FROM conditions WHERE notice_id=?', (nid,))]
            self.db.executemany('DELETE FROM reviews WHERE condition_id=?', [(i,) for i in ids])
            self.db.execute('DELETE FROM notices WHERE id=?', (nid,))

    # --- 추출 실행 ------------------------------------------------------------
    def start_run(self, nid, agent, model):
        rid = new_id('run')
        with self.lock, self.db:
            self.db.execute('INSERT INTO runs(id, notice_id, agent, model, status, created_at) VALUES (?,?,?,?,?,?)',
                            (rid, nid, agent, model, 'running', now()))
        return rid

    def finish_run(self, rid, status, error=None, summary=None):
        with self.lock, self.db:
            self.db.execute('UPDATE runs SET status=?, error=?, summary=?, finished_at=? WHERE id=?',
                            (status, error, json.dumps(summary, ensure_ascii=False) if summary else None, now(), rid))

    def runs(self, nid):
        rows = self.db.execute('SELECT * FROM runs WHERE notice_id=? ORDER BY created_at DESC, id', (nid,)).fetchall()
        return [{**dict(r), 'summary': json.loads(r['summary']) if r['summary'] else None} for r in rows]

    def save_extraction(self, nid, rid, supplies, conditions, notes):
        """추출 결과는 모두 검토 대기로 저장한다. 같은 내용의 현재 조건은 다시 넣지 않는다."""
        with self.lock, self.db:
            existing = {self._key(json.loads(r['payload'])) for r in self.db.execute(
                'SELECT payload FROM conditions WHERE notice_id=? AND current=1 AND status != ?', (nid, 'rejected'))}
            added = 0
            for c in conditions:
                if self._key(c) in existing:
                    continue
                existing.add(self._key(c))
                cid = new_id('cond')
                payload = {**c, 'id': cid}
                self.db.execute('INSERT INTO conditions VALUES (?,?,?,?,?,?,?,?)',
                                (cid, 1, nid, rid, json.dumps(payload, ensure_ascii=False), 'pending', 1, now()))
                added += 1
            for s in supplies:
                self.db.execute('INSERT OR IGNORE INTO supplies VALUES (?,?,?,?)', (nid, s['id'], s['quote'], rid))
            for n in notes:
                self.db.execute('INSERT INTO notes VALUES (?,?,?,?,?)', (new_id('note'), nid, rid, n['text'], n['quote']))
        return added

    @staticmethod
    def _key(c):
        # 그룹이 다르면 AND/OR 의미가 다르므로 별개의 조건으로 본다.
        return (c['supply'], c['group'], c['attr'], c['op'], json.dumps(c['value'], ensure_ascii=False))

    # --- 조건과 검토 ------------------------------------------------------------
    def conditions(self, nid, status=None):
        sql = 'SELECT * FROM conditions WHERE notice_id=? AND current=1'
        args = [nid]
        if status:
            sql += ' AND status=?'
            args.append(status)
        rows = self.db.execute(sql + ' ORDER BY created_at, id', args).fetchall()
        return [{**json.loads(r['payload']), 'revision': r['revision'], 'status': r['status'], 'run_id': r['run_id']} for r in rows]

    def supplies(self, nid):
        return [dict(r) for r in self.db.execute('SELECT supply, quote, run_id FROM supplies WHERE notice_id=? ORDER BY supply', (nid,))]

    def notes(self, nid):
        return [dict(r) for r in self.db.execute('SELECT id, text, quote, run_id FROM notes WHERE notice_id=? ORDER BY rowid', (nid,))]

    def history(self, cid):
        return [dict(r) for r in self.db.execute('SELECT * FROM reviews WHERE condition_id=? ORDER BY id', (cid,))]

    def _current(self, cid):
        row = self.db.execute('SELECT * FROM conditions WHERE id=? AND current=1', (cid,)).fetchone()
        if not row:
            raise KeyError(f'조건을 찾을 수 없습니다: {cid}')
        return row

    def review(self, items, action, reviewer, reason):
        """items: [(condition_id, revision)]. 모두 현재 리비전일 때만 한꺼번에 반영한다."""
        if action not in ('approved', 'rejected', 'pending'):
            raise ValueError('검토 동작은 approved, rejected, pending 중 하나입니다.')
        if not reviewer.strip():
            raise ValueError('검토자 이름을 입력하세요.')
        with self.lock, self.db:
            for cid, revision in items:
                row = self._current(cid)
                if row['revision'] != revision:
                    raise ValueError(f'{cid}가 그 사이 수정되었습니다(r{row["revision"]}). 다시 확인하세요.')
            for cid, revision in items:
                self.db.execute('UPDATE conditions SET status=? WHERE id=? AND revision=?', (action, cid, revision))
                self.db.execute('INSERT INTO reviews(condition_id, revision, action, reviewer, reason, created_at) VALUES (?,?,?,?,?,?)',
                                (cid, revision, action, reviewer.strip(), reason.strip(), now()))
        return len(items)

    def revise(self, cid, revision, payload, reviewer, reason):
        """수정은 새 리비전을 만들고 다시 검토 대기로 돌린다."""
        with self.lock, self.db:
            row = self._current(cid)
            if row['revision'] != revision:
                raise ValueError('그 사이 수정된 조건입니다. 다시 불러오세요.')
            new_rev = revision + 1
            self.db.execute('UPDATE conditions SET current=0 WHERE id=?', (cid,))
            self.db.execute('INSERT INTO conditions VALUES (?,?,?,?,?,?,?,?)',
                            (cid, new_rev, row['notice_id'], row['run_id'], json.dumps({**payload, 'id': cid}, ensure_ascii=False),
                             'pending', 1, now()))
            self.db.execute('INSERT INTO reviews(condition_id, revision, action, reviewer, reason, created_at) VALUES (?,?,?,?,?,?)',
                            (cid, new_rev, 'revised', reviewer.strip() or '-', reason.strip(), now()))
        return new_rev

    # --- 판정 ---------------------------------------------------------------
    def save_judgment(self, nid, result):
        jid = new_id('judge')
        with self.lock, self.db:
            self.db.execute('INSERT INTO judgments VALUES (?,?,?,?)', (jid, nid, json.dumps(result, ensure_ascii=False), now()))
        return jid

    def judgments(self, nid):
        rows = self.db.execute('SELECT * FROM judgments WHERE notice_id=? ORDER BY created_at DESC, id', (nid,)).fetchall()
        return [{'id': r['id'], 'created_at': r['created_at'], **json.loads(r['result'])} for r in rows]
