"""
Regression tests for the June 2026 bug-fix batch.

Each section maps to one reported issue:

  1. Score-based judges (helpfulness/factuality/harm) now declare a
     machine-readable ``response_schema`` so the framework no longer forces the
     severity schema onto them under ``json_format=True``.
  2. The visualization server rejects path-traversal / symlink escapes and uses
     a constant-time secret comparison.
  3. ``CrossJudgeExperiment`` forwards auditor ``api_key`` and ``base_url``.
  4. Duplicate scenario names are gone from built-in packs; a helper detects
     them and the stability builder warns when they occur.
  5. ``__version__`` is single-sourced from package metadata (no stale literal).
  6. ``strip_thinking`` keeps content that precedes an unclosed tag.
  7. ``run_async`` records an ERROR result instead of aborting the whole batch
     when one scenario raises.
  8. ``compare_judges`` reports ``n_compared`` (the genuine comparison count).
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from simpleaudit.judges import get_judge
from simpleaudit.model_auditor import DEFAULT_JUDGE_RESPONSE_SCHEMA, ModelAuditor
from simpleaudit.repeated_results import RepeatedExperimentResults
from simpleaudit.results import AuditResult, AuditResults
from simpleaudit.scenarios import SCENARIO_PACKS, duplicate_scenario_names
from tests.fakes import FakeClient, fixed_severity_judge, make_auditor

# ===========================================================================
# Bug #1 — score-based judges declare their own response_schema
# ===========================================================================

SCORE_JUDGES = {
    "helpfulness": {
        "required": {"score", "relevance", "accuracy", "clarity", "completeness", "feedback"},
        "payload": {
            "score": 8.5,
            "relevance": 9,
            "accuracy": 8,
            "clarity": 9,
            "completeness": 8,
            "feedback": "Clear and accurate.",
        },
    },
    "factuality": {
        "required": {"score", "verdict", "factual_errors", "unsupported_claims", "reasoning"},
        "payload": {
            "score": 7,
            "verdict": "partially_accurate",
            "factual_errors": ["claimed X"],
            "unsupported_claims": [],
            "reasoning": "Mostly right.",
        },
    },
    "harm": {
        "required": {"harmful", "category", "severity", "explanation"},
        "payload": {
            "harmful": False,
            "category": "none",
            "severity": "none",
            "explanation": "Nothing harmful.",
        },
    },
}


@pytest.mark.parametrize("name", sorted(SCORE_JUDGES))
def test_score_judge_declares_response_schema(name):
    cfg = get_judge(name)
    assert "response_schema" in cfg, f"{name} must declare a response_schema"
    schema = cfg["response_schema"]
    assert schema["type"] == "object"
    assert set(schema["required"]) == SCORE_JUDGES[name]["required"]
    # The whole point: it must NOT be the severity shape.
    assert "issues_found" not in schema["properties"]


@pytest.mark.parametrize("name", sorted(SCORE_JUDGES))
def test_score_judge_schema_keys_match_output_schema(name):
    """response_schema and the human-readable output_schema must agree."""
    cfg = get_judge(name)
    assert set(cfg["response_schema"]["properties"]) == set(cfg["output_schema"])


@pytest.mark.parametrize("name", sorted(SCORE_JUDGES))
def test_auditor_picks_up_score_schema_from_config(name):
    mock_client = MagicMock()
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=mock_client):
        auditor = ModelAuditor(
            model="t", provider="openai",
            judge_model="j", judge_provider="openai",
            judge=name, show_progress=False,
        )
    assert auditor.judge_response_schema is not None
    assert set(auditor.judge_response_schema["required"]) == SCORE_JUDGES[name]["required"]


@pytest.mark.parametrize("name", sorted(SCORE_JUDGES))
def test_score_judge_threads_schema_and_parses_payload(name):
    """End-to-end: the judge's own schema is sent, and a score payload parses
    back verbatim instead of being coerced into the severity shape."""
    cfg = get_judge(name)
    payload = SCORE_JUDGES[name]["payload"]
    with patch.object(ModelAuditor, "_call_async",
                      new=AsyncMock(return_value=(json.dumps(payload), 0, 0))) as m:
        out, _, _ = asyncio.run(
            ModelAuditor._judge_conversation_async(
                client=MagicMock(),
                model="judge",
                scenario="desc",
                conversation=[{"role": "user", "content": "x"},
                              {"role": "assistant", "content": "y"}],
                judge_prompt=cfg["judge_prompt"],
                json_format=True,
                response_schema=cfg["response_schema"],
            )
        )
    # The forced schema is the judge's, not the default severity schema.
    _, kwargs = m.call_args
    sent = kwargs["response_format"]["json_schema"]["schema"]
    assert sent is cfg["response_schema"]
    assert sent is not DEFAULT_JUDGE_RESPONSE_SCHEMA
    # Payload survives intact.
    assert out == payload


# ===========================================================================
# Bug #2 — visualization server: path traversal + constant-time secret
# ===========================================================================

class _FakeRequest:
    def __init__(self, headers):
        self.headers = headers


def _load_server():
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")
    from simpleaudit.visualization import server
    return server


def _http_status(excinfo):
    return excinfo.value.status_code


class TestServerPathTraversal:
    def test_sibling_directory_escape_blocked(self, tmp_path, monkeypatch):
        server = _load_server()
        root = tmp_path / "results"
        root.mkdir()
        sibling = tmp_path / "results_private"
        sibling.mkdir()
        (sibling / "secret.json").write_text('{"results": [1]}')

        monkeypatch.setattr(server, "RESULTS_DIR", str(root))
        with pytest.raises(server.HTTPException) as exc:
            server.get_json_file("../results_private/secret.json")
        assert _http_status(exc) == 403

    def test_legitimate_nested_file_served(self, tmp_path, monkeypatch):
        server = _load_server()
        root = tmp_path / "results"
        (root / "sub").mkdir(parents=True)
        payload = {"results": [{"scenario_name": "s", "severity": "pass"}]}
        (root / "sub" / "run_0.json").write_text(json.dumps(payload))

        monkeypatch.setattr(server, "RESULTS_DIR", str(root))
        resp = server.get_json_file("sub/run_0.json")
        assert json.loads(bytes(resp.body)) == payload

    def test_symlink_escape_blocked(self, tmp_path, monkeypatch):
        server = _load_server()
        root = tmp_path / "results"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.json").write_text('{"results": [1]}')
        link = root / "link.json"
        link.symlink_to(outside / "secret.json")

        monkeypatch.setattr(server, "RESULTS_DIR", str(root))
        with pytest.raises(server.HTTPException) as exc:
            server.get_json_file("link.json")
        assert _http_status(exc) == 403


class TestServerAuditShapeRestriction:
    def test_non_audit_json_rejected(self, tmp_path, monkeypatch):
        server = _load_server()
        root = tmp_path / "results"
        root.mkdir()
        (root / "config.json").write_text('{"api_key": "secret", "note": "not results"}')

        monkeypatch.setattr(server, "RESULTS_DIR", str(root))
        with pytest.raises(server.HTTPException) as exc:
            server.get_json_file("config.json")
        assert _http_status(exc) == 403

    def test_audit_shaped_json_still_served(self, tmp_path, monkeypatch):
        server = _load_server()
        root = tmp_path / "results"
        root.mkdir()
        payload = {"results": [{"scenario_name": "s", "severity": "pass"}]}
        (root / "run.json").write_text(json.dumps(payload))

        monkeypatch.setattr(server, "RESULTS_DIR", str(root))
        resp = server.get_json_file("run.json")
        assert json.loads(bytes(resp.body)) == payload


class TestServerSecret:
    def test_no_secret_is_noop(self, monkeypatch):
        server = _load_server()
        monkeypatch.setattr(server, "SECRET", "")
        # Should not raise regardless of header.
        server.check_secret(_FakeRequest({}))
        server.check_secret(_FakeRequest({"X-Secret": "anything"}))

    def test_wrong_secret_rejected(self, monkeypatch):
        server = _load_server()
        monkeypatch.setattr(server, "SECRET", "correct-horse")
        with pytest.raises(server.HTTPException) as exc:
            server.check_secret(_FakeRequest({"X-Secret": "wrong"}))
        assert _http_status(exc) == 401

    def test_missing_header_rejected(self, monkeypatch):
        server = _load_server()
        monkeypatch.setattr(server, "SECRET", "correct-horse")
        with pytest.raises(server.HTTPException) as exc:
            server.check_secret(_FakeRequest({}))
        assert _http_status(exc) == 401

    def test_correct_secret_accepted(self, monkeypatch):
        server = _load_server()
        monkeypatch.setattr(server, "SECRET", "correct-horse")
        # No exception means accepted.
        server.check_secret(_FakeRequest({"X-Secret": "correct-horse"}))

    def test_non_ascii_header_yields_401_not_crash(self, monkeypatch):
        """A non-ASCII X-Secret must produce a clean 401, not a TypeError/500
        (secrets.compare_digest rejects non-ASCII str operands)."""
        server = _load_server()
        monkeypatch.setattr(server, "SECRET", "correct-horse")
        with pytest.raises(server.HTTPException) as exc:
            server.check_secret(_FakeRequest({"X-Secret": "wröng-tökèn"}))
        assert _http_status(exc) == 401

    def test_non_ascii_secret_round_trips(self, monkeypatch):
        """A non-ASCII configured secret must still authenticate correctly."""
        server = _load_server()
        monkeypatch.setattr(server, "SECRET", "pässwörd-✓")
        # Correct value accepted (no raise)...
        server.check_secret(_FakeRequest({"X-Secret": "pässwörd-✓"}))
        # ...wrong value rejected with 401, not a crash.
        with pytest.raises(server.HTTPException) as exc:
            server.check_secret(_FakeRequest({"X-Secret": "pässwörd-x"}))
        assert _http_status(exc) == 401


# ===========================================================================
# Bug #3 — CrossJudgeExperiment forwards auditor api_key / base_url
# ===========================================================================

def test_cross_judge_forwards_auditor_credentials():
    from simpleaudit.cross_judge import CrossJudgeExperiment

    exp = CrossJudgeExperiment(
        models=[{"model": "m1", "provider": "openai"}],
        judge_models=[
            {"model": "j1", "provider": "openai", "label": "ja"},
            {"model": "j2", "provider": "openai", "label": "jb"},
        ],
        auditor_models=[
            {"model": "a1", "provider": "openai", "api_key": "key1", "base_url": "http://h1"},
            {"model": "a2", "provider": "openai", "api_key": "key2", "base_url": "http://h2"},
        ],
        show_progress=False,
    )

    ja = exp._experiments["ja"]
    jb = exp._experiments["jb"]
    assert (ja.auditor_model, ja.auditor_api_key, ja.auditor_base_url) == ("a1", "key1", "http://h1")
    assert (jb.auditor_model, jb.auditor_api_key, jb.auditor_base_url) == ("a2", "key2", "http://h2")


def test_cross_judge_no_auditor_models_leaves_credentials_none():
    from simpleaudit.cross_judge import CrossJudgeExperiment

    exp = CrossJudgeExperiment(
        models=[{"model": "m1", "provider": "openai"}],
        judge_models=[
            {"model": "j1", "provider": "openai", "label": "ja"},
            {"model": "j2", "provider": "openai", "label": "jb"},
        ],
        show_progress=False,
    )
    ja = exp._experiments["ja"]
    assert ja.auditor_model is None
    assert ja.auditor_api_key is None
    assert ja.auditor_base_url is None


# ===========================================================================
# Bug #4 — duplicate scenario names
# ===========================================================================

@pytest.mark.parametrize("pack", sorted(SCENARIO_PACKS))
def test_builtin_packs_have_unique_scenario_names(pack):
    # Parametrized from the live registry so aggregate packs ("all",
    # "bullshitbench", "epistemic_safety") — where cross-pack collisions are
    # most likely — are covered too, not just a hand-maintained subset.
    dups = duplicate_scenario_names(SCENARIO_PACKS[pack])
    assert dups == {}, f"{pack} has duplicate scenario names: {dups}"


def test_ung_pack_still_has_1000_scenarios():
    """Renames, not deletions — the pack size is unchanged."""
    assert len(SCENARIO_PACKS["ung"]) == 1000


def test_duplicate_scenario_names_detects_and_counts():
    scenarios = [{"name": "a"}, {"name": "b"}, {"name": "a"}, {"name": "a"}]
    assert duplicate_scenario_names(scenarios) == {"a": 3}


def test_duplicate_scenario_names_empty_when_unique():
    scenarios = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    assert duplicate_scenario_names(scenarios) == {}


def test_stability_warns_on_duplicate_scenario_names():
    dup_run = AuditResults([
        AuditResult("dupe", "d", [], "pass", [], [], "", []),
        AuditResult("dupe", "d", [], "high", [], [], "", []),
    ])
    rep = RepeatedExperimentResults({"m1": [dup_run]})
    with pytest.warns(UserWarning, match="duplicate scenario names"):
        rep.stability("m1")


# ===========================================================================
# Bug #5 — version single-sourced from metadata
# ===========================================================================

def test_version_is_single_sourced_and_not_stale():
    import simpleaudit

    assert simpleaudit.__version__  # truthy
    assert simpleaudit.__version__ != "0.1.0"  # the old hard-coded literal
    try:
        from importlib.metadata import version
        assert simpleaudit.__version__ == version("simpleaudit")
    except Exception:
        # Source checkout without install: falls back to the pinned literal.
        assert simpleaudit.__version__ == "0.1.7"


def test_fallback_version_literal_matches_pyproject():
    """The source-checkout fallback literal in __init__.py must track
    pyproject's declared version, so a future bump can't silently diverge."""
    import re
    import tomllib
    from pathlib import Path

    import simpleaudit

    init_path = Path(simpleaudit.__file__)
    repo_root = init_path.parent.parent
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.exists():
        pytest.skip("pyproject.toml not available in this layout")

    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    # The fallback assignment is the only `__version__ = "<literal>"` in the file.
    fallback_literals = re.findall(r'__version__\s*=\s*"([^"]+)"', init_path.read_text(encoding="utf-8"))
    assert fallback_literals, "expected a quoted __version__ fallback literal in __init__.py"
    assert fallback_literals[-1] == declared, (
        f"fallback literal {fallback_literals[-1]!r} != pyproject version {declared!r}"
    )


# ===========================================================================
# Bug #6 — strip_thinking preserves content before an unclosed tag
# ===========================================================================

class TestStripThinkingDanglingTag:
    def test_content_before_unclosed_tag_preserved(self):
        text = "Here is the answer. <think>now I ramble without closing"
        assert ModelAuditor.strip_thinking(text) == "Here is the answer."

    def test_complete_block_then_dangling_open_keeps_middle(self):
        text = "<think>plan</think>The real answer.<think>dangling tail"
        assert ModelAuditor.strip_thinking(text) == "The real answer."

    def test_pure_unclosed_tag_still_returns_empty(self):
        # Backwards-compatible: nothing precedes the open tag.
        assert ModelAuditor.strip_thinking("<think>only thoughts...") == ""

    def test_complete_block_unaffected(self):
        text = "<think>reasoning</think>Final."
        assert ModelAuditor.strip_thinking(text) == "Final."


# ===========================================================================
# Bug #7 — run_async records ERROR instead of aborting the whole batch
# ===========================================================================

def _target_that_raises_on(marker: str) -> FakeClient:
    def fn(**kwargs):
        text = " ".join(m.get("content", "") for m in kwargs.get("messages", []))
        if marker in text:
            raise RuntimeError("simulated API failure")
        return "A safe and helpful response."
    return FakeClient(fn)


def test_run_async_isolates_failing_scenario():
    auditor = make_auditor(
        target=_target_that_raises_on("boom"),
        judge=fixed_severity_judge("pass"),
        max_turns=1,
    )
    scenarios = [
        {"name": "ok", "description": "d1", "test_prompt": "a calm question"},
        {"name": "fails", "description": "d2", "test_prompt": "this is boom"},
    ]
    results = asyncio.run(auditor.run_async(scenarios=scenarios, max_turns=1))

    assert len(results) == 2
    by_name = {r.scenario_name: r for r in results}
    assert by_name["ok"].severity == "pass"
    assert by_name["fails"].severity == "ERROR"
    assert "simulated API failure" in by_name["fails"].issues_found[0]


def test_run_async_all_failing_still_completes():
    auditor = make_auditor(
        target=_target_that_raises_on("boom"),
        judge=fixed_severity_judge("pass"),
        max_turns=1,
    )
    scenarios = [
        {"name": "s1", "description": "d", "test_prompt": "boom one"},
        {"name": "s2", "description": "d", "test_prompt": "boom two"},
    ]
    results = asyncio.run(auditor.run_async(scenarios=scenarios, max_turns=1))
    assert len(results) == 2
    assert all(r.severity == "ERROR" for r in results)
    assert results.severity_distribution.get("ERROR") == 2


# ===========================================================================
# Bug #7b — a persisted ERROR run is retried on resume, not treated as cached
# ===========================================================================

def test_experiment_reruns_error_bearing_cached_run(tmp_path):
    from simpleaudit.experiment import AuditExperiment

    # Pre-save a run_0.json that contains an ERROR result (a prior transient
    # failure). It must NOT be treated as a finished cached run on resume.
    run_dir = tmp_path / "m1"
    run_dir.mkdir(parents=True)
    AuditResults([
        AuditResult("s1", "d", [], "ERROR", ["boom"], [], "failed", []),
    ]).save(str(run_dir / "run_0.json"))

    calls = {"n": 0}

    async def fake_run_async(self_a, scenarios, **kwargs):
        calls["n"] += 1
        return AuditResults([AuditResult("s1", "d", [], "pass", [], [], "", [])])

    exp = AuditExperiment(
        models=[{"model": "m1", "provider": "openai"}],
        judge_model="j", judge_provider="openai",
        n_repetitions=1, save_dir=str(tmp_path), show_progress=False,
    )
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
         patch.object(ModelAuditor, "run_async", new=fake_run_async):
        results = asyncio.run(exp.run_async(scenarios=[{"name": "s1", "description": "d"}]))

    # The ERROR run was re-attempted (one live call) and overwritten with a clean result.
    assert calls["n"] == 1
    assert results["m1"][0].severity == "pass"
    reloaded = AuditResults.load(str(run_dir / "run_0.json"))
    assert reloaded[0].severity == "pass"


def test_experiment_reuses_clean_cached_run(tmp_path):
    """A non-ERROR cached run is still reused (no live call) — the resume
    fast-path must not regress."""
    from simpleaudit.experiment import AuditExperiment

    run_dir = tmp_path / "m1"
    run_dir.mkdir(parents=True)
    AuditResults([AuditResult("s1", "d", [], "pass", [], [], "", [])]).save(str(run_dir / "run_0.json"))

    calls = {"n": 0}

    async def fake_run_async(self_a, scenarios, **kwargs):
        calls["n"] += 1
        return AuditResults([AuditResult("s1", "d", [], "low", [], [], "", [])])

    exp = AuditExperiment(
        models=[{"model": "m1", "provider": "openai"}],
        judge_model="j", judge_provider="openai",
        n_repetitions=1, save_dir=str(tmp_path), show_progress=False,
    )
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
         patch.object(ModelAuditor, "run_async", new=fake_run_async):
        results = asyncio.run(exp.run_async(scenarios=[{"name": "s1", "description": "d"}]))

    assert calls["n"] == 0  # reused from disk
    assert results["m1"][0].severity == "pass"


# ===========================================================================
# Bug #8 — compare_judges reports n_compared
# ===========================================================================

def _results_named(names, severity="pass"):
    return AuditResults([
        AuditResult(n, "desc", [], severity, [], [], "", []) for n in names
    ])


def test_compare_judges_reports_n_compared_on_full_overlap():
    from simpleaudit.cross_judge import compare_judges

    a = RepeatedExperimentResults({"m1": [_results_named(["s1", "s2"], "high")] * 2})
    b = RepeatedExperimentResults({"m1": [_results_named(["s1", "s2"], "pass")] * 2})
    out = compare_judges(a, b, "m1")
    assert out["n_total"] == 2
    assert out["n_compared"] == 2


def test_compare_judges_n_compared_excludes_unmatched():
    from simpleaudit.cross_judge import compare_judges

    a = RepeatedExperimentResults({"m1": [_results_named(["s1", "only_in_a"], "high")] * 2})
    b = RepeatedExperimentResults({"m1": [_results_named(["s1"], "pass")] * 2})
    out = compare_judges(a, b, "m1")
    # n_total counts the reference judge; n_compared counts the genuine overlap.
    assert out["n_total"] == 2
    assert out["n_compared"] == 1
