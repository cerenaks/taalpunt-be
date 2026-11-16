import json
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from taalpunt_api import handlers, scoring
from taalpunt_api.validation import ValidationError, parse_session

TODAY = datetime.now(timezone.utc).date()


def event(route, sub="user-1", body=None, headers=None, token_use="access"):
    return {
        "routeKey": route,
        "headers": headers or {"x-local-date": TODAY.isoformat()},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": sub, "token_use": token_use, "username": sub}}}},
    }


def session(**over):
    body = {
        "session_id": str(uuid.uuid4()),
        "kind": "lesson",
        "lesson_id": "w1",
        "local_date": TODAY.isoformat(),
        "hearts_left": 4,
        "completed": True,
        "answers": [
            {"item_id": "w1-01", "correct": True},
            {"item_id": "w1-02", "correct": False},
            {"item_id": "w1-03", "correct": True},
            {"item_id": "w1-02", "correct": True, "retry": True},
        ],
    }
    body.update(over)
    return body


def call(route, **kw):
    r = handlers.api(event(route, **kw), None)
    return r["statusCode"], json.loads(r["body"])


# ---------- pure rules ----------

def test_xp_and_stars():
    assert scoring.lesson_xp(10, 5, True) == 5 + 20 + 5
    assert scoring.lesson_xp(8, 3, True) == 5 + 16
    assert scoring.lesson_xp(8, 0, False) == 0
    assert [scoring.lesson_stars(n) for n in range(5)] == [0, 1, 2, 3, 3]


def test_streak_rules():
    d = date(2026, 11, 5)
    assert scoring.next_streak(0, None, d) == 1
    assert scoring.next_streak(3, d - timedelta(days=1), d) == 4
    assert scoring.next_streak(3, d, d) == 3
    assert scoring.next_streak(3, d - timedelta(days=2), d) == 1
    assert scoring.visible_streak(3, d - timedelta(days=2), d) == 0
    assert scoring.visible_streak(3, d - timedelta(days=1), d) == 3


# ---------- validation ----------

def test_rejects_hearts_that_do_not_match_answers():
    with pytest.raises(ValidationError, match="hearts_left"):
        parse_session(session(hearts_left=5))


def test_rejects_far_away_date():
    with pytest.raises(ValidationError, match="local_date"):
        parse_session(session(local_date=(TODAY - timedelta(days=3)).isoformat()))


def test_rejects_bad_item_ids_and_kind_mismatch():
    with pytest.raises(ValidationError, match="item_id"):
        parse_session(session(answers=[{"item_id": "../etc", "correct": True}], hearts_left=5))
    with pytest.raises(ValidationError, match="lesson_id"):
        parse_session(session(kind="vocab", lesson_id="w1"))


def test_unfinished_session_must_be_out_of_hearts():
    with pytest.raises(ValidationError, match="unfinished"):
        parse_session(session(completed=False))


def test_match_rounds_do_not_cost_hearts():
    s = parse_session(session(hearts_left=5, answers=[{"item_id": "match", "correct": False}, {"item_id": "w1-01", "correct": True}]))
    assert s.hearts_left == 5


# ---------- API with DynamoDB ----------

def test_auth_required(table):
    r = handlers.api({"routeKey": "GET /me/progress", "requestContext": {}}, None)
    assert r["statusCode"] == 401
    r = handlers.api(event("GET /me/progress", token_use="id"), None)
    assert r["statusCode"] == 401


def test_session_updates_progress_and_mistakes(table):
    status, res = call("POST /me/sessions", body=session())
    assert status == 201
    # 2 first-try correct, 4 hearts left -> 5 + 2*2 = 9 XP; first finished run -> 1 star
    # w1-01 and w1-03 were first-try correct: both become known -> 2 learned
    assert res == {"xp_gained": 9, "stars": 1, "streak": 1, "xp_total": 9, "learned": 2, "duplicate": False}

    status, prog = call("GET /me/progress")
    assert status == 200
    assert prog["xp_total"] == 9 and prog["today_xp"] == 9 and prog["streak"] == 1
    assert prog["lessons"]["w1"]["best_stars"] == 1
    assert prog["mistakes_count"] == 1
    assert prog["daily_goal_xp"] == 50
    assert prog["daily_goal_questions"] == 10
    assert prog["today_learned"] == 2
    assert prog["items"] == {"w1-01": ["known", TODAY.isoformat(), "1"], "w1-02": ["learning", "", "0"],
                             "w1-03": ["known", TODAY.isoformat(), "1"]}

    status, m = call("GET /me/mistakes")
    assert [x["item_id"] for x in m["mistakes"]] == ["w1-02"]


def test_correct_first_try_clears_mistake(table):
    call("POST /me/sessions", body=session())
    call("POST /me/sessions", body=session(hearts_left=5, answers=[{"item_id": "w1-02", "correct": True}]))
    _, m = call("GET /me/mistakes")
    assert m["mistakes"] == []


def test_retried_session_is_not_counted_twice(table):
    body = session()
    s1, r1 = call("POST /me/sessions", body=body)
    s2, r2 = call("POST /me/sessions", body=body)
    assert (s1, s2) == (201, 200)
    assert r2["duplicate"] is True and r2["xp_total"] == r1["xp_total"]
    _, prog = call("GET /me/progress")
    assert prog["xp_total"] == 9


def test_failed_session_records_mistakes_but_no_xp(table):
    answers = [{"item_id": f"w1-0{i}", "correct": False} for i in range(1, 6)]
    status, res = call("POST /me/sessions", body=session(hearts_left=0, completed=False, answers=answers))
    assert status == 201 and res["xp_gained"] == 0 and res["stars"] == 0
    _, prog = call("GET /me/progress")
    assert prog["xp_total"] == 0 and prog["mistakes_count"] == 5 and "w1" not in prog["lessons"]


def test_one_star_per_finished_run_up_to_three(table):
    stars = [call("POST /me/sessions", body=session())[1]["stars"] for _ in range(4)]
    assert stars == [1, 2, 3, 3]
    _, prog = call("GET /me/progress")
    assert prog["lessons"]["w1"]["best_stars"] == 3
    assert prog["lessons"]["w1"]["completions"] == 4


def test_failed_run_earns_no_star(table):
    call("POST /me/sessions", body=session())
    answers = [{"item_id": f"w1-0{i}", "correct": False} for i in range(1, 6)]
    _, res = call("POST /me/sessions", body=session(hearts_left=0, completed=False, answers=answers))
    assert res["stars"] == 0
    _, prog = call("GET /me/progress")
    assert prog["lessons"]["w1"]["best_stars"] == 1


def test_users_are_isolated(table):
    call("POST /me/sessions", body=session(), sub="alice")
    _, prog = call("GET /me/progress", sub="bob")
    assert prog["xp_total"] == 0 and prog["mistakes_count"] == 0


def test_progress_returns_about_four_months_of_days(table):
    from taalpunt_api import store
    for n in range(150):
        table.put_item(Item={"PK": "USER#user-1", "SK": f"DAY#{(TODAY - timedelta(days=n)).isoformat()}", "xp": 10})
    _, prog = call("GET /me/progress")
    dates = [d["date"] for d in prog["days"]]
    assert len(dates) == store.DAY_HISTORY == 120
    assert dates[0] == TODAY.isoformat()  # newest first, so the calendar always has the latest days


def test_mix_sessions_do_not_get_stars(table):
    _, res = call("POST /me/sessions", body=session(kind="mix", lesson_id="mix"))
    assert res["stars"] == 0
    _, prog = call("GET /me/progress")
    assert prog["lessons"] == {} and prog["xp_total"] == 9


def test_bad_json_and_unknown_route(table):
    r = handlers.api({**event("POST /me/sessions"), "body": "{not json"}, None)
    assert r["statusCode"] == 400
    assert call("GET /nope")[0] == 404


def test_delete_account_removes_all_data(table, monkeypatch):
    call("POST /me/sessions", body=session())
    calls = []

    class FakeCognito:
        def admin_delete_user(self, **kw):
            calls.append(kw)

    import boto3 as real_boto3
    orig = real_boto3.client
    monkeypatch.setattr(real_boto3, "client", lambda name, *a, **k: FakeCognito() if name == "cognito-idp" else orig(name, *a, **k))
    monkeypatch.setenv("USER_POOL_ID", "eu-north-1_test")

    r = handlers.delete_account(event("DELETE /me"), None)
    assert r["statusCode"] == 200
    assert calls == [{"UserPoolId": "eu-north-1_test", "Username": "user-1"}]
    assert table.scan()["Items"] == []


# ---------- mastery (FR-002) ----------

def _item(table, qid, sub="user-1"):
    return table.get_item(Key={"PK": f"USER#{sub}", "SK": f"ITEM#{qid}"}).get("Item")


def test_review_the_next_day_moves_up_a_box(table):
    yesterday = (TODAY - timedelta(days=1)).isoformat()
    _, res = call("POST /me/sessions", body=session(local_date=yesterday))
    assert res["learned"] == 2
    _, res = call("POST /me/sessions", body=session())
    # Already known: a review isn't "learned" again, but box 1 was due, so it moves to box 2.
    assert res["learned"] == 0
    _, prog = call("GET /me/progress")
    assert prog["items"]["w1-01"] == ["known", TODAY.isoformat(), "2"]
    assert prog["items"]["w1-02"] == ["learning", "", "0"]
    assert prog["days"][-1]["learned"] == 2 and prog["today_learned"] == 0


def test_same_day_review_changes_nothing(table):
    call("POST /me/sessions", body=session())
    _, res = call("POST /me/sessions", body=session())
    assert res["learned"] == 0
    _, prog = call("GET /me/progress")
    assert prog["items"]["w1-01"] == ["known", TODAY.isoformat(), "1"]


def test_rows_from_the_two_day_version_show_with_the_new_rule(table):
    yesterday = (TODAY - timedelta(days=1)).isoformat()
    table.put_item(Item={"PK": "USER#user-1", "SK": "ITEM#w2-01", "state": "learning", "last_correct": yesterday})
    table.put_item(Item={"PK": "USER#user-1", "SK": "ITEM#w2-02", "state": "learning"})
    _, prog = call("GET /me/progress")
    assert prog["items"]["w2-01"] == ["known", yesterday, "1"]
    assert prog["items"]["w2-02"] == ["learning", "", "0"]


def test_miss_sends_known_back_to_learning(table):
    call("POST /me/sessions", body=session())
    assert _item(table, "w1-01")["state"] == "known"
    miss = session(answers=[{"item_id": "w1-01", "correct": False, "picked": "De gemeente"},
                            {"item_id": "w1-01", "correct": True, "retry": True}], hearts_left=4)
    _, res = call("POST /me/sessions", body=miss)
    assert res["learned"] == 0
    item = _item(table, "w1-01")
    assert item["state"] == "learning" and "last_correct" not in item and item["box"] == 0
    assert item["wrong_picks"] == ["De gemeente"]


def test_wrong_picks_keep_the_last_three(table):
    for pick in ["A", "B", "C", "D"]:
        body = session(answers=[{"item_id": "w1-05", "correct": False, "picked": pick},
                                {"item_id": "w1-05", "correct": True, "retry": True}], hearts_left=4)
        call("POST /me/sessions", body=body)
    assert _item(table, "w1-05")["wrong_picks"] == ["B", "C", "D"]


def test_matching_rounds_get_no_mastery(table):
    body = session(kind="vocab", lesson_id="voc-wonen",
                   answers=[{"item_id": "wonen-v01", "correct": True}, {"item_id": "match", "correct": True}],
                   hearts_left=5)
    status, res = call("POST /me/sessions", body=body)
    assert status == 201 and res["learned"] == 0
    _, prog = call("GET /me/progress")
    assert set(prog["items"]) == {"wonen-v01"}  # words get mastery since FR-004


def test_picked_must_be_short_text():
    with pytest.raises(ValidationError):
        parse_session(session(answers=[{"item_id": "w1-01", "correct": False, "picked": 5}], hearts_left=4))
    with pytest.raises(ValidationError):
        parse_session(session(answers=[{"item_id": "w1-01", "correct": False, "picked": "x" * 201}], hearts_left=4))


# ---------- Free and Premium (FR-001) ----------

from taalpunt_api import store  # noqa: E402


def test_no_premium_by_default(table):
    _, prog = call("GET /me/progress")
    assert prog["premium"] == {"active": False, "until": None, "sources": []}


def test_manual_premium_grant_and_revoke(table):
    store.grant_premium("user-1", "manual", note="tester")
    _, prog = call("GET /me/progress")
    assert prog["premium"] == {"active": True, "until": None, "sources": ["manual"]}
    store.revoke_premium("user-1", "manual")
    _, prog = call("GET /me/progress")
    assert prog["premium"]["active"] is False


def test_premium_until_a_date(table):
    store.grant_premium("user-1", "manual", until="2000-01-01T00:00:00+00:00")
    assert store.get_premium("user-1")["active"] is False  # already ended
    store.grant_premium("user-1", "manual", until="2999-01-01T00:00:00+00:00")
    assert store.get_premium("user-1") == {"active": True, "until": "2999-01-01T00:00:00+00:00", "sources": ["manual"]}


def test_premium_content_needs_premium(table, monkeypatch):
    monkeypatch.setattr(handlers, "_premium_bundle", lambda: '{"units": []}')
    status, body = call("GET /me/content/premium")
    assert status == 403 and body["error"] == "premium_required"
    store.grant_premium("user-1", "manual")
    r = handlers.api(event("GET /me/content/premium"), None)
    assert r["statusCode"] == 200 and r["body"] == '{"units": []}'
    assert r["headers"]["cache-control"] == "private, no-store"


def _premium_session():
    return session(lesson_id="k2", answers=[{"item_id": "k2-01", "correct": True}], hearts_left=5)


def test_premium_lessons_are_open_while_the_switch_is_off(table, monkeypatch):
    monkeypatch.setenv("PREMIUM_ENABLED", "false")
    assert call("POST /me/sessions", body=_premium_session())[0] == 201


def test_premium_lessons_need_premium_when_switched_on(table, monkeypatch):
    monkeypatch.setenv("PREMIUM_ENABLED", "true")
    monkeypatch.setenv("PREMIUM_LESSONS", "k2,voc-werk,test-werk")
    monkeypatch.setenv("PREMIUM_UNITS", "werk")
    status, body = call("POST /me/sessions", body=_premium_session())
    assert status == 403 and body["error"] == "premium_required"
    # Free lessons still work, and a Mix with only free questions too.
    assert call("POST /me/sessions", body=session())[0] == 201
    store.grant_premium("user-1", "manual")
    assert call("POST /me/sessions", body=_premium_session())[0] == 201


# ---------- FR-004: exam practice, My list, word mastery, best score ----------

def exam_answers(n=40, wrong=0):
    ids = [f"w{1 + i // 10}-{1 + i % 10:02d}" for i in range(n)]
    return [{"item_id": q, "correct": i >= wrong} for i, q in enumerate(ids)]


def test_exam_has_no_hearts_and_no_retries():
    s = parse_session(session(kind="exam", lesson_id="exam", hearts_left=5, answers=exam_answers(40, wrong=12)))
    assert s.passed and s.unique_items == 40
    with pytest.raises(ValidationError, match="hearts"):
        parse_session(session(kind="exam", lesson_id="exam", hearts_left=0, answers=exam_answers(40, wrong=12)))
    with pytest.raises(ValidationError, match="retries"):
        parse_session(session(kind="exam", lesson_id="exam", hearts_left=5,
                              answers=exam_answers(3) + [{"item_id": "w1-01", "correct": True, "retry": True}]))
    with pytest.raises(ValidationError, match="ended"):
        parse_session(session(kind="exam", lesson_id="exam", hearts_left=5, completed=False, answers=exam_answers(3)))


def test_exam_counts_for_mastery(table):
    code, body = call("POST /me/sessions", body=session(kind="exam", lesson_id="exam", hearts_left=5,
                                                        answers=exam_answers(40, wrong=10)))
    assert code == 201 and body["learned"] == 30
    _, progress = call("GET /me/progress")
    assert progress["items"]["w1-01"][0] == "learning" and progress["items"]["w4-10"][0] == "known"
    assert "exam" not in progress["lessons"]


def test_exam_needs_premium_when_switched_on(table, monkeypatch):
    monkeypatch.setenv("PREMIUM_ENABLED", "true")
    body = session(kind="exam", lesson_id="exam", hearts_left=5, answers=exam_answers(5))
    code, err = call("POST /me/sessions", body=body)
    assert code == 403 and err["error"] == "premium_required"
    from taalpunt_api import store
    store.grant_premium("user-1", "manual")
    assert call("POST /me/sessions", body=body)[0] == 201


def test_words_get_mastery_but_dont_count_as_learned(table):
    answers = [{"item_id": "wonen-v01", "correct": True}, {"item_id": "wonen-v02", "correct": False},
               {"item_id": "match", "correct": True}]
    code, body = call("POST /me/sessions", body=session(kind="vocab", lesson_id="voc-wonen", hearts_left=4,
                                                        answers=answers))
    assert code == 201 and body["learned"] == 0
    _, progress = call("GET /me/progress")
    assert progress["items"]["wonen-v01"][0] == "known" and progress["items"]["wonen-v02"][0] == "learning"
    assert "match" not in progress["items"] and progress["today_learned"] == 0


def test_practice_test_keeps_its_best_score(table):
    def run(wrong):
        answers = [{"item_id": f"w1-{i:02d}", "correct": i > wrong} for i in range(1, 11)]
        call("POST /me/sessions", body=session(kind="test", lesson_id="test-wonen", hearts_left=5 - wrong,
                                               answers=answers))
        return call("GET /me/progress")[1]["lessons"]["test-wonen"]["best_score"]
    assert run(3) == 70
    assert run(1) == 90
    assert run(4) == 90  # a worse run doesn't lower it


def test_my_list(table):
    assert call("GET /me/progress")[1]["saved"] == []
    assert call("POST /me/saved", body={"item_id": "w1-03", "saved": True}) == (200, {"item_id": "w1-03", "saved": True})
    call("POST /me/saved", body={"item_id": "wonen-v07", "saved": True})
    call("POST /me/saved", body={"item_id": "w1-03", "saved": True})  # saving again keeps the first time
    assert call("GET /me/progress")[1]["saved"] == ["w1-03", "wonen-v07"]
    call("POST /me/saved", body={"item_id": "w1-03", "saved": False})
    assert call("GET /me/progress")[1]["saved"] == ["wonen-v07"]
    for bad in ({"item_id": "match", "saved": True}, {"item_id": "w1-03"}, {"item_id": "../x", "saved": False}, []):
        assert call("POST /me/saved", body=bad)[0] == 400


def test_my_list_round_is_accepted(table):
    answers = [{"item_id": "w1-03", "correct": True}, {"item_id": "wonen-v07", "correct": True}]
    assert call("POST /me/sessions", body=session(kind="saved", lesson_id="saved", hearts_left=5,
                                                  answers=answers))[0] == 201


def test_exam_date_and_how_it_went(table):
    assert call("GET /me/progress")[1]["exam"] == {"date": None, "passed_on": None}
    assert call("POST /me/exam", body={"exam_date": "2026-12-01"}) == (200, {"exam": {"date": "2026-12-01", "passed_on": None}})
    assert call("POST /me/exam", body={"exam_date": None})[1]["exam"]["date"] is None
    call("POST /me/exam", body={"exam_date": "2026-11-12"})
    # Taking it again: the old exam is kept as "retake", then a new date.
    call("POST /me/exam", body={"exam_date": "2026-11-12", "outcome": "retake"})
    call("POST /me/exam", body={"exam_date": "2027-01-20"})
    code, body = call("POST /me/exam", body={"exam_date": "2027-01-20", "outcome": "passed"})
    assert code == 200 and body["exam"] == {"date": None, "passed_on": "2027-01-20"}
    from taalpunt_api import store
    rows = {i["SK"]: i["outcome"] for i in store._query_prefix("user-1", "EXAM#")}
    assert rows == {"EXAM#2026-11-12": "retake", "EXAM#2027-01-20": "passed"}


def test_exam_requests_are_checked(table):
    for bad in ({}, {"exam_date": "12-01-2026"}, {"exam_date": 5}, {"exam_date": "2026-12-01", "outcome": "maybe"},
                {"exam_date": None, "outcome": "passed"}, {"exam_date": "1999-01-01"}):
        assert call("POST /me/exam", body=bad)[0] == 400, bad
