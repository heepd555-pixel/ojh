# -*- coding: utf-8 -*-
"""
patterns.py -- 실무 386문항을 '유형' 단위로 다시 묶는다.

회차 순서로 386장을 넘기면 매번 새 문제처럼 보이지만, 묶어 보면 유형은 40개가
안 된다. 같은 유형은 숫자와 거래처만 바뀌고 분개 구조는 그대로다. 문항을 푸는
자료가 아니라 "이 유형은 항상 이 분개"를 읽는 교재를 만들기 위한 묶음이다.

묶는 기준:
  - 매입매출전표  : 부가세 유형코드 (11.과세 / 51.과세 / 54.불공 ...)
  - 일반전표·결산 : 결합관계 (비용의 발생 / 자산의 감소 ...)
  - 그 밖         : 분개가 없는 문항(기초정보·장부조회·부가세신고·원천징수)은
                    시험지 구간 그대로 묶는다. 답이 KcLep 화면이라 분개가 없다.

설명 문구는 새로 쓰지 않는다. 유형코드 설명은 accounts.VAT_TYPES,
결합관계 설명은 아래 ELEMENT_HINTS 열 줄에서 조립된다.
"""
import collections
import re

import accounts

BANDS = ("매입매출", "일반전표·결산", "KcLep 직접입력")

# 거래 8요소를 초보자 말로 한 줄씩. 결합관계 설명은 전부 여기서 나온다.
ELEMENT_HINTS = {
    "자산의 증가": "재산이 늘었다 (현금·물건·받을 돈)",
    "자산의 감소": "재산이 줄었다 (돈이 나갔거나 물건이 빠졌다)",
    "부채의 증가": "갚을 빚이 늘었다 (나중에 줄 돈이 생겼다)",
    "부채의 감소": "갚을 빚이 줄었다 (빚을 갚았다)",
    "자본의 증가": "회사 밑천이 늘었다 (증자·이익)",
    "자본의 감소": "회사 밑천이 줄었다 (감자·배당)",
    "수익의 발생": "번 돈이 생겼다",
    "수익의 감소": "번 돈을 되돌렸다 (매출 취소·에누리)",
    "비용의 발생": "쓴 돈이 생겼다",
    "비용의 감소": "쓴 돈을 되돌렸다 (환급·취소)",
}

_COUNT_SUFFIX = re.compile(r"\s*\(\d+건\)")


def has_entry(p):
    return bool(p.get("entry_sets")) and any(
        s.get("debit") or s.get("credit") for s in p["entry_sets"])


def _combo_key(p):
    parts = accounts.describe(p["entry_sets"])
    if not parts:
        return None
    # 오류수정은 수정전/수정후 두 세트라 마지막(고친 뒤)이 그 문항의 성격이다.
    # "(2건)"은 같은 결합관계가 여러 줄이라는 표시일 뿐이라 묶을 때는 떼어낸다.
    return _COUNT_SUFFIX.sub("", parts[-1])


def _side_hint(side_text):
    """'자산의 증가 + 비용의 발생' -> 두 줄 설명을 이어 붙인다."""
    return " + ".join(ELEMENT_HINTS[e] for e in side_text.split(" + ")
                      if e in ELEMENT_HINTS)


def entry_sig(p):
    """계정 조합만 남긴 지문. 금액·거래처를 뺀 '분개 모양'이다."""
    s = p["entry_sets"][-1]
    return (tuple(accounts.clean(e["account"]) for e in s.get("debit") or []),
            tuple(accounts.clean(e["account"]) for e in s.get("credit") or []))


def _classify(p, label):
    kind = accounts.vat_type((p.get("vat") or {}).get("유형", ""))
    if kind:
        code, name, side, text = kind
        return BANDS[0], f"{code}.{name}", f"{side} · {text}"
    key = _combo_key(p)
    if key:
        left, _, right = key.partition(" / ")
        hint = " / ".join(x for x in (_side_hint(left), _side_hint(right)) if x)
        return BANDS[1], key, hint
    return (BANDS[2], label(p),
            "분개가 없고 메뉴에 직접 입력하는 문항. 정답이 KcLep 화면으로 실려 있어 "
            "화면을 그대로 보고 익힌다.")


def build(practice, label):
    """[그룹] 을 돌려준다. label(p) 은 '장부조회'처럼 구간 이름을 주는 함수.

    그룹 하나 = 교재 한 페이지. items 는 practice 안의 인덱스라 세션 쿠키에도
    그대로 넣을 수 있다(/silmu 와 같은 방식).
    """
    buckets = {}
    for i, p in enumerate(practice):
        band, title, hint = _classify(p, label)
        g = buckets.setdefault((band, title),
                               {"band": band, "title": title, "hint": hint,
                                "items": []})
        g["items"].append(i)

    groups = []
    for g in sorted(buckets.values(),
                    key=lambda g: (BANDS.index(g["band"]), -len(g["items"]),
                                   g["title"])):
        items = [practice[i] for i in g["items"]]
        sigs = collections.Counter(entry_sig(p) for p in items if has_entry(p))
        g["count"] = len(g["items"])
        g["exams"] = sorted({p["exam"] for p in items})
        g["points"] = sum(p.get("points") or 0 for p in items)
        # 대표 = 이 유형에서 가장 흔한 분개 모양의 첫 문항. 나머지는 '변형'으로
        # 세어서 보여준다. 숫자만 바뀐다는 걸 눈으로 보는 게 이 페이지의 전부다.
        if sigs:
            top = sigs.most_common(1)[0][0]
            g["rep"] = next(p for p in items if has_entry(p) and entry_sig(p) == top)
            g["variants"] = [(d, c, n) for (d, c), n in sigs.most_common()]
        else:
            g["rep"] = None
            g["variants"] = []
        g["slug"] = "g%d" % (len(groups) + 1)
        groups.append(g)
    return groups
