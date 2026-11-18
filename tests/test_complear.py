"""Tests of the Complear OS feed: the conformity record, the push and the generated pack."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from kit import complear  # noqa: E402
from kit.config import load_config  # noqa: E402

KIT = str(REPO / "kit.py")
CONFIG = REPO / "examples" / "acme-medsoft-lite" / "kit.config.json"
PACK = REPO / "complear" / ("%s@%s.json" % (complear.FRAMEWORK["key"], complear.FRAMEWORK["version"]))
SCHEMA = REPO / "complear" / "conformity-record.schema.json"


def citations(nodes):
    for node in nodes:
        yield node["citation"]
        yield from citations(node.get("children", []))


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class ComplearTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        evidence = self.tmp / "evidence"
        write(evidence / "CLAUDE" / "OQ-01" / "gates.json",
              '{"result": "pass", "generated_at_utc": "2026-11-16T09:20:00Z"}')
        write(evidence / "CLAUDE" / "OQ-01" / "gates.md", "# the Markdown twin")
        write(evidence / "CLAUDE" / "MA-01" / "admission.json", '{"result": "fail"}')
        write(evidence / "CLAUDE" / "_exploratory" / "baseline.json", '{"result": "pass"}')
        write(evidence / "CLAUDE" / "OQ-02" / "notes.json", '{"no": "result"}')
        write(evidence / "GEN" / "OQ-02" / "canary.json", '{"result": "inconclusive"}')
        write(evidence / "CODEX" / "OQ-01" / "gates.json", '{"result": "pass"}')

    def tearDown(self):
        self._tmp.cleanup()

    def record(self, **kw):
        cfg = kw.pop("cfg", None) or load_config(CONFIG)
        return complear.build_record(cfg, "CLAUDE", "2.1.0", self.tmp / "evidence", self.tmp, **kw)

    def test_one_evaluation_per_result_file_of_the_tool_and_gen(self):
        record = self.record()
        self.assertEqual([(a["id"], a["supports"][0]["citation"], a["data"]["tests"]) for a in record["artefacts"]], [
            ("evaluation-1", "MA-01", {"total": 1, "passed": 0, "failed": 1, "skipped": 0}),
            ("evaluation-2", "OQ-01", {"total": 1, "passed": 1, "failed": 0, "skipped": 0}),
            ("evaluation-3", "OQ-02", {"total": 1, "passed": 0, "failed": 0, "skipped": 1}),
        ])
        oq01 = record["artefacts"][1]
        self.assertEqual(oq01["source"]["path"], "evidence/CLAUDE/OQ-01/gates.json")
        self.assertEqual(len(oq01["source"]["sha256"]), 64)
        self.assertEqual(oq01["data"]["date"], "2026-11-16")
        self.assertEqual(record["subject"], {
            "name": "Claude Code", "version": "2.1.0", "provider": "Anthropic",
            "intended_purpose": "AI coding tool validated as QMS and development software "
                                "(ISO 13485:2016 clause 4.1.6) for Acme MedSoft"})

    def test_scope_answers_come_from_the_config(self):
        [lite] = self.record()["scopes"]
        self.assertEqual(lite["answers"], {
            "validation_profile": "Lite", "local_or_ci_surface": "Yes", "ai_pr_review": "Yes",
            "doc_drafting": "No", "agents_qms": "No", "in_use_before_validation": "Yes"})
        self.assertEqual(lite["clauses"], sorted(complear.LITE_TESTS))
        cfg = load_config(CONFIG)
        cfg["contexts"] = cfg["contexts"] + ["doc_drafting"]
        [full] = self.record(cfg=cfg, profile_override="full")["scopes"]
        self.assertEqual(full["answers"]["validation_profile"], "Full")
        self.assertEqual(set(full["clauses"]) - set(complear.FULL_TESTS), {"OQ-20", "RV-01"})

    def test_scope_names_only_what_the_pack_holds(self):
        pack = json.loads(PACK.read_text(encoding="utf-8"))
        held = set(citations(pack["requirements"]))
        questions = {q["key"]: q["options"] for q in pack["questions"]}
        cfg = load_config(CONFIG)
        cfg.update(contexts=cfg["contexts"] + ["doc_drafting", "agents_qms"],
                   agent_untrusted_with_secrets=True)
        [scope] = self.record(cfg=cfg, profile_override="full")["scopes"]
        self.assertLessEqual(set(scope["clauses"]), held)
        for key, answer in scope["answers"].items():
            self.assertIn(answer, questions[key], key)

    def test_committed_pack_is_generated_from_the_templates(self):
        self.assertEqual(PACK.read_text(encoding="utf-8"), complear.pack_json(REPO / "templates"),
                         "regenerate it: python3 kit.py complear pack --out %s" % PACK.relative_to(REPO))

    def test_record_matches_the_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is not installed (the kit itself needs only the standard library)")
        jsonschema.validate(self.record(), json.loads(SCHEMA.read_text(encoding="utf-8")),
                            format_checker=jsonschema.FormatChecker())

    def test_push_posts_the_record_with_the_key(self):
        path = self.tmp / "record.json"
        path.write_text('{"a": 1}', encoding="utf-8")
        sent = []

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"recorded": []}'

        def opener(request, timeout):
            sent.append(request)
            return Response()

        self.assertEqual(complear.push(path, "https://acme.example.com/", "ck_test", opener),
                         (200, {"recorded": []}))
        [request] = sent
        self.assertEqual(request.full_url, "https://acme.example.com/api/conformity-records/v1")
        self.assertEqual(request.get_header("Authorization"), "Bearer ck_test")
        self.assertEqual(request.data, b'{"a": 1}')

    def test_cli_writes_the_record_and_refuses_without_evidence(self):
        out = self.tmp / "out" / "record.json"
        args = [sys.executable, KIT, "complear", "record", "--config", str(CONFIG), "--tool", "CLAUDE",
                "--tool-version", "2.1.0", "--out", str(out)]
        done = subprocess.run(args + ["--evidence", str(self.tmp / "evidence")],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(json.loads(out.read_text(encoding="utf-8"))["artefacts"]), 3)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["artefacts"][0]["source"]["path"],
                         "../evidence/CLAUDE/MA-01/admission.json")
        empty = subprocess.run(args + ["--evidence", str(self.tmp / "none")],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(empty.returncode, 1)
        self.assertIn("no evidence JSON", empty.stderr)


if __name__ == "__main__":
    unittest.main()
