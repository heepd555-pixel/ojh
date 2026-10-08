# -*- coding: utf-8 -*-
"""
store.py -- 학습 기록을 서버에 저장하고, 복습 시기를 계산한다.

예전에는 오답노트를 세션 쿠키(약 4KB 한도)에 담았다. 브라우저를 바꾸거나 쿠키를
지우면 사라지고, "언제 다시 볼지" 같은 정보는 담을 자리가 없었다. 여기서는
SQLite 파일에 이름(사용자) 단위로 문항별 기록을 남긴다.

복습 간격: 맞히면 1일 -> 3일 -> 7일 -> 14일 -> 30일 뒤에 다시 나온다.
          틀리면 연속 정답이 0으로 돌아가고 바로(오늘) 복습 대상이 된다.

저장 위치: 환경변수 DATA_DIR (기본값: 이 폴더의 data/). 클라우드에 올릴 때는
          재배포해도 지워지지 않는 디스크(Render Disk 등)를 DATA_DIR 로 지정해야 한다.
"""
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
INTERVALS = (1, 3, 7, 14, 30)  # 연속 정답 1,2,3,4,5+ 번째 뒤 다음 복습까지의 일수

_DIR = os.environ.get("DATA_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(_DIR, "study.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS progress (
    user   TEXT NOT NULL,
    kind   TEXT NOT NULL,          -- 'theory' | 'silmu'
    qid    TEXT NOT NULL,
    exam   TEXT NOT NULL,
    grp    TEXT NOT NULL DEFAULT '',  -- 통계용 묶음 (이론: 회차, 실무: 유형)
    n_ok   INTEGER NOT NULL DEFAULT 0,
    n_bad  INTEGER NOT NULL DEFAULT 0,
    streak INTEGER NOT NULL DEFAULT 0,
    last_ok INTEGER NOT NULL DEFAULT 0,
    last_at TEXT NOT NULL,
    due    TEXT NOT NULL,
    PRIMARY KEY (user, kind, qid)
);
CREATE INDEX IF NOT EXISTS idx_due ON progress (user, kind, due);
"""


def today():
    return datetime.now(KST).date()


def norm(name):
    """이름 표기 차이(공백·대소문자)로 기록이 갈라지지 않게 맞춘다."""
    return " ".join((name or "").split()).casefold() or "guest"


def _connect():
    os.makedirs(_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def next_due(streak, on=None):
    on = on or today()
    return on + timedelta(days=INTERVALS[min(max(streak, 1), len(INTERVALS)) - 1])


def record(user, kind, qid, exam, grp, ok, on=None):
    """한 문항을 풀었다는 기록. 맞으면 간격을 늘리고, 틀리면 처음부터."""
    user, on = norm(user), on or today()
    with closing(_connect()) as con, con:
        row = con.execute(
            "SELECT streak FROM progress WHERE user=? AND kind=? AND qid=?",
            (user, kind, qid)).fetchone()
        streak = ((row["streak"] if row else 0) + 1) if ok else 0
        due = next_due(streak, on) if ok else on
        if row:
            con.execute(
                "UPDATE progress SET exam=?, grp=?, n_ok=n_ok+?, n_bad=n_bad+?,"
                " streak=?, last_ok=?, last_at=?, due=?"
                " WHERE user=? AND kind=? AND qid=?",
                (exam, grp, int(ok), int(not ok), streak, int(ok),
                 on.isoformat(), due.isoformat(), user, kind, qid))
        else:
            con.execute(
                "INSERT INTO progress (user,kind,qid,exam,grp,n_ok,n_bad,streak,"
                "last_ok,last_at,due) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (user, kind, qid, exam, grp, int(ok), int(not ok), streak,
                 int(ok), on.isoformat(), due.isoformat()))


def due_ids(user, kind, exam=None, on=None):
    """오늘 복습할 문항 id (틀린 문제 + 복습 시기가 된 문제)."""
    sql = "SELECT qid FROM progress WHERE user=? AND kind=? AND due<=?"
    args = [norm(user), kind, (on or today()).isoformat()]
    if exam:
        sql += " AND exam=?"
        args.append(exam)
    with closing(_connect()) as con:
        return {r["qid"] for r in con.execute(sql, args)}


def import_wrong(user, kind, items, on=None):
    """예전 쿠키 오답노트를 한 번 옮겨 담는다. items = [(qid, exam, grp)].
    이미 기록이 있는 문항은 건드리지 않는다."""
    user, on = norm(user), (on or today()).isoformat()
    with closing(_connect()) as con, con:
        for qid, exam, grp in items:
            con.execute(
                "INSERT OR IGNORE INTO progress (user,kind,qid,exam,grp,n_ok,n_bad,"
                "streak,last_ok,last_at,due) VALUES (?,?,?,?,?,0,1,0,0,?,?)",
                (user, kind, qid, exam, grp, on, on))


def stats(user, kind, exam=None, on=None):
    """묶음(grp)별 정답률 (누적 맞힘/틀림 기준).
    반환: [dict(grp, tried, right, wrong, pct, due)]"""
    sql = ("SELECT grp, COUNT(*) AS tried, SUM(n_ok) AS n_ok, SUM(n_bad) AS n_bad,"
           " SUM(CASE WHEN due<=? THEN 1 ELSE 0 END) AS due"
           " FROM progress WHERE user=? AND kind=?")
    args = [(on or today()).isoformat(), norm(user), kind]
    if exam:
        sql += " AND exam=?"
        args.append(exam)
    sql += " GROUP BY grp"
    out = []
    with closing(_connect()) as con:
        for r in con.execute(sql, args):
            ok, bad = r["n_ok"] or 0, r["n_bad"] or 0
            out.append({"grp": r["grp"], "tried": r["tried"],
                        "right": ok, "wrong": bad,
                        "pct": round(ok / (ok + bad) * 100) if ok + bad else 0,
                        "due": r["due"] or 0})
    return out
