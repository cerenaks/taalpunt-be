"""The mastery rule on its own (FR-002). The app's MasteryTests check the same cases."""
from taalpunt_api.mastery import is_progress, is_question, next_state

D1, D2 = "2026-11-14", "2026-11-15"


def test_first_correct_answer_is_learning():
    assert next_state("new", None, True, D1) == ("learning", D1)


def test_second_correct_day_makes_it_known():
    assert next_state("learning", D1, True, D2) == ("known", D2)


def test_same_day_does_not_count_twice():
    assert next_state("learning", D1, True, D1) == ("learning", D1)


def test_a_miss_resets_the_count():
    assert next_state("known", D1, False, D2) == ("learning", None)
    assert next_state("learning", None, True, D2) == ("learning", D2)


def test_known_stays_known_when_correct():
    assert next_state("known", D1, True, D2) == ("known", D2)


def test_only_correct_answers_that_move_up_count_as_learned():
    assert is_progress("new", "learning", True)
    assert is_progress("learning", "known", True)
    assert not is_progress("new", "learning", False)  # seen and missed: not learned
    assert not is_progress("learning", "learning", True)


def test_question_ids():
    assert is_question("w1-03") and is_question("h7-15")
    assert not is_question("wonen-v07") and not is_question("match")
