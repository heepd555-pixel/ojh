# -*- coding: utf-8 -*-
"""
web_app.py -- ERP 기출문제 퀴즈를 휴대폰 브라우저로 풀 수 있게 해주는 웹앱

quiz.py 의 문제 로딩/필터링/오답노트 로직을 그대로 재사용하고, 화면만
콘솔 대신 휴대폰 브라우저용 페이지로 바꾼 버전입니다. 진짜 설치형 앱은
아니지만, 같은 와이파이에 연결된 휴대폰에서 접속해서 "홈 화면에 추가"하면
아이콘이 생겨서 앱처럼 쓸 수 있습니다.

[ 사용법 ]
    pip install flask
    python web_app.py
    -> 화면에 뜨는 "휴대폰에서 접속할 주소" 를 휴대폰 브라우저에 입력

[ 참고 ]
이 서버는 같은 와이파이 안에서만 접속 가능합니다 (외부 인터넷에 공개되지 않음).
"""
import json
import os
import random
import re
import socket
import time
from datetime import timedelta

from flask import Flask, redirect, render_template, request, session, url_for

import accounts
import grader
import patterns
import store
from quiz import filter_questions, load_questions, round_sort_key

# 화면에 노출할 시험. 전산회계1급·전산세무2급만 쓴다.
# (ERP/FAT/TAT/분개연습 데이터는 questions.json 에 그대로 있고, 다시 쓰려면
#  아래 딕셔너리에 한 줄 추가하면 된다.)
EXAM_LABELS = {
    "전산회계1급": "전산회계1급",
    "전산세무2급": "전산세무2급",
}
DEFAULT_EXAM = "전산회계1급"
_YEAR_MONTH_ROUND_EXAMS = {"erp", "ERP실기"}


def is_correct(selected, answer):
    """복수정답 인정 문항은 정답이 "1,3" 처럼 저장된다."""
    return bool(selected) and selected in (answer or "").split(",")


def _round_key(exam, round_label):
    """회차 정렬 키. ERP는 'YYYY년 M월' 형식(round_sort_key)을,
    전산회계1급처럼 'N회' 형식만 있는 시험은 회차 번호로 정렬한다."""
    if exam in _YEAR_MONTH_ROUND_EXAMS:
        return round_sort_key(round_label)
    m = re.search(r"\d+", round_label)
    return (int(m.group()) if m else -1,)

app = Flask(__name__)
# 클라우드는 워커 프로세스가 여러 개 뜰 수 있어서, os.urandom() 으로 매번 새로
# 만들면 요청이 다른 워커로 갈 때마다 세션이 깨진다. 배포 시 SECRET_KEY 환경변수를
# 넣어주면 그걸 쓰고, 로컬 개인용 실행일 때만 임시 키를 씀.
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24)
# 오답노트를 세션 쿠키(휴대폰 브라우저)에 저장하므로, 서버가 재시작/재배포돼도
# 사라지지 않게 유지 기간을 길게 둠 (기본은 브라우저 닫으면 만료).
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)

QUESTIONS = load_questions()

@app.template_global()
def vat_reason(vat, question=""):
    """이 거래가 왜 그 유형코드인지 — 판단 경로. 유형코드 구조에서 나온다."""
    return accounts.vat_reason(vat, question)


@app.template_global()
def vat_confused(vat):
    """이 유형과 헷갈리는 짝, 그리고 둘을 가르는 기준."""
    return accounts.vat_confused(vat)


@app.template_global()
def why_side(entry_sets):
    """분개 한 줄마다 '왜 차변인가 / 왜 대변인가'. 거래 8요소에서 자동으로 나온다."""
    return accounts.why_side(entry_sets or [])


@app.template_global()
def eight_elements():
    return accounts.EIGHT_ELEMENTS


@app.template_global()
def acct_code(name):
    """KcLep에 찍어야 하는 계정과목 코드. 모르면 None (화면에는 '—'로 뜬다)."""
    return accounts.code_of(name)


@app.template_global()
def acct_code_note(name):
    """대손충당금처럼 코드가 하나로 안 정해지는 계정의 안내."""
    return accounts.code_note(name)


@app.template_global()
def account_gloss(q):
    """이론 문제 본문에 나온 계정과목의 뜻. 실무 카드와 같은 사전을 쓴다.
    초보자는 '선급비용'이 뭔지 모른 채로는 해설을 읽어도 남는 게 없다."""
    parts = [q.get("stem") or "", " ".join((q.get("options") or {}).values()),
             q.get("explanation") or "", q.get("interp") or ""]
    return accounts.find_accounts(" ".join(parts))

TYPE_LABEL = {"theory": "이론", "bunkae": "분개연습", "practical": "실기"}

def _user():
    """기록을 구분하는 이름. 입력한 적이 없으면 'guest'."""
    return store.norm(session.get("name"))


def _migrate_cookie_notes():
    """예전 버전이 쿠키에 쌓아 둔 오답노트를 서버 기록으로 한 번 옮긴다."""
    old_theory = session.pop("wrong_review", None)
    old_silmu = session.pop("silmu_review", None)
    if old_theory:
        store.import_wrong(_user(), "theory", [
            (QUESTIONS[i]["id"], QUESTIONS[i].get("exam", "erp"), QUESTIONS[i]["round"])
            for i in old_theory if 0 <= i < len(QUESTIONS)])
    if old_silmu:
        store.import_wrong(_user(), "silmu", [
            (PRACTICE[i]["id"], PRACTICE[i]["exam"], _PAT_OF.get(PRACTICE[i]["id"], ""))
            for i in old_silmu if 0 <= i < len(PRACTICE)])


def _theory_due(exam):
    """오늘 복습할 이론 문항 id (틀린 문제 + 복습 시기가 된 문제)."""
    return store.due_ids(_user(), "theory", exam)


def _record_theory(q, ok):
    store.record(_user(), "theory", q["id"], q.get("exam", "erp"), q["round"], ok)


class _Args:
    """quiz.filter_questions() 는 argparse.Namespace 모양을 기대하므로 흉내만 냄."""
    def __init__(self, subject, level, round_, combo=None):
        self.subject = subject or None
        self.level = level or None
        self.round = round_ or None
        self.combo = combo or None


def _pick_exam(form_or_args):
    exam = form_or_args.get("exam") or session.get("exam") or DEFAULT_EXAM
    if exam not in EXAM_LABELS:
        exam = DEFAULT_EXAM
    session["exam"] = exam
    return exam


PASS_PCT = 70  # 합격 기준(참고용): 100점 만점에 70점 이상
LEVEL_ORDER = ["하", "중", "상"]


def _catalog(exam):
    qs = [q for q in QUESTIONS if q.get("exam", "erp") == exam]
    subjects = sorted({q["subject"] for q in qs})
    levels = {q["level"] for q in qs}
    if levels <= set(LEVEL_ORDER):
        levels = [l for l in LEVEL_ORDER if l in levels]
    else:
        levels = sorted(levels)
    rounds = sorted({q["round"] for q in qs}, key=lambda r: _round_key(exam, r), reverse=True)
    combos = sorted({q["combo"] for q in qs if q.get("combo")})
    return subjects, levels, rounds, combos


def _local_ip():
    """같은 와이파이의 휴대폰이 접속할 이 PC의 사설 IP 주소를 추정."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


@app.route("/")
def setup():
    exam = _pick_exam(request.args)
    subjects, levels, rounds, combos = _catalog(exam)
    name = session.get("name", "")
    _migrate_cookie_notes()
    wrong_count = len(_theory_due(exam))
    return render_template(
        "setup.html", subjects=subjects, levels=levels, rounds=rounds, combos=combos,
        wrong_count=wrong_count, name=name,
        exam=exam, exam_labels=EXAM_LABELS,
    )


SUBJECT_ORDER = ["회계", "생산", "인사", "물류"]


@app.route("/exam")
def exam_setup():
    """시험모드: 과목+급수와 범위(특정 회차 또는 연도 전체)를 골라서 그 이론
    문제를 실제 시험처럼 풀고(문제마다 정답 공개 없음), 끝까지 다 풀면
    한번에 채점+해설을 보여준다."""
    exam = _pick_exam(request.args)
    theory_qs = [
        q for q in QUESTIONS
        if q.get("exam", "erp") == exam and q["type"] == "theory" and q.get("answer")
    ]

    present_subjects = {q["subject"] for q in theory_qs}
    subject_levels = [
        {"value": f"{s}|{l}", "label": f"{s}{l}"}
        for s in SUBJECT_ORDER if s in present_subjects
        for l in sorted({q["level"] for q in theory_qs if q["subject"] == s})
    ] or [
        {"value": f"{s}|{l}", "label": f"{s}{l}"}
        for s, l in sorted({(q["subject"], q["level"]) for q in theory_qs})
    ]

    # ERP는 회차가 "YYYY년 M월" 형식이라 연도로 묶어서 "연도 전체" 옵션을 만들 수
    # 있지만, 전산회계1급처럼 "N회" 형식뿐인 시험은 묶을 연도 개념이 없어서 스킵.
    scopes = []
    if exam == "erp":
        rounds_by_year = {}
        for q in theory_qs:
            year = round_sort_key(q["round"])[0]
            rounds_by_year.setdefault(year, set()).add(q["round"])
        for year in sorted(rounds_by_year, reverse=True):
            scopes.append({"value": f"year:{year}", "label": f"{year}년 전체", "is_year": True})
            for r in sorted(rounds_by_year[year], key=round_sort_key, reverse=True):
                scopes.append({"value": f"round:{r}", "label": f"　{r}", "is_year": False})
    else:
        for r in sorted({q["round"] for q in theory_qs}, key=lambda r: _round_key(exam, r), reverse=True):
            scopes.append({"value": f"round:{r}", "label": r, "is_year": False})

    return render_template(
        "exam_setup.html", subject_levels=subject_levels, scopes=scopes,
        name=session.get("name", ""), exam=exam, exam_labels=EXAM_LABELS,
    )


@app.route("/exam/start", methods=["POST"])
def exam_start():
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("exam_setup"))
    session["name"] = name
    session.permanent = True

    exam = _pick_exam(request.form)
    subject_level = request.form.get("subject_level", "")
    sl_parts = subject_level.split("|")
    scope = request.form.get("scope", "")
    scope_parts = scope.split(":", 1)
    if len(sl_parts) != 2 or len(scope_parts) != 2:
        return redirect(url_for("exam_setup"))
    subject, level = sl_parts
    scope_type, scope_value = scope_parts

    def matches_scope(q):
        if scope_type == "year":
            return round_sort_key(q["round"])[0] == int(scope_value)
        return q["round"] == scope_value

    pool = [
        q for q in QUESTIONS
        if q.get("exam", "erp") == exam and q["subject"] == subject and q["level"] == level
        and q["type"] == "theory" and q.get("answer") and matches_scope(q)
    ]
    pool.sort(key=lambda q: (_round_key(exam, q["round"]), q["num"]))

    try:
        minutes = max(0, int(request.form.get("minutes") or 0))
    except ValueError:
        minutes = 0

    session["exam_ids"] = [q["id"] for q in pool]
    session["exam_idx"] = 0
    session["exam_answers"] = {}
    session["exam_recorded"] = False
    session["exam_deadline"] = int(time.time()) + minutes * 60 if minutes else 0
    return redirect(url_for("exam_quiz"))


def _exam_left():
    """시험 남은 초. 시간제한이 없으면 None, 지났으면 0."""
    deadline = session.get("exam_deadline", 0)
    return None if not deadline else max(0, deadline - int(time.time()))


@app.route("/exam/quiz")
def exam_quiz():
    ids = session.get("exam_ids")
    if not ids:
        return redirect(url_for("exam_setup"))

    idx = session.get("exam_idx", 0)
    left = _exam_left()
    if idx >= len(ids) or left == 0:
        return redirect(url_for("exam_result"))

    q = _question_by_id(ids[idx])
    return render_template(
        "exam_quiz.html", q=q, idx=idx + 1, total=len(ids),
        is_last=(idx + 1 == len(ids)), left=left,
    )


@app.route("/exam/answer", methods=["POST"])
def exam_answer():
    ids = session.get("exam_ids")
    idx = session.get("exam_idx", 0)
    if not ids or idx >= len(ids):
        return redirect(url_for("exam_setup"))

    if _exam_left() == 0:  # 시간이 끝난 뒤 들어온 답은 받지 않는다
        return redirect(url_for("exam_result"))

    qid = ids[idx]
    selected = request.form.get("choice")
    answers = session.get("exam_answers", {})
    answers[qid] = selected
    session["exam_answers"] = answers
    session["exam_idx"] = idx + 1
    return redirect(url_for("exam_quiz"))


@app.route("/exam/result")
def exam_result():
    ids = session.get("exam_ids") or []
    answers = session.get("exam_answers", {})
    if not ids:
        return redirect(url_for("exam_setup"))

    rows = []
    score = 0
    wrong_ids = []
    for qid in ids:
        q = _question_by_id(qid)
        selected = answers.get(qid)
        correct = is_correct(selected, q["answer"])
        if correct:
            score += 1
        else:
            wrong_ids.append(qid)
        rows.append({"q": q, "selected": selected, "correct": correct})

    # 결과 화면은 새로고침해도 다시 열리므로, 기록은 시험 한 번에 한 번만 남긴다.
    if not session.get("exam_recorded"):
        for row in rows:
            _record_theory(row["q"], row["correct"])
        session["exam_recorded"] = True

    pct = round(score / len(ids) * 100) if ids else 0
    return render_template(
        "exam_result.html", rows=rows, score=score, total=len(ids), pct=pct,
        passed=pct >= PASS_PCT, pass_pct=PASS_PCT,
        timed_out=_exam_left() == 0,
    )


@app.route("/start", methods=["POST"])
def start():
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("setup"))
    session["name"] = name
    session.permanent = True

    exam = _pick_exam(request.form)
    exam_qs = [q for q in QUESTIONS if q.get("exam", "erp") == exam]
    review = request.form.get("review") == "on"
    count = int(request.form.get("count") or 20)

    if review:
        due = _theory_due(exam)
        pool = [q for q in exam_qs if q["id"] in due and q.get("answer")]
    else:
        args = _Args(
            request.form.get("subject"), request.form.get("level"), request.form.get("round"),
            request.form.get("combo"),
        )
        pool = filter_questions(exam_qs, args)

    random.shuffle(pool)
    picked = pool[:count]

    session["ids"] = [q["id"] for q in picked]
    session["idx"] = 0
    session["score"] = 0
    session["wrong_ids"] = []
    session["revealed"] = False
    session["selected"] = None
    return redirect(url_for("quiz"))


def _question_by_id(qid):
    for q in QUESTIONS:
        if q["id"] == qid:
            return q
    return None


@app.route("/quiz")
def quiz():
    ids = session.get("ids")
    if not ids:
        return redirect(url_for("setup"))

    idx = session.get("idx", 0)
    if idx >= len(ids):
        return redirect(url_for("summary"))

    q = _question_by_id(ids[idx])
    return render_template(
        "quiz.html", q=q, type_label=TYPE_LABEL.get(q["type"], q["type"]),
        idx=idx + 1, total=len(ids),
        revealed=session.get("revealed", False),
        selected=session.get("selected"),
        is_last=(idx + 1 == len(ids)),
    )


@app.route("/reveal", methods=["POST"])
def reveal():
    """분개연습처럼 객관식이 아닌(자가채점) 문제 전용: 점수에 반영하지 않고
    정답만 화면에 펼쳐 보여준다. 맞았는지 틀렸는지는 이후 /answer 에서
    사용자가 직접 눌러서 채점한다."""
    session["revealed"] = True
    return redirect(url_for("quiz"))


@app.route("/answer", methods=["POST"])
def answer():
    ids = session.get("ids")
    idx = session.get("idx", 0)
    if not ids or idx >= len(ids):
        return redirect(url_for("setup"))

    q = _question_by_id(ids[idx])
    selected = request.form.get("choice")
    session["selected"] = selected
    session["revealed"] = True
    ok = is_correct(selected, q["answer"])
    if q.get("answer"):  # 정답이 없는 문항은 채점 대상이 아니라 기록하지 않는다
        _record_theory(q, ok)
    if ok:
        session["score"] = session.get("score", 0) + 1
    else:
        session["wrong_ids"] = session.get("wrong_ids", []) + [q["id"]]
    return redirect(url_for("quiz"))


@app.route("/next", methods=["POST"])
def next_question():
    session["idx"] = session.get("idx", 0) + 1
    session["revealed"] = False
    session["selected"] = None
    return redirect(url_for("quiz"))


@app.route("/summary")
def summary():
    ids = session.get("ids") or []
    score = session.get("score", 0)
    wrong_ids = session.get("wrong_ids", [])
    attempted = score + len(wrong_ids)
    wrong_qs = [_question_by_id(i) for i in wrong_ids]

    pct = round(score / attempted * 100) if attempted else 0
    return render_template(
        "summary.html", score=score, attempted=attempted, pct=pct, wrong_qs=wrong_qs,
    )


# ─────────────────────────────────────────────────────────────────────
# 실무 카드 (전산회계1급 · 전산세무2급 확정답안)
#
# 이론처럼 객관식으로 채점할 수 없는 실무시험용 화면. 거래를 보고 머리로 분개한
# 다음 답을 펼쳐서 스스로 맞춰 보는 방식이고, 부가세신고서·원천징수처럼 분개로
# 떨어지지 않는 문항은 확정답안에 실린 KcLep 화면을 그대로 보여준다.
# 데이터는 extract_official.py 가 official.json 으로 만들어 둔다.
# ─────────────────────────────────────────────────────────────────────
_OFFICIAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "official.json")
try:
    with open(_OFFICIAL, encoding="utf-8") as _f:
        PRACTICE = json.load(_f)
except FileNotFoundError:
    PRACTICE = []

# 세션 쿠키가 4KB를 넘으면 통째로 날아가므로, 이론 오답노트와 같은 방식으로
# 긴 문항 id 대신 정수 인덱스를 담는다.
_P_INDEX = {p["id"]: i for i, p in enumerate(PRACTICE)}
SECTION_LABELS = {
    ("전산회계1급", "문제1"): "기초정보·전기분",
    ("전산회계1급", "문제2"): "일반전표",
    ("전산회계1급", "문제3"): "매입매출전표",
    ("전산회계1급", "문제4"): "오류수정",
    ("전산회계1급", "문제5"): "결산정리",
    ("전산회계1급", "문제6"): "장부조회",
    ("전산세무2급", "문제1"): "일반전표",
    ("전산세무2급", "문제2"): "매입매출전표",
    ("전산세무2급", "문제3"): "부가가치세",
    ("전산세무2급", "문제4"): "결산정리",
    ("전산세무2급", "문제5"): "원천징수",
}


def _section_label(p):
    return SECTION_LABELS.get((p["exam"], p["section"]), p["section"])


def _practice_catalog(exam):
    qs = [p for p in PRACTICE if p["exam"] == exam]
    rounds = sorted({p["round"] for p in qs},
                    key=lambda r: int(re.search(r"\d+", r).group()), reverse=True)
    seen, sections = set(), []
    for p in sorted(qs, key=lambda p: p["section"]):
        if p["section"] not in seen:
            seen.add(p["section"])
            sections.append((p["section"], _section_label(p)))
    return rounds, sections


# 386문항을 유형(부가세 유형코드 / 결합관계)으로 묶은 교재용 목차.
# 문항 자체는 그대로 두고 보는 순서만 바꾸는 것이라 한 번만 만들어 두면 된다.
PATTERNS = patterns.build(PRACTICE, _section_label)
_PAT_BY_SLUG = {g["slug"]: g for g in PATTERNS}


# 문항 id -> 유형 이름(통계 묶음). 약점 화면이 "어떤 유형을 자주 틀리나"를 보여준다.
_PAT_OF = {PRACTICE[i]["id"]: g["title"] for g in PATTERNS for i in g["items"]}


def _practice_review():
    """오늘 복습할 실무 카드 id (헷갈렸던 것 + 복습 시기가 된 것)."""
    return store.due_ids(_user(), "silmu")


@app.route("/silmu")
def silmu_setup():
    exam = request.args.get("exam")
    if exam not in ("전산회계1급", "전산세무2급"):
        exam = "전산회계1급"
    _migrate_cookie_notes()
    rounds, sections = _practice_catalog(exam)
    return render_template(
        "silmu_setup.html", exam=exam, rounds=rounds, sections=sections,
        hard_count=len(_practice_review()), name=session.get("name", ""), total=len([p for p in PRACTICE if p["exam"] == exam]),
    )


@app.route("/silmu/start", methods=["GET", "POST"])
def silmu_start():
    """GET도 받는다: /silmu/start?exam=전산세무2급&section=문제2 처럼 특정 묶음을
    바로 열 수 있어 휴대폰에 북마크해 두고 쓸 수 있다."""
    f = request.values
    if f.get("name", "").strip():
        session["name"] = f["name"].strip()
        session.permanent = True
    exam = f.get("exam", "전산회계1급")
    if f.get("review") == "on":
        pool = [p for p in PRACTICE if p["id"] in _practice_review()]
    elif f.get("pat"):
        # 유형 교재에서 "이 유형만 카드로" 로 넘어온 경우
        g = _PAT_BY_SLUG.get(f["pat"])
        pool = [PRACTICE[i] for i in g["items"]] if g else []
    else:
        rnd = f.get("round") or ""
        sec = f.get("section") or ""
        pool = [p for p in PRACTICE if p["exam"] == exam
                and (not rnd or p["round"] == rnd)
                and (not sec or p["section"] == sec)]
    if f.get("shuffle") == "on":
        random.shuffle(pool)
    else:
        pool.sort(key=lambda p: (-int(re.search(r"\d+", p["round"]).group()),
                                 p["section"], p["no"]))
    count = int(f.get("count") or 0)
    if count:
        pool = pool[:count]
    if not pool:
        return redirect(url_for("silmu_setup", exam=exam))

    session["silmu_ids"] = [_P_INDEX[p["id"]] for p in pool]
    session["silmu_idx"] = 0
    session["silmu_reveal"] = False
    session["silmu_hard"] = []
    session["silmu_ok"] = 0
    session.permanent = True
    return redirect(url_for("silmu_card"))


@app.route("/silmu/card")
def silmu_card():
    idxs = session.get("silmu_ids")
    if not idxs:
        return redirect(url_for("silmu_setup"))
    i = session.get("silmu_idx", 0)
    if i >= len(idxs):
        return redirect(url_for("silmu_done"))
    p = PRACTICE[idxs[i]]
    return render_template(
        "silmu_card.html", p=p, section_label=_section_label(p),
        idx=i + 1, total=len(idxs), revealed=session.get("silmu_reveal", False),
        is_last=(i + 1 == len(idxs)),
        # 분개만 보여주면 왜 그렇게 되는지 알 수 없다. 결합관계(무엇이 늘고 줄었나)와
        # 쓰인 계정의 뜻, 유형코드 설명을 같이 붙인다. 전부 계정과목 사전에서 나온다.
        combos=accounts.describe(p["entry_sets"]),
        glossary=accounts.glossary(p["entry_sets"]),
        vat_kind=accounts.vat_type((p.get("vat") or {}).get("유형", "")),
    )


@app.route("/silmu/flip", methods=["POST"])
def silmu_flip():
    session["silmu_reveal"] = True
    return redirect(url_for("silmu_card"))


@app.route("/silmu/mark", methods=["POST"])
def silmu_mark():
    """스스로 채점하고 다음 장으로. '헷갈림'은 다음 번에 그것만 모아 볼 수 있게 남긴다."""
    idxs = session.get("silmu_ids") or []
    i = session.get("silmu_idx", 0)
    if i < len(idxs):
        p = PRACTICE[idxs[i]]
        hard = request.form.get("how") == "hard"
        store.record(_user(), "silmu", p["id"], p["exam"], _PAT_OF.get(p["id"], ""), not hard)
        if hard:
            session["silmu_hard"] = session.get("silmu_hard", []) + [idxs[i]]
        else:
            session["silmu_ok"] = session.get("silmu_ok", 0) + 1
    session["silmu_idx"] = i + 1
    session["silmu_reveal"] = False
    return redirect(url_for("silmu_card"))


@app.route("/silmu/done")
def silmu_done():
    idxs = session.get("silmu_ids") or []
    hard = [PRACTICE[i] for i in session.get("silmu_hard", []) if i < len(PRACTICE)]
    return render_template(
        "silmu_done.html", total=len(idxs), ok=session.get("silmu_ok", 0),
        hard=hard, section_label=_section_label, left=len(_practice_review()),
    )


# ─────────────────────────────────────────────────────────────────────
# 실기 환경: 전표 직접 입력 연습(/lab) + 회차 단위 시험 시뮬레이션(/sim)
#
# 실무 카드는 답을 펼쳐 놓고 스스로 맞춰 보는 방식이라, 실제 시험에서 손으로 입력하는
# 감각이 안 길러진다. 여기서는 KcLep 입력칸과 비슷한 폼에 직접 입력하고
# grader.py 가 정답 분개와 비교해 채점한다. 시뮬레이션은 이론 30점 + 실무 70점으로
# 회차 한 세트를 제한 시간 안에 풀게 한다.
# ─────────────────────────────────────────────────────────────────────
_P_BY_ID = {p["id"]: p for p in PRACTICE}
_Q_BY_ID = {q["id"]: q for q in QUESTIONS}
EXAM_MINUTES = {"전산회계1급": 60, "전산세무2급": 90}  # 실제 시험 시간(이론+실무)
THEORY_TOTAL = 30       # 이론 배점. 실무는 회차마다 70점.
SIM_EXAMS = ("전산회계1급", "전산세무2급")
SIM_GRACE = 30          # 시간 종료 직후 마지막 답을 저장해 주는 유예(초)


@app.template_global()
def acct_options():
    """계정 입력칸 자동완성 목록 [(이름, 코드)]."""
    return sorted(accounts.CODES.items(), key=lambda kv: kv[1])


@app.template_global()
def vat_options():
    return [(c, "%s.%s (%s)" % (c, v[0], v[1])) for c, v in sorted(accounts.VAT_TYPES.items())]


@app.template_global()
def entry_rows(sub):
    rows = list((sub or {}).get("rows") or [])
    if rows:
        return rows
    return ([{"side": "debit", "acct": "", "amt": ""} for _ in range(2)]
            + [{"side": "credit", "acct": "", "amt": ""} for _ in range(2)])


def _parse_entry_form(form):
    """입력 폼 -> grader 가 받는 제출물. 빈 줄은 버려 세션 쿠키를 아낀다."""
    rows = []
    for s, a, m in zip(form.getlist("side"), form.getlist("acct"), form.getlist("amt")):
        a, m = a.strip()[:40], m.strip()[:20]
        if a or m:
            rows.append({"side": "credit" if s == "credit" else "debit", "acct": a, "amt": m})
    sub = {"rows": rows[:14]}
    if "vat_type" in form:
        sub["vat"] = {"type": form.get("vat_type", "")[:4],
                      "supply": form.get("vat_supply", "").strip()[:20],
                      "vat": form.get("vat_vat", "").strip()[:20]}
    return sub


def _round_no(r):
    m = re.search(r"\d+", str(r or ""))
    return int(m.group()) if m else -1


# ── 전표 입력 연습 ────────────────────────────────────────────────────
def _lab_pool(exam):
    return [(i, p) for i, p in enumerate(PRACTICE) if p["exam"] == exam and grader.gradable(p)]


@app.route("/lab")
def lab_setup():
    exam = request.args.get("exam")
    if exam not in SIM_EXAMS:
        exam = "전산회계1급"
    _migrate_cookie_notes()
    pool = _lab_pool(exam)
    sections, seen = [], set()
    for _, p in sorted(pool, key=lambda ip: ip[1]["section"]):
        if p["section"] not in seen:
            seen.add(p["section"])
            sections.append((p["section"], _section_label(p)))
    due = _practice_review()
    return render_template(
        "lab_setup.html", exam=exam, sections=sections, name=session.get("name", ""),
        rounds=sorted({p["round"] for _, p in pool}, key=_round_no, reverse=True),
        total=len(pool), due=len([1 for _, p in pool if p["id"] in due]),
    )


@app.route("/lab/start", methods=["POST"])
def lab_start():
    f = request.form
    if f.get("name", "").strip():
        session["name"] = f["name"].strip()
        session.permanent = True
    exam = f.get("exam") if f.get("exam") in SIM_EXAMS else "전산회계1급"
    due = _practice_review() if f.get("review") == "on" else None
    pool = [(i, p) for i, p in _lab_pool(exam)
            if (not f.get("section") or p["section"] == f["section"])
            and (not f.get("round") or p["round"] == f["round"])
            and (due is None or p["id"] in due)]
    if f.get("shuffle") == "on":
        random.shuffle(pool)
    else:
        pool.sort(key=lambda ip: (-_round_no(ip[1]["round"]), ip[1]["section"], _round_no(ip[1]["no"])))
    try:
        count = max(1, int(f.get("count") or 10))
    except ValueError:
        count = 10
    pool = pool[:count]
    if not pool:
        return redirect(url_for("lab_setup", exam=exam))
    session["lab_ids"] = [i for i, _ in pool]
    session["lab_idx"] = 0
    session["lab_sub"] = None
    session["lab_pts"], session["lab_max"], session["lab_full"] = 0.0, 0.0, 0
    session.permanent = True
    return redirect(url_for("lab_q"))


@app.route("/lab/q")
def lab_q():
    ids = session.get("lab_ids")
    if not ids:
        return redirect(url_for("lab_setup"))
    i = session.get("lab_idx", 0)
    if i >= len(ids):
        return redirect(url_for("lab_done"))
    p = PRACTICE[ids[i]]
    sub = session.get("lab_sub")
    labels = [s.get("label") for s in p.get("entry_sets") or []]
    return render_template(
        "lab_q.html", p=p, section_label=_section_label(p), idx=i + 1, total=len(ids),
        sub=sub, r=grader.grade(p, sub) if sub is not None else None,
        is_last=(i + 1 == len(ids)), has_fix="수정 후" in labels,
    )


@app.route("/lab/grade", methods=["POST"])
def lab_grade():
    ids = session.get("lab_ids")
    i = session.get("lab_idx", 0)
    if not ids or i >= len(ids) or session.get("lab_sub") is not None:
        return redirect(url_for("lab_q"))  # 새로고침으로 같은 답을 두 번 세지 않는다
    p = PRACTICE[ids[i]]
    sub = _parse_entry_form(request.form)
    r = grader.grade(p, sub)
    store.record(_user(), "silmu", p["id"], p["exam"], _PAT_OF.get(p["id"], ""), r["full"])
    session["lab_sub"] = sub
    session["lab_pts"] = session.get("lab_pts", 0.0) + r["score"]
    session["lab_max"] = session.get("lab_max", 0.0) + r["points"]
    session["lab_full"] = session.get("lab_full", 0) + int(r["full"])
    return redirect(url_for("lab_q"))


@app.route("/lab/next", methods=["POST"])
def lab_next():
    session["lab_idx"] = session.get("lab_idx", 0) + 1
    session["lab_sub"] = None
    return redirect(url_for("lab_q"))


@app.route("/lab/done")
def lab_done():
    return render_template(
        "lab_done.html", pts=round(session.get("lab_pts", 0.0), 1), mx=session.get("lab_max", 0.0),
        full=session.get("lab_full", 0), total=len(session.get("lab_ids") or []),
    )


# ── 실기 시험 시뮬레이션 ───────────────────────────────────────────────
def _sim_exam(args):
    exam = args.get("exam")
    return exam if exam in SIM_EXAMS else "전산회계1급"


def _sim_load():
    run = session.get("sim_run")
    return (run, store.sim_load(_user(), run)) if run else (None, None)


def _sim_left(st):
    """남은 초(시간제한 없으면 None, 지나면 0 이하)."""
    return None if not st.get("deadline") else st["deadline"] - int(time.time())


def _filled(a):
    sub = a.get("sub") or {}
    vat = sub.get("vat") or {}
    return bool(a.get("c") or a.get("memo") or sub.get("rows") or any(vat.values()))


@app.route("/sim")
def sim_setup():
    exam = _sim_exam(request.args)
    theory_n = {}
    for q in QUESTIONS:
        if q.get("exam") == exam and q["type"] == "theory" and q.get("answer"):
            theory_n[q["round"]] = theory_n.get(q["round"], 0) + 1
    rounds = sorted({p["round"] for p in PRACTICE if p["exam"] == exam}, key=_round_no, reverse=True)
    return render_template(
        "sim_setup.html", exam=exam, name=session.get("name", ""),
        rounds=[(r, theory_n.get(r, 0)) for r in rounds], minutes=EXAM_MINUTES[exam],
    )


@app.route("/sim/start", methods=["POST"])
def sim_start():
    f = request.form
    if f.get("name", "").strip():
        session["name"] = f["name"].strip()
        session.permanent = True
    exam = _sim_exam(f)
    rnd = f.get("round", "")
    theory = sorted((q for q in QUESTIONS if q.get("exam") == exam and q["type"] == "theory"
                     and q.get("answer") and q["round"] == rnd), key=lambda q: _round_no(q["num"]))
    prac = sorted((p for p in PRACTICE if p["exam"] == exam and p["round"] == rnd),
                  key=lambda p: (p["section"], _round_no(p["no"])))
    if not prac:
        return redirect(url_for("sim_setup", exam=exam))
    try:
        minutes = max(0, int(f.get("minutes") or 0))
    except ValueError:
        minutes = 0
    now = int(time.time())
    state = {"exam": exam, "round": rnd, "start": now, "deadline": now + minutes * 60 if minutes else 0,
             "items": [{"k": "t", "id": q["id"]} for q in theory] + [{"k": "p", "id": p["id"]} for p in prac],
             "ans": {}, "self": {}, "done": False, "end": 0}
    session["sim_run"] = store.sim_create(_user(), state)
    session.permanent = True
    return redirect(url_for("sim_q", i=0))


@app.route("/sim/q/<int:i>")
def sim_q(i):
    run, st = _sim_load()
    if not st:
        return redirect(url_for("sim_setup"))
    left = _sim_left(st)
    if st["done"] or (left is not None and left <= 0):
        return redirect(url_for("sim_result"))
    items = st["items"]
    if not 0 <= i < len(items):
        return redirect(url_for("sim_q", i=0))
    item = items[i]
    ctx = dict(state=st, i=i, total=len(items), item=item, left=left,
               saved=st["ans"].get(str(i), {}),
               answered=[_filled(st["ans"].get(str(j), {})) for j in range(len(items))])
    if item["k"] == "t":
        n_t = sum(1 for it in items if it["k"] == "t")
        ctx.update(q=_Q_BY_ID[item["id"]], tpoints=THEORY_TOTAL / n_t)
    else:
        p = _P_BY_ID[item["id"]]
        ctx.update(p=p, gradable=grader.gradable(p), section_label=_section_label(p))
    return render_template("sim_q.html", **ctx)


def _sim_compute(st):
    items = st["items"]
    n_t = sum(1 for it in items if it["k"] == "t")
    tpts = THEORY_TOTAL / n_t if n_t else 0.0
    rows, theory, prac, by_sec = [], 0.0, 0.0, {}
    for i, it in enumerate(items):
        a = st["ans"].get(str(i), {})
        if it["k"] == "t":
            q = _Q_BY_ID[it["id"]]
            ok = is_correct(a.get("c"), q["answer"])
            earned, mx, label = (tpts if ok else 0.0), tpts, "이론"
            theory += earned
            rows.append({"item": it, "index": i, "q": q, "chosen": a.get("c"), "ok": ok,
                         "earned": earned, "max": mx, "label": label})
        else:
            p = _P_BY_ID[it["id"]]
            mx, label = float(p.get("points") or 0), _section_label(p)
            row = {"item": it, "index": i, "p": p, "max": mx, "label": label, "r": None,
                   "memo": a.get("memo", ""), "self_ok": bool(st.get("self", {}).get(str(i)))}
            if grader.gradable(p):
                row["r"] = grader.grade(p, a.get("sub") or {"rows": []})
                earned = row["r"]["score"]
            else:
                earned = mx if row["self_ok"] else 0.0
            row["earned"] = earned
            prac += earned
            rows.append(row)
        s = by_sec.setdefault(label, [0.0, 0.0])
        s[0] += earned
        s[1] += mx
    manual = [r for r in rows if r["item"]["k"] == "p" and r["r"] is None]
    return {"rows": rows, "theory": theory, "prac": prac, "total": theory + prac,
            "by_section": [(k, v[0], v[1]) for k, v in by_sec.items()],
            "self_total": len(manual),
            "self_pending": len([r for r in manual if not r["self_ok"]]),
            "self_pending_pts": sum(r["max"] for r in manual if not r["self_ok"])}


def _sim_finish(run, st):
    """제출/시간 종료. 학습 기록(복습 간격)은 이때 한 번만 남긴다."""
    if st["done"]:
        return
    st["done"], st["end"] = True, int(time.time())
    for row in _sim_compute(st)["rows"]:
        if row["item"]["k"] == "t":
            store.record(_user(), "theory", row["q"]["id"], row["q"].get("exam", ""),
                         row["q"]["round"], row["ok"])
        elif row["r"] is not None:
            p = row["p"]
            store.record(_user(), "silmu", p["id"], p["exam"], _PAT_OF.get(p["id"], ""), row["r"]["full"])
    store.sim_save(_user(), run, st)


@app.route("/sim/save/<int:i>", methods=["POST"])
def sim_save(i):
    run, st = _sim_load()
    if not st or st["done"]:
        return redirect(url_for("sim_result" if st else "sim_setup"))
    left = _sim_left(st)
    items = st["items"]
    if 0 <= i < len(items) and (left is None or left > -SIM_GRACE):
        f = request.form
        if items[i]["k"] == "t":
            if f.get("choice") in ("1", "2", "3", "4"):
                st["ans"][str(i)] = {"c": f["choice"]}
        elif grader.gradable(_P_BY_ID[items[i]["id"]]):
            st["ans"][str(i)] = {"sub": _parse_entry_form(f)}
        else:
            st["ans"][str(i)] = {"memo": f.get("memo", "")[:500]}
        store.sim_save(_user(), run, st)
    to = request.form.get("to", "")
    if to == "submit" or (left is not None and left <= 0):
        _sim_finish(run, st)
        return redirect(url_for("sim_result"))
    return redirect(url_for("sim_q", i=int(to) if to.isdigit() else i))


@app.route("/sim/result")
def sim_result():
    run, st = _sim_load()
    if not st:
        return redirect(url_for("sim_setup"))
    left = _sim_left(st)
    timed_out = left is not None and left <= 0
    if not st["done"]:
        if not timed_out:
            return redirect(url_for("sim_q", i=0))
        _sim_finish(run, st)
    res = _sim_compute(st)
    used = max(0, (st["end"] or int(time.time())) - st["start"])
    return render_template(
        "sim_result.html", state=st, timed_out=timed_out and used >= (st["deadline"] - st["start"]) - 1,
        time_used="%d분 %02d초" % (used // 60, used % 60),
        total=res["total"], theory_pts=res["theory"], prac_pts=res["prac"], rows=res["rows"],
        by_section=res["by_section"], self_total=res["self_total"],
        self_pending=res["self_pending"], self_pending_pts=res["self_pending_pts"],
    )


@app.route("/sim/selfmark", methods=["POST"])
def sim_selfmark():
    run, st = _sim_load()
    if not st or not st["done"]:
        return redirect(url_for("sim_setup"))
    manual = {str(i) for i, it in enumerate(st["items"])
              if it["k"] == "p" and not grader.gradable(_P_BY_ID[it["id"]])}
    st["self"] = {i: True for i in request.form.getlist("ok") if i in manual}
    store.sim_save(_user(), run, st)
    return redirect(url_for("sim_result"))


@app.route("/stats")
def stats():
    """약점 화면: 어디서 자주 틀리는지, 오늘 복습할 게 얼마나 되는지."""
    exam = _pick_exam(request.args)
    _migrate_cookie_notes()
    user = _user()
    silmu_rows = store.stats(user, "silmu", exam)
    silmu_rows.sort(key=lambda r: (r["pct"], -r["wrong"]))  # 정답률 낮은 유형이 위로
    theory_rows = store.stats(user, "theory", exam)
    theory_rows.sort(key=lambda r: _round_key(exam, r["grp"]), reverse=True)

    def total(rows):
        ok, bad = sum(r["right"] for r in rows), sum(r["wrong"] for r in rows)
        return {"right": ok, "wrong": bad, "pct": round(ok / (ok + bad) * 100) if ok + bad else 0,
                "due": sum(r["due"] for r in rows), "tried": sum(r["tried"] for r in rows)}

    return render_template(
        "stats.html", exam=exam, exam_labels=EXAM_LABELS, name=session.get("name", ""),
        silmu_rows=silmu_rows, theory_rows=theory_rows,
        silmu_total=total(silmu_rows), theory_total=total(theory_rows),
        theory_pool=len([q for q in QUESTIONS if q.get("exam", "erp") == exam and q.get("answer")]),
        silmu_pool=len([p for p in PRACTICE if p["exam"] == exam]),
    )


@app.route("/pattern")
def pattern_index():
    band = request.args.get("band") or patterns.BANDS[0]
    if band not in patterns.BANDS:
        band = patterns.BANDS[0]
    return render_template(
        "pattern_index.html", bands=patterns.BANDS, band=band,
        groups=[g for g in PATTERNS if g["band"] == band],
        totals={b: sum(g["count"] for g in PATTERNS if g["band"] == b)
                for b in patterns.BANDS},
    )


@app.route("/pattern/<slug>")
def pattern_page(slug):
    g = _PAT_BY_SLUG.get(slug)
    if not g:
        return redirect(url_for("pattern_index"))
    same = [x for x in PATTERNS if x["band"] == g["band"]]
    at = same.index(g)
    rep = g["rep"]
    return render_template(
        "pattern_page.html", g=g, band_groups=same,
        prev=same[at - 1] if at else None,
        next=same[at + 1] if at + 1 < len(same) else None,
        items=[PRACTICE[i] for i in g["items"]],
        section_label=_section_label,
        combos=accounts.describe(rep["entry_sets"]) if rep else [],
        glossary=accounts.glossary(rep["entry_sets"]) if rep else [],
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    ip = _local_ip()
    print("=" * 60)
    print(f"  이 컴퓨터에서 확인:      http://127.0.0.1:{port}")
    print(f"  휴대폰(같은 와이파이):   http://{ip}:{port}")
    print("  (휴대폰 브라우저에 위 주소 입력 -> '홈 화면에 추가'하면 앱처럼 사용 가능)")
    print("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False)
