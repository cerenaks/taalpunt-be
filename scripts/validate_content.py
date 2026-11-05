#!/usr/bin/env python3
"""Validate content/knm.json against content/schema.json plus rules a schema can't express.

Usage: python scripts/validate_content.py [path/to/knm.json]
Exit code 0 = valid, 1 = problems found.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "content" / "schema.json"


def semantic_checks(doc: dict) -> list[str]:
    problems: list[str] = []
    seen_ids: set[str] = set()
    for unit in doc["units"]:
        for lesson in unit["lessons"]:
            for q in lesson["questions"]:
                qid = q["id"]
                if qid in seen_ids:
                    problems.append(f"{qid}: duplicate question id")
                seen_ids.add(qid)
                if not qid.startswith(lesson["id"] + "-"):
                    problems.append(f"{qid}: id must start with lesson id '{lesson['id']}-'")
                t = q["type"]
                if t in ("multiple_choice", "fill_blank"):
                    if not isinstance(q["answer"], str):
                        problems.append(f"{qid}: answer must be text")
                    opts = [q["answer"], *q.get("distractors", [])]
                    if len(opts) < 2:
                        problems.append(f"{qid}: needs at least one distractor")
                    if len(set(opts)) != len(opts):
                        problems.append(f"{qid}: answer repeated among distractors")
                    if "prompt" not in q:
                        problems.append(f"{qid}: missing prompt")
                if t == "fill_blank" and "___" not in q.get("prompt", ""):
                    problems.append(f"{qid}: fill_blank prompt needs ___")
                if t == "true_false" and not isinstance(q["answer"], bool):
                    problems.append(f"{qid}: true_false answer must be true/false")
                if t == "build_sentence":
                    if not isinstance(q["answer"], str) or len(q["answer"].split()) < 2:
                        problems.append(f"{qid}: build_sentence answer must be a sentence")
                    if "prompt_en" not in q:
                        problems.append(f"{qid}: build_sentence needs prompt_en")
        vocab_ids = [v["id"] for v in unit["vocabulary"]]
        if len(set(vocab_ids)) != len(vocab_ids):
            problems.append(f"{unit['id']}: duplicate vocabulary ids")
        if len(unit["vocabulary"]) < 4:
            problems.append(f"{unit['id']}: needs at least 4 words for multiple-choice rounds")
    return problems


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "content" / "knm.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(doc), key=lambda e: list(e.path))
    problems = [f"schema: {'/'.join(map(str, e.path))}: {e.message}" for e in errors]
    if not problems:
        problems = semantic_checks(doc)
    if problems:
        print("\n".join(problems))
        return 1
    n_q = sum(len(l["questions"]) for u in doc["units"] for l in u["lessons"])
    n_v = sum(len(u["vocabulary"]) for u in doc["units"])
    print(f"OK: {doc['content_version']} - {len(doc['units'])} units, {n_q} questions, {n_v} words")
    return 0


if __name__ == "__main__":
    sys.exit(main())
