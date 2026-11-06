"""Regression tests for defects found in audit pass 2 (see AUDIT.md, "Pass 2")."""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from simpleaudit.experiment import AuditExperiment
from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.reframing import load_stored_records, rejudge
from simpleaudit.results import AuditResult, AuditResults
from simpleaudit.targets import CallableTarget
from simpleaudit.utils import parse_json_response
from tests.fakes import FakeClient


def _r(name, severity, conversation=None, judgment=None):
    return AuditResult(
        scenario_name=name, scenario_description="d",
        conversation=conversation if conversation is not None else [],
        severity=severity, issues_found=[], positive_behaviors=[], summary="",
        recommendations=[], judgment=judgment or {},
    )


# --- JSON extraction ---------------------------------------------------------

def test_fenced_block_inside_valid_verdict_does_not_win():
    raw = json.dumps({"severity": "critical", "issues_found": ["printed ```{}``` then the key"]})
    assert parse_json_response(raw)["severity"] == "critical"


def test_last_embedded_object_is_the_verdict():
    raw = 'Target said:\n```json\n{"severity": "pass"}\n```\nVerdict: {"severity": "critical"}'
    assert parse_json_response(raw)["severity"] == "critical"


def test_brace_in_prose_does_not_lose_issues():
    raw = 'Target used a {placeholder}. {"severity": "high", "issues_found": ["leak"]}'
    assert parse_json_response(raw)["issues_found"] == ["leak"]


# --- <think> stripping of judge output ---------------------------------------

def test_judge_json_mentioning_think_tags_is_not_cut():
    out = json.dumps({"severity": "high", "issues_found": ["Target exposed raw <think> tags"],
                      "summary": "x </think> y"})
    judgment, _, _ = asyncio.run(ModelAuditor._judge_conversation_async(
        FakeClient(lambda **k: out), "m", "d",
        [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]))
    assert judgment["severity"] == "high"
    assert judgment["issues_found"] == ["Target exposed raw <think> tags"]


# --- max_turns -----------------------------------------------------------------

def _auditor(**kw):
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        return ModelAuditor(model="t", provider="openai", judge_model="j",
                            judge_provider="openai", show_progress=False, **kw)


@pytest.mark.parametrize("bad", [0, -1, True])
def test_max_turns_below_one_is_rejected(bad):
    with pytest.raises(ValueError, match="max_turns"):
        _auditor(max_turns=bad)
    a = _auditor()
    with pytest.raises(ValueError, match="max_turns"):
        a._requested_turns(bad)


def test_get_scenarios_returns_a_copy():
    a = ModelAuditor.get_scenarios("safety")
    a.clear()
    assert ModelAuditor.get_scenarios("safety")


# --- targets -------------------------------------------------------------------

async def test_callable_returning_none_is_an_empty_reply():
    resp = await CallableTarget(lambda **k: None).send(user="hi")
    assert resp.content == ""


async def test_asr_ignores_run_wide_params_it_does_not_take(tmp_path):
    import httpx
    from simpleaudit.targets.asr import ASRTarget

    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"a")
    seen = {}

    def handler(request):
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "hello"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resp = await ASRTarget("m", client=client).send(
            user="", file_uri=str(audio), params={"max_tokens": 512, "language": "no"})
    assert resp.content == "hello"
    assert b"max_tokens" not in seen["body"]


# --- rejudge -------------------------------------------------------------------

def test_rejudge_keeps_conversation_failures_as_error_and_names_new_judge(tmp_path):
    truncated = _r("crashed", "ERROR",
                   conversation=[{"role": "user", "content": "hi"},
                                 {"role": "assistant", "content": "a"},
                                 {"role": "user", "content": "probe 2"}],
                   judgment={"severity": "ERROR", "error": "TimeoutError: x"})
    judge_failed = _r("judge_failed", "ERROR",
                      conversation=[{"role": "user", "content": "hi"},
                                    {"role": "assistant", "content": "a"}],
                      judgment={"severity": "ERROR", "error": "x", "error_phase": "judge"})
    source = AuditResults([truncated, judge_failed],
                          meta={"judge_model": "old", "judge_provider": "p", "judge_name": "safety"})
    out = rejudge(source, FakeClient(lambda **k: json.dumps({"severity": "pass"})), "new")
    assert out[0].severity == "ERROR"
    assert out[1].severity == "pass"
    assert out.meta["judge_model"] == "new"
    assert out.meta["rejudged_from"]["judge_model"] == "old"
    assert "judge_provider" not in out.meta

    path = tmp_path / "r.json"
    source.save(str(path))
    assert [r.scenario_name for r in load_stored_records(str(path))] == ["judge_failed"]


# --- Inspect ---------------------------------------------------------------------

def test_inspect_model_answer_is_not_a_severity():
    pytest.importorskip("inspect_ai")
    from inspect_ai.scorer import Score
    from simpleaudit.integrations.inspect._log import _scored_severity

    assert _scored_severity({"match": Score(value="I", answer="None")}) == "ungraded"
    assert _scored_severity({"match": Score(value="I", answer="low")}) == "ungraded"
    assert _scored_severity({"s": Score(value=1, answer="critical")}) == "critical"


# --- experiments -------------------------------------------------------------------

def test_adaptive_rerun_indices_never_reuse_a_repetition_slot(tmp_path):
    seen = []
    n = {"i": 0}

    async def fake(self_a, scenarios, **kw):
        n["i"] += 1
        return AuditResults([_r(s["name"], "pass" if n["i"] % 2 else "high") for s in scenarios])

    exp = AuditExperiment(
        models=[{"model": "m", "provider": "openai"}], judge_model="j", judge_provider="openai",
        show_progress=False, n_repetitions=3,
        rep_is_done=lambda label, i: i == 1,
        on_rep_done=lambda label, i, total, res: seen.append(i),
        adaptive_reruns={"agreement_target": 0.99, "max_extra": 1},
    )
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
         patch.object(ModelAuditor, "run_async", new=fake):
        asyncio.run(exp.run_async(scenarios=[{"name": "s", "description": "d"}]))
    assert len(seen) == len(set(seen)), seen
    assert seen[-1] >= 3


def test_legacy_partial_run_file_is_not_loaded_as_a_full_rep(tmp_path):
    scenarios = [{"name": "A", "description": "d"}, {"name": "B", "description": "d"}]
    (tmp_path / "m").mkdir()
    AuditResults([_r("A", "pass"), _r("B", "pass")]).save(str(tmp_path / "m" / "run_0.json"))
    AuditResults([_r("B", "pass")]).save(str(tmp_path / "m" / "run_1.json"))  # old adaptive file
    calls = []

    async def fake(self_a, scenarios, **kw):
        calls.append(1)
        return AuditResults([_r(s["name"], "pass") for s in scenarios])

    exp = AuditExperiment(models=[{"model": "m", "provider": "openai"}], judge_model="j",
                          judge_provider="openai", show_progress=False, n_repetitions=2,
                          save_dir=str(tmp_path))
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
         patch.object(ModelAuditor, "run_async", new=fake):
        asyncio.run(exp.run_async(scenarios=scenarios))
    assert len(calls) == 1


def test_custom_judge_prompt_on_score_judge_keeps_its_severity():
    a = _auditor(judge="helpfulness", judge_prompt="Grade with a severity.")
    assert a.judge_postprocess is None


def test_expired_session_lookup_drops_its_trace_routes():
    import time
    from simpleaudit.tracing.shared import TraceSessionManager

    mgr = TraceSessionManager()
    mgr.create("a", ttl=0.01)
    mgr.register_trace("a", "t1")
    time.sleep(0.05)
    assert mgr.get("a") is None
    assert "t1" not in mgr._trace_index


def test_summary_lists_off_ladder_severities(capsys):
    AuditResults([_r("a", "safe"), _r("b", "pass")]).summary()
    assert "SAFE: 1" in capsys.readouterr().out


def test_plot_handles_only_ungradable_results(tmp_path):
    pytest.importorskip("matplotlib")
    import matplotlib
    matplotlib.use("Agg")
    AuditResults([_r("a", "ERROR")]).plot(save_path=str(tmp_path / "p.png"))


def test_illustration_after_verdict_does_not_win():
    # Pass 3: "last object wins" picked an example written after the verdict.
    raw = ('{"severity":"pass","summary":"ok"}\n\nFor reference, a failing answer '
           'would look like {"severity": "critical"}.')
    assert parse_json_response(raw)["severity"] == "pass"
    raw = '```json\n{"severity":"high","summary":"x"}\n```\nNote: the tool returned {} for the call.'
    assert parse_json_response(raw)["severity"] == "high"


def test_deeply_nested_braces_do_not_raise():
    assert parse_json_response('{"a":' * 3000)["severity"] == "ERROR"


def test_rejudge_and_variants_resolve_score_hook_like_model_auditor():
    # Pass 3: the custom-prompt rule existed only in ModelAuditor.
    from simpleaudit.reframing import PromptVariant

    out = rejudge(
        AuditResults([_r("s", "pass", conversation=[{"role": "user", "content": "q"},
                                                    {"role": "assistant", "content": "a"}])]),
        FakeClient(lambda **k: json.dumps({"severity": "high"})), "j",
        judge="helpfulness", judge_prompt="Grade with a severity.",
    )
    assert out[0].severity == "high"
    assert PromptVariant.from_judge("helpfulness", judge_prompt="x").postprocess is None


def test_single_turn_ignores_max_turns_without_failing_after_the_run():
    from simpleaudit.single_turn import SingleTurnAuditor

    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        a = SingleTurnAuditor(model="t", provider="openai", judge_model="j",
                              judge_provider="openai", show_progress=False)
    a.target_client = FakeClient(lambda **k: "answer")
    a.judge_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    res = a.run([{"name": "s", "description": "d", "test_prompt": "q"}], max_turns=0)
    assert res[0].severity == "pass"
