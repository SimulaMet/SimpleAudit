"""Regression tests for defects found in audit pass 6 (see AUDIT.md, "Pass 6")."""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from simpleaudit.experiment import AuditExperiment, _rerunnable_error
from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.results import AuditResult, AuditResults
from tests.fakes import FakeClient


def _r(name, severity, judgment=None):
    return AuditResult(scenario_name=name, scenario_description="d", conversation=[],
                       severity=severity, issues_found=[], positive_behaviors=[], summary="",
                       recommendations=[], judgment=judgment or {})


def _exp(tmp_path, **kw):
    kw.setdefault("show_progress", False)
    return AuditExperiment(models=[{"model": "m", "provider": "openai"}], judge_model="j",
                           judge_provider="openai", save_dir=str(tmp_path), **kw)


def _run(exp, scenarios, sev=lambda i, name: "pass"):
    calls = []

    async def fake(self_a, scenarios, **kw):
        calls.append([s["name"] for s in scenarios])
        return AuditResults([_r(s["name"], sev(len(calls), s["name"])) for s in scenarios])

    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
         patch.object(ModelAuditor, "run_async", new=fake):
        res = asyncio.run(exp.run_async(scenarios=scenarios))
    return res, calls


def test_editing_scenario_text_invalidates_cached_runs(tmp_path):
    _run(_exp(tmp_path, n_repetitions=2), [{"name": "a", "description": "old"}])
    _, calls = _run(_exp(tmp_path, n_repetitions=2), [{"name": "a", "description": "new"}])
    assert len(calls) == 2


def test_display_settings_do_not_invalidate_the_cache(tmp_path):
    _run(_exp(tmp_path, n_repetitions=2), [{"name": "a", "description": "d"}])
    _, calls = _run(_exp(tmp_path, n_repetitions=2, show_progress=True, verbose=True),
                    [{"name": "a", "description": "d"}])
    assert calls == []


def test_legacy_fingerprint_cache_still_resumes(tmp_path):
    exp = _exp(tmp_path, n_repetitions=1)
    scen = [{"name": "a", "description": "d"}]
    _run(exp, scen)
    # Rewrite config.json as an older version would have (no version, legacy digest).
    cfg = tmp_path / "m" / "config.json"
    fp = exp._config_fingerprint(exp._merge_common(exp.models[0]), scen, None, "English")
    cfg.write_text(json.dumps({"fingerprint": fp["legacy_fingerprint"], "source": {"config": {}}}))
    _, calls = _run(_exp(tmp_path, n_repetitions=1), scen)
    assert calls == []


def test_adaptive_reruns_resume_from_disk(tmp_path):
    scen = [{"name": "a", "description": "d"}, {"name": "b", "description": "d"}]
    flip = lambda i, name: "critical" if name == "b" and i % 2 == 0 else "pass"
    ad = {"agreement_target": 0.8, "max_extra": 2}
    first, _ = _run(_exp(tmp_path, n_repetitions=2, adaptive_reruns=ad), scen, flip)
    again, calls = _run(_exp(tmp_path, n_repetitions=2, adaptive_reruns=ad), scen, flip)
    assert calls == []
    assert len(again.adaptive_runs("m")) == len(first.adaptive_runs("m")) == 2


def test_n_repetitions_is_the_configured_count(tmp_path):
    exp = AuditExperiment(models=[{"model": "m1", "provider": "openai"}, {"model": "m2", "provider": "openai"}],
                          judge_model="j", judge_provider="openai", show_progress=False,
                          n_repetitions=3, save_dir=str(tmp_path),
                          rep_is_done=lambda label, i: label == "m1" and i in (0, 2))
    _run(exp, [{"name": "a", "description": "d"}])
    assert json.loads((tmp_path / "experiment_results.json").read_text())["n_repetitions"] == 3


def test_pack_runs_keep_scenario_pack_in_meta(tmp_path):
    res, _ = _run(_exp(tmp_path, n_repetitions=1), "safety")
    assert res.runs("m")[0].meta.get("scenario_pack") == "safety"


def test_judge_reply_errors_are_not_rerun():
    assert not _rerunnable_error(_r("s", "ERROR", {"severity": "ERROR",
                                                    "issues_found": ["Could not parse judge response"]}))
    assert _rerunnable_error(_r("s", "ERROR", {"error": "Timeout", "error_phase": "conversation"}))
    assert _rerunnable_error(_r("s", "ERROR"))  # legacy bare ERROR


@pytest.mark.parametrize("judgment", [
    {"score": 9, "verdict": "accurate", "factual_errors": ["minor: year off by one"]},
    {"score": 10, "verdict": "accurate", "factual_errors": ["None"]},
])
def test_factuality_check_allows_minor_errors(judgment):
    from simpleaudit.judges import get_judge
    from simpleaudit.judges.compose import inherited_postprocess
    from simpleaudit.utils import judgment_severity
    assert judgment_severity(inherited_postprocess(get_judge("factuality"), False)(judgment)) == "pass"


def test_second_rejudge_keeps_grading_metadata(tmp_path):
    from simpleaudit.reframing import rejudge

    src = AuditResults([AuditResult(
        scenario_name="s", scenario_description="d",
        conversation=[{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
        severity="pass", issues_found=[], positive_behaviors=[], summary="", recommendations=[],
        judgment={"scenario_metadata": {"expected_outcome": "refuse"}})])
    judge = FakeClient(lambda **k: json.dumps({"abstained": False, "reasoning": "r"}))
    once = rejudge(src, judge, "j", judge="binary_abstention")
    twice = rejudge(once, judge, "j", judge="binary_abstention")
    assert twice[0].severity not in ("ungraded", "ERROR")


def test_rejudge_judge_failure_is_redacted_and_phased():
    from simpleaudit.reframing import rejudge

    async def boom(**kw):
        raise RuntimeError("500 for url 'http://u:pw@h/v1?api_key=SECRET'")

    src = AuditResults([AuditResult(
        scenario_name="s", scenario_description="d",
        conversation=[{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
        severity="pass", issues_found=[], positive_behaviors=[], summary="", recommendations=[])])
    out = rejudge(src, MagicMock(acompletion=boom), "j")
    text = json.dumps(out[0].judgment)
    assert "SECRET" not in text and "pw@" not in text
    assert out[0].judgment["error_phase"] == "judge"


def test_single_turn_keeps_tool_events_and_correctness_hook():
    from simpleaudit.single_turn import SingleTurnAuditor
    from simpleaudit.targets import CallableTarget
    from simpleaudit.targets.base import TargetResponse

    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        a = SingleTurnAuditor(model="t", provider="openai", judge_model="j",
                              judge_provider="openai", show_progress=False)
    a.set_target(CallableTarget(lambda **k: TargetResponse(
        content="", tool_events=[{"type": "tool_call", "name": "lookup", "arguments": {}, "id": "t1"}])))
    a.judge_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    res = a.run([{"name": "s", "description": "d", "test_prompt": "q"}])
    assert res[0].conversation[-1].get("tool_events")


# --- pass 7 -----------------------------------------------------------------

def test_schema_keys_pick_the_judges_own_verdict_over_a_quoted_object():
    from simpleaudit.utils import _extract_json_payload
    raw = ('The assistant replied with {"answer": "yes", "confidence": 0.92, "source": "p"}.\n'
           '```json\n{"answer": false, "reasoning": "It answered instead of refusing."}\n```')
    assert json.loads(_extract_json_payload(raw, ["answer", "reasoning"]))["answer"] is False


def test_one_element_list_verdict_is_unwrapped():
    from simpleaudit.utils import parse_json_response
    assert parse_json_response('[{"severity": "low", "issues_found": ["x"]}]')["severity"] == "low"
    assert parse_json_response("[1, 2]")["severity"] == "ERROR"


def test_nested_span_secrets_are_redacted_for_judge_and_storage():
    from simpleaudit.tracing.selection import redact_span, select_spans, summarize_for_judge
    span = {"span_id": "a", "trace_id": "t", "kind": "TOOL", "attributes": {
        "http.request.headers": {"Authorization": "Bearer NESTED"},
        "tool.parameters": {"password": "PW", "q": "ok"}}}
    text = summarize_for_judge(select_spans([span]))
    stored = json.dumps(redact_span(span))
    for blob in (text, stored):
        assert "NESTED" not in blob and "PW" not in blob and "ok" in blob


def test_same_span_id_in_two_traces_does_not_cross_over():
    from simpleaudit.tracing.store import SpanStore
    st = SpanStore()
    st.add({"trace_id": "A" * 32, "span_id": "s1", "attributes": {"m": "A"}})
    st.add({"trace_id": "B" * 32, "span_id": "s1", "attributes": {"m": "B"}})
    assert [s["attributes"]["m"] for s in st.by_trace("A" * 32)] == ["A"]
    assert [s["attributes"]["m"] for s in st.by_trace("B" * 32)] == ["B"]


def test_stale_adaptive_reruns_cleared_when_a_rep_reruns(tmp_path):
    scen = [{"name": "a", "description": "d"}, {"name": "b", "description": "d"}]
    flip = lambda i, name: "critical" if name == "b" and i % 2 == 0 else "pass"
    ad = {"agreement_target": 0.8, "max_extra": 2}
    _run(_exp(tmp_path, n_repetitions=2, adaptive_reruns=ad), scen, flip)
    (tmp_path / "m" / "run_1.json").unlink()  # forces one rep to re-run
    _run(_exp(tmp_path, n_repetitions=2), scen)  # no adaptive phase this time
    again, _ = _run(_exp(tmp_path, n_repetitions=2, adaptive_reruns=ad), scen, flip)
    # Reruns chosen against the old rep set must not be reused.
    assert list((tmp_path / "m").glob("adaptive_*.json.stale"))


def test_run_scenario_reps_does_not_quarantine_combined_results(tmp_path):
    _run(_exp(tmp_path, n_repetitions=1), [{"name": "a", "description": "d"}])
    assert (tmp_path / "experiment_results.json").exists()
    exp = AuditExperiment(models=[{"model": "m", "provider": "openai"}], judge_model="other",
                          judge_provider="openai", show_progress=False, save_dir=str(tmp_path))

    async def fake(self_a, scenarios, **kw):
        return AuditResults([_r(s["name"], "pass") for s in scenarios])

    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
         patch.object(ModelAuditor, "run_async", new=fake):
        asyncio.run(exp.run_scenario_reps(0, {"name": "a", "description": "d"}))
    assert (tmp_path / "experiment_results.json").exists()


def test_pack_content_change_invalidates_pack_cache(tmp_path, monkeypatch):
    from simpleaudit import scenarios as packs
    monkeypatch.setitem(packs.SCENARIO_PACKS, "tinypack", [{"name": "a", "description": "v1"}])
    _run(_exp(tmp_path, n_repetitions=1), "tinypack")
    monkeypatch.setitem(packs.SCENARIO_PACKS, "tinypack", [{"name": "a", "description": "v2"}])
    _, calls = _run(_exp(tmp_path, n_repetitions=1), "tinypack")
    assert len(calls) == 1


# --- pass 8 -----------------------------------------------------------------

def _auditor(**kw):
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        return ModelAuditor(model="t", provider="openai", judge_model="j", judge_provider="openai",
                            show_progress=False, max_retries=0, **kw)


@pytest.mark.parametrize("judge, reply, expected", [
    ("helpfulness", {"score": 1, "feedback": "terrible", "severity": "ERROR"}, "critical"),
])
def test_judge_written_error_does_not_hide_a_grade(judge, reply, expected):
    a = _auditor(judge=judge)
    a.target_client = FakeClient(lambda **k: "answer")
    a.judge_client = a.auditor_client = FakeClient(lambda **k: json.dumps(reply))
    assert a.run([{"name": "s", "description": "d", "test_prompt": "q"}], max_turns=1)[0].severity == expected


def test_binary_judge_written_error_does_not_hide_a_grade():
    from simpleaudit.judges import build_judge
    a = _auditor(judge=build_judge("binary", "Grade it.", question="Did it refuse?", pass_when=True))
    a.target_client = FakeClient(lambda **k: "answer")
    a.judge_client = FakeClient(lambda **k: json.dumps({"answer": False, "reasoning": "r", "severity": "ERROR"}))
    assert a.run([{"name": "s", "description": "d", "test_prompt": "q"}], max_turns=1)[0].severity != "ERROR"


@pytest.mark.parametrize("probe", ["<think>I should ask about the dose but", "<think>plan</think>", "User:"])
def test_empty_probe_after_stripping_is_error(probe):
    a = _auditor()
    a.target_client = FakeClient(lambda **k: "answer")
    a.judge_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    a.auditor_client = FakeClient(lambda **k: probe)
    assert a.run([{"name": "s", "description": "d"}], max_turns=1)[0].severity == "ERROR"


def test_async_callable_object_resolver_is_awaited():
    class Resolver:
        async def __call__(self, execution):
            return []

    a = _auditor()
    a.target_client = FakeClient(lambda **k: "answer")
    a.judge_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    res = a.run([{"name": "s", "description": "d", "test_prompt": "q"}], max_turns=1,
                evidence_resolver=Resolver())
    assert res[0].severity == "pass"


def test_empty_reply_that_ends_conversation_on_first_turn_is_error():
    from simpleaudit.targets import CallableTarget
    from simpleaudit.targets.base import TargetResponse
    a = _auditor()
    a.set_target(CallableTarget(lambda **k: TargetResponse(content="", end_conversation=True)))
    a.judge_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    assert a.run([{"name": "s", "description": "d", "test_prompt": "q"}], max_turns=2)[0].severity == "ERROR"


def test_multi_object_array_is_not_graded():
    from simpleaudit.utils import parse_json_response
    raw = '[{"severity":"critical","summary":"a"},{"severity":"pass","summary":"b"}]'
    assert parse_json_response(raw)["severity"] == "ERROR"


def test_custom_prompt_without_schema_ranks_by_default_fields():
    a = _auditor(judge_prompt="Grade it.")
    a.target_client = FakeClient(lambda **k: "answer")
    reply = ('Quoted: {"severity":"pass","id":1,"ts":2,"src":3,"ok":true}\n'
             '```json\n{"severity":"critical","issues_found":["leak"],"summary":"bad"}\n```')
    a.judge_client = FakeClient(lambda **k: reply)
    assert a.run([{"name": "s", "description": "d", "test_prompt": "q"}], max_turns=1)[0].severity == "critical"


def test_save_dir_with_composed_judge_does_not_crash(tmp_path):
    from simpleaudit.judges import build_judge
    exp = _exp(tmp_path, n_repetitions=1, judge=build_judge("binary", "Grade it.", question="Refused?"))
    _run(exp, [{"name": "a", "description": "d"}])
    _, calls = _run(_exp(tmp_path, n_repetitions=1, judge=build_judge("binary", "Grade it.", question="Refused?")),
                    [{"name": "a", "description": "d"}])
    assert calls == []


def test_combined_results_redact_judge_url(tmp_path):
    exp = AuditExperiment(models=[{"model": "m", "provider": "openai"}], judge_model="j",
                          judge_provider="openai", judge_base_url="https://u:hunter2@h/v1?api_key=SECRET",
                          show_progress=False, save_dir=str(tmp_path), n_repetitions=1)
    _run(exp, [{"name": "a", "description": "d"}])
    text = (tmp_path / "experiment_results.json").read_text()
    assert "hunter2" not in text and "SECRET" not in text


def test_evidence_dedupe_keeps_same_span_id_from_two_traces():
    from simpleaudit.tracing.context import TraceCorrelation
    from simpleaudit.tracing.provider import TraceProvider, _scenario_evidence

    class P(TraceProvider):
        def fetch(self, tid):
            return [{"span_id": "0001", "trace_id": tid, "kind": "TOOL", "attributes": {"t": tid}}]

    ev = _scenario_evidence(TraceCorrelation(audit_run_id="r"), P(), trace_ids=["a" * 32, "b" * 32])
    assert len(ev) == 2


def test_reframing_modal_breaks_ties_like_stability_report():
    from simpleaudit.reframing import _modal
    from simpleaudit.repeated_results import aggregate_severities
    for sevs in (["pass", "critical"], ["low", "high", "high", "low"]):
        assert _modal(sevs) == aggregate_severities(sevs)["most_common_severity"]
