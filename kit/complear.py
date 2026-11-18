"""Feed the kit's evidence to Complear OS as a conformity record.

  python3 kit.py complear record --config kit.config.json --tool CLAUDE --out record.json
  python3 kit.py complear push record.json          # COMPLEAR_URL, COMPLEAR_UPLOAD_KEY
  python3 kit.py complear pack --out complear/ai_coding_tool_validation@2026.1.json

The record follows the conformity record schema 0.3 (complear/conformity-record.schema.json),
the format Complear's conformity records API reads. Each evidence JSON with a top-level
"result" under evidence/<TOOL>/<TEST-ID>/ and evidence/GEN/<TEST-ID>/ becomes one evaluation,
fingerprinted (SHA-256) and tagged with its test id in Complear's ai_coding_tool_validation
framework. Complear files each one for review and links it to that test; people conclude.

The framework itself is generated from this kit's templates by `pack`, so its requirement and
test ids are the ones the scripts and templates use.

Python 3.9+ standard library only.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from kit import __version__
from kit.config import effective_profile

FRAMEWORK = {"key": "ai_coding_tool_validation", "version": "2026.1"}
SCHEMA_VERSION = "0.3"
TEST_ID = re.compile(r"^(IQ|OQ|MA|RV|PQ)-\d\d$")
OUTCOMES = {"pass": "passed", "fail": "failed"}  # anything else (inconclusive) is recorded as skipped

# The tests each profile runs (docs/lite-vs-full.md), before the context tests.
LITE_TESTS = ["IQ-03", "OQ-01", "OQ-02", "OQ-03", "OQ-04", "OQ-05", "OQ-06", "OQ-07", "OQ-08",
              "OQ-09", "OQ-10", "MA-01", "PQ-01", "PQ-02"]
FULL_TESTS = sorted(LITE_TESTS + ["IQ-01", "IQ-02", "IQ-04", "IQ-05", "OQ-11", "OQ-12", "OQ-13"])


class RecordError(Exception):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tool(cfg: Dict[str, Any], tool_id: str) -> Dict[str, Any]:
    for tool in cfg.get("tools", []):
        if tool.get("tool_id") == tool_id:
            return tool
    raise RecordError("no tool with tool_id %s in the config" % tool_id)


def _yes(flag: bool) -> str:
    return "Yes" if flag else "No"


def scope(cfg: Dict[str, Any], tool: Dict[str, Any], profile: str) -> Dict[str, Any]:
    """The framework's scoping answers and the tests in scope, as far as the config decides them.

    Whether an agent reads untrusted content (OQ-40) is confirmed in the plan, not the config,
    so it is answered only when the config's hard stop L9 says so.
    """
    contexts, surfaces = cfg.get("contexts", []), tool.get("surfaces", [])
    full = profile == "full"
    untrusted = cfg.get("agent_untrusted_with_secrets") is True
    answers = {
        "validation_profile": profile.capitalize(),
        "local_or_ci_surface": _yes(bool({"local_cli_ide", "ci_headless"} & set(surfaces))),
        "ai_pr_review": _yes("pr_review" in contexts or "pr_review_bot" in surfaces),
        "doc_drafting": _yes("doc_drafting" in contexts),
        "agents_qms": _yes("agents_qms" in contexts or cfg.get("agent_qms_write") is True),
        "in_use_before_validation": _yes(cfg.get("already_in_use") is True),
    }
    if untrusted:
        answers["untrusted_content"] = "Yes"
    clauses = list(FULL_TESTS if full else LITE_TESTS)
    clauses += [test for test, applies in (
        ("OQ-20", full and answers["doc_drafting"] == "Yes"),
        ("OQ-30", answers["agents_qms"] == "Yes"),
        ("OQ-40", untrusted),
        ("RV-01", full and answers["in_use_before_validation"] == "Yes")) if applies]
    return {"framework": dict(FRAMEWORK), "method": "supplied", "answers": answers,
            "clauses": sorted(clauses)}


def evaluations(evidence_dirs: List[Path], base: Path) -> List[Dict[str, Any]]:
    """One evaluation per evidence JSON with a "result" under <dir>/<TEST-ID>/, by test id."""
    found: List[Tuple[str, str, Dict[str, Any]]] = []
    for directory in evidence_dirs:
        for path in sorted(directory.glob("*/**/*.json")):
            test_id = path.relative_to(directory).parts[0]
            if not TEST_ID.match(test_id):
                continue
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise RecordError("%s is not JSON: %s" % (path, exc))
            if not isinstance(doc, dict) or "result" not in doc:
                continue  # not the outcome of a check (a Markdown twin is never read)
            outcome = OUTCOMES.get(doc["result"], "skipped")
            tests = {"total": 1, "passed": 0, "failed": 0, "skipped": 0}
            tests[outcome] = 1
            name = "%s %s" % (test_id, path.stem)
            data = {"tool": "AI Coding Tool Validation Kit", "name": name, "tests": tests}
            if isinstance(doc.get("generated_at_utc"), str):
                data["date"] = doc["generated_at_utc"][:10]
            rel = Path(os.path.relpath(path.resolve(), base.resolve())).as_posix()
            found.append((test_id, rel, {
                "type": "evaluation",
                "title": "%s: %s" % (name, {"passed": "pass", "failed": "fail"}.get(outcome, "inconclusive")),
                "supports": [{"framework": FRAMEWORK["key"], "citation": test_id}],
                "source": {"path": rel, "sha256": _sha256(path)},
                "data": data}))
    artefacts = [art for _, _, art in sorted(found, key=lambda item: (item[0], item[1]))]
    if len(artefacts) > 200:
        raise RecordError("%d evidence files; a record holds at most 200" % len(artefacts))
    for index, art in enumerate(artefacts, 1):
        art["id"] = "evaluation-%d" % index
    return [{key: art[key] for key in ("id", "type", "title", "supports", "source", "data")}
            for art in artefacts]


def build_record(cfg: Dict[str, Any], tool_id: str, tool_version: str, evidence_root: Path,
                 base: Path, profile_override: str = None,
                 now: datetime.datetime = None) -> Dict[str, Any]:
    """The conformity record of one tool. Evidence paths are written relative to ``base``,
    the directory of the record, so a reader finds each file next to it."""
    tool = _tool(cfg, tool_id)
    profile = effective_profile(cfg, profile_override)
    dirs = [d for d in (evidence_root / tool_id, evidence_root / "GEN") if d.is_dir()]
    artefacts = evaluations(dirs, base)
    if not artefacts:
        raise RecordError("no evidence JSON with a \"result\" under %s/<TEST-ID>/ or %s/<TEST-ID>/"
                          % (evidence_root / tool_id, evidence_root / "GEN"))
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return {
        "schema_version": SCHEMA_VERSION,
        "record_id": str(uuid.uuid4()),
        "created_at": now.replace(microsecond=0).isoformat(),
        "collector": {"name": "ai-coding-tool-validation-kit", "version": __version__},
        "subject": {"name": tool["tool_name"], "version": tool_version,
                    "provider": tool["vendor"],
                    "intended_purpose": "AI coding tool validated as QMS and development software "
                                        "(ISO 13485:2016 clause 4.1.6) for %s" % cfg.get("org_name", "the organisation")},
        "scopes": [scope(cfg, tool, profile)],
        "artefacts": artefacts,
    }


def push(record_path: Path, url: str, key: str,
         opener: Callable[..., Any] = urllib.request.urlopen) -> Tuple[int, Dict[str, Any]]:
    """POST the record to Complear's conformity records API. Returns (status, response JSON)."""
    body = record_path.read_bytes()
    request = urllib.request.Request(
        url.rstrip("/") + "/api/conformity-records/v1", data=body, method="POST",
        headers={"Authorization": "Bearer %s" % key, "Content-Type": "application/json",
                 "Content-Length": str(len(body)),
                 "User-Agent": "ai-coding-tool-validation-kit/%s" % __version__})
    try:
        with opener(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


# --- The framework pack, generated from the templates -------------------------------------

_LITE_EXCLUDED = ["REQ-11", "REQ-12", "REQ-13", "REQ-14", "IQ-01", "IQ-02", "IQ-04", "IQ-05",
                  "OQ-11", "OQ-12", "OQ-13", "OQ-20", "OQ-30", "OQ-40", "RV-01"]
_GROUPS = [("IQ", "Installation qualification"), ("OQ", "Operational qualification"),
           ("MA", "Model admission"), ("RV", "Retrospective review"),
           ("PQ", "Performance qualification")]


def _plain(text: str) -> str:
    text = text.replace(" ({{doc_id_wi}})", "").replace("`", "")
    text = re.sub(r"\{\{#policy_pinned\}\}.*?\{\{/policy_pinned\}\}", "", text)
    return re.sub(r"\{\{[#/^]?[^}]*\}\}", "", text).strip()


def _sentence(text: str) -> str:
    text = text.rstrip(".") + "."
    return text[0].upper() + text[1:]


def _question(key: str, question: str, instructions: str, excluded: List[str],
              options=("Yes", "No"), when: str = "No") -> Dict[str, Any]:
    return {"key": key, "question": question, "options": list(options),
            "instructions": instructions,
            "rules": [{"when_answer": when, "effect": "excluded", "citations": excluded}]}


def build_pack(templates_dir: Path) -> Dict[str, Any]:
    """Complear's ai_coding_tool_validation framework from templates/full/."""
    req_src = (templates_dir / "full" / "02-requirements.md.tmpl").read_text(encoding="utf-8")
    test_src = (templates_dir / "full" / "05-test-protocol.md.tmpl").read_text(encoding="utf-8")
    heads = dict(re.findall(r"^### ((?:REQ|IQ|OQ|MA|RV|PQ)-\d\d) (.+)$", req_src + "\n" + test_src,
                            re.M))
    reqs: Dict[str, Tuple[str, str, str]] = {}
    for m in re.finditer(r"^\| (REQ-\d\d) \| ([^|]+) \| ([^|]+) \|", req_src, re.M):
        if m[1] not in reqs:
            title, _, statement = m[2].partition(": ")
            reqs[m[1]] = (_plain(title), _plain(statement), m[3].strip().strip("`"))
    tests: Dict[str, Tuple[str, str, str]] = {}
    for m in re.finditer(r"^\| ((?:IQ|OQ|MA|RV|PQ)-\d\d) \| ([^|]+) \| (REQ-[^|]+) \| ([^|]+) \|",
                         test_src, re.M):
        if m[1] not in tests:
            tests[m[1]] = (_plain(m[2]), m[3].strip(), m[4].strip())
    if len(reqs) != 17 or len(tests) != 25:
        raise RecordError("expected 17 requirements and 25 tests in the templates, found %d and %d"
                          % (len(reqs), len(tests)))

    requirements = [{"citation": k, "kind": "clause", "title": heads[k], "ordinal": i,
                     "body": _sentence(statement or title), "attributes": {"context": [context]}}
                    for i, (k, (title, statement, context)) in enumerate(sorted(reqs.items()), 1)]
    parts = []
    for gi, (prefix, name) in enumerate(_GROUPS, 1):
        children = [{"citation": k, "kind": "clause", "title": heads[k], "ordinal": i,
                     "body": "%s Verifies %s. Test type: %s." % (_sentence(title), verifies, kind),
                     "attributes": {"verifies": [r.strip() for r in verifies.split(",")],
                                    "test_type": [kind]}}
                    for i, (k, (title, verifies, kind)) in
                    enumerate(sorted(t for t in tests.items() if t[0].startswith(prefix)), 1)]
        parts.append({"citation": prefix, "kind": "chapter", "title": name, "ordinal": gi,
                      "children": children})

    questions = [
        _question("validation_profile", "Which validation profile applies to this tool?",
                  "Full when any of L1 to L9 applies: the AI output itself counts as a control or as "
                  "evidence (pinned model policy); an agent writes to QMS systems outside the code-host "
                  "gate; AI drafts controlled records without independent verification against sources; "
                  "high-impact GxP use; a CSA high process risk determination; an auditor, notified body "
                  "or customer expects more depth; earlier audit findings on the tool; AI output used as "
                  "verification evidence without independent review; an agent with credentials beyond "
                  "the pull-request path reads untrusted content. Otherwise lite: a qualified human and "
                  "required CI checks verify every AI-assisted change before it reaches a protected branch.",
                  _LITE_EXCLUDED, options=("Lite", "Full"), when="Lite"),
        _question("local_or_ci_surface", "Is a local CLI / IDE surface, or a CI / headless surface, in scope?",
                  "Yes when people run the tool on their own machines or a pipeline runs it headless.",
                  ["REQ-07"]),
        _question("ai_pr_review", "Is AI pull-request review in scope (an advisory reviewer or a PR review bot)?",
                  "Yes when an AI tool comments on or reviews pull requests.", ["REQ-15"]),
        _question("doc_drafting", "Does the tool draft controlled records (documents under document control)?",
                  "Yes when AI drafts any document that becomes a controlled record.",
                  ["REQ-13", "OQ-20"]),
        _question("agents_qms", "Do agents write to QMS systems (eQMS, issue tracker, document system) "
                                "outside a pull request?",
                  "Yes for any write path that bypasses the code-host gate: an MCP server, API token "
                  "or scheduled agent.", ["REQ-14", "OQ-30"]),
        _question("untrusted_content", "Does an agent read untrusted content (outside comments, web pages, "
                                       "MCP results)?",
                  "Yes when the agent can read text written by people outside the team.", ["OQ-40"]),
        _question("in_use_before_validation", "Was the tool in use before this validation?",
                  "Yes when changes made with the tool were merged before the release decision; RV-01 "
                  "then reviews that period.", ["RV-01"]),
    ]
    return {
        "framework": {
            "key": FRAMEWORK["key"], "name": "AI coding tool validation (kit %s)" % __version__,
            "version": FRAMEWORK["version"],
            "publisher": "AI Coding Tool Validation Kit, Bernardo Plaza (CC BY 4.0)",
            "region": "International", "regulation": "ISO 13485 4.1.6", "classes": [],
            "license": "public", "scope": "both", "default_availability": "selected"},
        "requirements": [
            {"citation": "REQ", "kind": "part", "title": "Requirements", "ordinal": 1,
             "children": requirements},
            {"citation": "TEST", "kind": "part", "title": "Tests", "ordinal": 2, "children": parts}],
        "questions": questions,
    }


def pack_json(templates_dir: Path) -> str:
    return json.dumps(build_pack(templates_dir), indent=2, ensure_ascii=False) + "\n"
