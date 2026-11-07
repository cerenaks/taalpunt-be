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
    assert res == {"xp_gained": 9, "stars": 1, "streak": 1, "xp_total": 9, "duplicate": False}

    status, prog = call("GET /me/progress")
    assert status == 200
    assert prog["xp_total"] == 9 and prog["today_xp"] == 9 and prog["streak"] == 1
    assert prog["lessons"]["w1"]["best_stars"] == 1
    assert prog["mistakes_count"] == 1
    assert prog["daily_goal_xp"] == 50

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
