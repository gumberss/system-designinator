"""Run: python app.py --help. Uses only the Python standard library."""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sqlite3
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

from learning import (
    DemoError,
    Purchase,
    Store,
    choose_audits,
    load_categories,
    load_purchases,
    retrieve_memories,
)
from llm import ChatModel, load_env

ROOT = Path(__file__).resolve().parent


def emit(event: str, **values) -> None:
    print(json.dumps({"event": event, **values}, ensure_ascii=True), flush=True)


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="CSV purchase categorization with periodic teacher audits and sleep memory."
    )
    result.add_argument("--db", type=Path, default=ROOT / "state" / "learning.sqlite3")
    result.add_argument("--categories", type=Path, default=ROOT / "categories.json")
    result.add_argument("--env-file", type=Path, help="Defaults to .env next to app.py, if present.")
    commands = result.add_subparsers(dest="command", required=True)
    learn = commands.add_parser("learn", help="Classify, audit samples, and consolidate memories.")
    learn.add_argument("--csv", type=Path, required=True)
    learn.add_argument("--audit-every", type=positive_int, default=6)
    learn.add_argument("--audit-size", type=positive_int, default=2)
    learn.add_argument("--sleep-every", type=positive_int, default=2, help="Audit rounds per sleep.")
    learn.add_argument("--seed", type=int, default=42, help="Controls audit sampling, not LLM output.")
    learn.add_argument("--memory-limit", type=positive_int, default=3)
    categorize = commands.add_parser("categorize", help="Classify a CSV without teacher calls.")
    categorize.add_argument("--csv", type=Path, required=True)
    categorize.add_argument("--output", type=Path, required=True)
    categorize.add_argument("--without-memory", action="store_true")
    categorize.add_argument("--memory-limit", type=positive_int, default=3)
    evaluate = commands.add_parser("evaluate", help="Compare baseline and memory on labeled holdout.")
    evaluate.add_argument("--csv", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, default=ROOT / "reports" / "evaluation.json")
    evaluate.add_argument("--memory-limit", type=positive_int, default=3)
    commands.add_parser("sleep", help="Consolidate pending audits without model calls.")
    commands.add_parser("status", help="Show persistent counts and agreement, not accuracy.")
    return result


def model_from_env(name: str) -> ChatModel:
    value = os.environ.get(name, "").strip()
    if not value:
        raise DemoError(f"Set {name} in the environment or in .env. No model is assumed.")
    return ChatModel(value)


def learn(
    args: argparse.Namespace, purchases: list[Purchase], categories: dict[str, str], store: Store
) -> None:
    student = model_from_env("STUDENT_MODEL")
    teacher = model_from_env("TEACHER_MODEL")
    if student.model == teacher.model:
        raise DemoError("Use different STUDENT_MODEL and TEACHER_MODEL names for this demonstration.")
    rng = random.Random(args.seed)
    run_id = uuid.uuid4().hex
    pending = []
    rounds = 0
    emit("run_started", run_id=run_id, student=student.model, teacher=teacher.model)
    for index, purchase in enumerate(purchases, 1):
        memories = retrieve_memories(purchase.description, store.memories(), args.memory_limit)
        prediction = student.classify(purchase.description, categories, memories)
        decision_id = store.record_decision(run_id, purchase, prediction, student.model)
        pending.append((decision_id, purchase, prediction))
        emit(
            "prediction", purchase_id=purchase.id, category=prediction.category,
            memory_examples=len(memories), revision=store.revision,
        )
        if len(pending) < args.audit_every and index != len(purchases):
            continue
        selected = choose_audits(pending, args.audit_size, rng)
        for selected_id, item, original in selected:
            assessment = teacher.assess(item.description, categories)
            reward = store.record_audit(selected_id, assessment, teacher.model)
            emit(
                "audit", purchase_id=item.id, student_category=original.category,
                teacher_category=assessment.category, certain=assessment.certain,
                reward=reward, reason=assessment.reason,
            )
        pending.clear()
        rounds += 1
        if rounds % args.sleep_every == 0:
            emit("sleep", **store.sleep())
    emit("sleep", **store.sleep())
    emit(
        "run_completed", run_id=run_id, student_calls=student.calls,
        teacher_calls=teacher.calls, cumulative_stats=store.stats(),
    )


def categorize(
    args: argparse.Namespace, purchases: list[Purchase], categories: dict[str, str], store: Store
) -> None:
    student = model_from_env("STUDENT_MODEL")
    memories = [] if args.without_memory else store.memories()
    rows = []
    for purchase in purchases:
        selected = retrieve_memories(purchase.description, memories, args.memory_limit)
        prediction = student.classify(purchase.description, categories, selected)
        rows.append({
            "id": purchase.id, "description": purchase.description,
            **asdict(prediction), "memory_examples": len(selected),
        })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    emit("categorized", purchases=len(rows), student_calls=student.calls, output=str(args.output))


def evaluate(
    args: argparse.Namespace, purchases: list[Purchase], categories: dict[str, str], store: Store
) -> None:
    store.assert_holdout(purchases)
    memories = store.memories()
    if not memories:
        raise DemoError("No consolidated memories. Run learn (and sleep if needed) first.")
    student = model_from_env("STUDENT_MODEL")
    rows = []
    for index, purchase in enumerate(purchases):
        selected = retrieve_memories(purchase.description, memories, args.memory_limit)
        # Alternate order to avoid always giving one condition the earlier API call.
        predictions = {}
        conditions = [("baseline", []), ("with_memory", selected)]
        if index % 2:
            conditions.reverse()
        for name, examples in conditions:
            predictions[name] = student.classify(purchase.description, categories, examples)
        rows.append({
            "id": purchase.id, "description": purchase.description,
            "expected_category": purchase.expected_category,
            "baseline": asdict(predictions["baseline"]),
            "with_memory": asdict(predictions["with_memory"]),
            "baseline_correct": predictions["baseline"].category == purchase.expected_category,
            "memory_correct": predictions["with_memory"].category == purchase.expected_category,
            "retrieved_examples": [asdict(memory) for memory in selected],
        })
    baseline = sum(row["baseline_correct"] for row in rows) / len(rows)
    with_memory = sum(row["memory_correct"] for row in rows) / len(rows)
    report = {
        "student_model": student.model, "memory_revision": store.revision,
        "purchases": len(rows), "student_calls": student.calls,
        "baseline_accuracy": baseline, "memory_accuracy": with_memory,
        "delta_percentage_points": (with_memory - baseline) * 100,
        "improved_cases": sum(not row["baseline_correct"] and row["memory_correct"] for row in rows),
        "regressions": sum(row["baseline_correct"] and not row["memory_correct"] for row in rows),
        "note": "Against supplied labels, not teacher agreement. LLM outputs may vary; no weight updates.",
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    emit("evaluation", **{key: value for key, value in report.items() if key != "rows"})
    emit("report_saved", output=str(args.output))


def run(args: argparse.Namespace) -> None:
    env_file = args.env_file if args.env_file is not None else ROOT / ".env"
    if args.env_file is not None or env_file.exists():
        load_env(env_file)
    categories = load_categories(args.categories)
    purchases: list[Purchase] = []
    if args.command in {"learn", "categorize", "evaluate"}:
        purchases = load_purchases(
            args.csv, categories, require_labels=args.command == "evaluate"
        )
    if args.command != "learn" and not args.db.is_file():
        raise DemoError("Database does not exist. Run learn first, or select an existing --db.")
    if hasattr(args, "output"):
        protected = {args.csv.resolve(), args.categories.resolve(), args.db.resolve(), env_file.resolve()}
        if args.output.resolve() in protected:
            raise DemoError("--output must not overwrite input, database, categories, or .env.")
    store = Store(args.db, categories)
    try:
        if args.command == "learn":
            learn(args, purchases, categories, store)
        elif args.command == "categorize":
            categorize(args, purchases, categories, store)
        elif args.command == "evaluate":
            evaluate(args, purchases, categories, store)
        elif args.command == "sleep":
            emit("sleep", **store.sleep())
        else:
            emit("status", **store.stats())
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        run(args)
    except (DemoError, OSError, ValueError, csv.Error, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print(
            "Stopped. Any completed learning decisions/audits remain in SQLite. "
            "No fabricated predictions or automatic retries were used.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
