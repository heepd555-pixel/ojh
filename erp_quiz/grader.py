# -*- coding: utf-8 -*-
"""
grader.py -- 직접 입력한 전표를 확정답안 분개와 비교해 채점한다.

입력(submission):
    {"rows": [{"side": "debit"|"credit", "acct": "현금" 또는 "101", "amt": "1,000,000"}],
     "vat":  {"type": "51", "supply": "1,000,000", "vat": "100,000"}}   # vat 는 매입매출 문항만

채점 기준
  - 답안의 줄(차/대변 + 계정 + 금액) 하나가 한 칸. 같은 줄을 입력했으면 맞은 것으로 센다.
  - 매입매출 문항은 유형 · 공급가액 · 부가세가 각각 한 칸씩 더 붙는다.
  - 답에 없는 줄을 더 입력하면 감점(분모에 더해진다).
  - 점수 = 배점 x (맞은 칸 / (전체 칸 + 군더더기 줄)). 시험의 실제 부분점수 규칙이 아니라
    연습용 기준이다.
  - 계정은 이름이나 KcLep 코드(예: 101, 0101)로 입력할 수 있다. 제조/판관 구분
    (제)/(판)이 답에 있고 입력에도 있으면 서로 같아야 맞다.
  - 수정 전/후가 있는 오류수정은 '수정 후' 분개를, 정답이 여러 개('또는')면 가장
    잘 맞은 것을 기준으로 한다.
"""
import re

import accounts

# 코드 -> 계정 이름. 앞의 0은 떼고 비교한다 (101 = 0101).
_REV = {}
for _name, _code in accounts.CODES.items():
    _REV.setdefault(_code.lstrip("0"), _name)


def parse_amount(text):
    """'1,000,000원' -> 1000000. 숫자가 없으면 None."""
    digits = re.sub(r"[^\d]", "", str(text or ""))
    return int(digits) if digits else None


def resolve(text):
    """입력한 계정을 (이름, 제/판 꼬리표) 로. 비었으면 None.
    '101', '0101', '101 현금', '현금', '복리후생비(판)' 를 모두 받는다."""
    t = (text or "").strip()
    if not t:
        return None
    m = re.match(r"^(\d{3,4})\s*(.*)$", t)
    if m:
        hit = _REV.get(m.group(1).lstrip("0"))
        if hit:
            t = hit
        elif m.group(2):
            t = m.group(2)
        else:
            return ("#" + m.group(1), None)  # 모르는 코드
    tag = accounts._SIDE_TAG.search(t)
    return (accounts.clean(t), tag.group(1) if tag else None)


def _answer_lines(entry_set):
    out = []
    for side in ("debit", "credit"):
        for e in entry_set.get(side) or []:
            tag = accounts._SIDE_TAG.search(e["account"])
            out.append({"side": side, "acct": e["account"], "base": accounts.clean(e["account"]),
                        "tag": tag.group(1) if tag else None, "amt": int(e["amount"])})
    return out


def _user_lines(sub):
    out = []
    for r in (sub or {}).get("rows") or []:
        side, acct, amt = r.get("side"), resolve(r.get("acct")), parse_amount(r.get("amt"))
        if side not in ("debit", "credit") or (acct is None and amt is None):
            continue  # 빈 줄
        out.append({"side": side, "raw": (r.get("acct") or "").strip(),
                    "base": acct[0] if acct else None, "tag": acct[1] if acct else None,
                    "amt": amt, "used": False})
    return out


def _same_account(a, u):
    if a["base"] != u["base"]:
        return False
    return not (a["tag"] and u["tag"] and a["tag"] != u["tag"])


def _pick_sets(p):
    sets = [s for s in (p.get("entry_sets") or []) if s.get("debit") or s.get("credit")]
    after = [s for s in sets if s.get("label") == "수정 후"]
    return after or sets


def _grade_set(p, entry_set, sub):
    ans, usr = _answer_lines(entry_set), _user_lines(sub)
    done = [None] * len(ans)
    # 1차: 차/대변·계정·금액이 모두 같은 줄을 먼저 확정한다.
    for i, a in enumerate(ans):
        hit = next((u for u in usr if not u["used"] and u["side"] == a["side"]
                    and _same_account(a, u) and u["amt"] == a["amt"]), None)
        if hit:
            hit["used"] = True
            note = ""
            if a["tag"] and not hit["tag"] and not re.match(r"^\d", hit["raw"]):
                note = "제조(제)/판관(판) 구분 필요"
            done[i] = {**a, "status": "ok", "note": note}
    # 2차: 남은 줄 중 계정만 같으면 '금액 틀림', 없으면 '빠짐'.
    for i, a in enumerate(ans):
        if done[i]:
            continue
        near = next((u for u in usr if not u["used"] and u["side"] == a["side"]
                     and _same_account(a, u)), None)
        if near:
            near["used"] = True
            done[i] = {**a, "status": "amount", "given": near["amt"], "note": ""}
        else:
            done[i] = {**a, "status": "missing", "note": ""}
    extras = [u for u in usr if not u["used"]]

    vat_rows, hits, slots = [], 0, 0
    v = p.get("vat")
    if v:
        got = (sub or {}).get("vat") or {}
        m = re.match(r"\s*(\d{2})", v.get("유형") or "")
        checks = [("유형", m.group(1) if m else None,
                   (re.match(r"\s*(\d{2})", got.get("type") or "") or [None, None])[1])]
        if v.get("공급가액"):
            checks.append(("공급가액", parse_amount(v["공급가액"]), parse_amount(got.get("supply"))))
        if v.get("부가세"):
            checks.append(("부가세", parse_amount(v["부가세"]), parse_amount(got.get("vat"))))
        for label, want, have in checks:
            if want is None:
                continue
            ok = want == have
            slots += 1
            hits += ok
            vat_rows.append({"label": label, "want": want, "have": have, "ok": ok})

    total = len(ans) + slots
    got_n = sum(r["status"] == "ok" for r in done) + hits
    denom = total + len(extras)
    frac = got_n / denom if denom else 0.0
    return {"frac": frac, "lines": done, "extras": extras, "vat": vat_rows,
            "label": entry_set.get("label"), "hits": got_n, "total": total}


def gradable(p):
    return bool(_pick_sets(p))


def grade(p, sub):
    """-> {"score", "points", "frac", "full", + 비교표}. 답이 여러 개면 가장 잘 맞은 것."""
    best = max((_grade_set(p, s, sub) for s in _pick_sets(p)), key=lambda r: r["frac"])
    points = float(p.get("points") or 0)
    best["points"] = points
    best["score"] = round(points * best["frac"], 1)
    best["full"] = best["frac"] >= 0.999 and not best["extras"]
    return best
