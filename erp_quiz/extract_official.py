# -*- coding: utf-8 -*-
"""
extract_official.py -- 한국세무사회 '확정답안' PDF에서 실무시험 문항을 구조화해 뽑는다.

기존 extract_jsanhoe.py / extract_jstax.py 는 이론시험(객관식 15문항)만 다루고
실무시험은 "KcLep으로 풀어야 한다"는 이유로 건너뛴다. 그런데 확정답안 PDF에는
실무 문항의 지문·정답분개·유형코드·계산근거가 모두 텍스트로 들어 있고, 신고서
화면처럼 텍스트로 옮길 수 없는 답은 KcLep 화면 캡처 이미지로 박혀 있다.
이 파일은 그 둘을 함께 뽑아 학습용 카드 데이터로 만든다.

금액은 읽기 순서로 파싱하면 어긋난다("원"이 별도 줄로 떨어지고, 차변 2줄/대변
1줄처럼 줄 수가 안 맞는다). 그래서 좌표를 쓴다. 한 행에서 (차)와 (대) 마커의
x좌표를 찾아 그 사이를 차변, (대) 이후를 대변으로 가른다. 마커 x좌표는 회차마다
다르므로(118회 (대)=244, 126회 (대)=318) 고정값을 쓰지 않고 행에서 직접 읽는다.

[ 사용법 ]
    python extract_official.py --source "../세무회계_기출_118-126" --out official.json
    python extract_official.py --source "..." --out official.json --images img
"""
import argparse
import glob
import json
import os
import re

import fitz  # PyMuPDF

# 한 줄로 볼 y 오차(pt). 같은 표 행인데 글자 크기가 달라 y가 1~2pt 어긋난다.
Y_TOL = 3.0

AMOUNT = re.compile(r"^[\d,]{3,}\s*원?$")
ONLY_WON = re.compile(r"^원+$")
PAGE_HDR = re.compile(r"^\[제\s*\d+\s*회.*확정답안\]$")
# 쪽번호는 "12/19(뒷면 계속)" 이지만 마지막 장만 "19/19(마지막 장)" 이라 괄호 안을
# 특정 문구로 묶으면 마지막 장 19개가 설명줄에 섞여 들어온다.
PAGE_NUM = re.compile(r"^\d+\s*/\s*\d+\s*(\([^)]*\))?$")
# [답] 뒤에 오는 것이 메뉴 이름이 아니라 답 자체인 문항(장부조회: "[답] 125,000원",
# "[답] 영원상사,8건"). 메뉴 이름(일반전표입력·부가가치세신고서…)에는 숫자가 없으므로
# 숫자가 섞여 있으면 답으로 본다.
ANSWER_VALUE = re.compile(r"\d")
ITEM_NO = re.compile(r"^\[(\d{1,2})\]$")
# "[답]" 안에 공백이 들어간 회차가 있다("[ 답] 일반전표입력"). 글자 그대로 비교하면
# 그 문항의 답이 통째로 문제 쪽으로 넘어가 분개가 사라진다.
ANSWER_MARK = re.compile(r"\[\s*답\s*\]")
MARK_TOKEN = re.compile(r"^[\[\]답]+$")
SECTION = re.compile(r"^문제\s*([1-9])$")
POINTS = re.compile(r"\((\d{1,2})점\)")
DATE = re.compile(r"^(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})\.?$")
ROUND = re.compile(r"제?(\d{2,3})회")
# "유형 : 53. 면세, 공급가액 : 920,000원, 부가세 : 0원, ..." 꼴.
# 값을 쉼표까지로 끊으면 금액이 잘린다(920,000원 -> "920"). 그래서 쉼표가 아니라
# "다음 항목 이름 +:" 이 나오는 자리까지를 한 값으로 본다. 항목 사이가 쉼표가
# 아니라 공백뿐인 회차도 있어서 쉼표는 있어도 없어도 되게 둔다.
VAT_FIELD = re.compile(r"([가-힣]+)\s*[:：]\s*(.+?)(?=\s*,?\s*[가-힣]+\s*[:：]|$)")


def to_int(tok):
    """'12,000,000원' -> 12000000. 금액이 아니면 None."""
    t = tok.replace("원", "").replace(",", "").strip()
    return int(t) if t.isdigit() else None


MARKER = re.compile(r"\((?:차|대)\)")
WHITE = 16777215  # 0xFFFFFF


def visible_words(page):
    """페이지의 '보이는' 글자만 (x0, y0, x1, y1, 글자) 로 돌려준다.

    확정답안 PDF에는 흰 바탕에 흰 글자로 깔린 텍스트가 섞여 있다(전체 40군데).
    PyMuPDF의 get_text("words")는 이걸 같이 긁어오는데, 124회 전산회계1급
    문제4 [2]에서는 보이지 않는 '1,' 이 보이는 '300,000원' 바로 앞에 붙어
    '1,300,000원'으로 읽혔다. 눈으로 본 답과 다른 금액이 조용히 들어가므로
    색으로 걸러낸다.
    """
    out = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if span.get("color") == WHITE:
                    continue
                buf = []
                for ch in span["chars"]:
                    if ch["c"].isspace():
                        if buf:
                            out.append(_word(buf))
                            buf = []
                    else:
                        buf.append(ch)
                if buf:
                    out.append(_word(buf))
    return out


def _word(chars):
    x0 = min(c["bbox"][0] for c in chars)
    y0 = min(c["bbox"][1] for c in chars)
    x1 = max(c["bbox"][2] for c in chars)
    y1 = max(c["bbox"][3] for c in chars)
    return (x0, y0, x1, y1, "".join(c["c"] for c in chars))


def unglue(words):
    """'원(대)' 처럼 (차)/(대) 마커가 다른 글자에 붙어 나온 것을 떼어낸다.

    마커를 못 떼면 그 행의 차변·대변 경계를 못 찾아 금액이 한쪽으로 몰린다
    (125회 회계1급 문제5 [1]이 이 경우였다). 글자폭을 일정하다고 보고 문자
    위치 비율로 x를 나눠 준다 - 좌우 순서만 맞으면 되므로 이 근사로 충분하다.
    """
    out = []
    for x0, y0, x1, y1, text, *rest in words:
        if len(text) > 3 and MARKER.search(text):
            span = (x1 - x0) / max(len(text), 1)
            at = 0
            for part in re.split(r"(\(차\)|\(대\))", text):
                if part:
                    out.append((x0 + span * at, y0, x0 + span * (at + len(part)), y1, part, *rest))
                    at += len(part)
        else:
            out.append((x0, y0, x1, y1, text, *rest))
    return out


def logical_pages(doc):
    """(물리페이지, 반쪽표시, words) 목록.

    125회 전산세무2급만 841x595 가로형으로, 한 장에 두 페이지가 나란히 조판돼
    있다(14장 = 27쪽). 그대로 행을 묶으면 좌우 페이지가 한 줄로 섞인다. 그래서
    가로형이면 좌/우 반쪽을 각각 한 페이지로 보고, 오른쪽 반쪽은 x를 왼쪽
    기준으로 평행이동해 좌표계를 맞춘다.
    """
    for page in doc:
        r = page.rect
        words = unglue(visible_words(page))
        if r.width <= r.height:
            yield page.number + 1, "", words
            continue
        mid = r.x0 + r.width / 2
        yield page.number + 1, "L", [w for w in words if w[0] < mid]
        shift = mid - r.x0
        yield page.number + 1, "R", [
            (w[0] - shift, w[1], w[2] - shift, *w[3:]) for w in words if w[0] >= mid
        ]


def group_rows(words):
    """words -> [(y, [(x, 글자), ...]), ...]. y가 Y_TOL 안이면 같은 행으로 묶는다."""
    buckets = []
    for x0, y0, _x1, _y1, text, *_ in words:
        for b in buckets:
            if abs(b[0] - y0) <= Y_TOL:
                b[1].append((x0, text))
                break
        else:
            buckets.append((y0, [(x0, text)]))
    return [(y, sorted(toks)) for y, toks in sorted(buckets, key=lambda b: b[0])]


def doc_rows(doc):
    """문서 전체를 행 목록으로. 페이지 머리말/쪽번호는 버린다."""
    out = []
    for pageno, half, words in logical_pages(doc):
        for y, toks in group_rows(words):
            line = " ".join(t for _x, t in toks).strip()
            if PAGE_HDR.match(line) or PAGE_NUM.match(line):
                continue
            out.append({"page": pageno, "half": half, "y": y, "toks": toks, "line": line})
    return out


def page_images(doc, min_w=400, min_h=120):
    """페이지별 큰 이미지 목록 [(xref, half, y)]. 로고·아이콘 같은 잔챙이는 건너뛴다.

    y와 half(가로형 2-up의 좌/우)를 같이 담는 이유는, 이 이미지가 문제에 딸린
    서식(세금계산서·사업자등록증)인지 답으로 실린 KcLep 화면인지를 [답] 위치와
    비교해 가려내기 위해서다. 문제 쪽 서식을 답 펼친 뒤에 보여주면 정작 풀 때
    봐야 할 자료가 가려진다.
    """
    found = {}
    for page in doc:
        r = page.rect
        mid = r.x0 + r.width / 2
        keep = []
        for info in page.get_images(full=True):
            xref, w, h = info[0], info[2], info[3]
            if w < min_w or h < min_h:
                continue
            rects = page.get_image_rects(xref)
            box = rects[0] if rects else r
            half = "" if r.width <= r.height else ("L" if box.x0 < mid else "R")
            keep.append((xref, half, box.y0))
        if keep:
            found[page.number + 1] = keep
    return found


def split_sides(toks, x_debit, x_credit):
    """행의 글자들을 차변/대변으로 나눈다. 마커 자체와 떠돌이 '원'은 버린다."""
    left, right = [], []
    for x, t in toks:
        if t in ("(차)", "(대)") or ONLY_WON.match(t):
            continue
        if x_credit is not None and x >= x_credit - 2:
            right.append((x, t))
        elif x >= x_debit - 2:
            left.append((x, t))
    return left, right


def side_entry(toks):
    """[(x,글자)] -> {"account":..., "amount":...}. 금액 없으면 amount=None(계속 줄)."""
    if not toks:
        return None
    amount, words = None, []
    for _x, t in toks:
        if AMOUNT.match(t) and amount is None:
            amount = to_int(t)
        else:
            words.append(t)
    account = " ".join(words).strip(" ,")
    if not account and amount is None:
        return None
    return {"account": account, "amount": amount}


# 확정답안 원문 자체의 오식. 대차검산에 걸린 것만 눈으로 확인해 올린다.
# (문항id, 차/대, 계정, 인쇄된 금액) -> (고친 금액, 근거)
CORRECTIONS = {
    ("전산회계1급|125회|문제2|3", "credit", "자본금", 100000000): (
        10000000, "원문에 '10,000,0000원'으로 0이 하나 더 찍힘. 보통주 1,000주 × 액면가 10,000원 = 10,000,000원",
    ),
}


def apply_corrections(item_id, side, entries):
    for e in entries:
        key = (item_id, side, e["account"], e["amount"])
        if key in CORRECTIONS:
            fixed, why = CORRECTIONS[key]
            e["printed_amount"] = e["amount"]
            e["amount"] = fixed
            e["correction"] = why
    return entries


ALT_MARK = re.compile(r"^\((?:출금|입금)")


def _cut_alt(entries, target):
    """(출금)/(입금) 표기가 나오는 지점, 없으면 누적합이 target과 맞는 지점에서 자른다."""
    for i, e in enumerate(entries):
        if ALT_MARK.match(e["account"]):
            return entries[:i], entries[i:]
    run = 0
    for i, e in enumerate(entries):
        run += e["amount"]
        if run == target and i + 1 < len(entries):
            return entries[: i + 1], entries[i + 1:]
    return entries, []


def strip_alt(debit, credit):
    """확정답안이 같은 답을 '또는 (출금전표)…' 로 한 번 더 적은 부분을 떼어낸다.

    새 (차) 마커 없이 이어 적는 회차가 있어서 표 블록 수로는 구분되지 않는다.
    한쪽 합만 부풀어 있을 때, 그 쪽에서만 잘라낸다.
    """
    dsum = sum(e["amount"] for e in debit)
    csum = sum(e["amount"] for e in credit)
    if not debit or not credit or dsum == csum:
        return debit, credit, []
    if dsum > csum:
        debit, alt = _cut_alt(debit, csum)
    else:
        credit, alt = _cut_alt(credit, dsum)
    return debit, credit, [f"{e['account']} {e['amount']:,}원" for e in alt]


# 블록이 무엇인지 말해 주는 구조 라벨. '또는'은 "분개 : 현금 또는 혼합" 같은 유형
# 줄에도 나와서 '수정 전/후'를 덮어 버리므로, 구조 라벨을 먼저 찾고 없을 때만 본다.
BLOCK_LABEL = re.compile(r"(수정\s*전|수정\s*후|삭제\s*후|삭제|추가)")
ALT_LABEL = re.compile(r"(또는|입금전표|출금전표)")


def _label_of(text):
    m = BLOCK_LABEL.search(text)
    if m:
        return m.group(1)
    m = ALT_LABEL.search(text)
    return m.group(1) if m else None


def _label_from_notes(notes):
    """설명줄에서 블록 라벨을 찾는다. 구조 라벨이 하나라도 있으면 그걸 쓴다."""
    for pat in (BLOCK_LABEL, ALT_LABEL):
        for n in reversed(notes):
            if "유형" in n and pat is ALT_LABEL:
                continue  # 유형 줄의 '또는'은 분개 방법 선택지일 뿐이다
            m = pat.search(n)
            if m:
                return m.group(1)
    return None


def parse_entries(rows):
    """답 영역 행들에서 (차)/(대) 표를 읽어 표 블록 목록과 나머지 설명줄을 돌려준다.

    블록을 합치지 않는 게 중요하다. 문제4(오류수정)의 정답은 '수정 전'과 '수정 후'
    두 세트이고, 합쳐 버리면 차·대 합이 우연히 맞아 검산도 통과하면서 내용은
    틀린 카드가 된다. 블록마다 바로 앞 설명줄을 라벨로 달아 그대로 보존한다.
    """
    blocks, notes, date = [], [], None
    cur, notes_used = None, 0
    for r in rows:
        toks = r["toks"]
        flat = [t for _x, t in toks]
        has_debit = "(차)" in flat
        has_credit = "(대)" in flat

        if has_debit:
            x_debit = next(x for x, t in toks if t == "(차)")
            x_credit = next((x for x, t in toks if t == "(대)"), None)
            # 라벨은 (차) 왼쪽에 같은 줄로 붙기도 하고("ㆍ수정 전：2025.09.30. (차) …"),
            # 앞줄에 따로 놓이기도 한다("ㆍ수정 전 : 매입매출전표입력"). 둘 다 본다.
            # 앞 블록이 이미 가져간 설명줄은 다시 쓰지 않는다 - 그러면 둘째 블록까지
            # '수정 전'으로 잘못 붙는다.
            same_row = " ".join(t for x, t in toks if x < x_debit)
            lbl = _label_of(same_row) or _label_from_notes(notes[notes_used:])
            # 오류수정 문항은 '수정 전' 다음 블록이 곧 '수정 후'다.
            if lbl is None and blocks and blocks[-1]["label"] == "수정 전":
                lbl = "수정 후"
            notes_used = len(notes)
            cur = {"label": lbl, "x_debit": x_debit, "x_credit": x_credit,
                   "debit": [], "credit": []}
            blocks.append(cur)
            for x, t in toks:
                if DATE.match(t) and x < x_debit:
                    date = t
        elif cur is None:
            for x, t in toks:
                if DATE.match(t):
                    date = t
            notes.append(r["line"])
            continue
        elif has_credit and cur["x_credit"] is None:
            cur["x_credit"] = next(x for x, t in toks if t == "(대)")
        elif toks[0][0] < cur["x_debit"] - 2:
            # 표의 이어지는 줄은 차변 칸 안쪽에서 시작한다. 왼쪽 여백에서 시작하면
            # 표가 끝나고 해설이 시작된 것이다("ㆍ무상으로 취득하는 자산의…").
            # 안 끊으면 해설 문장이 계정과목의 부기로 들러붙는다.
            notes.append(r["line"])
            cur = None
            continue

        left, right = split_sides(toks, cur["x_debit"], cur["x_credit"])
        if not left and not right:
            if r["line"]:
                notes.append(r["line"])
                cur = None
            continue
        for side, got in (("debit", left), ("credit", right)):
            e = side_entry(got)
            if e is None:
                continue
            if e["amount"] is None and cur[side]:
                # 금액 없는 줄은 앞 계정의 부기(적요·"또는 미지급비용")
                prev = cur[side][-1]
                prev["note"] = (prev.get("note", "") + " " + e["account"]).strip()
            else:
                cur[side].append(e)

    out = []
    for b in blocks:
        d = [e for e in b["debit"] if e.get("amount") is not None]
        c = [e for e in b["credit"] if e.get("amount") is not None]
        d, c, alt = strip_alt(d, c)
        if alt:
            notes.append("(별해 표기: " + ", ".join(alt) + ")")
        if d or c:
            out.append({"label": b["label"], "debit": d, "credit": c})
    return out, notes, date


# 유형 값은 "51.과세", "14. 건별 또는 22. 현과" 꼴. 뒤에 오는 항목은 콜론이 빠진
# 회차가 있어서("공급가액 3,000,000원") 일반 규칙으로는 유형이 줄 끝까지 삼켜진다.
# 그래서 유형만 먼저 떼어내고 나머지를 따로 읽는다.
VAT_CODE = re.compile(r"(\d{2}\s*\.?\s*[가-힣]+(?:\s*또는\s*\d{2}\s*\.?\s*[가-힣]+)?)")


def parse_vat(lines):
    """'유형 : 53. 면세, 공급가액 : 920,000원, ...' 줄을 딕셔너리로."""
    for line in lines:
        if line and "유형" in line and ("공급가액" in line or "분개" in line):
            head, _sep, tail = line.partition("유형")
            got = {}
            m = VAT_CODE.search(tail)
            if m:
                got["유형"] = re.sub(r"\s+", " ", m.group(1)).strip()
                tail = tail[m.end():]
            for k, v in VAT_FIELD.findall(head + " " + tail):
                if k != "유형":
                    got[k] = v.strip().rstrip(",")
            if got:
                return got
    return None


def iter_items(pdf_path, exam, level):
    doc = fitz.open(pdf_path)
    rows = doc_rows(doc)
    imgs = page_images(doc)
    m = ROUND.search(os.path.basename(pdf_path))
    rnd = f"{m.group(1)}회" if m else "?"

    # 실무는 첫 "문제1" 행부터. 그 앞은 이론(기존 파서 담당).
    start = next((i for i, r in enumerate(rows) if SECTION.match(r["line"])), None)
    if start is None:
        return
    rows = rows[start:]

    section = section_title = None
    items, cur = [], None
    for r in rows:
        line = r["line"]
        first_tok = r["toks"][0][1] if r["toks"] else ""
        m = SECTION.match(line)
        if m:
            section, section_title, cur = f"문제{m.group(1)}", None, None
            continue
        if section and section_title is None and line and not ITEM_NO.match(first_tok):
            # 문제N 바로 뒤 첫 줄에 "[일반전표입력] 메뉴를 이용하여…" 식 제목이 온다
            t = re.search(r"\[([^\]]+)\]", line)
            section_title = t.group(1) if t else line[:40]
        m = ITEM_NO.match(first_tok)
        if m and r["toks"][0][0] < 80:
            cur = {
                "exam": exam, "level": level, "round": rnd,
                "section": section, "section_title": section_title,
                "no": int(m.group(1)), "q_rows": [], "a_rows": [],
                "in_answer": False, "pages": set(), "menu": None,
                "answer_at": None, "answer_text": None,
            }
            items.append(cur)
        if cur is None:
            continue
        cur["pages"].add(r["page"])
        m_ans = ANSWER_MARK.search(line)
        if m_ans:
            if not cur["in_answer"]:
                cur["answer_at"] = (r["page"], r["half"], r["y"])
            cur["in_answer"] = True
            tail = line[m_ans.end():].strip()
            # [답] 뒤에 올 수 있는 건 세 가지다: 답 자체("125,000원"), 입력 메뉴 이름
            # ("일반전표입력"), 그리고 답안 첫 단계 줄("[계정과목및적요등록]＞825.…").
            # 단계 줄은 길고 메뉴경로 기호가 들어 있다. 이건 설명줄로 흘려보낸다
            # (토큰에서 [답]만 떼어 a_rows 로 이미 넘어간다).
            is_step = (len(tail) > 25 or any(ch in tail for ch in "＞>[]")
                       or "(차)" in tail)
            if not is_step and ANSWER_VALUE.search(tail):
                # 쉼표는 천단위 구분(1,717,352,000원)에도 쓰이므로 손대지 않는다.
                cur["answer_text"] = re.sub(r"\s+", " ", tail)
            elif not is_step and tail:
                cur["menu"] = re.sub(r"\s+", "", tail.strip("[]"))
            elif tail:
                # 단계 줄은 설명줄로 넘긴다. line 도 다시 만들어야 한다 - 안 그러면
                # "[답] …" 로 시작하는 채로 남아 중복 제거 필터에 걸려 사라진다.
                rest = [(x, t) for x, t in r["toks"] if not MARK_TOKEN.match(t)]
                cur["a_rows"].append({**r, "toks": rest,
                                      "line": " ".join(t for _x, t in rest).strip()})
            continue
        (cur["a_rows"] if cur["in_answer"] else cur["q_rows"]).append(r)

    for it in items:
        q = " ".join(r["line"] for r in it["q_rows"]).strip()
        pts = POINTS.search(q)
        sets, notes, date = parse_entries(it["a_rows"])
        vat = parse_vat(notes + [it["menu"]])
        item_id = f"{exam}|{it['round']}|{it['section']}|{it['no']}"
        for s in sets:
            apply_corrections(item_id, "debit", s["debit"])
            apply_corrections(item_id, "credit", s["credit"])
            s["debit_sum"] = sum(e["amount"] for e in s["debit"])
            s["credit_sum"] = sum(e["amount"] for e in s["credit"])
            s["balanced"] = bool(s["debit"]) and s["debit_sum"] == s["credit_sum"]
        first = sets[0] if sets else {"debit": [], "credit": [], "debit_sum": 0,
                                      "credit_sum": 0, "balanced": False}
        yield {
            "id": item_id,
            "exam": exam, "level": level, "round": it["round"],
            "section": it["section"], "section_title": it["section_title"],
            "no": it["no"], "points": int(pts.group(1)) if pts else None,
            "type": "practice", "menu": it["menu"],
            "answer_text": it["answer_text"], "date": date,
            "question": re.sub(r"\s+", " ", q),
            # 대표 분개(카드 앞면용)는 첫 블록. 수정전/수정후처럼 여러 세트면 entry_sets에 다 있다.
            "debit": first["debit"], "credit": first["credit"],
            "debit_sum": first["debit_sum"], "credit_sum": first["credit_sum"],
            "entry_sets": sets,
            "balanced": bool(sets) and all(s["balanced"] for s in sets),
            "has_entry": bool(sets),
            "vat": vat,
            # "[답] 125,000원" 은 answer_text/menu 로 이미 뽑았으니 설명줄에서 뺀다.
            "notes": [n for n in notes if n and "유형" not in n and not ANSWER_MARK.match(n)],
            # 증빙 서식이 '텍스트 표'로 짜인 문항은 글자만 뽑으면 라벨 없는 숫자
            # 나열이 된다("920,000 220,000 700,000"). 그런 문항은 문제 영역을
            # PDF에서 그대로 잘라 이미지로 보여주는 편이 시험지와 똑같아 낫다.
            "needs_render": bool(FORM_RE.search(q)),
            "q_span": ([it["q_rows"][0]["page"], it["q_rows"][0]["half"],
                        it["q_rows"][0]["y"]] + list(it["answer_at"]))
                      if it["q_rows"] and it["answer_at"] else None,
            "q_image_refs": _pick_images(imgs, it, before=True),
            "image_refs": _pick_images(imgs, it, before=False),
            "pages": sorted(it["pages"]),
        }


# 사업자등록번호(123-45-67890)나 승인번호(8-8-8)가 보이면 증빙 서식이 딸린 문항이다.
FORM_RE = re.compile(r"\d{3}-\d{2}-\d{5}|\d{8}-\d{8}-\d{8}")


def shrink_png(path):
    """PNG를 256색 팔레트로 줄인다. 서식·프로그램 화면은 쓰는 색이 몇 개 안 되어서
    눈에 띄는 차이 없이 용량이 절반이 된다(배포 레포에 다 담아야 하므로 중요).
    JPEG은 다시 압축하면 화질만 나빠지므로 손대지 않는다."""
    try:
        from PIL import Image
    except ImportError:
        return
    with Image.open(path) as im:
        if im.mode == "P":
            return
        im.convert("RGB").quantize(colors=256).save(path, optimize=True)


def render_question(doc, item, outdir, dpi=150):
    """문제 영역(문항 첫 줄 ~ [답] 직전)을 PDF에서 잘라 그림으로 저장한다."""
    span = item.get("q_span")
    if not span:
        return []
    p0, half, y0, p1, _half1, y1 = span
    names = []
    for pageno in range(p0, p1 + 1):
        page = doc[pageno - 1]
        r = page.rect
        x0, x1 = r.x0, r.x1
        if half:  # 가로형 2-up은 해당 반쪽만
            mid = r.x0 + r.width / 2
            x0, x1 = (r.x0, mid) if half == "L" else (mid, r.x1)
        top = (y0 - 6) if pageno == p0 else r.y0
        bottom = (y1 - 2) if pageno == p1 else r.y1
        clip = fitz.Rect(x0, max(top, r.y0), x1, min(bottom, r.y1))
        if clip.height < 20:
            continue
        name = f"q_{item['exam']}_{item['round']}_{item['section']}_{item['no']}_p{pageno}.png"
        out = os.path.join(outdir, name)
        page.get_pixmap(clip=clip, dpi=dpi).save(out)
        shrink_png(out)
        names.append(name)
    return names


def _pick_images(imgs, it, before):
    """문항이 걸친 페이지의 이미지를 [답] 위치 기준으로 문제 쪽/정답 쪽으로 가른다."""
    at = it["answer_at"]
    out = []
    for p in sorted(it["pages"]):
        for xref, half, y in imgs.get(p, []):
            if at is None:
                is_before = before  # [답]을 못 찾으면 나누지 않고 요청한 쪽에 몰아준다
            else:
                is_before = (p, half, y) < at
            if is_before == before:
                out.append((p, xref))
    return out


EXAMS = [("전산회계 1급", "전산회계1급", "1급"), ("전산세무 2급", "전산세무2급", "2급")]


def answer_pdfs(source, folder):
    return sorted(glob.glob(os.path.join(source, folder, "*답안", "*.pdf")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="세무회계_기출_118-126 폴더")
    ap.add_argument("--out", required=True)
    ap.add_argument("--images", help="KcLep 화면 캡처를 저장할 폴더 (생략하면 참조만)")
    a = ap.parse_args()

    all_items = []
    for folder, exam, level in EXAMS:
        for p in answer_pdfs(a.source, folder):
            items = list(iter_items(p, exam, level))
            all_items += items
            ent = [i for i in items if i["has_entry"]]
            bad = [i for i in ent if not i["balanced"]]
            print(f"  {os.path.basename(p)[:14]} {len(items):3d}문항  분개 {len(ent):3d}  "
                  f"대차불일치 {len(bad):2d}  "
                  f"서식 {sum(len(i['q_image_refs']) for i in items):3d}  "
                  f"정답화면 {sum(len(i['image_refs']) for i in items):3d}")

    if a.images:
        os.makedirs(a.images, exist_ok=True)
        saved = {}
        for folder, exam, _level in EXAMS:
            for p in answer_pdfs(a.source, folder):
                doc = fitz.open(p)
                rnd = ROUND.search(os.path.basename(p)).group(1) + "회"
                for it in all_items:
                    if it["exam"] != exam or it["round"] != rnd:
                        continue
                    for src, dst in (("q_image_refs", "q_images"), ("image_refs", "images")):
                        names = []
                        for pg, xref in it[src]:
                            key = (exam, rnd, xref)
                            if key not in saved:
                                img = doc.extract_image(xref)
                                name = f"{exam}_{rnd}_p{pg}_{xref}.{img['ext']}"
                                out = os.path.join(a.images, name)
                                with open(out, "wb") as f:
                                    f.write(img["image"])
                                if img["ext"] == "png":
                                    shrink_png(out)
                                saved[key] = name
                            names.append(saved[key])
                        it[dst] = names
                    if it["needs_render"]:
                        it["q_render"] = render_question(doc, it, a.images)
    for it in all_items:
        it.pop("image_refs", None)
        it.pop("q_image_refs", None)

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(all_items, f, ensure_ascii=False, indent=1)

    ent = [i for i in all_items if i["has_entry"]]
    bad = [i for i in ent if not i["balanced"]]
    print(f"\n총 {len(all_items)}문항 | 분개 {len(ent)} | 대차일치 {len(ent) - len(bad)} "
          f"| 검수필요 {len(bad)} | 화면 {sum(len(i.get('images', [])) for i in all_items)}장 -> {a.out}")
    for i in bad[:15]:
        print(f"   ! {i['id']}  차 {i['debit_sum']:,} / 대 {i['credit_sum']:,}")


if __name__ == "__main__":
    main()
