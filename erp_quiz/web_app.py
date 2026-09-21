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
from datetime import timedelta

from flask import Flask, redirect, render_template, request, session, url_for

import accounts
from quiz import filter_questions, load_questions, round_sort_key

EXAM_LABELS = {
    "erp": "ERP 정보관리사",
    "전산회계1급": "전산회계1급",
    "FAT1급": "FAT1급",
    "전산세무2급": "전산세무2급",
    "TAT2급": "TAT2급",
    "분개연습": "분개연습",
}
DEFAULT_EXAM = "erp"
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
TYPE_LABEL = {"theory": "이론", "bunkae": "분개연습", "practical": "실기"}

# 오답노트를 문제 id(긴 문자열) 그대로 쿠키에 쌓으면 금방 브라우저 쿠키 용량
# 한도(약 4KB)를 넘어서 조용히 통째로 날아갈 수 있다. QUESTIONS 안에서의
# 정수 인덱스로 바꿔서 저장하면 훨씬 압축되어 안전하다.
_ID_TO_INDEX = {q["id"]: i for i, q in enumerate(QUESTIONS)}


def _get_review_ids():
    """세션 쿠키에 저장된 오답노트(정수 인덱스)를 문제 id 집합으로 변환."""
    idxs = session.get("wrong_review", [])
    ids = set()
    for i in idxs:
        if 0 <= i < len(QUESTIONS):
            ids.add(QUESTIONS[i]["id"])
    return ids


def _set_review_ids(ids):
    idxs = sorted(_ID_TO_INDEX[i] for i in ids if i in _ID_TO_INDEX)
    session["wrong_review"] = idxs
    session.permanent = True


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
    wrong_count = len(_get_review_ids())
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

    session["exam_ids"] = [q["id"] for q in pool]
    session["exam_idx"] = 0
    session["exam_answers"] = {}
    return redirect(url_for("exam_quiz"))


@app.route("/exam/quiz")
def exam_quiz():
    ids = session.get("exam_ids")
    if not ids:
        return redirect(url_for("exam_setup"))

    idx = session.get("exam_idx", 0)
    if idx >= len(ids):
        return redirect(url_for("exam_result"))

    q = _question_by_id(ids[idx])
    return render_template(
        "exam_quiz.html", q=q, idx=idx + 1, total=len(ids),
        is_last=(idx + 1 == len(ids)),
    )


@app.route("/exam/answer", methods=["POST"])
def exam_answer():
    ids = session.get("exam_ids")
    idx = session.get("exam_idx", 0)
    if not ids or idx >= len(ids):
        return redirect(url_for("exam_setup"))

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

    if ids:
        existing = _get_review_ids()
        existing.update(wrong_ids)
        existing.difference_update(i for i in ids if i not in wrong_ids)
        _set_review_ids(existing)

    pct = round(score / len(ids) * 100) if ids else 0
    return render_template("exam_result.html", rows=rows, score=score, total=len(ids), pct=pct)


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
        wrong_ids = _get_review_ids()
        pool = [q for q in exam_qs if q["id"] in wrong_ids and q.get("answer")]
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
    if is_correct(selected, q["answer"]):
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

    if attempted:
        existing = _get_review_ids()
        existing.update(wrong_ids)
        correct_ids = [i for i in ids[:attempted] if i not in wrong_ids]
        existing.difference_update(correct_ids)
        _set_review_ids(existing)

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


def _practice_review():
    return {PRACTICE[i]["id"] for i in session.get("silmu_review", []) if i < len(PRACTICE)}


@app.route("/silmu")
def silmu_setup():
    exam = request.args.get("exam")
    if exam not in ("전산회계1급", "전산세무2급"):
        exam = "전산회계1급"
    rounds, sections = _practice_catalog(exam)
    return render_template(
        "silmu_setup.html", exam=exam, rounds=rounds, sections=sections,
        hard_count=len(_practice_review()), total=len([p for p in PRACTICE if p["exam"] == exam]),
    )


@app.route("/silmu/start", methods=["GET", "POST"])
def silmu_start():
    """GET도 받는다: /silmu/start?exam=전산세무2급&section=문제2 처럼 특정 묶음을
    바로 열 수 있어 휴대폰에 북마크해 두고 쓸 수 있다."""
    f = request.values
    exam = f.get("exam", "전산회계1급")
    if f.get("review") == "on":
        pool = [p for p in PRACTICE if p["id"] in _practice_review()]
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
        review = set(session.get("silmu_review", []))
        if request.form.get("how") == "hard":
            review.add(idxs[i])
            session["silmu_hard"] = session.get("silmu_hard", []) + [idxs[i]]
        else:
            review.discard(idxs[i])
            session["silmu_ok"] = session.get("silmu_ok", 0) + 1
        session["silmu_review"] = sorted(review)
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    ip = _local_ip()
    print("=" * 60)
    print(f"  이 컴퓨터에서 확인:      http://127.0.0.1:{port}")
    print(f"  휴대폰(같은 와이파이):   http://{ip}:{port}")
    print("  (휴대폰 브라우저에 위 주소 입력 -> '홈 화면에 추가'하면 앱처럼 사용 가능)")
    print("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False)
