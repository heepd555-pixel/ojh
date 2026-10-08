# -*- coding: utf-8 -*-
import json, os, unittest
import grader

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "official.json"), encoding="utf-8") as f:
    PRACTICE = json.load(f)


def perfect(p, code=False):
    """정답 분개를 그대로 입력한 제출물."""
    s = grader._pick_sets(p)[0]
    rows = [{"side": ln["side"], "acct": ln["acct"], "amt": "{:,}".format(ln["amt"])}
            for ln in grader._answer_lines(s)]
    sub = {"rows": rows}
    v = p.get("vat")
    if v:
        sub["vat"] = {"type": v.get("유형", ""), "supply": v.get("공급가액", ""), "vat": v.get("부가세", "")}
    return sub


class GraderTest(unittest.TestCase):
    def test_every_gradable_item_scores_full_with_its_own_answer(self):
        n = 0
        for p in PRACTICE:
            if not grader.gradable(p):
                continue
            r = grader.grade(p, perfect(p))
            self.assertTrue(r["full"], (p["id"], r["lines"], r["vat"]))
            self.assertEqual(r["score"], float(p["points"]), p["id"])
            n += 1
        self.assertGreater(n, 200)

    def test_empty_submission_is_zero(self):
        p = next(p for p in PRACTICE if grader.gradable(p))
        r = grader.grade(p, {"rows": []})
        self.assertEqual(r["score"], 0)
        self.assertFalse(r["full"])

    def test_wrong_amount_is_partial_and_flagged(self):
        p = PRACTICE[[x["id"] for x in PRACTICE].index("전산회계1급|118회|문제2|1")]
        sub = perfect(p)
        sub["rows"][0]["amt"] = "999"
        r = grader.grade(p, sub)
        self.assertEqual(r["lines"][0]["status"], "amount")
        self.assertTrue(0 < r["score"] < 3)

    def test_extra_line_costs_points(self):
        p = PRACTICE[[x["id"] for x in PRACTICE].index("전산회계1급|118회|문제2|1")]
        sub = perfect(p)
        sub["rows"].append({"side": "debit", "acct": "현금", "amt": "1"})
        r = grader.grade(p, sub)
        self.assertFalse(r["full"])
        self.assertLess(r["score"], 3)

    def test_account_code_and_blank_rows(self):
        self.assertEqual(grader.resolve("101")[0], "현금")
        self.assertEqual(grader.resolve("0101")[0], "현금")
        self.assertEqual(grader.resolve("101 현금")[0], "현금")
        self.assertIsNone(grader.resolve("  "))
        self.assertEqual(grader.parse_amount("1,000,000원"), 1000000)
        self.assertIsNone(grader.parse_amount(""))

    def test_error_correction_grades_the_after_entry(self):
        p = next(p for p in PRACTICE if p["section"] == "문제4" and p["exam"] == "전산회계1급"
                 and len(p["entry_sets"]) == 2)
        r = grader.grade(p, perfect(p))
        self.assertTrue(r["full"])
        self.assertEqual(r["label"], "수정 후")


if __name__ == "__main__":
    unittest.main()
