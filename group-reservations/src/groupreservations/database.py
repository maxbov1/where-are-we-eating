"""Normalized persistence for local SQLite and production Aurora parity."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings
from .scoring import score_confidence


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    path = Path(settings.database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def init_db() -> None:
    with _connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY, email TEXT UNIQUE, cognito_sub TEXT UNIQUE,
          guest_token_hash TEXT UNIQUE, is_temporary INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS surveys (
          id TEXT PRIMARY KEY, organizer_id TEXT NOT NULL REFERENCES users(id),
          public_token TEXT NOT NULL UNIQUE, event_name TEXT NOT NULL,
          location TEXT NOT NULL, dates_json TEXT NOT NULL, times_json TEXT NOT NULL,
          questions_json TEXT NOT NULL DEFAULT '{}', location_place_id TEXT,
          location_lat REAL, location_lng REAL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS survey_questions (
          id TEXT PRIMARY KEY, survey_id TEXT NOT NULL REFERENCES surveys(id) ON DELETE CASCADE,
          question_key TEXT NOT NULL, label TEXT NOT NULL, input_type TEXT NOT NULL,
          max_selections INTEGER, sort_order INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          UNIQUE(survey_id, question_key)
        );
        CREATE TABLE IF NOT EXISTS survey_options (
          id TEXT PRIMARY KEY, question_id TEXT NOT NULL REFERENCES survey_questions(id) ON DELETE CASCADE,
          value TEXT NOT NULL, label TEXT NOT NULL, sort_order INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          UNIQUE(question_id, value)
        );
        CREATE TABLE IF NOT EXISTS survey_responses (
          id TEXT PRIMARY KEY, survey_id TEXT NOT NULL REFERENCES surveys(id) ON DELETE CASCADE,
          respondent_user_id TEXT NOT NULL REFERENCES users(id), dates_json TEXT NOT NULL DEFAULT '[]',
          times_json TEXT NOT NULL DEFAULT '[]', submitted_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          origin_place_id TEXT, origin_label TEXT, origin_lat REAL, origin_lng REAL,
          UNIQUE(survey_id, respondent_user_id)
        );
        CREATE TABLE IF NOT EXISTS response_answers (
          response_id TEXT NOT NULL REFERENCES survey_responses(id) ON DELETE CASCADE,
          question_id TEXT NOT NULL REFERENCES survey_questions(id) ON DELETE CASCADE,
          option_id TEXT NOT NULL REFERENCES survey_options(id) ON DELETE CASCADE,
          PRIMARY KEY(response_id, question_id, option_id)
        );
        """)
        # Additive columns keep existing local databases bootable during migration.
        columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        if "cognito_sub" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN cognito_sub TEXT")
        if "guest_token_hash" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN guest_token_hash TEXT")
        survey_columns = {row["name"] for row in db.execute("PRAGMA table_info(surveys)")}
        if "questions_json" not in survey_columns:
            db.execute("ALTER TABLE surveys ADD COLUMN questions_json TEXT NOT NULL DEFAULT '{}'")
        for column, definition in (("location_place_id", "TEXT"), ("location_lat", "REAL"), ("location_lng", "REAL")):
            if column not in survey_columns:
                db.execute(f"ALTER TABLE surveys ADD COLUMN {column} {definition}")
        response_columns = {row["name"] for row in db.execute("PRAGMA table_info(survey_responses)")}
        if "dates_json" not in response_columns:
            db.execute("ALTER TABLE survey_responses ADD COLUMN dates_json TEXT NOT NULL DEFAULT '[]'")
        if "times_json" not in response_columns:
            db.execute("ALTER TABLE survey_responses ADD COLUMN times_json TEXT NOT NULL DEFAULT '[]'")
        for column, definition in (("origin_place_id", "TEXT"), ("origin_label", "TEXT"), ("origin_lat", "REAL"), ("origin_lng", "REAL")):
            if column not in response_columns:
                db.execute(f"ALTER TABLE survey_responses ADD COLUMN {column} {definition}")
        for survey in db.execute("SELECT id, questions_json FROM surveys").fetchall():
            questions = json.loads(survey["questions_json"] or "{}")
            for order, (key, options) in enumerate(questions.items()):
                existing_question = db.execute("SELECT id FROM survey_questions WHERE survey_id=? AND question_key=?", (survey["id"], key)).fetchone()
                qid = existing_question["id"] if existing_question else f"legacy-q-{survey['id']}-{key}"
                db.execute("INSERT OR IGNORE INTO survey_questions (id,survey_id,question_key,label,input_type,max_selections,sort_order) VALUES (?,?,?,?,?,?,?)", (qid, survey["id"], key, key.replace("_", " ").title(), "checkbox" if key in {"cuisine", "dietary"} else "radio", 2 if key == "cuisine" else None, order))
                for option_order, value in enumerate(options):
                    db.execute("INSERT OR IGNORE INTO survey_options (id,question_id,value,label,sort_order) VALUES (?,?,?,?,?)", (f"legacy-o-{survey['id']}-{key}-{option_order}", qid, value, value, option_order))
            if "responses_json" in survey_columns and not db.execute("SELECT 1 FROM survey_responses WHERE survey_id=? LIMIT 1", (survey["id"],)).fetchone():
                legacy = db.execute("SELECT responses_json,dates_json,times_json FROM surveys WHERE id=?", (survey["id"],)).fetchone()
                for item in json.loads(legacy["responses_json"] or "[]"):
                    legacy_token = _hash_token(f"legacy:{item['respondent_user_id']}")
                    guest = db.execute("SELECT id FROM users WHERE guest_token_hash=?", (legacy_token,)).fetchone()
                    guest_id = guest["id"] if guest else secrets.token_urlsafe(12)
                    if not guest:
                        db.execute("INSERT INTO users (id,guest_token_hash,is_temporary,created_at) VALUES (?,?,1,?)", (guest_id, legacy_token, _now()))
                    response_id = secrets.token_urlsafe(12); now = _now()
                    db.execute("INSERT INTO survey_responses (id,survey_id,respondent_user_id,dates_json,times_json,submitted_at,updated_at) VALUES (?,?,?,?,?,?,?)", (response_id, survey["id"], guest_id, json.dumps(item.get("dates", json.loads(legacy["dates_json"]))), json.dumps(item.get("times", json.loads(legacy["times_json"]))), now, now))
                    for key in ("cuisine", "distance", "vibe", "price"):
                        values = item.get("cuisines", []) if key == "cuisine" else ([item[key]] if item.get(key) else [])
                        q = db.execute("SELECT id FROM survey_questions WHERE survey_id=? AND question_key=?", (survey["id"], key)).fetchone()
                        if q:
                            for value in values:
                                option = db.execute("SELECT id FROM survey_options WHERE question_id=? AND value=?", (q["id"], value)).fetchone()
                                if option:
                                    db.execute("INSERT OR IGNORE INTO response_answers (response_id,question_id,option_id) VALUES (?,?,?)", (response_id, q["id"], option["id"]))


def create_user(email: str, cognito_sub: str | None = None) -> dict[str, Any]:
    init_db()
    with _connect() as db:
        existing = db.execute("SELECT * FROM users WHERE email=? OR (? IS NOT NULL AND cognito_sub=?)", (email, cognito_sub, cognito_sub)).fetchone()
        if existing:
            return dict(existing)
        user = {"id": secrets.token_urlsafe(12), "email": email, "cognito_sub": cognito_sub, "is_temporary": False}
        db.execute("INSERT INTO users (id,email,cognito_sub,is_temporary,created_at) VALUES (?,?,?,0,?)", (user["id"], email, cognito_sub, _now()))
        return user


def _insert_questions(db: sqlite3.Connection, survey_id: str, questions: dict[str, list[str]]) -> None:
    for order, (key, options) in enumerate(questions.items()):
        qid = secrets.token_urlsafe(12)
        db.execute("INSERT INTO survey_questions (id,survey_id,question_key,label,input_type,max_selections,sort_order) VALUES (?,?,?,?,?,?,?)", (qid, survey_id, key, key.replace("_", " ").title(), "checkbox" if key in {"cuisine", "dietary"} else "radio", 2 if key == "cuisine" else None, order))
        for option_order, value in enumerate(options):
            db.execute("INSERT INTO survey_options (id,question_id,value,label,sort_order) VALUES (?,?,?,?,?)", (secrets.token_urlsafe(12), qid, value, value, option_order))


def create_survey(organizer_id: str, event_name: str, location: str, dates: list[str], times: list[str], questions: dict[str, list[str]], location_place_id: str | None = None, location_lat: float | None = None, location_lng: float | None = None) -> dict[str, Any]:
    init_db()
    survey = {"id": secrets.token_urlsafe(12), "organizer_id": organizer_id, "public_token": secrets.token_urlsafe(18), "event_name": event_name, "location": location, "location_place_id": location_place_id, "location_lat": location_lat, "location_lng": location_lng, "dates": dates, "times": times, "questions": questions, "responses": [], "created_at": _now()}
    with _connect() as db:
        db.execute("INSERT INTO surveys (id,organizer_id,public_token,event_name,location,location_place_id,location_lat,location_lng,dates_json,times_json,questions_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (survey["id"], organizer_id, survey["public_token"], event_name, location, location_place_id, location_lat, location_lng, json.dumps(dates), json.dumps(times), json.dumps(questions), survey["created_at"]))
        _insert_questions(db, survey["id"], questions)
    return survey


def _question_map(db: sqlite3.Connection, survey_id: str) -> dict[str, tuple[str, dict[str, str]]]:
    rows = db.execute("SELECT q.id,q.question_key,o.id option_id,o.value FROM survey_questions q JOIN survey_options o ON o.question_id=q.id WHERE q.survey_id=? AND q.enabled=1 AND o.enabled=1 ORDER BY q.sort_order,o.sort_order", (survey_id,)).fetchall()
    result: dict[str, tuple[str, dict[str, str]]] = {}
    for row in rows:
        result.setdefault(row["question_key"], (row["id"], {}))[1][row["value"]] = row["option_id"]
    return result


def _responses(db: sqlite3.Connection, survey_id: str) -> list[dict[str, Any]]:
    rows = db.execute("SELECT r.id,r.respondent_user_id,r.dates_json,r.times_json,r.origin_place_id,r.origin_label,r.origin_lat,r.origin_lng,q.question_key,o.value FROM survey_responses r LEFT JOIN response_answers a ON a.response_id=r.id LEFT JOIN survey_questions q ON q.id=a.question_id LEFT JOIN survey_options o ON o.id=a.option_id WHERE r.survey_id=? ORDER BY r.submitted_at", (survey_id,)).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        response = result.setdefault(row["id"], {"response_id": row["id"], "respondent_user_id": row["respondent_user_id"], "dates": json.loads(row["dates_json"]), "times": json.loads(row["times_json"]), "origin_place_id": row["origin_place_id"], "origin_label": row["origin_label"], "origin_lat": row["origin_lat"], "origin_lng": row["origin_lng"]})
        if row["question_key"] and row["value"]:
            response.setdefault(row["question_key"], []).append(row["value"])
    return list(result.values())


def get_survey(identifier: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as db:
        row = db.execute("SELECT * FROM surveys WHERE id=? OR public_token=?", (identifier, identifier)).fetchone()
        if not row:
            return None
        survey = dict(row)
        survey["dates"] = json.loads(survey.pop("dates_json")); survey["times"] = json.loads(survey.pop("times_json")); survey.pop("questions_json", None)
        survey["questions"] = {key: list(options) for key, (_, options) in _question_map(db, survey["id"]).items()}
        survey["responses"] = _responses(db, survey["id"])
        return survey


def append_response(public_token: str, guest_token: str, dates: list[str], times: list[str], answers: dict[str, list[str]], origin_place_id: str | None = None, origin_label: str | None = None, origin_lat: float | None = None, origin_lng: float | None = None) -> dict[str, Any]:
    init_db(); token_hash = _hash_token(guest_token)
    with _connect() as db:
        survey = db.execute("SELECT id FROM surveys WHERE public_token=?", (public_token,)).fetchone()
        if not survey:
            raise LookupError("Survey not found")
        guest = db.execute("SELECT id FROM users WHERE guest_token_hash=?", (token_hash,)).fetchone()
        if guest:
            guest_id = guest["id"]
        else:
            guest_id = secrets.token_urlsafe(12)
            db.execute("INSERT INTO users (id,guest_token_hash,is_temporary,created_at) VALUES (?,?,1,?)", (guest_id, token_hash, _now()))
        now = _now(); response_id = secrets.token_urlsafe(12)
        db.execute("INSERT INTO survey_responses (id,survey_id,respondent_user_id,dates_json,times_json,origin_place_id,origin_label,origin_lat,origin_lng,submitted_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(survey_id,respondent_user_id) DO UPDATE SET dates_json=excluded.dates_json,times_json=excluded.times_json,origin_place_id=excluded.origin_place_id,origin_label=excluded.origin_label,origin_lat=excluded.origin_lat,origin_lng=excluded.origin_lng,updated_at=excluded.updated_at", (response_id, survey["id"], guest_id, json.dumps(dates), json.dumps(times), origin_place_id, origin_label, origin_lat, origin_lng, now, now))
        response = db.execute("SELECT id FROM survey_responses WHERE survey_id=? AND respondent_user_id=?", (survey["id"], guest_id)).fetchone()
        db.execute("DELETE FROM response_answers WHERE response_id=?", (response["id"],))
        question_map = _question_map(db, survey["id"])
        for key, values in answers.items():
            if key not in question_map:
                continue
            qid, options = question_map[key]
            for value in values:
                if value in options:
                    db.execute("INSERT INTO response_answers (response_id,question_id,option_id) VALUES (?,?,?)", (response["id"], qid, options[value]))
        return {"response_id": response["id"], "respondent_user_id": guest_id, "dates": dates, "times": times, "origin_place_id": origin_place_id, "origin_label": origin_label, "origin_lat": origin_lat, "origin_lng": origin_lng, **{key: values for key, values in answers.items() if key in question_map}}


def aggregate_survey(identifier: str) -> dict[str, Any] | None:
    survey = get_survey(identifier)
    if not survey:
        return None
    summary: dict[str, dict[str, int]] = {}
    for response in survey["responses"]:
        for key, values in response.items():
            if key in {"response_id", "respondent_user_id"}:
                continue
            bucket = summary.setdefault(key, {})
            for value in values:
                bucket[value] = bucket.get(value, 0) + 1

    def leaders(key: str) -> list[dict[str, Any]]:
        return [{"value": value, "votes": votes} for value, votes in sorted(summary.get(key, {}).items(), key=lambda item: (-item[1], item[0])) if votes == max(summary.get(key, {}).values(), default=0)]

    def consensus(key: str) -> str:
        votes = summary.get(key, {})
        if not votes:
            return "no responses"
        top = max(votes.values())
        if list(votes.values()).count(top) > 1:
            return "tie"
        if top == len(survey["responses"]):
            return "unanimous"
        if top >= len(survey["responses"])*0.75:
            return "strong"
        if top >= len(survey["responses"])*0.5:
            return "moderate"
        return "split"

    # Score every schedule and active question, even ones nobody answered yet
    # (consensus() returns "no responses" for those, which scoring turns into a note).
    scored_dimensions = ["dates", "times", *[key for key in survey["questions"] if key not in {"dates", "times"}]]
    confidence = score_confidence(
        len(survey["responses"]),
        summary,
        {key: consensus(key) for key in scored_dimensions},
    )
    pair_counts = [
        {
            "date": date,
            "time": time,
            "votes": sum(
                date in response.get("dates", []) and time in response.get("times", [])
                for response in survey["responses"]
            ),
        }
        for date in survey["dates"]
        for time in survey["times"]
    ]
    pair_top = max((pair["votes"] for pair in pair_counts), default=0)
    pair_leaders = [pair for pair in pair_counts if pair["votes"] == pair_top]

    report = {
        "event": {"name": survey["event_name"], "location": survey["location"]},
        "response_count": len(survey["responses"]),
        "active_questions": list(survey["questions"]),
        "schedule": {"date_leaders": leaders("dates"), "time_leaders": leaders("times"), "pair_leaders": pair_leaders, "date_consensus": consensus("dates"), "time_consensus": consensus("times")},
        "preferences": {key: {"leaders": leaders(key), "consensus": consensus(key), "votes": values} for key, values in summary.items() if key not in {"dates", "times"}},
        "preference_summary": summary,
        "confidence": confidence,
        "responses": [{key: value for key, value in response.items() if key not in {"respondent_user_id", "response_id"}} for response in survey["responses"]],
    }
    return {"survey_id": survey["id"], "event_name": survey["event_name"], "location": survey["location"], "dates": survey["dates"], "times": survey["times"], "questions": survey["questions"], "response_count": len(survey["responses"]), "preference_summary": summary, "confidence": confidence, "report": report, "responses": report["responses"]}
