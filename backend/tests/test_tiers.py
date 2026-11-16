"""Free and Premium content rules (FR-001)."""
import json
from pathlib import Path

from taalpunt_api import tiers

DOC = json.loads((Path(__file__).resolve().parents[2] / "content" / "knm.json").read_text(encoding="utf-8"))


def test_wonen_and_every_first_lesson_are_free():
    """Free: all of Wonen and the first lesson of every other theme. A split first lesson (FR-004: i1 and
    i1b) stays free in both halves; its questions keep the first lesson's id."""
    for i, unit in enumerate(DOC["units"]):
        first = unit["lessons"][0]["id"]
        for lesson in unit["lessons"]:
            from_first = all(q["id"].rsplit("-", 1)[0] == first for q in lesson["questions"])
            assert tiers.is_premium_lesson(lesson) == (i > 0 and not from_first), lesson["id"]


def test_premium_ids():
    lessons, units = tiers.premium_ids(DOC)
    assert "w1" not in lessons and "voc-wonen" not in lessons and "test-wonen" not in lessons
    assert "k1" not in lessons and "k2" in lessons
    assert "voc-werk" in lessons and "test-werk" in lessons
    assert "wonen" not in units and "werk" in units


def test_free_bundle_has_140_questions_and_no_premium_words():
    free = tiers.free_bundle(DOC)
    assert sum(len(l["questions"]) for u in free["units"] for l in u["lessons"]) == 140
    assert free["units"][0]["vocabulary"] and not free["units"][1]["vocabulary"]
    # The original is untouched.
    assert sum(len(l["questions"]) for u in DOC["units"] for l in u["lessons"]) == 609


def test_free_bundle_keeps_the_counts():
    free = tiers.free_bundle(DOC)
    assert sum(l["question_count"] for u in free["units"] for l in u["lessons"]) == 609
    assert sum(u["word_count"] for u in free["units"]) == sum(len(u["vocabulary"]) for u in DOC["units"])
    assert "question_count" not in DOC["units"][0]["lessons"][0]


def test_needs_premium():
    lessons, units = tiers.premium_ids(DOC)
    assert not tiers.needs_premium("w1", ["w1-01"], lessons, units)
    assert not tiers.needs_premium("k1", ["k1-01"], lessons, units)
    assert tiers.needs_premium("k2", ["k2-01"], lessons, units)
    assert tiers.needs_premium("voc-werk", ["werk-v01"], lessons, units)
    # A Mix round can sneak in Premium questions or words.
    assert tiers.needs_premium("mix", ["w1-01", "p3-02"], lessons, units)
    assert tiers.needs_premium("mistakes", ["politiek-v03"], lessons, units)
    assert not tiers.needs_premium("mix", ["w1-01", "wonen-v02", "match"], lessons, units)
