from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from capture_app.brd_linker.adapters import BrdAdapter, EvidenceAdapter
from capture_app.brd_linker.generated import current_region_hash
from capture_app.brd_linker.sync import (SyncError, apply_plan, build_plan, restore,
                                         rollback_journal, resume_journal, write_plan)


class SyncFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        self.brd_dir = self.root / "BRDs"
        self.runs_dir = self.vault / "Reports" / "native_runs"
        self.capture_db = self.vault / ".capture_index" / "index.sqlite"
        self.vault.mkdir(parents=True)
        self.brd_dir.mkdir()
        self.runs_dir.mkdir(parents=True)
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text("test: true\n", encoding="utf-8")
        self.config = {
            "vault": str(self.vault),
            "sync": {"brd_dir": str(self.brd_dir), "evidence_dir": str(self.runs_dir),
                     "capture_db": str(self.capture_db), "state_db": ".capture_index/sync.sqlite",
                     "rename_threshold": 0.85, "stale_after_runs": 2,
                     "max_delete_fraction": 0.2},
            "key_normalization": {"numeric_segments": True, "uuid_segments": True, "patterns": []},
            "retrieval": {"top_k": 10, "drop_below": 0.35},
            "embed": {"model": ""}, "judge": {"model": "", "max_candidates": 8},
        }
        self.brd1 = self.brd_dir / "BRD-Users.md"
        self.brd1.write_text("# User Registration\n\nUsers can register with an email address.\n", encoding="utf-8")
        self.brd2 = self.brd_dir / "BRD-Orders.md"
        self.brd2.write_text("# Orders\n\nUsers can view order history.\n", encoding="utf-8")
        self._write_capture_db()
        self.run_path = self.runs_dir / "run-1.json"
        self._write_run("run-1", "passed", [
            {"method": "POST", "url": "https://shop.test/users/123", "status": 201},
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def _write_capture_db(self):
        self.capture_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.capture_db)
        conn.execute("CREATE TABLE session_api_events(session_id TEXT,seq INTEGER,method TEXT,status INTEGER,request_url TEXT,request_body TEXT,response_body TEXT,action_id TEXT)")
        conn.execute("INSERT INTO session_api_events VALUES(?,?,?,?,?,?,?,?)",
                     ("session-1", 4, "POST", 201, "https://shop.test/users/123", '{"email":"private@example.com","active":true}', '{"id":1}', "action-4"))
        conn.commit()
        conn.close()

    def _write_run(self, run_id, status, observations):
        self.run_path.write_text(json.dumps({
            "run_id": run_id, "session_id": "session-1", "started_at": "2026-10-01T00:00:00Z",
            "status": status, "steps": [{"sequence": 1, "status": "passed" if status == "passed" else "not_run"}],
            "api_observations": observations,
        }), encoding="utf-8")

    def plan(self):
        return build_plan(self.config, self.config_path)


class AdapterTests(SyncFixture):
    def test_adapters_normalize_and_report_scope(self):
        run, entities = EvidenceAdapter(self.capture_db, self.config["key_normalization"]).read(self.run_path)
        self.assertEqual(run.run_id, "run-1")
        self.assertTrue(run.complete)
        api = next(item for item in entities if item.kind == "api")
        self.assertEqual(api.key, "POST /users/{id}")
        self.assertEqual(api.seen_in, ("session-1",))
        param = next(item for item in entities if item.kind == "param" and item.name == "email")
        self.assertEqual(param.attrs["location"], "request")
        self.assertNotIn("private@example.com", json.dumps(param.to_dict()))
        segments = BrdAdapter().read([self.brd1, self.brd2])
        self.assertEqual({s.brd_id for s in segments}, {"BRD-Users", "BRD-Orders"})
        self.assertTrue(all(s.heading_path and s.text.startswith("#") for s in segments))


class SyncTests(SyncFixture):
    def test_plan_apply_is_idempotent_and_updates_only_changed_segment(self):
        first = self.plan()
        self.assertGreater(first["counts"].get("ADD", 0), 0)
        self.assertGreater(first["counts"].get("RELINK", 0), 0)
        plan_file = self.root / "out" / "sync_plan.json"
        md_file = self.root / "out" / "sync_plan.md"
        write_plan(first, plan_file, md_file)
        self.assertEqual(first, self.plan())
        result = apply_plan(plan_file)
        self.assertGreater(result["applied"], 0)
        second = self.plan()
        self.assertEqual(second["counts"], {})
        source = BrdAdapter().read(self.brd1)[0].source_id
        row_path = self.vault / "BRDs" / "segments" / (source.replace(":", "_").replace("#", "_") + ".md")
        self.assertTrue(row_path.is_file())
        self.brd1.write_text("# User Registration\n\nUsers can register with an email address and verify their account.\n", encoding="utf-8")
        third = self.plan()
        updates = [op for op in third["operations"] if op["action"] == "UPDATE"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["source_id"], source)

    def test_unambiguous_rename_keeps_note_path(self):
        long_text = ("Customers can register, validate email, protect accounts, recover credentials, "
                     "manage profile details, review preferences, and maintain secure identity information.\n")
        self.brd1.write_text("# Old heading\n\n" + long_text, encoding="utf-8")
        initial = self.plan()
        plan_file = self.root / "out" / "rename.json"
        write_plan(initial, plan_file, self.root / "out" / "rename.md")
        apply_plan(plan_file)
        old = next(op for op in initial["operations"] if op["action"] == "ADD" and op["kind"] == "segment" and op["source_id"].startswith("brd:BRD-Users#"))
        self.brd1.write_text("# New heading\n\n" + long_text, encoding="utf-8")
        renamed = self.plan()
        operation = next(op for op in renamed["operations"] if op["action"] == "RENAME" and op["source_id"] == old["source_id"])
        self.assertEqual(operation["note_path"], old["note_path"])
        self.assertEqual(operation["match"], "token_jaccard")

    def test_partial_evidence_marks_absent_entity_stale_and_brd_delete_archives(self):
        initial = self.plan()
        path = self.root / "out" / "initial.json"
        write_plan(initial, path, self.root / "out" / "initial.md")
        apply_plan(path)
        self._write_run("run-2", "failed", [])
        partial = self.plan()
        self.assertTrue(any(op["action"] == "STALE" and op["kind"] == "api" for op in partial["operations"]))
        self.brd2.unlink()
        deleted = self.plan()
        segment_delete = next(op for op in deleted["operations"] if op["action"] == "DELETE" and op["kind"] == "segment")
        self.assertEqual(segment_delete["reason"], "BRD source segment disappeared")
        write_plan(deleted, path, self.root / "out" / "initial.md")
        # Restore the source directory guard: one BRD remains.
        apply_plan(path, force=True)
        from capture_app.brd_linker.sync import status_report
        removed = next(row for row in status_report(self.vault, self.vault / ".capture_index" / "sync.sqlite")["entities"]
                       if row["source_id"] == segment_delete["source_id"])
        self.assertEqual(removed["status"], "removed")
        restored = restore(self.vault, removed["source_id"], self.vault / ".capture_index" / "sync.sqlite")
        self.assertTrue((self.vault / restored).is_file())

    def test_evidence_removal_requires_two_complete_absent_runs(self):
        first = self.plan()
        plan_file = self.root / "out" / "initial.json"
        write_plan(first, plan_file, self.root / "out" / "initial.md")
        apply_plan(plan_file)

        self._write_run("run-2", "passed", [])
        second = self.plan()
        api_id = next(op["source_id"] for op in first["operations"]
                      if op["action"] == "ADD" and op["kind"] == "api")
        stale = next(op for op in second["operations"]
                     if op["source_id"] == api_id and op["action"] == "STALE")
        self.assertEqual(stale["absence_count"], 1)
        write_plan(second, plan_file, self.root / "out" / "initial.md")
        apply_plan(plan_file, force=True)

        self._write_run("run-3", "passed", [])
        third = self.plan()
        deleted = next(op for op in third["operations"]
                       if op["source_id"] == api_id and op["action"] == "DELETE")
        self.assertEqual(deleted["absence_count"], 2)
        self.assertIn("2 consecutive complete evidence runs", deleted["reason"])

    def test_generated_region_conflict_is_reported_not_overwritten(self):
        initial = self.plan()
        plan_file = self.root / "out" / "plan.json"
        write_plan(initial, plan_file, self.root / "out" / "plan.md")
        apply_plan(plan_file)
        segment = next(op for op in initial["operations"] if op["action"] == "ADD" and op["kind"] == "segment" and op["source_id"].startswith("brd:BRD-Users#"))
        note = self.vault / segment["note_path"]
        text = note.read_text(encoding="utf-8")
        note.write_text(text.replace("Users can register", "A human edit Users can register"), encoding="utf-8")
        self.brd1.write_text("# User Registration\n\nUsers can register with a unique email address.\n", encoding="utf-8")
        plan = self.plan()
        conflict = next(op for op in plan["operations"] if op["action"] == "CONFLICT" and op.get("source_id") == segment["source_id"])
        self.assertIn("changed by a human", conflict["reason"])
        write_plan(plan, plan_file, self.root / "out" / "plan.md")
        apply_plan(plan_file)
        from capture_app.brd_linker.sync import status_report
        self.assertTrue(status_report(self.vault, self.vault / ".capture_index" / "sync.sqlite")["conflicts"])
        self.assertEqual(self.plan()["counts"], {})

    def test_ambiguous_rename_goes_to_review(self):
        shared = ("User account profile login registration email required secure account access and identity management. "
                  "Customers create accounts, validate contact details, authenticate safely, recover credentials, "
                  "review preferences, update personal information, and protect their private account records.\n")
        self.brd1.write_text("# Old A\n\n" + shared, encoding="utf-8")
        self.brd2.write_text("# Old B\n\n" + shared, encoding="utf-8")
        initial = self.plan()
        plan_file = self.root / "out" / "first.json"
        write_plan(initial, plan_file, self.root / "out" / "first.md")
        apply_plan(plan_file)
        self.brd1.write_text("# New heading\n\n" + shared, encoding="utf-8")
        self.brd2.unlink()
        plan = self.plan()
        self.assertTrue(any(op["action"] == "CONFLICT" and "ambiguous rename" in op["reason"] for op in plan["operations"]))

    def test_fingerprint_rejects_changed_vault(self):
        plan = self.plan()
        plan_file = self.root / "out" / "plan.json"
        write_plan(plan, plan_file, self.root / "out" / "plan.md")
        (self.vault / "human.md").write_text("changed after plan", encoding="utf-8")
        with self.assertRaisesRegex(Exception, "Vault changed"):
            apply_plan(plan_file)

    def test_empty_brd_gate_and_backup_rollback(self):
        initial = self.plan()
        plan_file = self.root / "out" / "plan.json"
        write_plan(initial, plan_file, self.root / "out" / "plan.md")
        applied = apply_plan(plan_file)
        rollback = rollback_journal(applied["journal"])
        self.assertTrue(rollback["rolled_back"])
        self.assertFalse((self.vault / ".capture_index" / "sync.sqlite").exists())
        self.brd1.unlink()
        self.brd2.unlink()
        empty = self.plan()
        self.assertTrue(any("BRD source directory is missing or empty" in reason for reason in empty["blockers"]))
        write_plan(empty, plan_file, self.root / "out" / "plan.md")
        with self.assertRaisesRegex(SyncError, "blockers"):
            apply_plan(plan_file)

    def test_interrupted_journal_can_resume_idempotently(self):
        plan = self.plan()
        plan_file = self.root / "out" / "plan.json"
        write_plan(plan, plan_file, self.root / "out" / "plan.md")
        applied = apply_plan(plan_file)
        journal_path = Path(applied["journal"])
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["state"] = "applying"
        journal["completed"] = []
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        resumed = resume_journal(journal_path)
        self.assertIn("journal", resumed)
        self.assertEqual(resumed["conflicts"], [])


if __name__ == "__main__":
    unittest.main()
