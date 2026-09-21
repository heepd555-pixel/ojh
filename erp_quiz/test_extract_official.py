# -*- coding: utf-8 -*-
"""extract_official.py 자체 점검.  python test_extract_official.py [기출폴더]

확정답안 PDF에서 뽑은 분개가 눈으로 본 원문과 같은지 확인한다. 네 가지 함정이
있었고 각각 금액을 조용히 틀리게 만들었으므로, 그 네 개를 그대로 고정해 둔다.
"""
import os
import sys

from extract_official import EXAMS, answer_pdfs, iter_items

SOURCE = sys.argv[1] if len(sys.argv) > 1 else "../세무회계_기출_118-126"


def load():
    items = {}
    for folder, exam, level in EXAMS:
        for p in answer_pdfs(SOURCE, folder):
            for it in iter_items(p, exam, level):
                items[it["id"]] = it
    return items


def accounts(entries):
    return [(e["account"], e["amount"]) for e in entries]


def main():
    if not os.path.isdir(SOURCE):
        sys.exit(f"기출 폴더를 못 찾음: {SOURCE}")
    items = load()

    # 회차마다 문항 수가 정해져 있다. 하나라도 어긋나면 문항 쪼개기가 깨진 것.
    assert len(items) == 386, f"문항 수 {len(items)} (386이어야 함)"
    hoegye = [i for i in items.values() if i["exam"] == "전산회계1급"]
    semu = [i for i in items.values() if i["exam"] == "전산세무2급"]
    assert len(hoegye) == 207, len(hoegye)
    assert len(semu) == 179, len(semu)

    # 대차 검산: 분개가 있는 문항은 블록마다 차변합 = 대변합
    bad = [i["id"] for i in items.values() if i["has_entry"] and not i["balanced"]]
    assert not bad, f"대차 불일치: {bad}"

    # 1) 평범한 분개 - 차변 2줄/대변 1줄이라 읽기 순서로는 어긋나던 자리
    it = items["전산회계1급|126회|문제2|2"]
    assert accounts(it["debit"]) == [("예수금", 695000), ("복리후생비(판)", 200000)], it["debit"]
    assert accounts(it["credit"]) == [("보통예금", 895000)], it["credit"]

    # 2) 흰 글씨(보이지 않는 텍스트)가 '1,'을 덧붙여 1,300,000원으로 읽히던 자리
    it = items["전산회계1급|124회|문제4|2"]
    later = [e for s in it["entry_sets"] for e in s["debit"] if e["account"] == "선납세금"]
    assert later and later[0]["amount"] == 300000, later

    # 3) 문제4는 '수정 전'과 '수정 후' 두 세트가 정답이다. 합치면 안 된다.
    assert len(it["entry_sets"]) >= 2, it["entry_sets"]
    assert all(s["balanced"] for s in it["entry_sets"]), it["entry_sets"]

    # 3b) 오류수정 18문항 전부 '수정 전'/'수정 후'로 이름이 붙어야 한다.
    #     "분개 : 현금 또는 혼합" 같은 유형 줄의 '또는'이 이 라벨을 덮은 적이 있다.
    p4 = [i for i in items.values() if i["exam"] == "전산회계1급" and i["section"] == "문제4"]
    assert len(p4) == 18, len(p4)
    mislabeled = [i["id"] for i in p4
                  if [s["label"] for s in i["entry_sets"]][:2] != ["수정 전", "수정 후"]]
    assert not mislabeled, mislabeled

    # 4) 원문 오식(10,000,0000원)을 정오표로 바로잡았는지
    it = items["전산회계1급|125회|문제2|3"]
    cap = [e for e in it["credit"] if e["account"] == "자본금"][0]
    assert cap["amount"] == 10000000 and cap["printed_amount"] == 100000000, cap

    # 5) 125회 전산세무2급만 가로형 2-up 조판. 반쪽으로 안 쪼개면 문항이 반 토막 난다.
    only = [i for i in semu if i["round"] == "125회"]
    assert len(only) == 20, f"125회 세무2급 {len(only)}문항 (20이어야 함)"

    # 5b) 문제에 딸린 증빙 서식은 [답]보다 앞에 있으므로 문제 쪽 이미지로 가려져야 한다.
    #     한데 묶으면 세금계산서를 답 펼친 뒤에야 볼 수 있어 문제를 풀 수가 없다.
    #     (--images 없이 돌리면 q_images 키가 없으므로 참조 목록으로 확인)
    forms = [i for i in items.values() if i["q_image_refs"]]
    assert len(forms) > 50, f"문제 쪽 서식이 붙은 문항 {len(forms)}개 (너무 적음)"
    vat_forms = [i for i in forms if i["section_title"] == "매입매출전표입력"]
    assert len(vat_forms) >= 50, f"매입매출 서식 {len(vat_forms)}개"

    # 6) 매입매출전표는 유형코드가 붙어야 카드에서 유형을 물어볼 수 있다
    vat_items = [i for i in items.values() if i["section_title"] == "매입매출전표입력"]
    with_vat = [i for i in vat_items if i["vat"] and "유형" in i["vat"]]
    assert len(with_vat) / len(vat_items) > 0.9, f"유형코드 {len(with_vat)}/{len(vat_items)}"

    # 6b) 공급가액은 "920,000원" 처럼 쉼표가 든 금액이다. 값을 쉼표에서 끊으면
    #     "920"이 되어 카드에 엉뚱한 금액이 뜬다.
    cut = [i["id"] for i in with_vat
           if i["vat"].get("공급가액", "").replace("원", "").strip().isdigit()
           and len(i["vat"]["공급가액"].replace("원", "").strip()) <= 3]
    assert not cut, f"공급가액이 잘린 문항: {cut[:5]}"

    print(f"OK  {len(items)}문항 (회계1급 {len(hoegye)} / 세무2급 {len(semu)})")
    print(f"OK  대차일치 {sum(1 for i in items.values() if i['has_entry'])}건 전부")
    print(f"OK  매입매출 유형코드 {len(with_vat)}/{len(vat_items)}")


if __name__ == "__main__":
    main()
