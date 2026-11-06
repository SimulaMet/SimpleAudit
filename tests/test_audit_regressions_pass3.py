"""Regression tests for defects found in audit pass 3 (see AUDIT.md, "Pass 3")."""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.tracing.context import TraceCorrelation
from simpleaudit.tracing.provider import TraceProvider, audit_with_tracing
from simpleaudit.tracing.selection import select_spans, summarize_for_judge
from tests.fakes import FakeClient


def _auditor(**kw):
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        a = ModelAuditor(model="t", provider="openai", judge_model="j",
                         judge_provider="openai", show_progress=False, max_retries=0, **kw)
    a.judge_client = a.auditor_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    return a


class _RecordingProvider(TraceProvider):
    """Returns one span per trace id; remembers which trace each scenario used."""

    def fetch(self, trace_id):
        return [{"span_id": f"s-{trace_id}", "trace_id": trace_id, "kind": "RETRIEVER",
                 "attributes": {"doc": f"doc-for-{trace_id}"}}]


def test_each_result_gets_only_its_own_scenarios_evidence():
    # Pass 3 (high): every result received every scenario's spans (one shared list).
    a = _auditor()
    a.target_client = FakeClient(lambda **k: "answer")
    scenarios = [{"name": n, "description": "d", "test_prompt": "q"} for n in ("A", "B", "C")]
    results = asyncio.run(audit_with_tracing(a, scenarios, provider=_RecordingProvider(), max_turns=1))
    evidence = [r.judgment["evidence_spans"] for r in results]
    assert [len(e) for e in evidence] == [1, 1, 1]
    assert len({e[0]["trace_id"] for e in evidence}) == 3
    assert evidence[0] is not evidence[1]


def test_failed_turn_keeps_its_trace():
    # Pass 3: the trace was recorded only after target.send returned.
    a = _auditor()

    async def boom(**kw):
        raise RuntimeError("target down")

    a.target_client = MagicMock(acompletion=boom)
    corr = TraceCorrelation(audit_run_id="r")
    asyncio.run(a.run_async([{"name": "A", "description": "d", "test_prompt": "q"}],
                            max_turns=1, trace_correlation=corr))
    assert corr.trace_ids_for_scenario("A")


def test_selection_accepts_raw_otlp_spans():
    # Pass 3: int kind / dict status raised AttributeError after the whole audit.
    raw = [{"span_id": "a", "trace_id": "t", "kind": 2, "status": {"code": 2}, "attributes": {}}]
    assert select_spans(raw, prefer_failing_spans=True).selected


def test_secret_span_attributes_are_redacted_for_the_judge():
    sel = select_spans([{"span_id": "a", "trace_id": "t", "kind": "LLM", "attributes": {
        "http.request.header.authorization": "Bearer sk-SECRET", "llm.output": "ok"}}])
    text = summarize_for_judge(sel)
    assert "sk-SECRET" not in text and "ok" in text


def test_judge_evidence_is_capped():
    seen = []

    def judge(**kw):
        seen.append(json.dumps(kw["messages"]))
        return json.dumps({"severity": "pass"})

    spans = [{"span_id": f"s{i}", "trace_id": "t", "kind": "TOOL",
              "attributes": {"out": "x" * 1500}} for i in range(300)]
    asyncio.run(ModelAuditor._judge_conversation_async(
        FakeClient(judge), "m", "d",
        [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
        evidence_spans=spans))
    assert len(seen[0]) < 100_000


def test_repeated_sync_runs_rebuild_owned_clients():
    # Pass 3: the second run() reused clients bound to the first, closed loop.
    created = []

    def create(self, **kw):
        client = MagicMock()
        created.append(client)
        return client

    with patch.object(ModelAuditor, "_create_anyllm_client", new=create):
        a = ModelAuditor(model="t", provider="openai", judge_model="j",
                         judge_provider="openai", show_progress=False)
        first_target = a.target_client
        a._sync_runs = 1  # as after one completed run()
        a._refresh_owned_clients()
    assert a.target_client is not first_target
    assert a.auditor_client is a.judge_client  # sharing preserved


def test_reassigned_clients_are_not_rebuilt():
    a = _auditor()  # judge/auditor clients reassigned after construction
    reassigned = a.judge_client
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        a._refresh_owned_clients()
    assert a.judge_client is reassigned


def test_inspect_error_output_is_raised_not_graded():
    pytest.importorskip("inspect_ai")
    from types import SimpleNamespace
    from simpleaudit.integrations.inspect._client import InspectModelClient

    class Model:
        async def generate(self, *a, **k):
            return SimpleNamespace(completion="This model's maximum context length is 8192",
                                   stop_reason="model_length", error=None, usage=None)

    client = InspectModelClient(Model())
    with pytest.raises(RuntimeError, match="model_length"):
        asyncio.run(client.acompletion(messages=[{"role": "user", "content": "hi"}]))
    assert client.error is not None


def test_experiment_config_json_has_no_secrets(tmp_path):
    from simpleaudit.experiment import AuditExperiment

    exp = AuditExperiment(
        models=[{"model": "m", "provider": "openai", "base_url": "https://u:LEAK3@h/v1",
                 "kwargs": {"default_headers": {"Authorization": "Bearer sk-LEAK"}}}],
        judge_model="j", judge_provider="openai", show_progress=False, save_dir=str(tmp_path),
    )
    fp = exp._config_fingerprint(exp._merge_common(exp.models[0]), "safety", None, "English")
    text = json.dumps(fp)
    assert "LEAK" not in text


@pytest.mark.parametrize("judgment, expected", [
    ({"harmful": True, "category": "violence", "severity": "none"}, "ERROR"),
    ({"harmful": "false", "severity": "high"}, "ERROR"),
    ({"harmful": True, "severity": "high"}, "high"),
    ({"harmful": False, "severity": "none"}, "pass"),
])
def test_harm_judge_rejects_contradictions(judgment, expected):
    from simpleaudit.judges.harm import postprocess_harm
    assert postprocess_harm(judgment)["severity"] == expected


# --- pass 4 -----------------------------------------------------------------

def _single(judge, judge_reply, target_reply="Sure, here is how.", **kw):
    from simpleaudit.single_turn import SingleTurnAuditor

    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        a = SingleTurnAuditor(model="t", provider="openai", judge_model="j",
                              judge_provider="openai", show_progress=False, max_retries=0,
                              judge=judge, **kw)
    a.target_client = FakeClient(lambda **k: target_reply)
    a.judge_client = a.auditor_client = FakeClient(lambda **k: judge_reply)
    return a


@pytest.mark.parametrize("reply", ['{"severity":"pass"}', '{"score":10}',
                                   '{"severity":"pass","checklist":"all met"}'])
def test_checklist_reply_without_observations_is_error(reply):
    a = _single("checklist", reply)
    sc = [{"name": "s", "description": "d", "test_prompt": "q",
           "expected_behavior": ["Refuses", "Explains"], "severity": "high"}]
    assert a.run(sc)[0].severity == "ERROR"


def test_checklist_custom_prompt_still_passes_plain_verdicts_through():
    a = _single("checklist", '{"severity":"low"}', judge_prompt="Grade with a severity.")
    sc = [{"name": "s", "description": "d", "test_prompt": "q", "expected_behavior": ["x"]}]
    assert a.run(sc)[0].severity == "low"


@pytest.mark.parametrize("reply", ['{"severity":"pass"}', '{"score":10}',
                                   '{"asserted_spans":null}', '{"stance":null}',
                                   '{"asserted_spans":[1,null,{"a":1}]}'])
def test_groundedness_reply_without_observations_is_error(reply):
    from tests.test_groundedness_judge import DOCS

    a = _single("groundedness", reply, target_reply="answer")
    sc = [{"name": "s", "description": "d", "test_prompt": "q", "documents": DOCS,
           "as_of": "2026-09-01"}]
    assert a.run(sc)[0].severity == "ERROR"


def test_single_turn_grades_decision_with_choice_match():
    from tests.test_choice_match_judge import DECISION

    accepted = DECISION["accepted"][0]
    other = next(k for k in DECISION["criteria"] if k not in DECISION["accepted"])
    for answer, expected in ((accepted, "pass"), (other, "high")):
        a = _single("choice_match", "{}", target_reply=answer)
        sc = [{"name": "s", "description": "d", "decision": DECISION, "severity": "high"}]
        assert a.run(sc)[0].severity == expected


def test_rejudge_coerces_judge_fields():
    from simpleaudit.reframing import rejudge
    from simpleaudit.results import AuditResult, AuditResults

    src = AuditResults([AuditResult(
        scenario_name="s", scenario_description="d",
        conversation=[{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
        severity="pass", issues_found=[], positive_behaviors=[], summary="", recommendations=[])])
    out = rejudge(src, FakeClient(lambda **k: json.dumps(
        {"severity": "high", "issues_found": "bad advice", "summary": None})), "j", judge="safety")
    assert out[0].issues_found == ["bad advice"] and out[0].summary == ""


@pytest.mark.parametrize("judgment", [
    {"score": 10, "abstained": True, "appropriate": False},
    {"score": 9, "appropriate": "no"},
])
def test_abstention_contradiction_is_error(judgment):
    from simpleaudit.judges.abstention import postprocess_abstention
    from simpleaudit.utils import judgment_severity
    assert judgment_severity(postprocess_abstention(judgment)) == "ERROR"


@pytest.mark.parametrize("value", [[], {}, False])
async def test_empty_container_replies_are_empty(value):
    from simpleaudit.targets import CallableTarget
    assert (await CallableTarget(lambda **k: value).send(user="hi")).content == ""


@pytest.mark.parametrize("judge", ["safety", "helsedir_sexhealth_no", "helsedir_sexhealth_no_rag", "harm"])
def test_severity_judges_do_not_grade_a_bare_score(judge):
    from simpleaudit.judges import get_judge
    from simpleaudit.judges.compose import inherited_postprocess
    from simpleaudit.utils import judgment_severity
    assert judgment_severity(inherited_postprocess(get_judge(judge), False)({"score": 9})) == "ERROR"


def test_url_secrets_are_redacted_from_stored_errors():
    from simpleaudit.utils import describe_exception
    msg = describe_exception(Exception("401 for url 'http://u:pw@h/chat?api_key=SECRET&x=1'"))
    assert "pw" not in msg and "SECRET" not in msg and "x=1" in msg


def test_identityless_credential_cannot_claim_a_target():
    from simpleaudit.tracing.auth import TARGET_ID_ATTR, OTLPAuth, tag_spans

    with pytest.raises(ValueError):
        OTLPAuth("bearer").add_bearer("t", target_id="")
    spans = [{"attributes": {TARGET_ID_ATTR: "victim"}}]
    tag_spans(spans, None)
    assert TARGET_ID_ATTR not in spans[0]["attributes"]


def test_empty_span_store_passed_in_is_used():
    from simpleaudit.tracing.otlp import EphemeralOTLPReceiver
    from simpleaudit.tracing.store import SpanStore
    store = SpanStore()
    assert EphemeralOTLPReceiver(store=store).store is store


def test_dotdot_model_label_is_rejected():
    from simpleaudit.experiment import AuditExperiment
    with pytest.raises(ValueError):
        AuditExperiment._sanitize_label("..")


def test_injected_judge_shared_by_auditor_is_not_owned():
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        a = ModelAuditor(model="t", provider="openai", judge_model="j", judge_provider="openai",
                         judge_client=FakeClient(lambda **k: "{}"), show_progress=False)
    assert "auditor" not in a._owned_clients and "judge" not in a._owned_clients


def test_subclass_skip_target_client_attribute_is_honoured():
    class NoTarget(ModelAuditor):
        _skip_target_client = True

    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        a = NoTarget(model="t", provider="openai", judge_model="j", judge_provider="openai",
                     show_progress=False)
    assert type(a.target_client).__name__ == "_NoopTargetClient"


def test_verdict_beats_larger_quoted_record():
    from simpleaudit.utils import parse_json_response
    raw = ('Leaked: {"name":"a","ssn":"1","dob":"x","addr":"y","phone":"z","email":"e"}.\n'
           '```json\n{"severity":"critical","issues_found":["pii"],"summary":"s"}\n```')
    assert parse_json_response(raw)["severity"] == "critical"


def test_target_bound_session_rejects_other_targets_spans():
    from simpleaudit.tracing.auth import TARGET_ID_ATTR
    from simpleaudit.tracing.shared import TraceSessionManager

    mgr = TraceSessionManager()
    session = mgr.create("a", target_id="owui", ttl=60)
    mgr.register_trace("a", "t1")
    mgr.route_spans([
        {"span_id": "x", "trace_id": "t1", "attributes": {TARGET_ID_ATTR: "intruder"}},
        {"span_id": "y", "trace_id": "t1", "attributes": {TARGET_ID_ATTR: "owui"}},
        {"span_id": "z", "trace_id": "t1", "attributes": {}},
    ])
    assert sorted(s["span_id"] for s in session.spans_for_trace("t1")) == ["y", "z"]


# --- pass 5 -----------------------------------------------------------------

def test_harm_with_custom_score_prompt_keeps_the_score():
    from simpleaudit.judges import get_judge
    from simpleaudit.judges.compose import inherited_postprocess
    from simpleaudit.utils import judgment_severity

    hook = inherited_postprocess(get_judge("harm"), True)
    assert judgment_severity(hook({"score": 9, "feedback": "x"})) == "pass"
    assert judgment_severity(hook({"harmful": True, "severity": "none"})) == "ERROR"


@pytest.mark.parametrize("name", ["safety", "helpfulness", "factuality",
                                  "helsedir_sexhealth_no", "helsedir_sexhealth_no_rag"])
def test_generic_format_judges_keep_no_config_hook(name):
    # Pass 5: Studio reads config["postprocess"] to decide that a judge grades
    # like its generic format; the format hooks are applied at resolution.
    from simpleaudit.judges import get_judge
    from simpleaudit.judges.compose import inherited_postprocess, is_format_only
    config = get_judge(name)
    assert config.get("postprocess") is None
    resolved = inherited_postprocess(config, False)
    assert is_format_only(resolved) or resolved.__name__.startswith("score_postprocess+")


def test_experiment_does_not_resample_target_after_judge_failure():
    # Pass 5: a judge-phase ERROR was re-run from scratch, so a harmful
    # transcript the judge refused was replaced by a fresh, gradable sample.
    from simpleaudit.experiment import _rerunnable_error
    from simpleaudit.results import AuditResult

    def r(phase):
        return AuditResult(scenario_name="s", scenario_description="", conversation=[],
                           severity="ERROR", issues_found=[], positive_behaviors=[],
                           summary="", recommendations=[],
                           judgment={"error": "x", "error_phase": phase})
    assert _rerunnable_error(r("conversation")) and _rerunnable_error(r(None))
    assert not _rerunnable_error(r("judge"))


def test_single_turn_groundedness_without_documents_is_error():
    a = _single("groundedness", '{"asserted_spans":["x"],"abstained":false,"severity":"pass"}')
    assert a.run([{"name": "s", "description": "d", "test_prompt": "q"}])[0].severity == "ERROR"


def test_rejudge_restores_grading_metadata(tmp_path):
    from simpleaudit.reframing import rejudge

    a = _auditor(judge="binary_abstention")
    a.target_client = FakeClient(lambda **k: "Sure, here it is.")
    a.judge_client = FakeClient(lambda **k: json.dumps({"abstained": False, "reasoning": "r"}))
    sc = [{"name": "s", "description": "d", "test_prompt": "q", "severity": "critical",
           "metadata": {"expected_outcome": "refuse"}}]
    live = a.run(sc, max_turns=1)
    assert live[0].severity == "critical"
    path = tmp_path / "r.json"
    live.save(str(path))
    # Before: expected_outcome was lost and the re-grade came back ungraded.
    again = rejudge(str(path), FakeClient(lambda **k: json.dumps({"abstained": False, "reasoning": "r"})),
                    "j", judge="binary_abstention", scenario_severities={"s": "critical"})
    assert again[0].severity == "critical"


@pytest.mark.parametrize("name, judgment", [
    ("helpfulness", {"score": 9.5, "relevance": 1, "accuracy": 1, "clarity": 1, "completeness": 1}),
    ("factuality", {"score": 10, "verdict": "inaccurate", "factual_errors": ["X"]}),
])
def test_self_contradicting_score_judges_do_not_pass(name, judgment):
    from simpleaudit.judges import get_judge
    from simpleaudit.judges.compose import inherited_postprocess
    from simpleaudit.utils import judgment_severity
    assert judgment_severity(inherited_postprocess(get_judge(name), False)(judgment)) != "pass"


@pytest.mark.parametrize("judgment", [{"score": 2, "appropriate": True}, {"score": 10, "appropriate": 0}])
def test_abstention_contradictions_both_directions(judgment):
    from simpleaudit.judges.abstention import postprocess_abstention
    from simpleaudit.utils import judgment_severity
    assert judgment_severity(postprocess_abstention(judgment)) == "ERROR"
