"""Deterministic tests. The local HTTP fixture is not a real LLM or a benchmark."""

from __future__ import annotations

import csv
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from learning import (
    Assessment,
    DemoError,
    Memory,
    Prediction,
    Purchase,
    Store,
    choose_audits,
    load_categories,
    load_purchases,
    normalize,
    retrieve_memories,
)
from llm import ChatModel, load_env

ROOT = Path(__file__).resolve().parent
CATEGORIES = load_categories(ROOT / "categories.json")


class TemporaryCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def csv_file(self, text: str) -> Path:
        path = self.directory / "input.csv"
        path.write_text(text, encoding="utf-8-sig")
        return path

    def store(self) -> Store:
        store = Store(self.directory / "learning.sqlite3", CATEGORIES)
        self.addCleanup(store.close)
        return store


class InputTests(TemporaryCase):
    def test_comma_and_semicolon_with_bom(self):
        for delimiter in (",", ";", "\t"):
            with self.subTest(delimiter=delimiter):
                path = self.csv_file(
                    f"id{delimiter}description\n1{delimiter}UBER *TRIP CENTRO\n"
                )
                self.assertEqual(
                    load_purchases(path, CATEGORIES), [Purchase("1", "UBER *TRIP CENTRO")]
                )

    def test_labels_are_required_for_evaluation(self):
        path = self.csv_file("id,description\n1,CINEMA\n")
        with self.assertRaisesRegex(DemoError, "expected_category"):
            load_purchases(path, CATEGORIES, require_labels=True)

    def test_unknown_label_is_rejected(self):
        path = self.csv_file("id,description,expected_category\n1,CINEMA,invalid\n")
        with self.assertRaisesRegex(DemoError, "Invalid expected_category"):
            load_purchases(path, CATEGORIES)

    def test_duplicate_ids_are_rejected(self):
        path = self.csv_file("id,description\n1,CINEMA\n1,FARMACIA\n")
        with self.assertRaisesRegex(DemoError, "Duplicate purchase id"):
            load_purchases(path, CATEGORIES)

    def test_duplicate_evaluation_descriptions_are_rejected(self):
        path = self.csv_file(
            "id,description,expected_category\n1,Uber *TRIP,transporte\n"
            "2,UBER TRIP,transporte\n"
        )
        with self.assertRaisesRegex(DemoError, "repeat normalized"):
            load_purchases(path, CATEGORIES, require_labels=True)

    def test_invalid_csv_is_rejected(self):
        for text in (
            "id,description\n1,\n",
            "id,description\n1\n",
            "id,id,description\n1,2,CINEMA\n",
            "id,description\n",
        ):
            with self.subTest(text=text):
                with self.assertRaises((DemoError, csv.Error)):
                    load_purchases(self.csv_file(text), CATEGORIES)

    def test_sample_datasets_are_valid_and_disjoint(self):
        learning = load_purchases(ROOT / "samples" / "learning.csv", CATEGORIES)
        holdout = load_purchases(
            ROOT / "samples" / "holdout.csv", CATEGORIES, require_labels=True
        )
        self.assertEqual(len(learning), 24)
        self.assertEqual(len(holdout), 12)
        self.assertFalse(
            {normalize(item.description) for item in learning}
            & {normalize(item.description) for item in holdout}
        )

    def test_hundred_purchase_dataset_is_valid_complete_and_disjoint(self):
        purchases = load_purchases(
            ROOT / "samples" / "purchases_100.csv", CATEGORIES, require_labels=True
        )
        self.assertEqual(len(purchases), 100)
        self.assertEqual(len({purchase.id for purchase in purchases}), 100)
        self.assertEqual(
            {purchase.expected_category for purchase in purchases}, set(CATEGORIES)
        )
        descriptions = {normalize(purchase.description) for purchase in purchases}
        self.assertEqual(len(descriptions), 100)
        for filename in ("learning.csv", "holdout.csv"):
            with self.subTest(filename=filename):
                existing = load_purchases(ROOT / "samples" / filename, CATEGORIES)
                self.assertFalse(
                    descriptions & {normalize(purchase.description) for purchase in existing}
                )

    def test_env_preserves_process_values_and_accepts_quotes(self):
        path = self.directory / ".env"
        path.write_text('# example\nSTUDENT_MODEL="from-file"\nTEACHER_MODEL=teacher\n')
        with patch.dict(os.environ, {"STUDENT_MODEL": "existing"}, clear=True):
            load_env(path)
            self.assertEqual(os.environ["STUDENT_MODEL"], "existing")
            self.assertEqual(os.environ["TEACHER_MODEL"], "teacher")

    def test_invalid_env_is_an_explicit_error(self):
        path = self.directory / ".env"
        path.write_text('STUDENT_MODEL="unclosed\n')
        with self.assertRaisesRegex(DemoError, "Unclosed"):
            load_env(path)


class MemoryTests(TemporaryCase):
    def add_audit(
        self, store: Store, description: str, student: str, teacher: str, certain: bool = True
    ):
        decision = store.record_decision(
            "test-run", Purchase("1", description), Prediction(student, "Student reason"), "student"
        )
        reward = store.record_audit(
            decision, Assessment(teacher, "Teacher correction", certain), "teacher"
        )
        return decision, reward

    def test_failure_is_a_useful_memory_and_survives_reopen(self):
        store = self.store()
        _, reward = self.add_audit(store, "UBER *TRIP CENTRO", "alimentacao", "transporte")
        self.assertEqual(reward, -1)
        self.assertEqual(store.memories(), [])
        result = store.sleep()
        self.assertEqual(result["memories"], 1)
        reopened = self.store()
        self.assertEqual(reopened.memories()[0].category, "transporte")
        self.assertEqual(reopened.revision, 1)

    def test_correct_example_also_becomes_memory(self):
        store = self.store()
        _, reward = self.add_audit(store, "CINEMA", "lazer", "lazer")
        self.assertEqual(reward, 1)
        store.sleep()
        self.assertEqual(store.memories()[0].category, "lazer")
        self.assertEqual(store.stats()["teacher_agreement"], 1)

    def test_uncertain_teacher_is_neither_punishment_nor_memory(self):
        store = self.store()
        _, reward = self.add_audit(
            store, "PIX", "indeterminado", "outros", certain=False
        )
        self.assertIsNone(reward)
        store.sleep()
        self.assertEqual(store.memories(), [])
        self.assertIsNone(store.stats()["teacher_agreement"])
        self.assertEqual(store.stats()["uncertain_audits"], 1)
        self.assertEqual(store.stats()["pending_sleep"], 0)

    def test_conflicting_teacher_labels_remove_existing_memory(self):
        store = self.store()
        self.add_audit(store, "LOJA X", "outros", "outros")
        store.sleep()
        self.add_audit(store, "Loja X", "outros", "alimentacao")
        result = store.sleep()
        self.assertEqual(store.memories(), [])
        self.assertEqual(result["conflicting_descriptions_excluded"], 1)

    def test_sleep_is_idempotent_without_new_audits(self):
        store = self.store()
        self.add_audit(store, "CINEMA", "lazer", "lazer")
        store.sleep()
        self.assertEqual(store.sleep()["pending_audits"], 0)
        self.assertEqual(store.revision, 1)
        self.assertEqual(store.memories()[0].support, 1)

    def test_repeated_evidence_is_consolidated_without_duplicate_memories(self):
        store = self.store()
        for _ in range(2):
            self.add_audit(store, "CINEMA", "lazer", "lazer")
        store.sleep()
        self.assertEqual(len(store.memories()), 1)
        self.assertEqual(store.memories()[0].support, 2)

    def test_changed_taxonomy_does_not_reuse_memories(self):
        self.store()
        with self.assertRaisesRegex(DemoError, "Categories changed"):
            Store(self.directory / "learning.sqlite3", {**CATEGORIES, "new": "New category"})

    def test_holdout_rejects_normalized_training_overlap(self):
        store = self.store()
        store.record_decision(
            "run", Purchase("1", "UBER *TRIP"), Prediction("transporte", "Ride"), "student"
        )
        with self.assertRaisesRegex(DemoError, "overlaps"):
            store.assert_holdout([Purchase("different-id", "Uber trip", "transporte")])

    def test_retrieval_selects_related_examples_with_limit(self):
        memories = [
            Memory("CINEMA ESTRELA", "lazer", "Movie", 1),
            Memory("UBER *TRIP CENTRO", "transporte", "Ride", 1),
            Memory("UBER EATS RESTAURANTE", "alimentacao", "Food", 1),
        ]
        result = retrieve_memories("UBER *TRIP AEROPORTO", memories, 1)
        self.assertEqual([item.category for item in result], ["transporte"])
        self.assertEqual(retrieve_memories("EXAME SANGUE", memories, 3), [])

    def test_audits_include_random_sample_and_abstentions(self):
        decisions = [
            (i, Purchase(str(i), f"Purchase {i}"), Prediction("lazer", "Reason"))
            for i in range(10)
        ]
        decisions[-1] = (9, Purchase("9", "PIX"), Prediction("indeterminado", "Insufficient"))
        first = choose_audits(decisions, 2, random.Random(42))
        second = choose_audits(decisions, 2, random.Random(42))
        self.assertEqual(first, second)
        self.assertIn(9, [item[0] for item in first])
        self.assertGreaterEqual(len(first), 2)
        self.assertEqual(len({item[0] for item in first}), len(first))


class ProviderHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        if self.server.fail_status is not None and (
            self.server.fail_model is None or self.server.fail_model == body["model"]
        ):
            self.send_response(self.server.fail_status)
            self.end_headers()
            self.wfile.write(b"Private provider error body must not appear in logs")
            return
        payload = json.loads(body["messages"][1]["content"])
        if body["model"] == "teacher-test":
            result = {"category": "transporte", "reason": "A ride, not food delivery.", "certain": True}
        else:
            examples = payload.get("verified_examples", [])
            result = {
                "category": examples[0]["category"] if examples else "alimentacao",
                "reason": "Deterministic test fixture, not an actual LLM.",
            }
        content = (
            self.server.response_content
            if self.server.response_content is not None else json.dumps(result)
        )
        envelope = {"choices": [{"message": {"content": content}}]}
        encoded = json.dumps(envelope).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class TestProvider(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), ProviderHandler)
        self.requests = []
        self.fail_status = None
        self.fail_model = None
        self.response_content = None


class ClientTests(unittest.TestCase):
    def model(self):
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://127.0.0.1:1/v1"}, clear=True):
            return ChatModel("test-model")

    def test_invalid_category_is_not_silently_replaced(self):
        model = self.model()
        with patch.object(model, "_request", return_value={"category": "missing", "reason": "x"}):
            with self.assertRaisesRegex(DemoError, "unknown"):
                model.classify("CINEMA", CATEGORIES, [])

    def test_teacher_certain_must_be_boolean(self):
        model = self.model()
        result = {"category": "lazer", "reason": "Movie", "certain": "true"}
        with patch.object(model, "_request", return_value=result):
            with self.assertRaisesRegex(DemoError, "boolean"):
                model.assess("CINEMA", CATEGORIES)

    def test_empty_reason_is_rejected(self):
        model = self.model()
        with patch.object(model, "_request", return_value={"category": "lazer", "reason": " "}):
            with self.assertRaisesRegex(DemoError, "reason"):
                model.classify("CINEMA", CATEGORIES, [])

    def test_non_loopback_plain_http_is_rejected(self):
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://example.com/v1"}, clear=True):
            with self.assertRaisesRegex(DemoError, "HTTPS"):
                ChatModel("student")

    def test_remote_endpoint_requires_key(self):
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "https://example.com/v1"}, clear=True):
            with self.assertRaisesRegex(DemoError, "OPENAI_API_KEY"):
                ChatModel("student")


class WorkflowTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.server = TestProvider()
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.env = {
            **os.environ,
            "OPENAI_BASE_URL": f"http://127.0.0.1:{self.server.server_port}/v1",
            "OPENAI_API_KEY": "",
            "STUDENT_MODEL": "student-test",
            "TEACHER_MODEL": "teacher-test",
        }
        self.db = self.directory / "state.sqlite3"

    def cli(self, *arguments, success=True):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "app.py"), "--db", str(self.db), *arguments],
            env=self.env, cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        if success:
            self.assertEqual(completed.returncode, 0, completed.stderr)
        else:
            self.assertNotEqual(completed.returncode, 0)
        return completed

    def train(self):
        path = self.directory / "train.csv"
        path.write_text("id,description\n1,UBER *TRIP CENTRO\n", encoding="utf-8")
        return self.cli(
            "learn", "--csv", str(path), "--audit-every", "1",
            "--audit-size", "1", "--sleep-every", "1",
        )

    def test_persistent_end_to_end_and_blind_teacher(self):
        trained = self.train()
        events = [json.loads(line) for line in trained.stdout.splitlines()]
        self.assertEqual(next(row for row in events if row["event"] == "audit")["reward"], -1)
        status_before = json.loads(self.cli("status").stdout)
        self.assertEqual(status_before["memories"], 1)
        teacher_request = next(
            row for row in self.server.requests if row["model"] == "teacher-test"
        )
        self.assertEqual(
            json.loads(teacher_request["messages"][1]["content"]),
            {"description": "UBER *TRIP CENTRO"},
        )
        holdout = self.directory / "holdout.csv"
        holdout.write_text(
            "id,description,expected_category\n2,UBER *TRIP RODOVIARIA,transporte\n",
            encoding="utf-8",
        )
        report_path = self.directory / "report.json"
        self.cli("evaluate", "--csv", str(holdout), "--output", str(report_path))
        report = json.loads(report_path.read_text())
        self.assertEqual(report["baseline_accuracy"], 0)
        self.assertEqual(report["memory_accuracy"], 1)
        self.assertEqual(report["improved_cases"], 1)
        self.assertEqual(report["regressions"], 0)
        self.assertEqual(report["student_calls"], 2)
        self.assertEqual(json.loads(self.cli("status").stdout), status_before)
        for request in self.server.requests:
            payload = json.loads(request["messages"][1]["content"])
            self.assertNotIn("expected_category", payload)
        output = self.directory / "predictions.csv"
        self.cli("categorize", "--csv", str(holdout), "--output", str(output))
        with output.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["category"], "transporte")
        self.assertEqual(rows[0]["memory_examples"], "1")
        self.cli(
            "categorize", "--csv", str(holdout), "--output", str(output), "--without-memory"
        )
        with output.open(newline="") as stream:
            self.assertEqual(next(csv.DictReader(stream))["category"], "alimentacao")
        self.assertEqual(json.loads(self.cli("status").stdout), status_before)
        self.cli("sleep")
        self.assertEqual(json.loads(self.cli("status").stdout), status_before)

    def test_provider_failure_has_no_fabricated_success_or_error_body(self):
        self.server.fail_status = 429
        path = self.csv_file("id,description\n1,CINEMA\n")
        result = self.cli("learn", "--csv", str(path), success=False)
        self.assertIn("HTTP 429", result.stderr)
        self.assertNotIn("Private provider", result.stderr)
        self.assertNotIn("run_completed", result.stdout)
        self.assertEqual(json.loads(self.cli("status").stdout)["decisions"], 0)
        self.assertEqual(len(self.server.requests), 1)

    def test_partial_final_block_and_sleep_affect_later_purchases(self):
        path = self.directory / "seven.csv"
        path.write_text(
            "id,description\n"
            + "".join(f"{number},UBER *TRIP LOCAL {number}\n" for number in range(7)),
            encoding="utf-8",
        )
        completed = self.cli(
            "learn", "--csv", str(path), "--audit-every", "3",
            "--audit-size", "1", "--sleep-every", "1",
        )
        events = [json.loads(line) for line in completed.stdout.splitlines()]
        predictions = [event for event in events if event["event"] == "prediction"]
        self.assertEqual([event["memory_examples"] for event in predictions[:3]], [0, 0, 0])
        self.assertTrue(all(event["memory_examples"] > 0 for event in predictions[3:]))
        self.assertEqual(events[-1]["student_calls"], 7)
        self.assertEqual(events[-1]["teacher_calls"], 3)
        self.assertEqual(events[-1]["cumulative_stats"]["audited"], 3)

    def test_teacher_failure_keeps_decision_but_does_not_invent_an_audit(self):
        self.server.fail_status = 503
        self.server.fail_model = "teacher-test"
        path = self.csv_file("id,description\n1,UBER *TRIP CENTRO\n")
        failed = self.cli("learn", "--csv", str(path), success=False)
        self.assertIn("HTTP 503", failed.stderr)
        stats = json.loads(self.cli("status").stdout)
        self.assertEqual(stats["decisions"], 1)
        self.assertEqual(stats["audited"], 0)
        self.assertEqual(stats["memories"], 0)

    def test_invalid_model_json_fails_without_a_prediction(self):
        self.server.response_content = "```json\n{}\n```"
        path = self.csv_file("id,description\n1,UBER *TRIP CENTRO\n")
        failed = self.cli("learn", "--csv", str(path), success=False)
        self.assertIn("JSON object", failed.stderr)
        self.assertEqual(json.loads(self.cli("status").stdout)["decisions"], 0)

    def test_evaluation_overlap_fails_before_network(self):
        self.train()
        path = self.csv_file(
            "id,description,expected_category\nother,Uber trip centro,transporte\n"
        )
        calls_before = len(self.server.requests)
        result = self.cli("evaluate", "--csv", str(path), success=False)
        self.assertIn("overlaps", result.stderr)
        self.assertEqual(len(self.server.requests), calls_before)

    def test_output_cannot_overwrite_input(self):
        self.train()
        path = self.csv_file("id,description\n2,UBER *TRIP AEROPORTO\n")
        before = path.read_bytes()
        result = self.cli(
            "categorize", "--csv", str(path), "--output", str(path), success=False
        )
        self.assertIn("must not overwrite", result.stderr)
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
