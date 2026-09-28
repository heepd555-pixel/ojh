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


def test_pages():
    c = web_app.app.test_client()
    for url in ("/", "/silmu", "/pattern"):
        assert c.get(url).status_code == 200, url
    for b in patterns.BANDS:
        assert c.get("/pattern", query_string={"band": b}).status_code == 200, b
    for g in web_app.PATTERNS:
        r = c.get("/pattern/" + g["slug"])
        assert r.status_code == 200, g["slug"]
        assert "왜 이쪽인가" in r.get_data(as_text=True) or not g["rep"], g["title"]
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
    test_pages()
