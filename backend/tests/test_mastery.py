"""The mastery and spaced-review rule on its own (FR-002). The app's MasteryTests check the same cases."""
from taalpunt_api.mastery import INTERVALS, is_progress, is_question, next_state, normalize

D1, D2, D4 = "2026-11-14", "2026-11-15", "2026-11-17"


def test_a_correct_first_try_answer_makes_it_known():
    assert next_state("new", None, 0, True, D1) == ("known", D1, 1)
    assert next_state("learning", None, 0, True, D1) == ("known", D1, 1)


def test_a_miss_makes_it_not_known_and_starts_over():
    assert next_state("known", D1, 3, False, D2) == ("learning", None, 0)


def test_review_when_due_moves_up_a_box():
    # Box 1 is due after 1 day.
    assert next_state("known", D1, 1, True, D2) == ("known", D2, 2)


def test_review_before_it_is_due_changes_nothing():
    # Box 2 is due after 3 days; 1 day later doesn't count (no credit for cramming).
    assert next_state("known", D1, 2, True, D2) == ("known", D1, 2)
    assert next_state("known", D1, 2, True, D4) == ("known", D4, 3)


def test_top_box_stays_top():
    assert next_state("known", "2026-01-01", 5, True, D1)[2] == 5


def test_intervals_grow():
    assert list(INTERVALS) == [0, 1, 3, 7, 16, 35]


def test_rows_from_the_two_day_version_are_read_with_the_new_rule():
    assert normalize({"state": "learning", "last_correct": D1}) == ("known", D1, 1)
    assert normalize({"state": "learning"}) == ("learning", None, 0)
    assert normalize({"state": "known", "last_correct": D1}) == ("known", D1, 2)
    assert normalize({"state": "known", "last_correct": D1, "box": 4}) == ("known", D1, 4)
    assert normalize({}) == ("new", None, 0)


def test_only_answers_that_make_it_known_count_as_learned():
    assert is_progress("new", "known", True)
    assert is_progress("learning", "known", True)
    assert not is_progress("known", "known", True)
    assert not is_progress("new", "learning", False)


def test_question_ids():
    assert is_question("w1-03") and is_question("h7-15")
    assert not is_question("wonen-v07") and not is_question("match")
