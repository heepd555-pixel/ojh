# -*- coding: utf-8 -*-
"""
store.py -- 학습 기록을 서버에 저장하고, 복습 시기를 계산한다.

예전에는 오답노트를 세션 쿠키(약 4KB 한도)에 담았다. 브라우저를 바꾸거나 쿠키를
지우면 사라지고, "언제 다시 볼지" 같은 정보는 담을 자리가 없었다. 여기서는
이름(사용자) 단위로 문항별 기록을 데이터베이스에 남긴다.

복습 간격: 맞히면 1일 -> 3일 -> 7일 -> 14일 -> 30일 뒤에 다시 나온다.
          틀리면 연속 정답이 0으로 돌아가고 바로(오늘) 복습 대상이 된다.

[ 어디에 저장되나 ]
  환경변수 DATABASE_URL 이 있으면 -> PostgreSQL (Neon 등 외부 DB)
  없으면                        -> 이 폴더의 data/study.db (SQLite)

Render 무료 플랜은 파일이 남지 않는다. 재배포뿐 아니라 15분 쉬었다 깨어날 때도
디스크가 초기화되므로, SQLite 로 두면 기록이 매번 사라진다. 그래서 실제 배포에서는
DATABASE_URL 을 반드시 지정한다. SQLite 쪽은 인터넷 없이 로컬에서 돌려보기 위한
길이다.

두 DB 의 SQL 차이는 자리표시자(? 와 %s)뿐이라 _sql() 한 줄로 흡수한다.
"""
import json
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
INTERVALS = (1, 3, 7, 14, 30)  # 연속 정답 1,2,3,4,5+ 번째 뒤 다음 복습까지의 일수

DB_URL = (os.environ.get("DATABASE_URL") or "").strip()
IS_PG = DB_URL.startswith(("postgres://", "postgresql://"))

_DIR = os.environ.get("DATA_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(_DIR, "study.db")

if IS_PG:  # psycopg2 는 배포에서만 필요하므로 여기서만 들여온다
    import psycopg2
    import psycopg2.extras

# user 는 PostgreSQL 예약어라 컬럼 이름을 uid 로 쓴다.
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS progress (
        uid    TEXT NOT NULL,
        kind   TEXT NOT NULL,             -- 'theory' | 'silmu'
        qid    TEXT NOT NULL,
        exam   TEXT NOT NULL,
        grp    TEXT NOT NULL DEFAULT '',  -- 통계용 묶음 (이론: 회차, 실무: 유형)
        n_ok   INTEGER NOT NULL DEFAULT 0,
        n_bad  INTEGER NOT NULL DEFAULT 0,
        streak INTEGER NOT NULL DEFAULT 0,
        last_ok INTEGER NOT NULL DEFAULT 0,
        last_at TEXT NOT NULL,
        due    TEXT NOT NULL,
        PRIMARY KEY (uid, kind, qid)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_due ON progress (uid, kind, due)",
    """CREATE TABLE IF NOT EXISTS sim (    -- 실기 시험 시뮬레이션 한 판의 진행 상태
        run_id  TEXT PRIMARY KEY,
        uid     TEXT NOT NULL,
        state   TEXT NOT NULL,            -- JSON
        created TEXT NOT NULL
    )""",
)

_ready = False


def today():
    return datetime.now(KST).date()


def norm(name):
    """이름 표기 차이(공백·대소문자)로 기록이 갈라지지 않게 맞춘다."""
    return " ".join((name or "").split()).casefold() or "guest"


def _sql(q):
    """자리표시자만 바꾼다. SQLite 는 ?, PostgreSQL 은 %s 를 쓴다."""
    return q.replace("?", "%s") if IS_PG else q


def _connect():
    """새 연결. 표를 아직 안 만들었으면 이때 한 번 만든다."""
    global _ready
    if IS_PG:
        con = psycopg2.connect(
            DB_URL, connect_timeout=10,
            cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        os.makedirs(_DIR, exist_ok=True)
        con = sqlite3.connect(DB_PATH, timeout=10)
        con.row_factory = sqlite3.Row
    if not _ready:
        with con:
            for stmt in _SCHEMA:
                con.cursor().execute(stmt)
        _ready = True
    return con


def _run(con, q, args=()):
    """실행하고 커서를 돌려준다. 두 드라이버 모두 cursor() 를 쓴다."""
    cur = con.cursor()
    cur.execute(_sql(q), tuple(args))
    return cur


def next_due(streak, on=None):
    on = on or today()
    return on + timedelta(days=INTERVALS[min(max(streak, 1), len(INTERVALS)) - 1])


def record(user, kind, qid, exam, grp, ok, on=None):
    """한 문항을 풀었다는 기록. 맞으면 간격을 늘리고, 틀리면 처음부터."""
    user, ok, on = norm(user), bool(ok), on or today()
    with closing(_connect()) as con, con:
        row = _run(con, "SELECT streak FROM progress"
                        " WHERE uid=? AND kind=? AND qid=?",
                   (user, kind, qid)).fetchone()
        streak = ((row["streak"] if row else 0) + 1) if ok else 0
        due = next_due(streak, on) if ok else on
        if row:
            _run(con, "UPDATE progress SET exam=?, grp=?, n_ok=n_ok+?, n_bad=n_bad+?,"
                      " streak=?, last_ok=?, last_at=?, due=?"
                      " WHERE uid=? AND kind=? AND qid=?",
                 (exam, grp, int(ok), int(not ok), streak, int(ok),
                  on.isoformat(), due.isoformat(), user, kind, qid))
        else:
            _run(con, "INSERT INTO progress (uid,kind,qid,exam,grp,n_ok,n_bad,streak,"
                      "last_ok,last_at,due) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (user, kind, qid, exam, grp, int(ok), int(not ok), streak,
                  int(ok), on.isoformat(), due.isoformat()))


def due_ids(user, kind, exam=None, on=None):
    """오늘 복습할 문항 id (틀린 문제 + 복습 시기가 된 문제)."""
    q = "SELECT qid FROM progress WHERE uid=? AND kind=? AND due<=?"
    args = [norm(user), kind, (on or today()).isoformat()]
    if exam:
        q += " AND exam=?"
        args.append(exam)
    with closing(_connect()) as con:
        return {r["qid"] for r in _run(con, q, args).fetchall()}


def import_wrong(user, kind, items, on=None):
    """예전 쿠키 오답노트를 한 번 옮겨 담는다. items = [(qid, exam, grp)].
    이미 기록이 있는 문항은 건드리지 않는다."""
    user, on = norm(user), (on or today()).isoformat()
    with closing(_connect()) as con, con:
        for qid, exam, grp in items:
            _run(con, "INSERT INTO progress (uid,kind,qid,exam,grp,n_ok,n_bad,"
                      "streak,last_ok,last_at,due) VALUES (?,?,?,?,?,0,1,0,0,?,?)"
                      " ON CONFLICT (uid,kind,qid) DO NOTHING",
                 (user, kind, qid, exam, grp, on, on))


def stats(user, kind, exam=None, on=None):
    """묶음(grp)별 정답률 (누적 맞힘/틀림 기준).
    반환: [dict(grp, tried, right, wrong, pct, due)]"""
    q = ("SELECT grp, COUNT(*) AS tried, SUM(n_ok) AS n_ok, SUM(n_bad) AS n_bad,"
         " SUM(CASE WHEN due<=? THEN 1 ELSE 0 END) AS due"
         " FROM progress WHERE uid=? AND kind=?")
    args = [(on or today()).isoformat(), norm(user), kind]
    if exam:
        q += " AND exam=?"
        args.append(exam)
    q += " GROUP BY grp"
    out = []
    with closing(_connect()) as con:
        for r in _run(con, q, args).fetchall():
            ok, bad = r["n_ok"] or 0, r["n_bad"] or 0
            out.append({"grp": r["grp"], "tried": r["tried"],
                        "right": ok, "wrong": bad,
                        "pct": round(ok / (ok + bad) * 100) if ok + bad else 0,
                        "due": r["due"] or 0})
    return out


# ── 시험 시뮬레이션 상태 ────────────────────────────────────────────────
# 답안 전체를 세션 쿠키(4KB)에 담을 수 없어 서버에 JSON 으로 둔다.
def sim_create(user, state):
    run_id = uuid.uuid4().hex[:12]
    with closing(_connect()) as con, con:
        _run(con, "INSERT INTO sim (run_id,uid,state,created) VALUES (?,?,?,?)",
             (run_id, norm(user), json.dumps(state, ensure_ascii=False),
              datetime.now(KST).isoformat(timespec="seconds")))
    return run_id


def sim_load(user, run_id):
    with closing(_connect()) as con:
        row = _run(con, "SELECT state FROM sim WHERE run_id=? AND uid=?",
                   (run_id, norm(user))).fetchone()
    return json.loads(row["state"]) if row else None


def sim_save(user, run_id, state):
    with closing(_connect()) as con, con:
        _run(con, "UPDATE sim SET state=? WHERE run_id=? AND uid=?",
             (json.dumps(state, ensure_ascii=False), run_id, norm(user)))


def demo():
    """자체 점검. DATABASE_URL 이 걸려 있으면 그 DB 에, 없으면 SQLite 에 대고 돈다.

        python store.py
    """
    from datetime import date
    u = "__selfcheck__"
    with closing(_connect()) as con, con:
        _run(con, "DELETE FROM progress WHERE uid=?", (norm(u),))
        _run(con, "DELETE FROM sim WHERE uid=?", (norm(u),))

    d0 = date(2026, 1, 1)
    # 틀리면 오늘 바로 다시, 맞히면 간격이 1 -> 3 -> 7 로 벌어진다
    record(u, "theory", "q1", "전산회계1급", "126회", False, d0)
    assert due_ids(u, "theory", on=d0) == {"q1"}
    record(u, "theory", "q1", "전산회계1급", "126회", True, d0)
    assert due_ids(u, "theory", on=d0) == set()
    assert due_ids(u, "theory", on=d0 + timedelta(days=1)) == {"q1"}
    record(u, "theory", "q1", "전산회계1급", "126회", True, d0 + timedelta(days=1))
    assert due_ids(u, "theory", on=d0 + timedelta(days=3)) == set()
    assert due_ids(u, "theory", on=d0 + timedelta(days=4)) == {"q1"}
    # 틀리면 연속 정답이 0으로 돌아가 바로 복습 대상
    record(u, "theory", "q1", "전산회계1급", "126회", False, d0 + timedelta(days=4))
    assert due_ids(u, "theory", on=d0 + timedelta(days=4)) == {"q1"}

    d4 = d0 + timedelta(days=4)
    # 이름 표기가 달라도 같은 기록
    assert due_ids("  " + u.upper() + " ", "theory", on=d4) == {"q1"}
    # 시험(exam)으로 거르기
    assert due_ids(u, "theory", exam="전산세무2급", on=d4) == set()
    # 종류(kind)가 다르면 섞이지 않는다
    assert due_ids(u, "silmu", on=d4) == set()

    # 이전 오답노트 옮기기 — 이미 있는 문항은 덮어쓰지 않는다
    import_wrong(u, "theory", [("q1", "전산회계1급", "126회"),
                               ("q2", "전산회계1급", "125회")], d0)
    rows = {r["grp"]: r for r in stats(u, "theory")}
    assert set(rows) == {"126회", "125회"}, rows
    assert rows["126회"]["tried"] == 1 and rows["126회"]["wrong"] == 2, rows["126회"]
    assert rows["126회"]["right"] == 2 and rows["126회"]["pct"] == 50, rows["126회"]
    assert rows["125회"]["tried"] == 1 and rows["125회"]["right"] == 0

    # 시뮬레이션 상태는 넣은 그대로 돌아와야 한다 (한글·중첩 포함)
    run = sim_create(u, {"답": [1, 2], "메모": "가나다"})
    assert sim_load(u, run) == {"답": [1, 2], "메모": "가나다"}
    sim_save(u, run, {"답": [3]})
    assert sim_load(u, run) == {"답": [3]}
    assert sim_load("남", run) is None      # 남의 판은 못 읽는다
    assert sim_load(u, "없는run") is None

    with closing(_connect()) as con, con:
        _run(con, "DELETE FROM progress WHERE uid=?", (norm(u),))
        _run(con, "DELETE FROM sim WHERE uid=?", (norm(u),))
    print("OK  store (%s)" % ("PostgreSQL" if IS_PG else "SQLite " + DB_PATH))


if __name__ == "__main__":
    demo()
