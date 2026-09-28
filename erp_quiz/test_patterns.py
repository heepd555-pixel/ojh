# -*- coding: utf-8 -*-
"""유형 교재가 386문항을 빠짐없이 묶는지, 화면이 실제로 뜨는지 확인한다.

    python test_patterns.py
"""
import accounts
import patterns
import web_app


def test_groups():
    gs = web_app.PATTERNS
    assert sum(g["count"] for g in gs) == len(web_app.PRACTICE), "문항이 새거나 겹친다"
    assert len({g["slug"] for g in gs}) == len(gs), "slug 중복"
    for g in gs:
        assert g["band"] in patterns.BANDS
        assert g["hint"], g["title"]
        # 분개가 있는 묶음은 반드시 대표 분개를 고를 수 있어야 한다
        if any(patterns.has_entry(web_app.PRACTICE[i]) for i in g["items"]):
            assert g["rep"] is not None, g["title"]
            assert g["variants"]
    # 매입매출은 전부 부가세 유형코드로 묶인다
    for g in gs:
        if g["band"] == patterns.BANDS[0]:
            assert accounts.vat_type(g["title"]), g["title"]
    print(f"OK  {len(gs)}묶음 / {len(web_app.PRACTICE)}문항")


def test_vat_reason():
    """유형코드 풀이가 확정답안의 실제 분개와 어긋나지 않는지 전수 대조한다.

    "공제된다"고 써 놓고 분개에 부가세대급금이 없으면 틀린 걸 가르치는 것이다.
    면세·영세는 세액이 0원이라 애초에 부가세대급금이 안 나오므로 뺀다.
    """
    checked = 0
    for p in web_app.PRACTICE:
        vat = p.get("vat") or {}
        kind = accounts.vat_type(vat.get("유형", ""))
        if not kind:
            continue
        steps = accounts.vat_reason(vat, p["question"])
        assert steps, p["id"]
        assert [s[0][0] for s in steps] == list("①②③④"[:len(steps)]), p["id"]
        if kind[2] != "매입" or kind[1] in ("면세", "카면", "현면", "영세"):
            continue
        claim = steps[-1][1]
        got_input_vat = any(
            accounts.clean(e["account"]) == "부가세대급금"
            for s in p["entry_sets"] for e in s["debit"] + s["credit"])
        assert (claim == "된다") == got_input_vat, (p["id"], vat["유형"], claim)
        checked += 1
    print(f"OK  유형코드 풀이 {checked}문항을 분개와 대조")


def test_pages():
    c = web_app.app.test_client()
    for url in ("/", "/silmu", "/pattern"):
        assert c.get(url).status_code == 200, url
    for b in patterns.BANDS:
        assert c.get("/pattern", query_string={"band": b}).status_code == 200, b
    for g in web_app.PATTERNS:
        r = c.get("/pattern/" + g["slug"])
        assert r.status_code == 200, g["slug"]
        body = r.get_data(as_text=True)
        assert "왜 이쪽인가" in body or not g["rep"], g["title"]
        # 매입매출 유형은 '왜 이 코드인가' 가 본론이라 반드시 떠야 한다
        if g["band"] == patterns.BANDS[0]:
            assert "왜 이 유형인가" in body and "헷갈린다" in body, g["title"]
    assert c.get("/pattern/없는것").status_code == 302
    # 유형 -> 카드로 넘어가면 그 유형 문항만 들어가야 한다
    g = next(x for x in web_app.PATTERNS if x["rep"])
    r = c.get("/silmu/start", query_string={"pat": g["slug"]})
    assert r.status_code == 302, r.status_code
    body = c.get("/silmu/card").get_data(as_text=True)
    assert "1 / %d" % g["count"] in body, g["count"]
    print(f"OK  화면 {len(web_app.PATTERNS) + 5}개")


if __name__ == "__main__":
    accounts.demo()
    test_groups()
    test_vat_reason()
    test_pages()
