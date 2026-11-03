"""Regression tests: provider failures must not be scored, and must say why.

Covers the failure seen with OpenRouter in the AuditExperiment pilot:
OpenRouter answers some upstream failures with HTTP 200 and a body holding
only {"id": ..., "error": {...}}; any-llm then raises
``TypeError: int() argument must be ... not 'NoneType'`` while converting it.
"""
import asyncio
import json
import types

import pytest

import simpleaudit.model_auditor as MA
from simpleaudit import ModelAuditor
from simpleaudit.experiment import AuditExperiment
from simpleaudit.provider_errors import (
    BLOCKED_REPLY,
    EmptyResponseError,
    ProviderResponseError,
    blocked_reply_text,
    coerce_judgment_fields,
    extract_completion,
)
from simpleaudit.repeated_results import _build_model_aggregate, _build_stability_report
from simpleaudit.results import AuditResult, AuditResults

NS = types.SimpleNamespace
VERDICT = {"severity": "pass", "issues_found": [], "positive_behaviors": [], "summary": "ok",
           "recommendations": []}


def completion(content, finish="stop"):
    return NS(choices=[NS(message=NS(content=content), finish_reason=finish)],
              usage=NS(prompt_tokens=1, completion_tokens=1))


ANTHROPIC_BLOCK = "Response blocked by Anthropic content filtering."


def blocked():
    """What any-llm returns when Anthropic's safety filter stops a reply."""
    return NS(choices=[NS(message=NS(content="", refusal=ANTHROPIC_BLOCK),
                          finish_reason="content_filter")],
              usage=NS(prompt_tokens=1, completion_tokens=0))


class FakeClient:
    """One client for all three roles; `target` is a list of behaviours consumed per call."""

    def __init__(self, target=None, judge=None):
        self.target = list(target or [])
        self.judge = judge
        self.calls = {"target": 0, "auditor": 0, "judge": 0}

    async def acompletion(self, **kw):
        system = next((m["content"] for m in kw["messages"] if m["role"] == "system"), "") or ""
        if "red-team auditor" in str(system):
            self.calls["auditor"] += 1
            return completion("Probe?")
        if "response_format" in kw or "safety evaluator" in str(system):
            self.calls["judge"] += 1
            if self.judge == "blocked":
                return blocked()
            return completion(json.dumps(self.judge or VERDICT))
        self.calls["target"] += 1
        step = self.target.pop(0) if self.target else "ok"
        if step == "ok":
            return completion("A normal answer.")
        if step == "openrouter_200_error":
            # what any-llm raises for OpenRouter's {"id", "error"} body
            raise TypeError("int() argument must be a string, a bytes-like object or a real "
                            "number, not 'NoneType'")
        if step == "empty":
            return completion("")
        if step == "length":
            return completion("", finish="length")
        if step == "list_content":
            return completion([{"type": "thinking", "thinking": "x"}, {"type": "text", "text": "Hi"}])
        if step == "blocked":
            return blocked()
        if step == "filtered":
            return completion(None, finish="content_filter")
        if step == "choices_none":
            return NS(choices=None, usage=None)
        raise AssertionError(step)


@pytest.fixture
def fake(monkeypatch):
    holder = {}

    def install(client):
        holder["client"] = client
        monkeypatch.setattr(MA.ModelAuditor, "_create_anyllm_client",
                            staticmethod(lambda *a, **k: client))
        return client

    return install


def audit(**kw):
    defaults = dict(model="m", provider="openrouter", judge_model="j", judge_provider="openrouter",
                    max_turns=2, show_progress=False, max_retries=2, retry_backoff=0.001)
    defaults.update(kw)
    return ModelAuditor(**defaults)


SCEN = [{"name": "s1", "description": "d"}]


# --- the TypeError itself -------------------------------------------------

def test_transient_openrouter_error_is_retried(fake):
    client = fake(FakeClient(target=["openrouter_200_error", "ok", "ok"]))
    res = audit().run(SCEN)
    assert res[0].severity == "pass"
    assert client.calls["target"] == 3


def test_persistent_openrouter_error_is_error_with_readable_cause(fake):
    fake(FakeClient(target=["openrouter_200_error"] * 10))
    res = audit().run(SCEN)
    r = res[0]
    assert r.severity == "ERROR"
    assert r.judgment["error"].startswith("ProviderResponseError")
    assert "HTTP 200" in r.judgment["error"] and "TypeError" in r.judgment["error"]
    assert "Traceback" in r.judgment["traceback"]
    assert res.score is None  # nothing graded -> no score, not 0


@pytest.mark.parametrize("step", ["list_content", "choices_none"])
def test_odd_response_shapes_do_not_raise_typeerror(fake, step):
    fake(FakeClient(target=[step, "ok", "ok", "ok"]))
    r = audit().run(SCEN)[0]
    assert "TypeError" not in str(r.judgment.get("error"))
    assert r.severity == "pass"


# --- empty responses ------------------------------------------------------

def test_empty_reply_is_retried_then_error_not_judged(fake):
    client = fake(FakeClient(target=["empty"] * 10))
    r = audit().run(SCEN)[0]
    assert r.severity == "ERROR"
    assert r.judgment["error"].startswith("EmptyResponseError")
    assert client.calls["judge"] == 0  # an empty transcript is never scored


def test_empty_reply_recovers_on_retry(fake):
    fake(FakeClient(target=["empty", "ok", "ok"]))
    r = audit().run(SCEN)[0]
    assert r.severity == "pass"
    assert all(m["content"] for m in r.conversation)


def test_length_finish_is_not_retried(fake):
    client = fake(FakeClient(target=["length"] * 10))
    r = audit().run(SCEN)[0]
    assert r.severity == "ERROR" and "max_tokens" in r.judgment["error"]
    assert client.calls["target"] == 1


def test_empty_response_judge_mode_keeps_old_behaviour(fake):
    client = fake(FakeClient(target=["empty", "empty"]))
    r = audit(empty_response="judge").run(SCEN)[0]
    assert r.severity == "pass" and client.calls["judge"] == 1


# --- provider safety filter on the target --------------------------------

def test_target_blocked_by_safety_filter_is_judged_as_refusal(fake):
    client = fake(FakeClient(target=["blocked", "blocked"]))
    r = audit().run(SCEN)[0]
    assert r.severity == "pass" and client.calls["judge"] == 1
    assert client.calls["target"] == 2  # one call per turn, not retried
    replies = [m["content"] for m in r.conversation if m["role"] == "assistant"]
    assert replies == [ANTHROPIC_BLOCK, ANTHROPIC_BLOCK]


def test_content_filter_without_refusal_text_is_judged(fake):
    fake(FakeClient(target=["filtered", "filtered"]))
    r = audit().run(SCEN)[0]
    assert r.severity == "pass"
    assert r.conversation[1]["content"] == BLOCKED_REPLY


def test_blocked_judge_is_still_error(fake):
    client = fake(FakeClient(judge="blocked"))
    r = audit().run(SCEN)[0]
    assert r.severity == "ERROR" and "content_filter" in r.judgment["error"]
    assert client.calls["judge"] == 1  # a safety stop is not retried


def test_blocked_reply_text():
    assert blocked_reply_text(blocked()) == ANTHROPIC_BLOCK
    assert blocked_reply_text(completion("", finish="content_filter")) == BLOCKED_REPLY
    assert blocked_reply_text(completion("", finish="stop")) is None
    assert blocked_reply_text({"choices": [{"message": {"content": None, "refusal": "No."}}]}) == "No."


# --- judge output that used to crash after a valid verdict ---------------

def test_null_summary_from_judge_keeps_the_verdict(fake):
    fake(FakeClient(judge={"severity": "low", "issues_found": None, "summary": None,
                           "recommendations": None}))
    res = audit().run(SCEN)
    assert res[0].severity == "low"
    assert res.all_issues == [] and res.to_dict()["summary"]["score"] == 75.0


def test_coerce_string_issue_list():
    assert coerce_judgment_fields({"issues_found": "one issue"})["issues_found"] == ["one issue"]


def test_extract_completion_error_body():
    with pytest.raises(ProviderResponseError, match="HTTP 200"):
        extract_completion({"id": "gen-1", "error": {"message": "Upstream", "code": 502}})
    with pytest.raises(ProviderResponseError):
        extract_completion({"choices": []})


# --- scoring --------------------------------------------------------------

def _r(name, sev):
    return AuditResult(scenario_name=name, scenario_description="", conversation=[], severity=sev,
                       issues_found=[], positive_behaviors=[], summary="", recommendations=[])


def test_errors_are_not_failures_and_not_scored():
    res = AuditResults([_r("a", "pass"), _r("b", "critical"), _r("c", "ERROR")])
    assert res.score == 50.0 and res.errors == 1 and res.failed == 1 and res.n_scored == 2


def test_all_error_run_does_not_drag_mean_down():
    good = AuditResults([_r("a", "pass"), _r("b", "pass")])
    outage = AuditResults([_r("a", "ERROR"), _r("b", "ERROR")])
    rep = _build_stability_report("m", [good, good, outage])
    assert rep.mean_score == 100.0 and rep.std_score == 0.0
    assert rep.n_runs_scored == 2 and rep.n_errors == 2 and rep.pooled_score == 100.0
    agg = _build_model_aggregate([good, good, outage])
    assert agg["score"]["mean"] == 100.0 and agg["score"]["n_runs_scored"] == 2


def test_per_scenario_stats_ignore_error_runs():
    runs = [AuditResults([_r("a", "pass")]), AuditResults([_r("a", "ERROR")]),
            AuditResults([_r("a", "pass")])]
    st = _build_stability_report("m", runs).per_scenario["a"]
    assert st.pass_rate == 1.0 and st.most_common_severity == "pass"
    assert st.agreement_rate == 1.0 and st.n_errors == 1 and st.n_observations == 2


# --- experiment: only errored scenarios are re-run -------------------------

def test_experiment_reruns_only_errored_scenarios(fake, tmp_path):
    class Flaky(FakeClient):
        async def acompletion(self, **kw):
            text = json.dumps(kw["messages"])
            system = next((m["content"] for m in kw["messages"] if m["role"] == "system"), "") or ""
            is_target = "red-team auditor" not in str(system) and "response_format" not in kw
            if is_target and "BREAK" in text and not getattr(self, "broke", False):
                self.broke = True
                self.calls["target"] += 1
                raise TypeError("int() argument must be ... not 'NoneType'")
            return await super().acompletion(**kw)

    client = fake(Flaky())
    scenarios = [{"name": "ok1", "description": "d", "test_prompt": "hello"},
                 {"name": "flaky", "description": "d", "test_prompt": "BREAK"}]
    exp = AuditExperiment(models=[{"model": "m", "provider": "openrouter", "max_retries": 0}],
                          judge_model="j", judge_provider="openrouter", show_progress=False)
    out = exp.run(scenarios, max_turns=1)
    runs = out._runs["m"]
    assert [r.severity for r in runs[0]] == ["pass", "pass"]
    # 1 broken + 1 retry for "flaky", 1 for "ok1" -> 3 target calls, not 4
    assert client.calls["target"] == 3
