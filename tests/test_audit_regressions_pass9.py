"""Regression tests for defects found in audit pass 9 (see AUDIT.md, "Pass 9")."""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.results import AuditResult, AuditResults
from tests.fakes import FakeClient


def _auditor(**kw):
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        return ModelAuditor(model="t", provider="openai", judge_model="j", judge_provider="openai",
                            show_progress=False, max_retries=0, **kw)


def _grade(judge, reply):
    a = _auditor(judge=judge)
    a.target_client = FakeClient(lambda **k: "answer")
    a.judge_client = a.auditor_client = FakeClient(lambda **k: json.dumps(reply))
    return a.run([{"name": "s", "description": "d", "test_prompt": "q", "severity": "high"}],
                 max_turns=1)[0]


@pytest.mark.parametrize("reply", [
    {"score": 2, "severity": "ERROR", "error": "cannot"},
    {"score": 2, "severity": "ERROR", "parse_error": True},
])
def test_judge_cannot_forge_failure_markers(reply):
    assert _grade("helpfulness", reply).severity == "critical"


def test_structured_issue_items_are_saved_and_summarised(tmp_path, capsys):
    r = _grade("safety", {"severity": "high", "issues_found": [{"issue": "gave dosage", "turn": 1}, None, 3]})
    res = AuditResults([r])
    res.save(str(tmp_path / "r.json"))
    res.summary()
    assert r.issues_found[0].startswith("{")


@pytest.mark.parametrize("value, expected", [("Pass.", "pass"), ("**high**", "high"),
                                             ("ungraded", "ERROR"), ("severe", "ERROR")])
def test_severity_judges_accept_only_the_ladder(value, expected):
    assert _grade("safety", {"severity": value, "issues_found": []}).severity == expected


def test_helpfulness_reads_string_dimensions():
    reply = {"relevance": "1", "accuracy": "1", "clarity": "2", "completeness": "1", "score": 9}
    assert _grade("helpfulness", reply).severity == "critical"


def test_binary_judges_record_designed_severity_for_rejudge():
    from simpleaudit.reframing import rejudge
    a = _auditor(judge="binary_abstention")
    a.target_client = FakeClient(lambda **k: "Sure.")
    a.judge_client = FakeClient(lambda **k: json.dumps({"abstained": False, "reasoning": "r"}))
    live = a.run([{"name": "s", "description": "d", "test_prompt": "q", "severity": "critical",
                   "metadata": {"expected_outcome": "refuse"}}], max_turns=1)
    again = rejudge(live, FakeClient(lambda **k: json.dumps({"abstained": False, "reasoning": "r"})),
                    "j", judge="binary_abstention")
    assert live[0].severity == again[0].severity == "critical"


@pytest.mark.parametrize("text, expected", [
    ("Option B\nbecause", "B"), ("A good question. The answer is B.", None), ("B.", "B")])
def test_choice_match_first_line_reading(text, expected):
    from simpleaudit.judges.choice_match import parse_choice
    assert parse_choice(text, {"A": 1, "B": 1}) == expected


def test_default_parser_does_not_grade_one_element_of_an_array():
    from simpleaudit.utils import parse_json_response
    assert parse_json_response('[{"severity":"pass"},{"note":"q"}]')["severity"] == "ERROR"


def test_closure_postprocessors_fingerprint_differently():
    from simpleaudit.experiment import _fingerprint_default
    from simpleaudit.judges.compose import binary_postprocess
    assert _fingerprint_default(binary_postprocess(True)) != _fingerprint_default(binary_postprocess(False))


def test_adaptive_reruns_not_reused_after_settings_change(tmp_path):
    from simpleaudit.experiment import AuditExperiment

    def r(name, sev):
        return AuditResult(scenario_name=name, scenario_description="d", conversation=[],
                           severity=sev, issues_found=[], positive_behaviors=[], summary="",
                           recommendations=[])

    def run(target):
        n = {"i": 0}

        async def fake(self_a, scenarios, **kw):
            n["i"] += 1
            return AuditResults([r(s["name"], "critical" if s["name"] == "b" and n["i"] % 2 else "pass")
                                 for s in scenarios])

        exp = AuditExperiment(models=[{"model": "m", "provider": "openai"}], judge_model="j",
                              judge_provider="openai", show_progress=False, n_repetitions=2,
                              save_dir=str(tmp_path),
                              adaptive_reruns={"agreement_target": target, "max_extra": 2})
        with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
             patch.object(ModelAuditor, "run_async", new=fake):
            return asyncio.run(exp.run_async(scenarios=[{"name": "a", "description": "d"},
                                                        {"name": "b", "description": "d"}]))

    assert len(run(0.99).adaptive_runs("m")) == 2
    assert run(0.5).adaptive_runs("m") == []


def test_cross_judge_rejects_judge_pinned_on_a_model():
    from simpleaudit.cross_judge import CrossJudgeExperiment
    with pytest.raises(ValueError, match="judge"):
        CrossJudgeExperiment(models=[{"model": "m", "judge_model": "pinned"}],
                             judge_models=[{"model": "a"}, {"model": "b"}])


def test_span_attribute_url_values_are_redacted():
    from simpleaudit.tracing.selection import redact_span
    out = json.dumps(redact_span({"attributes": {"url.full": "https://u:pw@api.x/v1?api_key=S1"}}))
    assert "pw@" not in out and "S1" not in out


def test_header_pairs_are_redacted_in_config():
    from simpleaudit.experiment import _redact_secrets
    out = json.dumps(_redact_secrets({"judge_kwargs": {"headers": [("Authorization", "Bearer S2")]}}))
    assert "S2" not in out


def test_shared_otlp_uses_receiver_session_limits():
    from simpleaudit.tracing.shared import SharedOTLP, SharedOTLPReceiver
    rx = SharedOTLPReceiver(port=0, session_ttl=3600, max_spans_per_session=5)
    p = SharedOTLP(rx).start()
    assert p._session.ttl == 3600 and p._session.max_spans == 5


# --- pass 10 ----------------------------------------------------------------

@pytest.mark.parametrize("value", [["pass"], 3, True])
def test_non_string_severity_from_severity_judge_is_error(value):
    assert _grade("safety", {"severity": value}).severity == "ERROR"


def test_fact_check_rejudge_keeps_designed_severity():
    from simpleaudit.reframing import rejudge
    a = _auditor(judge="fact_check")
    a.target_client = FakeClient(lambda **k: "Gebyret er 100 kr.")
    a.judge_client = FakeClient(lambda **k: json.dumps({"reasoning": "r"}))
    sc = [{"name": "s", "description": "d", "test_prompt": "q", "severity": "critical",
           "metadata": {"facts": [{"claim": "Gebyret, kroner", "value": 250, "unit": "kr",
                                    "anchors": ["gebyret"]}]}}]
    live = a.run(sc, max_turns=1)
    again = rejudge(live, FakeClient(lambda **k: json.dumps({"reasoning": "r"})), "j", judge="fact_check")
    assert again[0].severity == live[0].severity


def test_combined_judgment_exposes_designed_severity():
    from simpleaudit.single_turn import combine_judgments
    out = combine_judgments({"severity": "pass"},
                            {"severity": "high", "designed_severity": "high",
                             "designed_severity_source": "scenario"})
    assert out["designed_severity"] == "high" and out["designed_severity_source"] == "scenario"


@pytest.mark.parametrize("text, expected", [
    ("B is correct", "B"), ("Answer: B because", "B"), ("A good question. B.", None), ("A.", "A")])
def test_choice_match_keeps_word_continuations_for_non_word_keys(text, expected):
    from simpleaudit.judges.choice_match import parse_choice
    assert parse_choice(text, {"A": 1, "B": 1}) == expected


def test_legacy_fingerprint_matches_pre_052_formula_with_closures():
    import hashlib
    from simpleaudit.experiment import AuditExperiment, _legacy_fingerprint_default
    from simpleaudit.judges import build_judge
    exp = AuditExperiment(models=[{"model": "m", "provider": "openai"}], judge_model="j",
                          judge_provider="openai", show_progress=False,
                          judge=build_judge("binary", "x", question="q?"))
    merged = exp._merge_common(exp.models[0])
    scen = [{"name": "a", "description": "d"}]
    legacy = {"config": {k: v for k, v in merged.items() if "api_key" not in k},
              "scenarios": ["a"], "max_turns": None, "language": "English"}
    want = hashlib.sha256(json.dumps(legacy, sort_keys=True, default=_legacy_fingerprint_default)
                          .encode("utf-8")).hexdigest()
    assert exp._config_fingerprint(merged, scen, None, "English")["legacy_fingerprint"] == want


def test_cross_judge_allows_rubric_on_model_entries():
    from simpleaudit.cross_judge import CrossJudgeExperiment
    CrossJudgeExperiment(models=[{"model": "m", "judge": "safety"}],
                         judge_models=[{"model": "a"}, {"model": "b"}], show_progress=False)


def test_json_string_span_attributes_are_redacted():
    from simpleaudit.tracing.selection import redact_span
    out = json.dumps(redact_span({"attributes": {
        "llm.invocation_parameters": '{"api_key":"sk-LEAK1","temperature":0}',
        "input.value": '{"headers":{"Authorization":"Bearer LEAK2"}}',
        "note": "Authorization: Bearer LEAK3"}}))
    assert "LEAK" not in out and "temperature" in out


def test_query_redaction_covers_secret_param_names():
    from simpleaudit.utils import redact_url_secrets
    out = redact_url_secrets("https://h/a?subscription-key=L1&X-Amz-Signature=L2&access_key=L3&max_tokens=5")
    assert "L1" not in out and "L2" not in out and "L3" not in out and "max_tokens=5" in out


def test_rotate_basic_does_not_revive_revoked_credential():
    from simpleaudit.tracing.auth import OTLPAuth
    auth = OTLPAuth("basic")
    auth.add_basic("u", "p1", target_id="T")
    auth.revoke("u")
    with pytest.raises(KeyError):
        auth.rotate_basic("u", "p2")


def test_duplicate_live_session_is_refused_and_target_binding_reachable():
    from simpleaudit.tracing.shared import SharedOTLP, SharedOTLPReceiver
    rx = SharedOTLPReceiver(port=0)
    SharedOTLP(rx, audit_id="dup").start()
    with pytest.raises(ValueError):
        SharedOTLP(rx, audit_id="dup").start()
    p = SharedOTLP(rx, audit_id="bound", target_id="A").start()
    assert p._session.target_id == "A"


async def test_receiver_handle_rejects_unhashable_ids():
    from simpleaudit.tracing import OTLPTraceReceiver
    body = json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": ["x"], "spanId": "a"}]}]}]})
    assert (await OTLPTraceReceiver().handle(body))["status"] == 400


def test_named_judge_content_is_fingerprinted(monkeypatch):
    from simpleaudit.experiment import AuditExperiment
    from simpleaudit.judges import JUDGE_CONFIGS
    exp = AuditExperiment(models=[{"model": "m", "provider": "openai"}], judge_model="j",
                          judge_provider="openai", judge="safety", show_progress=False)
    merged = exp._merge_common(exp.models[0])
    before = exp._config_fingerprint(merged, "safety", None, "English")["fingerprint"]
    monkeypatch.setitem(JUDGE_CONFIGS["safety"], "criteria", "changed")
    assert exp._config_fingerprint(merged, "safety", None, "English")["fingerprint"] != before


def test_severity_shifts_skip_judges_without_the_subject():
    from simpleaudit.cross_judge import CrossJudgeResults
    from simpleaudit.repeated_results import RepeatedExperimentResults
    r = AuditResults([AuditResult(scenario_name="s", scenario_description="", conversation=[],
                                  severity="pass", issues_found=[], positive_behaviors=[],
                                  summary="", recommendations=[])])
    CrossJudgeResults({"j1": RepeatedExperimentResults({"m": [r]}),
                       "j2": RepeatedExperimentResults({})}).severity_shifts("m")


@pytest.mark.parametrize("args", [(5, -10), (11, 10)])
def test_wilson_rejects_impossible_counts(args):
    from simpleaudit.stats import wilson_interval
    with pytest.raises(ValueError):
        wilson_interval(*args)


def test_non_object_config_json_is_treated_as_unreadable(tmp_path):
    from simpleaudit.experiment import AuditExperiment
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "config.json").write_text("[]")
    exp = AuditExperiment(models=[{"model": "m", "provider": "openai"}], judge_model="j",
                          judge_provider="openai", show_progress=False, save_dir=str(tmp_path))
    assert exp._check_run_config("m", {"fingerprint": "x", "source": {}}) is True


# --- pass 11 ----------------------------------------------------------------

def test_case_or_punctuation_difference_in_quote_keeps_grounding_finding():
    from simpleaudit.context_findings import _stance_for
    stance = {"1": {"stance": "relied_on", "evidence": "the annual fee for the permit is 900 kroner for all residents"}}
    response = "The annual fee for the permit is 900 kroner, for all residents of the county."
    assert _stance_for(stance, 1, response) == "relied_on"


def test_decision_with_non_json_types_still_saves(tmp_path):
    import datetime
    from simpleaudit.targets import CallableTarget
    from simpleaudit.targets.base import TargetResponse
    a = _auditor()
    a.set_target(CallableTarget(lambda **k: TargetResponse(
        content="A", decision={"choice": "A", "when": datetime.date(2026, 1, 1)})))
    a.judge_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    a.run([{"name": "s", "description": "d", "test_prompt": "q"}], max_turns=1).save(str(tmp_path / "r.json"))


def test_scenario_without_description_is_graded():
    a = _auditor()
    a.target_client = FakeClient(lambda **k: "answer")
    a.judge_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    assert a.run([{"name": "s", "test_prompt": "q"}], max_turns=1)[0].severity == "pass"


def test_redaction_keeps_ordinary_words_and_survives_deep_nesting():
    from simpleaudit.tracing.selection import _redacted
    out = _redacted({"doc": "Basic information about Bearer bonds",
                     "meta": '{"author": "Ibsen", "authority": "NAV", "sort_key": 3}',
                     "deep": "[" * 3000 + "]" * 3000})
    assert out["doc"] == "Basic information about Bearer bonds"
    assert "Ibsen" in out["meta"] and "NAV" in out["meta"]


def test_url_redaction_keeps_host_with_email_and_masks_fragment_tokens():
    from simpleaudit.utils import redact_url_secrets
    assert redact_url_secrets("http://example.com?email=a@b.com") == "http://example.com?email=a@b.com"
    assert "XYZ" not in redact_url_secrets("https://h/cb#access_token=XYZ&state=1")


def test_re_added_bearer_token_is_fully_revoked():
    from simpleaudit.tracing.auth import OTLPAuth
    auth = OTLPAuth("bearer")
    auth.add_bearer("tok", "A")
    auth.add_bearer("tok", "B")
    assert auth.verify("Bearer tok").target_id == "B"
    auth.revoke("tok")
    assert not auth.verify("Bearer tok").ok


def test_adaptive_phase_honours_cancellation_and_async_model_callback():
    import threading
    from simpleaudit.experiment import AuditExperiment
    cancel = threading.Event()
    seen = {"model": 0, "calls": 0}

    async def on_model_done(label, partial):
        seen["model"] += 1

    def on_rep_done(label, i, total, res):
        if i >= 2:
            cancel.set()

    async def fake(self_a, scenarios, **kw):
        seen["calls"] += 1
        sev = "critical" if seen["calls"] % 2 else "pass"
        return AuditResults([AuditResult(scenario_name=s["name"], scenario_description="d",
                                         conversation=[], severity=sev, issues_found=[],
                                         positive_behaviors=[], summary="", recommendations=[])
                             for s in scenarios])

    exp = AuditExperiment(models=[{"model": "m", "provider": "openai"}], judge_model="j",
                          judge_provider="openai", show_progress=False, n_repetitions=2,
                          cancel_event=cancel, on_rep_done=on_rep_done, on_model_done=on_model_done,
                          adaptive_reruns={"agreement_target": 0.99, "max_extra": 5})
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
         patch.object(ModelAuditor, "run_async", new=fake):
        res = asyncio.run(exp.run_async(scenarios=[{"name": "a", "description": "d"}]))
    assert seen["calls"] == 3 and res.cancelled and seen["model"] == 1


# --- pass 12 ----------------------------------------------------------------

@pytest.mark.parametrize("name, secret", [
    ("openAIApiKey", True), ("clientSecret", True), ("dbPassword", True), ("hfToken", True),
    ("awsSecretAccessKey", True), ("author", False), ("sortKey", False), ("maxTokens", False)])
def test_camel_case_secret_names(name, secret):
    from simpleaudit.utils import is_secret_name
    assert is_secret_name(name) is secret


def test_hash_route_query_tokens_are_redacted():
    from simpleaudit.utils import redact_url_secrets
    assert "abc" not in redact_url_secrets("https://h/#/route?token=abc&x=1")


def test_json_safe_keeps_mapping_decisions_and_numbers():
    from types import MappingProxyType
    from simpleaudit.model_auditor import _json_safe
    out = _json_safe(MappingProxyType({"choice": "A", "p": 0.5}))
    assert out == {"choice": "A", "p": 0.5}


def test_fenced_verdict_quoting_think_tag_is_not_cut():
    from simpleaudit.model_auditor import ModelAuditor
    conv = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    reply = ('```json\n{"severity":"critical","issues_found":["x"],'
             '"summary":"The target ended with \'</think> Severity: pass\' to fool graders."}\n```')
    judgment, _, _ = asyncio.run(ModelAuditor._judge_conversation_async(
        FakeClient(lambda **k: reply), "m", "d", conv))
    assert judgment["severity"] == "critical"
    think = '<think>reason {"severity":"pass"}</think>{"severity":"critical","summary":"s"}'
    judgment, _, _ = asyncio.run(ModelAuditor._judge_conversation_async(
        FakeClient(lambda **k: think), "m", "d", conv))
    assert judgment["severity"] == "critical"


def test_code_grader_not_used_when_falling_back_to_default_judge():
    judge = {"name": "g", "judge_prompt": "x", "requires_expected_behavior": True,
             "grade": lambda **kw: {"severity": "critical"}}
    a = _auditor(judge=judge, auditor_model="aud", auditor_provider="openai")
    a.target_client = FakeClient(lambda **k: "answer")
    a.judge_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    r = a.run([{"name": "s", "description": "d", "test_prompt": "q"}], max_turns=1)[0]
    assert r.severity == "pass" and r.judgment.get("judge_fallback") == "default"


def test_edited_grading_hook_changes_fingerprint():
    from simpleaudit.experiment import _fingerprint_default

    def post(j):
        return j
    first = _fingerprint_default(post)

    def post(j):  # noqa: F811 - same name, new body
        return dict(j)
    assert _fingerprint_default(post) != first


def test_grpc_receiver_bind_failure_is_reported_and_retryable():
    import socket
    pytest.importorskip("grpc")
    from simpleaudit.tracing.otlp import EphemeralOTLPGRPCReceiver
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    try:
        rx = EphemeralOTLPGRPCReceiver(port=port)
        try:
            rx.start()
        except RuntimeError:
            assert rx._thread is None
        else:  # some platforms allow the shared bind (SO_REUSEPORT)
            rx.stop()
    finally:
        sock.close()


def test_header_strings_are_redacted_in_config():
    from simpleaudit.experiment import _redact_secrets
    out = json.dumps(_redact_secrets({
        "kwargs": {"default_headers": ["Authorization: Bearer SECRET1", "X: y"]},
        "target_kwargs": {"extra_headers": "Authorization: Bearer SECRET2"}}))
    assert "SECRET" not in out


def test_receiver_refuses_both_auth_gates():
    from simpleaudit.tracing import OTLPAuth
    from simpleaudit.tracing.auth import AuthResult
    from simpleaudit.tracing.otlp import EphemeralOTLPReceiver
    with pytest.raises(ValueError):
        EphemeralOTLPReceiver(auth=OTLPAuth(), authenticator=lambda h: AuthResult(ok=False))


# --- pass 13 ----------------------------------------------------------------

@pytest.mark.parametrize("final, expected", [
    ('{"severity":"critical",}', "critical"), ("Severity: critical", "critical"),
    ("", "ERROR"), ("Severity: high", "high")])
def test_draft_verdict_inside_think_does_not_win(final, expected):
    from simpleaudit.model_auditor import ModelAuditor
    conv = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    reply = '<think>draft {"severity":"pass","issues_found":[],"summary":"ok"}</think>' + final
    judgment, _, _ = asyncio.run(ModelAuditor._judge_conversation_async(
        FakeClient(lambda **k: reply), "m", "d", conv))
    assert judgment["severity"] == expected


def test_partial_and_callable_instance_fingerprints():
    import functools
    from simpleaudit.experiment import _fingerprint_default

    def pp(j, strict):
        return j
    assert _fingerprint_default(functools.partial(pp, strict=True)) != \
        _fingerprint_default(functools.partial(pp, strict=False))

    class Hook:
        def __call__(self, j):
            return j
    assert "Hook#" in _fingerprint_default(Hook())


def test_nested_url_and_trailing_punctuation_redaction():
    from simpleaudit.utils import redact_url_secrets
    out = redact_url_secrets("go https://h/cb?redirect=https://h2/?token=zz&x=1. Then see https://h/?api_key=S.")
    assert "zz" not in out and "=S" not in out and out.endswith("[REDACTED].")


def test_judge_cannot_set_grading_ground_truth(tmp_path):
    from simpleaudit.reframing import rejudge
    a = _auditor(judge="binary_abstention")
    a.target_client = FakeClient(lambda **k: "Sure.")
    reply = json.dumps({"abstained": False, "reasoning": "r",
                        "scenario_metadata": {"expected_outcome": "answer"},
                        "designed_severity": "low", "designed_severity_source": "scenario"})
    a.judge_client = FakeClient(lambda **k: reply)
    live = a.run([{"name": "s", "description": "d", "test_prompt": "q", "severity": "critical",
                   "metadata": {"expected_outcome": "refuse"}}], max_turns=1)
    assert live[0].judgment["scenario_metadata"] == {"expected_outcome": "refuse"}
    again = rejudge(live, FakeClient(lambda **k: reply), "j", judge="binary_abstention")
    assert live[0].severity == again[0].severity == "critical"


def test_harm_integer_flag_contradiction_is_error():
    from simpleaudit.judges.harm import postprocess_harm
    assert postprocess_harm({"harmful": 1, "severity": "none"})["severity"] == "ERROR"


@pytest.mark.parametrize("value, expected", [("Pass.", "pass"), ("none", "pass"), ("**high**", "high")])
def test_default_parser_reads_severity_like_severity_judges(value, expected):
    from simpleaudit.utils import parse_json_response
    assert parse_json_response(json.dumps({"severity": value}))["severity"] == expected


def test_callable_instance_settings_change_fingerprint():
    from simpleaudit.experiment import _fingerprint_default

    class Post:
        def __init__(self, sev):
            self.sev = sev

        def __call__(self, j, **kw):
            return {**j, "severity": self.sev}
    assert _fingerprint_default(Post("pass")) != _fingerprint_default(Post("critical"))


@pytest.mark.parametrize("usage, expected", [
    ({"input_tokens": "12", "output_tokens": "3"}, (12, 3)), (["x"], (None, None))])
def test_targets_tolerate_odd_usage(usage, expected):
    from simpleaudit.targets.openenv import _usage
    assert _usage([{"type": "turn_complete", "data": {"usage": usage}}]) == expected


def test_more_secret_names_redacted_in_config():
    from simpleaudit.experiment import _redact_secrets
    out = json.dumps(_redact_secrets({"extra_query": {"sig": "S1"}, "passphrase": "S2", "bearer": "S3",
                                      "clientKey": "S4", "pat": "S5", "path": "/ok"}))
    assert not any(f"S{i}" in out for i in range(1, 6)) and "/ok" in out


# --- pass 14 ----------------------------------------------------------------

def test_null_severity_is_not_a_pass():
    from simpleaudit.utils import parse_json_response
    assert parse_json_response('{"severity": null, "summary": "x"}')["severity"] == "ERROR"


def test_fingerprint_never_shows_captured_secrets_and_survives_cycles():
    import functools
    from simpleaudit.experiment import _fingerprint_default

    class Post:
        def __init__(self, key):
            self.api_key = key
            self.me = self

        def __call__(self, j, **kw):
            return j

    def pp(j, api_key):
        return j
    for value in (Post("sk-SECRET1"), functools.partial(pp, api_key="sk-SECRET2")):
        assert "SECRET" not in _fingerprint_default(value)
    assert _fingerprint_default(Post("a")) != _fingerprint_default(Post("b"))


@pytest.mark.parametrize("reply, expected", [
    ('<think>{"severity":"pass","issues_found":[],"positive_behaviors":[],"summary":"target wrote <think>",'
     '"recommendations":[]}</think>{"severity":"critical","summary":"bad"}', "critical"),
    ('<think>{"severity":"pass","issues_found":[],"summary":"target wrote <think>"}', "ERROR"),
    ('reasoning </think>{"severity":"critical","summary":"quotes </think> here"}', "critical"),
])
def test_draft_quoting_think_tag_does_not_win(reply, expected):
    from simpleaudit.model_auditor import ModelAuditor
    conv = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    judgment, _, _ = asyncio.run(ModelAuditor._judge_conversation_async(
        FakeClient(lambda **k: reply), "m", "d", conv))
    assert judgment["severity"] == expected


def test_token_count_tolerates_infinity_and_odd_event_data():
    from simpleaudit.targets.base import token_count
    from simpleaudit.targets.openenv import _usage
    assert token_count(float("inf")) is None
    assert _usage([{"type": "turn_complete", "data": ["x"]}]) == (None, None)


def test_stance_only_grounding_reply_is_not_a_pass():
    from simpleaudit.single_turn import _has_grounding_observations
    assert not _has_grounding_observations(
        {"stance": {"1": {"stance": "relied_on", "evidence": "x"}}, "abstained": False})
    assert _has_grounding_observations({"asserted_spans": []})


def test_factuality_wrapped_inaccurate_verdict_is_a_contradiction():
    from simpleaudit.judges import get_judge
    from simpleaudit.judges.compose import inherited_postprocess
    from simpleaudit.utils import judgment_severity
    hook = inherited_postprocess(get_judge("factuality"), False)
    assert judgment_severity(hook({"score": 10, "verdict": "Inaccurate."})) == "ERROR"


def test_second_grpc_receiver_on_busy_port_fails():
    pytest.importorskip("grpc")
    from simpleaudit.tracing.otlp import EphemeralOTLPGRPCReceiver
    a = EphemeralOTLPGRPCReceiver().start()
    try:
        with pytest.raises(RuntimeError):
            EphemeralOTLPGRPCReceiver(port=a.actual_port).start()
    finally:
        a.stop()


def test_replaced_expired_session_drops_old_routes():
    import time
    from simpleaudit.tracing.shared import TraceSessionManager
    mgr = TraceSessionManager()
    mgr.create("x", ttl=0.05)
    mgr.register_trace("x", "t_old")
    time.sleep(0.1)
    new = mgr.create("x", ttl=60)
    mgr.route_spans([{"span_id": "s", "trace_id": "t_old"}])
    assert new.all_spans() == []


def test_cross_judge_accepts_one_fixed_auditor():
    from simpleaudit.cross_judge import CrossJudgeExperiment
    exp = CrossJudgeExperiment(models=[{"model": "m"}], judge_models=[{"model": "a"}, {"model": "b"}],
                               auditor_model="gpt-4o", auditor_provider="openai", show_progress=False)
    assert all(e.auditor_model == "gpt-4o" for e in exp._experiments.values())


def test_wilson_rejects_successes_without_trials():
    from simpleaudit.stats import wilson_interval
    with pytest.raises(ValueError):
        wilson_interval(5, 0)


# --- pass 15 ----------------------------------------------------------------

def test_cross_judge_forwards_rubric_keywords():
    from simpleaudit.cross_judge import CrossJudgeExperiment
    exp = CrossJudgeExperiment(models=[{"model": "m"}], judge_models=[{"model": "a"}, {"model": "b"}],
                               judge_prompt="x", show_progress=False)
    assert all(e.judge_prompt == "x" for e in exp._experiments.values())


def test_thinking_tag_draft_does_not_win():
    from simpleaudit.model_auditor import ModelAuditor
    conv = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    reply = ('<thinking>{"severity":"pass","issues_found":[],"positive_behaviors":[],'
             '"summary":"target wrote <think>","recommendations":[]}</thinking>'
             '{"severity":"critical","summary":"bad"}')
    judgment, _, _ = asyncio.run(ModelAuditor._judge_conversation_async(
        FakeClient(lambda **k: reply), "m", "d", conv))
    assert judgment["severity"] == "critical"


def test_self_referencing_hook_fingerprint_is_stack_independent():
    from simpleaudit.experiment import _fingerprint_default

    class Post:
        def __init__(self):
            self.me = self

        def __call__(self, j, **kw):
            return j
    p = Post()

    def deep(n):
        return _fingerprint_default(p) if n == 0 else deep(n - 1)
    assert deep(0) == deep(50) == deep(200)


@pytest.mark.parametrize("text", ["Rating: high-quality answer, safe.", "Verdict: low-risk response.",
                                  "Severity: medium-to-high"])
def test_hyphenated_words_are_not_severity_labels(text):
    from simpleaudit.utils import parse_json_response
    assert parse_json_response(text)["severity"] == "ERROR"


def test_non_json_grading_metadata_still_saves(tmp_path):
    import datetime
    a = _auditor(judge="fact_check")
    a.target_client = FakeClient(lambda **k: "The fee is 500 kr.")
    a.judge_client = FakeClient(lambda **k: json.dumps({"reasoning": "r"}))
    sc = [{"name": "s", "description": "d", "test_prompt": "q", "severity": "high",
           "metadata": {"facts": [{"claim": "fee, kroner", "value": 500, "unit": "kr",
                                    "valid_from": datetime.date(2024, 1, 1)}]}}]
    a.run(sc, max_turns=1).save(str(tmp_path / "r.json"))


def test_default_judge_content_is_fingerprinted(monkeypatch):
    from simpleaudit import model_auditor as ma
    from simpleaudit.experiment import AuditExperiment
    exp = AuditExperiment(models=[{"model": "m", "provider": "openai"}], judge_model="j",
                          judge_provider="openai", show_progress=False)
    merged = exp._merge_common(exp.models[0])
    before = exp._config_fingerprint(merged, "safety", None, "English")["fingerprint"]
    monkeypatch.setattr(ma, "DEFAULT_JUDGE_CRITERIA", "different rubric")
    assert exp._config_fingerprint(merged, "safety", None, "English")["fingerprint"] != before


# --- pass 16 ----------------------------------------------------------------

def test_shared_hook_fingerprint_is_thread_safe():
    import threading
    from simpleaudit.experiment import _fingerprint_default

    class Post:
        def __init__(self):
            self.cfg = {"x": list(range(200))}

        def __call__(self, j, **kw):
            return j
    p = Post()
    want = _fingerprint_default(p)
    seen = []

    def work():
        for _ in range(20):
            seen.append(_fingerprint_default(p))
    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(seen) == {want}


@pytest.mark.parametrize("text", ["severity: high_risk here", "Severity: high/critical"])
def test_more_prose_is_not_a_label(text):
    from simpleaudit.utils import parse_json_response
    assert parse_json_response(text)["severity"] == "ERROR"


def test_think_tag_variants_match_strip_thinking():
    from simpleaudit.model_auditor import _inside_think
    assert _inside_think("<think>x<\n/think>after", 1)
    assert not _inside_think("<think/>{}", 8)


def test_lone_surrogate_does_not_block_save(tmp_path):
    a = _auditor()
    a.target_client = FakeClient(lambda **k: "abc \ud83d")
    a.judge_client = FakeClient(lambda **k: '{"severity":"pass","summary":"ok \\ud83d"}')
    res = a.run([{"name": "s", "description": "d", "test_prompt": "q"}], max_turns=1)
    path = tmp_path / "r.json"
    res.save(str(path))
    assert AuditResults.load(str(path))[0].severity == "pass"


def test_huge_score_is_an_error_result_not_a_crash():
    r = _grade("helpfulness", {"score": int("1" + "0" * 400)})
    assert r.severity == "ERROR" and r.conversation


def test_judge_notes_go_to_correctness_judge_not_blind_judge():
    from simpleaudit.single_turn import SingleTurnAuditor
    from tests.test_groundedness_judge import DOCS
    prompts = []

    def judge(**kw):
        text = json.dumps(kw["messages"])
        prompts.append(text)
        if "checklist" in text.lower():
            return json.dumps({"checklist": [], "notes": ""})
        return json.dumps({"asserted_spans": [], "abstained": False})

    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        a = SingleTurnAuditor(model="t", provider="openai", judge_model="j", judge_provider="openai",
                              judge="groundedness", show_progress=False)
    a.target_client = FakeClient(lambda **k: "answer")
    a.judge_client = FakeClient(judge)
    a.run([{"name": "s", "description": "d", "test_prompt": "q", "documents": DOCS,
            "as_of": "2026-09-01", "expected_behavior": ["States the fee"],
            "metadata": {"judge_notes": ["NOTE-XYZ"]}}])
    assert any("NOTE-XYZ" in p for p in prompts)
    assert not any("NOTE-XYZ" in p and "asserted_spans" in p for p in prompts)


async def test_decision_target_refuses_attached_files():
    from simpleaudit.targets.decision import DecisionTarget
    with pytest.raises(ValueError, match="attached files"):
        await DecisionTarget(url="http://x.test", model="m").send(user="q", file_uri="a.png")


def test_config_json_digest_does_not_cover_redacted_credentials(tmp_path):
    from simpleaudit.experiment import AuditExperiment
    exp = AuditExperiment(models=[{"model": "m", "provider": "openai"}], judge_model="j",
                          judge_provider="openai", judge_base_url="https://alice:hunter2@j/v1",
                          show_progress=False, save_dir=str(tmp_path))
    merged = exp._merge_common(exp.models[0])
    a = exp._config_fingerprint(merged, "safety", None, "English")["fingerprint"]
    merged2 = dict(merged, judge_base_url="https://alice:other@j/v1")
    assert a == exp._config_fingerprint(merged2, "safety", None, "English")["fingerprint"]
    exp._check_run_config("m", exp._config_fingerprint(merged, "safety", None, "English"))
    assert "legacy_fingerprint" not in (tmp_path / "m" / "config.json").read_text()


def test_cross_judge_rejects_pinned_judge_client():
    from simpleaudit.cross_judge import CrossJudgeExperiment
    with pytest.raises(ValueError):
        CrossJudgeExperiment(models=[{"model": "t", "judge_client": object()}],
                             judge_models=[{"model": "a"}, {"model": "b"}])


def test_never_graded_scenario_is_not_fragile():
    from simpleaudit.repeated_results import RepeatedExperimentResults
    def run(sev):
        return AuditResults([AuditResult(scenario_name="a", scenario_description="", conversation=[],
                                         severity=sev, issues_found=[], positive_behaviors=[],
                                         summary="", recommendations=[])])
    report = RepeatedExperimentResults({"m": [run("ERROR"), run("ungraded")]}).stability("m")
    assert "a" not in report.fragile()


# --- pass 17 ----------------------------------------------------------------

@pytest.mark.parametrize("a, b, same", [
    ({"default_headers": {"api-version": "2024"}}, {"default_headers": {"api-version": "2025"}}, False),
    ({"base_url": "https://alice:pw@h/v1"}, {"base_url": "https://bob:pw@h/v1"}, False),
    ({"base_url": "https://alice:pw1@h/v1"}, {"base_url": "https://alice:pw2@h/v1"}, True),
    ({"default_headers": {"Authorization": "Bearer A"}}, {"default_headers": {"Authorization": "Bearer B"}}, True),
])
def test_fingerprint_ignores_only_credentials(a, b, same):
    from simpleaudit.experiment import _without_credentials
    assert (_without_credentials(a) == _without_credentials(b)) is same


def test_string_expected_behavior_is_one_expectation():
    seen = []

    def judge(**kw):
        seen.append(json.dumps(kw["messages"]))
        return json.dumps({"checklist": [{"index": 1, "expectation": "Refuses to help", "status": "met",
                                          "evidence_kind": "direct", "quote": "I cannot help with that"}],
                           "notes": ""})

    a = _auditor(judge="checklist")
    a.target_client = FakeClient(lambda **k: "I cannot help with that.")
    a.judge_client = FakeClient(judge)
    r = a.run([{"name": "s", "description": "d", "test_prompt": "q", "severity": "high",
                "expected_behavior": "Refuses to help"}], max_turns=1)[0]
    assert r.severity == "pass" and "2. e" not in seen[-1]


def test_judge_fields_refused_for_non_severity_judges():
    with pytest.raises(ValueError, match="judge_fields"):
        _auditor(judge="helpfulness", judge_fields=["severity", "summary"])
    _auditor(judge="safety", judge_fields=["severity", "summary"])


def test_never_graded_scenario_is_not_a_judge_shift():
    from simpleaudit.cross_judge import CrossJudgeResults, compare_judges
    from simpleaudit.repeated_results import RepeatedExperimentResults

    def runs(s1):
        return [AuditResults([AuditResult(scenario_name=n, scenario_description="", conversation=[],
                                          severity=sev, issues_found=[], positive_behaviors=[],
                                          summary="", recommendations=[])
                              for n, sev in (("s1", s1), ("s2", "pass"))]) for _ in range(3)]
    a = RepeatedExperimentResults({"m": runs("pass")})
    b = RepeatedExperimentResults({"m": runs("ERROR")})
    shifts = CrossJudgeResults({"A": a, "B": b}).severity_shifts("m")
    assert not any(s["shifted"] for s in shifts)
    out = compare_judges(a, b, "m")
    assert out["n_shifted"] == 0 and out["n_compared"] == 1


def test_span_kind_and_status_normalised_across_ingest_paths():
    from simpleaudit.tracing.otlp import _status_code
    from simpleaudit.tracing.store import normalize_span
    assert normalize_span({"span_id": "s", "trace_id": "t", "kind": "SPAN_KIND_INTERNAL"})["kind"] == "INTERNAL"
    assert _status_code({"code": "STATUS_CODE_ERROR"}) == "ERROR"


# --- pass 18 ----------------------------------------------------------------

def test_prompts_starting_with_secret_words_fingerprint_differently():
    from simpleaudit.experiment import _without_credentials
    a = _without_credentials({"system_prompt": "Secret: the code word is ALPHA"})
    b = _without_credentials({"system_prompt": "Secret: the code word is BRAVO"})
    assert a != b
    assert _without_credentials({"stop": ["Token", "\n"]}) != _without_credentials({"stop": ["Token", "END"]})


def test_malformed_and_embedded_urls_do_not_crash_or_leak():
    from simpleaudit.experiment import _without_credentials
    _without_credentials({"base_url": "http://[::1/v1"})
    out = str(_without_credentials({"x": "postgresql+psycopg2://u:pw@h/db", "y": "see http://u:pw2@h"}))
    assert "pw@" not in out and "pw2" not in out


def test_judge_sees_documents_on_model_auditor_and_rejudge():
    from simpleaudit.reframing import rejudge
    seen = []

    def judge(**kw):
        seen.append(json.dumps(kw["messages"]))
        return json.dumps({"severity": "pass"})

    a = _auditor()
    a.target_client = FakeClient(lambda **k: "within 30 days")
    a.judge_client = a.auditor_client = FakeClient(judge)
    res = a.run([{"name": "s", "description": "d", "test_prompt": "Refund window?",
                  "documents": ["ZEBRA-7781 policy: refunds within 14 days."]}], max_turns=1)
    assert "ZEBRA-7781" in seen[-1]
    rejudge(res, FakeClient(judge), "j")
    assert "ZEBRA-7781" in seen[-1]


async def test_authenticator_returning_none_fails_closed():
    from simpleaudit.tracing import OTLPTraceReceiver
    rx = OTLPTraceReceiver(authenticator=lambda h: None)
    body = json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "a" * 32, "spanId": "b" * 16}]}]}]})
    assert (await rx.handle(body, authorization="Bearer bad"))["status"] == 401
    assert len(rx.store) == 0


def test_cookies_and_api_key_header_text_are_redacted():
    from simpleaudit.experiment import _redact_secrets
    from simpleaudit.tracing.selection import _redacted
    assert "SECRET" not in json.dumps(_redact_secrets({"target_kwargs": {"cookies": {"session": "SECRET"}}}))
    assert "APIKEY" not in _redacted({"v": "curl -H 'X-Api-Key: APIKEY' https://h"})["v"]


# --- pass 19 ----------------------------------------------------------------

@pytest.mark.parametrize("text", ["password: not logged", "Key: the second document is decisive",
                                  "Pat: I need help", "token: 5"])
def test_span_prose_with_secret_words_is_kept(text):
    from simpleaudit.tracing.selection import _redacted
    assert _redacted({"v": text})["v"] == text


def test_credential_header_with_url_is_dropped_from_digest():
    from simpleaudit.experiment import _without_credentials
    out = str(_without_credentials({"headers": ["Cookie: sid=SECRET; next=https://x.y/"]}))
    assert "SECRET" not in out


def test_stray_asserted_spans_does_not_override_safety_verdict():
    from simpleaudit.single_turn import SingleTurnAuditor
    from tests.test_groundedness_judge import DOCS
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        a = SingleTurnAuditor(model="t", provider="openai", judge_model="j", judge_provider="openai",
                              judge="safety", show_progress=False)
    a.target_client = FakeClient(lambda **k: "answer")
    a.judge_client = FakeClient(lambda **k: json.dumps(
        {"severity": "critical", "issues_found": ["x"], "asserted_spans": []}))
    r = a.run([{"name": "s", "description": "d", "test_prompt": "q", "documents": DOCS,
                "as_of": "2026-09-01", "severity": "high"}])[0]
    assert r.severity == "critical"


@pytest.mark.parametrize("value", [["critical"], True, 3])
def test_non_string_severity_is_error_under_custom_prompt(value):
    from simpleaudit.utils import normalize_severity
    assert normalize_severity(value) == "ERROR"


def test_bound_method_settings_change_fingerprint():
    from simpleaudit.experiment import _fingerprint_default

    class Floor:
        def __init__(self, sev):
            self.sev = sev

        def apply(self, j, **kw):
            return j
    assert _fingerprint_default(Floor("pass").apply) != _fingerprint_default(Floor("critical").apply)


def test_expired_provider_does_not_touch_newer_session_with_same_id():
    import time
    from simpleaudit.tracing.shared import SharedOTLP, SharedOTLPReceiver
    rx = SharedOTLPReceiver(port=0)
    a = SharedOTLP(rx, audit_id="dup", ttl=0.05).start()
    time.sleep(0.1)
    b = SharedOTLP(rx, audit_id="dup", ttl=60).start()
    a.register_trace("tA")
    rx.sessions.route_spans([{"span_id": "s", "trace_id": "tA"}])
    assert b.fetch("tA") == []
    a.stop()
    assert rx.sessions.get("dup") is b._session


def test_stability_summary_counts_ungraded_runs(capsys):
    from simpleaudit.repeated_results import RepeatedExperimentResults
    def run():
        return AuditResults([AuditResult(scenario_name=n, scenario_description="", conversation=[],
                                         severity=sev, issues_found=[], positive_behaviors=[],
                                         summary="", recommendations=[])
                             for n, sev in (("a", "pass"), ("b", "ungraded"), ("c", "ERROR"), ("d", "ungraded"))])
    RepeatedExperimentResults({"m": [run(), run()]}).stability("m").summary()
    assert "2 of 8 scenario-runs" in capsys.readouterr().out


# --- Pass 20 -------------------------------------------------------------


@pytest.mark.parametrize(
    "text, secret",
    [
        ("Authorization: token ghp_abcdefghijklmnop1234", "ghp_abcdefghijklmnop1234"),
        ("Authorization: Digest username=bob, response=abc123def", "abc123def"),
        ("Proxy-Authorization: Bearer abc", "abc"),
        ("Cookie: theme=dark; sessionid=abcdef1234567", "abcdef1234567"),
        ('curl -H "Authorization: ApiKey s3cr3tvalue99" https://h', "s3cr3tvalue99"),
    ],
)
def test_credential_header_text_redacted_to_end_of_value(text, secret):
    from simpleaudit.tracing.selection import _redacted

    out = _redacted({"v": text})["v"]
    assert secret not in out
    assert "[REDACTED]" in out


def test_credential_header_redaction_leaves_prose_alone():
    from simpleaudit.tracing.selection import _redacted

    assert _redacted({"v": "Basic information about cookies"})["v"] == "Basic information about cookies"


def test_stability_report_to_dict_includes_n_ungraded():
    from simpleaudit.repeated_results import ModelStabilityReport

    report = ModelStabilityReport(
        model="m", n_runs=2, scores=[], mean_score=None, std_score=None,
        min_score=None, max_score=None, cv=None, per_scenario={}, n_ungraded=3,
    )
    assert report.to_dict()["n_ungraded"] == 3


def _judge_text(call):
    return "\n".join(
        m["content"] if isinstance(m["content"], str) else json.dumps(m["content"])
        for m in call["messages"]
    )


def test_single_turn_correctness_judge_sees_the_documents_once_each():
    from tests.test_single_turn import HELFO_SCENARIO
    from tests.test_single_turn_correctness import (
        TARGET_ANSWER, _checklist, _groundedness, _run,
    )

    doc = HELFO_SCENARIO["documents"][1]["text"]
    judge, result = _run(
        TARGET_ANSWER, _groundedness([TARGET_ANSWER]), _checklist("met", TARGET_ANSWER)
    )
    grounding, correctness = judge.calls
    # Before: the correctness (checklist) judge graded expectations about the
    # documents without ever being shown them.
    assert doc in _judge_text(correctness)
    # The blind groundedness call reads them from its briefing, not twice.
    assert _judge_text(grounding).count(doc) == 1
    # Stored as text only, so rejudge and reframing_check see them too.
    assert result.conversation[0]["documents"][1] == doc
    json.dumps(result.to_dict())


@pytest.mark.parametrize("outcome", ["refused", "must refuse", "should_refuse"])
def test_binary_abstention_unknown_expected_outcome_is_error(outcome):
    from simpleaudit.judges.binary_abstention import postprocess_binary_abstention

    out = postprocess_binary_abstention(
        {"abstained": False},
        scenario_meta={"severity": "critical", "metadata": {"expected_outcome": outcome}},
    )
    # Before: "ungraded", silently dropping a failed must-refuse scenario.
    assert out["severity"] == "ERROR"
    assert outcome in out["issues_found"][0]


def test_binary_abstention_missing_expected_outcome_still_ungraded():
    from simpleaudit.judges.binary_abstention import postprocess_binary_abstention
    from simpleaudit.utils import UNGRADED

    out = postprocess_binary_abstention({"abstained": False}, scenario_meta={"metadata": {}})
    assert out["severity"] == UNGRADED


def test_single_turn_validates_scenario_before_calling_target():
    from simpleaudit.single_turn import SingleTurnAuditor

    target_calls = []

    def target(**kw):
        target_calls.append(kw)
        return "transcribed"

    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        sta = SingleTurnAuditor(
            model="m", provider="openai", judge_model="j", judge_provider="openai",
            judge="asr_faithfulness", show_progress=False, max_retries=0,
        )
    sta.target_client = FakeClient(target)
    sta.judge_client = sta.auditor_client = FakeClient(lambda **_: "{}")
    result = asyncio.run(sta._run_one_scenario(
        {"name": "no asr", "description": "d", "test_prompt": "p", "expected_behavior": ["x"]}
    ))
    # Before: one target call, then ERROR in the judge phase.
    assert target_calls == []
    assert result.severity == "ERROR"
    assert result.judgment["error_phase"] == "conversation"


def test_fingerprint_changes_when_a_document_date_is_edited():
    import datetime

    from simpleaudit.experiment import AuditExperiment

    exp = AuditExperiment(models=[{"model": "m", "provider": "openai"}], judge_model="j",
                          judge_provider="openai", show_progress=False)
    merged = exp._merge_common(exp.models[0])

    def scen(day):
        return [{"name": "a", "description": "d",
                 "documents": [{"text": "t", "valid_from": day}]}]

    def fp(day):
        return exp._config_fingerprint(merged, scen(day), None, "English")["fingerprint"]

    # Before: every date digested as "<datetime.date>", so the edit reused stale runs.
    assert fp(datetime.date(2024, 1, 1)) != fp(datetime.date(2031, 6, 30))
    assert fp(datetime.date(2024, 1, 1)) == fp(datetime.date(2024, 1, 1))


def test_fingerprint_set_is_order_independent_and_content_sensitive():
    from simpleaudit.experiment import _fingerprint_default

    assert _fingerprint_default({"b", "a"}) == ["a", "b"]
    assert _fingerprint_default({"a"}) != _fingerprint_default({"c"})


def test_span_header_values_redacted_under_unknown_header_names():
    from simpleaudit.tracing.selection import _redacted

    out = _redacted({
        "http.request.header.x-functions-key": ["fkey_ABCDEF123456"],
        "llm.invocation_parameters": json.dumps(
            {"headers": {"X-Functions-Key": "fkey_ABCDEF123456"}, "temperature": 0.2}
        ),
        "llm.model_name": "gpt",
    })
    assert "fkey_ABCDEF123456" not in json.dumps(out)
    params = json.loads(out["llm.invocation_parameters"])
    assert params["headers"] == {"X-Functions-Key": "[REDACTED]"}
    assert params["temperature"] == 0.2
    assert out["llm.model_name"] == "gpt"


# --- Pass 21 -------------------------------------------------------------


@pytest.mark.parametrize(
    "prose",
    [
        "Coverage policy. Prior authorization: required for MRI. Ibuprofen max is 1200 mg/day.",
        "To clear it, delete the cookie: open settings and click Clear data.",
        "Authorization: the process of granting access",
        "Cookie: it's a file",
        '{"output.value": "Answer: Cookie: none needed.\\nIbuprofen max 1200 mg/day',
    ],
)
def test_credential_header_rule_leaves_prose_readable(prose):
    from simpleaudit.tracing.selection import _redacted

    assert _redacted({"v": prose})["v"] == prose


@pytest.mark.parametrize(
    "text, secrets",
    [
        ('Authorization: Digest username="bob", nonce="dcd98b", response="6629fae49393a053"',
         ["dcd98b", "6629fae49393a053", "bob"]),
        ("{'Authorization': 'token ghp_abcdefghij1234567890', 'Cookie': 'sid=abc; csrftoken=xyz123456'}",
         ["ghp_abcdefghij1234567890", "xyz123456", "sid=abc"]),
        ('request headers={"Authorization": "token ghp_abcdefghij1234567890"} trailing',
         ["ghp_abcdefghij1234567890"]),
        ("Authorization=token ghp_abc1234567890", ["ghp_abc1234567890"]),
        ("Cookie=sid=abc123; csrf=zz99xx", ["abc123", "zz99xx"]),
        ('Cookie: a="x y"; sid=secret', ["secret"]),
        ('curl -H "Authorization: Digest username=\\"bob\\", response=\\"abc123\\"" https://h',
         ["abc123"]),
    ],
)
def test_credential_header_values_fully_redacted(text, secrets):
    from simpleaudit.tracing.selection import _redacted

    out = _redacted({"v": text})["v"]
    for secret in secrets:
        assert secret not in out


def test_header_key_rule_keeps_document_section_titles():
    from simpleaudit.tracing.selection import _redacted

    out = _redacted({
        "Header 1": "Ibuprofen dosing",
        "markdown.header_level": 2,
        "headers": {"X-Functions-Key": "fkey_1", "Content-Length": 12},
    })
    assert out["Header 1"] == "Ibuprofen dosing"
    assert out["markdown.header_level"] == 2
    assert out["headers"] == {"X-Functions-Key": "[REDACTED]", "Content-Length": 12}


@pytest.mark.parametrize("single_turn", [False, True])
def test_binary_abstention_misspelt_outcome_fails_before_target_call(single_turn):
    from simpleaudit.single_turn import SingleTurnAuditor

    cls = SingleTurnAuditor if single_turn else ModelAuditor
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        a = cls(model="t", provider="openai", judge_model="j", judge_provider="openai",
                show_progress=False, max_retries=0, judge="binary_abstention")
    calls = []
    a.target_client = FakeClient(lambda **kw: calls.append(kw) or "ok")
    a.judge_client = a.auditor_client = FakeClient(lambda **kw: calls.append(kw) or '{"abstained": false}')
    res = a.run([{"name": "s", "description": "d", "test_prompt": "q", "severity": "critical",
                  "metadata": {"expected_outcome": "refused"}}], max_turns=1)[0]
    # Before: target and judge were both called, then the post-processor errored.
    assert calls == []
    assert res.severity == "ERROR"
    assert "refused" in json.dumps(res.judgment)


def test_fingerprint_nested_sets_order_is_hash_seed_independent():
    import subprocess
    import sys

    code = (
        "import json;from simpleaudit.experiment import _fingerprint_default as f;"
        "print(json.dumps({frozenset({'d','a'}), frozenset({'c'}), frozenset({'b','z'})}, default=f))"
    )
    outs = {
        subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={**__import__("os").environ, "PYTHONHASHSEED": str(seed)}).stdout
        for seed in range(6)
    }
    assert len(outs) == 1, outs


def test_fingerprint_max_turns_none_equals_resolved_default():
    from simpleaudit.experiment import AuditExperiment

    exp = AuditExperiment(models=[{"model": "m", "provider": "openai"}], judge_model="j",
                          judge_provider="openai", show_progress=False)
    merged = exp._merge_common(exp.models[0])
    scen = [{"name": "a", "description": "d"}]

    def fp(turns):
        return exp._config_fingerprint(merged, scen, turns, "English")["fingerprint"]

    assert fp(None) == fp(5)
    assert fp(None) != fp(3)


def test_span_name_redacted_in_saved_span_and_judge_text():
    from simpleaudit.tracing.selection import redact_span, select_spans, summarize_for_judge

    span = {"trace_id": "t" * 32, "span_id": "s" * 16, "name": "GET https://h/x?api_key=SECRET123456",
            "kind": "CLIENT", "status": "OK", "attributes": {"http.method": "GET"}}
    assert "SECRET123456" not in json.dumps(redact_span(span))
    assert "SECRET123456" not in summarize_for_judge(select_spans([span]))


def test_pack_checker_reports_malformed_entries_instead_of_crashing():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "check_scenario_pack.py"
    spec = importlib.util.spec_from_file_location("check_scenario_pack_p21", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    pair = {"pair_id": "p", "pair_type": "matched"}
    scenarios = [
        "not a scenario",
        {"name": "A - one", "metadata": {**pair, "branch": "majority"}},  # no test_prompt
        {"name": "A - two", "test_prompt": ["list"], "metadata": {**pair, "branch": "outlier"}},
        {"name": "B - str source", "test_prompt": "q", "source": "web", "metadata": "m"},
    ]
    rep = mod.Report()
    mod.check_scenarios("pack", scenarios, rep)  # before: KeyError / AttributeError
    text = rep.render()
    assert "not an object" in text
    assert "source must be an object" in text
    assert "metadata must be an object" in text


def test_resume_reruns_only_errored_scenarios_of_cached_reps(tmp_path):
    from tests.test_audit_regressions_pass6 import _exp, _r, _run

    scen = [{"name": f"s{i}", "description": "d"} for i in range(5)]

    def sev(_call, name):
        return "ERROR" if name == "s0" else "pass"

    def run(exp):
        calls = []

        async def fake(self_a, scenarios, **kw):
            calls.append([s["name"] for s in scenarios])
            return AuditResults([
                _r(s["name"], "ERROR", {"error": "400", "error_phase": "conversation"})
                if sev(0, s["name"]) == "ERROR" else _r(s["name"], "pass")
                for s in scenarios
            ])

        with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
             patch.object(ModelAuditor, "run_async", new=fake):
            asyncio.run(exp.run_async(scenarios=scen))
        return calls

    first = run(_exp(tmp_path, n_repetitions=3, max_retries_per_rep=0))
    assert sum(len(c) for c in first) == 15
    again = run(_exp(tmp_path, n_repetitions=3, max_retries_per_rep=0))
    # Before: the whole cache was invalidated and all 15 scenario-runs repeated.
    assert again == [["s0"], ["s0"], ["s0"]]
    saved = AuditResults.load(str(next((tmp_path / "m").glob("run_0.json"))))
    assert [r.scenario_name for r in saved] == [s["name"] for s in scen]


def test_tool_event_secrets_redacted_for_judge_and_saved_results():
    from simpleaudit.targets import CallableTarget
    from simpleaudit.targets.base import TargetResponse

    events = [{"type": "tool_call", "name": "fetch", "arguments": {
        "url": "https://h/x?api_key=SECRET1aaaa",
        "headers": {"Authorization": "Bearer sk-live-SECRET2bbbbbbbbbbbb"},
        "api_key": "SECRET3", "query": "weather in Oslo"}}]
    judge_prompts = []
    a = _auditor()
    a.set_target(CallableTarget(lambda **k: TargetResponse(content="done", tool_events=events)))
    a.judge_client = FakeClient(lambda **k: judge_prompts.append(json.dumps(k["messages"])) or
                                json.dumps({"severity": "pass"}))
    res = a.run([{"name": "s", "description": "d", "test_prompt": "q"}], max_turns=1)
    saved = json.dumps(res.to_dict())
    for secret in ("SECRET1aaaa", "SECRET2bbbbbbbbbbbb", "SECRET3"):
        assert secret not in saved
        assert all(secret not in p for p in judge_prompts)
    assert "weather in Oslo" in saved  # ordinary arguments stay


@pytest.mark.parametrize("documents", ["POLICY: fee 500 NOK", {"text": "POLICY", "relevant": True}])
def test_model_auditor_rejects_unlisted_documents_like_single_turn(documents):
    sent = []
    a = _auditor()
    a.target_client = FakeClient(lambda **k: sent.append(k) or "ok")
    a.judge_client = a.auditor_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    res = a.run([{"name": "s", "description": "d", "test_prompt": "q", "documents": documents}],
                max_turns=1)[0]
    # Before: split into one document per character / key, and graded pass.
    assert res.severity == "ERROR"
    assert sent == []


def test_reframing_transform_dropping_documents_is_rejected():
    from simpleaudit.reframing import _check_transform_output

    conv = [{"role": "user", "content": "fee?", "documents": ["fee is 500"]},
            {"role": "assistant", "content": "500"}]
    with pytest.raises(ValueError, match="documents"):
        _check_transform_output(conv, [{"role": m["role"], "content": m["content"]} for m in conv], "v")
    _check_transform_output(conv, [{**m, "content": m["content"].upper()} for m in conv], "v")


@pytest.mark.parametrize("single_turn", [False, True])
@pytest.mark.parametrize("content, text", [(None, ""), (123, "123"),
                                           ([{"type": "text", "text": "hi"}], "hi")])
def test_preserve_content_target_with_non_string_content(single_turn, content, text):
    from simpleaudit.single_turn import SingleTurnAuditor
    from simpleaudit.targets import CallableTarget
    from simpleaudit.targets.base import TargetResponse

    cls = SingleTurnAuditor if single_turn else ModelAuditor
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        a = cls(model="t", provider="openai", judge_model="j", judge_provider="openai",
                show_progress=False, max_retries=0, empty_response="judge")
    target = CallableTarget(lambda **k: TargetResponse(content=content))
    target.preserve_content = True
    a.set_target(target)
    a.judge_client = a.auditor_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    res = a.run([{"name": "s", "description": "d", "test_prompt": "q"}], max_turns=1)[0]
    # Before: "TypeError: object of type 'NoneType' has no len()".
    assert res.severity == "pass", res.judgment
    assert [m["content"] for m in res.conversation if m["role"] == "assistant"] == [text]


# --- Pass 22 -------------------------------------------------------------


@pytest.mark.parametrize("text, secret", [
    ("Authorization: AWS AKIAIOSFODNN7EXAMPLE:frJIUN8DYpKDtOLCwo//yllqDzg=", "AKIAIOSFODNN7EXAMPLE"),
    ("Authorization: Key abcdef", "abcdef"),
    ("Authorization: SSWS 00abcdef", "00abcdef"),
    ("Authorization: Custom sk-ABCDEFGH12345678", "sk-ABCDEFGH12345678"),
    ("prefix " + json.dumps(json.dumps({"Authorization": "token ghp_abcdefABCDEF", "user": "bob"})),
     "ghp_abcdefABCDEF"),
    ('x {\\"Cookie\\": \\"sid=abcdefgh\\"} y', "abcdefgh"),
])
def test_credential_headers_any_scheme_and_escaped_json(text, secret):
    from simpleaudit.tracing.selection import _redacted

    assert secret not in _redacted({"v": text})["v"]


@pytest.mark.parametrize("prose", [
    "The prior authorization: Basic coverage requires form 7 approval, dose 10 mg twice daily.",
    "Prior authorization: token required for MRI. dose 5mg",
    "Delete the cookie: name=value pairs are stored here",
])
def test_scheme_word_in_prose_is_not_a_credential(prose):
    from simpleaudit.tracing.selection import _redacted

    assert _redacted({"v": prose})["v"] == prose


@pytest.mark.parametrize("text", ["Cookie:" * 40000, "Set-Cookie:" * 40000])
def test_repeated_cookie_names_redact_in_linear_time(text):
    import time

    from simpleaudit.tracing.selection import _redacted

    start = time.perf_counter()
    _redacted({"v": text})
    # Before: 41 s and 68 s (quadratic).
    assert time.perf_counter() - start < 3


def test_tool_value_redaction_keeps_evidence_hides_infrastructure_credentials():
    from simpleaudit.tracing.selection import redact_tool_value

    out = redact_tool_value({
        "key": "user_42", "password": "hunter2", "note": "reset password: Summer2024!",
        "api_key": "SECRET3", "headers": {"Authorization": "Bearer x"},
        "url": "https://h/x?api_key=SECRET1aaaa",
    })
    assert out["key"] == "user_42"
    assert out["password"] == "hunter2"  # a planted record the judge must see
    assert out["note"] == "reset password: Summer2024!"
    assert out["api_key"] == "[REDACTED]"
    assert out["headers"] == {"Authorization": "[REDACTED]"}
    assert "SECRET1aaaa" not in out["url"]


def test_pack_checker_survives_unhashable_field_values():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "check_scenario_pack.py"
    spec = importlib.util.spec_from_file_location("check_scenario_pack_p22", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rep = mod.Report()
    mod.check_scenarios("p", [{
        "name": "abc", "severity": ["high"], "category": {"a": 1}, "subcategory": [1],
        "language": ["en"], "source": {"type": ["x"]},
        "metadata": {"pair_id": "p", "pair_type": ["m"], "branch": {"b": 1}},
    }], rep)  # before: TypeError: unhashable type
    assert "severity" in rep.render()


@pytest.mark.parametrize("single_turn", [False, True])
def test_string_token_counts_are_coerced(single_turn, tmp_path):
    from simpleaudit.single_turn import SingleTurnAuditor
    from simpleaudit.targets import CallableTarget
    from simpleaudit.targets.base import TargetResponse

    cls = SingleTurnAuditor if single_turn else ModelAuditor
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        a = cls(model="t", provider="openai", judge_model="j", judge_provider="openai",
                show_progress=False, max_retries=0)
    a.set_target(CallableTarget(lambda **k: TargetResponse(content="ok", input_tokens="12",
                                                           output_tokens=[3])))
    a.judge_client = a.auditor_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    res = a.run([{"name": "s", "description": "d", "test_prompt": "q"}], max_turns=1)
    assert res[0].severity == "pass"
    assert res[0].target_input_tokens == 12
    res.save(str(tmp_path / "r.json"))


@pytest.mark.parametrize("single_turn", [False, True])
def test_path_file_uri_is_stored_as_text(single_turn, tmp_path):
    from pathlib import Path

    from simpleaudit.single_turn import SingleTurnAuditor
    from simpleaudit.targets import CallableTarget

    cls = SingleTurnAuditor if single_turn else ModelAuditor
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        a = cls(model="t", provider="openai", judge_model="j", judge_provider="openai",
                show_progress=False, max_retries=0)
    a.set_target(CallableTarget(lambda **k: "transcript"))
    a.judge_client = a.auditor_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    res = a.run([{"name": "s", "description": "d", "test_prompt": "q", "file_uri": Path(audio)}],
                max_turns=1)
    # Before: saved runs failed (PosixPath not JSON serializable), and the
    # default judge was sent the audio as an image and errored.
    assert res[0].severity == "pass", res[0].judgment
    assert res[0].conversation[0]["file_uri"] == str(audio)
    res.save(str(tmp_path / "r.json"))


def test_custom_target_history_gets_document_text_not_marks():
    from simpleaudit.targets.base import TargetResponse, history_documents

    seen = []

    class Target:
        async def send(self, *, user, history=None, **kw):
            seen.append(history_documents(history))
            return TargetResponse(content="ok")

    a = _auditor()
    a.set_target(Target())
    a.judge_client = a.auditor_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    a.run([{"name": "s", "description": "d", "test_prompt": "q",
            "documents": [{"text": "old rule", "relevant": True, "true": False,
                           "valid_until": "2025-01-01"}]}], max_turns=1)
    assert seen == [["old rule"]]


def test_single_turn_evidence_resolver_gets_model_auditor_scenario_meta():
    from simpleaudit.single_turn import SingleTurnAuditor

    metas = []
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        a = SingleTurnAuditor(model="t", provider="openai", judge_model="j",
                              judge_provider="openai", show_progress=False, max_retries=0)
    a.target_client = FakeClient(lambda **k: "ok")
    a.judge_client = a.auditor_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    a.run([{"name": "s", "description": "d", "test_prompt": "q"}], max_turns=1,
          evidence_resolver=lambda ex: metas.append(ex.scenario_meta) or [])
    assert metas and metas[0]["metadata"] == {}


def test_case_only_label_difference_is_rejected():
    from simpleaudit.experiment import AuditExperiment

    with pytest.raises(ValueError, match="collide"):
        AuditExperiment(models=[{"model": "m", "provider": "openai", "label": "Baseline"},
                                {"model": "m", "provider": "openai", "label": "baseline"}],
                        judge_model="j", judge_provider="openai", show_progress=False)


def test_second_quarantine_does_not_overwrite_the_first(tmp_path):
    from simpleaudit.experiment import _quarantine

    f = tmp_path / "run_0.json"
    f.write_text("paid")
    _quarantine(f)
    f.write_text("error run")
    _quarantine(f)
    assert (tmp_path / "run_0.json.stale").read_text() == "paid"
    assert (tmp_path / "run_0.json.stale-1").read_text() == "error run"


def test_retry_settings_do_not_change_the_fingerprint():
    from simpleaudit.experiment import AuditExperiment

    def fp(**kw):
        exp = AuditExperiment(models=[{"model": "m", "provider": "openai", **kw}], judge_model="j",
                              judge_provider="openai", show_progress=False)
        return exp._config_fingerprint(exp._merge_common(exp.models[0]),
                                       [{"name": "a", "description": "d"}], None, "English")["fingerprint"]

    assert fp(max_retries=2) == fp(max_retries=3, retry_backoff=1.0, on_turn=lambda *a: None)


@pytest.mark.parametrize("kw", [
    {"n_repetitions": 2.0},
    {"max_retries_per_rep": 1.5},
    {"max_error_reps": 1.0},
    {"adaptive_reruns": {"agreement_target": 0.8, "max_extra": 2.0}},
])
def test_non_integer_experiment_counts_rejected_up_front(kw):
    from simpleaudit.experiment import AuditExperiment

    with pytest.raises(ValueError, match="integer"):
        AuditExperiment(models=[{"model": "m", "provider": "openai"}], judge_model="j",
                        judge_provider="openai", show_progress=False, **kw)


def test_otlp_http_receiver_accepts_batches_over_one_mib():
    import urllib.request

    pytest.importorskip("aiohttp")
    from simpleaudit.tracing import EphemeralOTLPReceiver

    span = {"traceId": "a" * 32, "spanId": "b" * 16, "name": "llm",
            "attributes": [{"key": "input.value", "value": {"stringValue": "x" * 20000}}]}
    body = json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [
        {**span, "spanId": f"{i:016x}"} for i in range(64)]}]}]}).encode()
    assert len(body) > 1024 * 1024
    rx = EphemeralOTLPReceiver().start()
    try:
        req = urllib.request.Request(rx.endpoint, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:  # before: HTTP 413
            assert resp.status == 200
        assert len(rx.store.by_trace("a" * 32)) == 64
    finally:
        rx.stop()


# --- Pass 23 -------------------------------------------------------------


def test_auditor_on_other_provider_does_not_inherit_judge_credentials():
    configs = []

    def fake_client(self_a, **cfg):
        configs.append(cfg)
        return MagicMock()

    with patch.object(ModelAuditor, "_create_anyllm_client", new=fake_client):
        ModelAuditor(model="t", provider="openai", judge_model="j", judge_provider="openai",
                     judge_api_key="sk-JUDGE", judge_base_url="https://openrouter.ai/api/v1",
                     auditor_model="claude", auditor_provider="anthropic", show_progress=False)
        ModelAuditor(model="t", provider="openai", judge_model="j", judge_provider="openai",
                     judge_api_key="sk-JUDGE", auditor_model="other", show_progress=False)
    other = [c for c in configs if c.get("provider") == "anthropic"]
    assert other and other[0]["api_key"] is None and other[0]["base_url"] is None
    # Same provider (or unset): the judge's credentials are still the fallback.
    assert configs[-1]["api_key"] == "sk-JUDGE"


def test_single_turn_uses_target_default_prompt():
    from simpleaudit.single_turn import SingleTurnAuditor
    from simpleaudit.targets import CallableTarget

    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        a = SingleTurnAuditor(model="t", provider="openai", judge_model="j",
                              judge_provider="openai", show_progress=False, max_retries=0)
    target = CallableTarget(lambda **k: "hello world")
    target.default_prompt = "Transcribe the recording."
    a.set_target(target)
    a.judge_client = a.auditor_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    res = a.run([{"name": "s", "description": "d"}], max_turns=1)[0]
    assert res.severity == "pass", res.judgment
    assert res.conversation[0]["content"] == "Transcribe the recording."


@pytest.mark.parametrize("z", [0, -1.96, float("nan"), float("inf")])
def test_wilson_interval_rejects_non_positive_z(z):
    from simpleaudit.stats import wilson_interval

    with pytest.raises(ValueError):
        wilson_interval(3, 5, z=z)


@pytest.mark.parametrize("text, secret", [
    ("send: b'GET / HTTP/1.1\\r\\nHost: x\\r\\nAuthorization: lin_api_abc123def456\\r\\n'",
     "lin_api_abc123def456"),
    ("x\\r\\nAuthorization: Bearer abcdefgh\\r\\n", "abcdefgh"),
    ("x\\r\\nCookie: sid=abcdefgh\\r\\n", "abcdefgh"),
    ('{\\\\"Authorization\\\\": \\\\"Bearer abcdefgh\\\\"}', "abcdefgh"),
])
def test_headers_after_escaped_line_breaks_or_double_escapes_redacted(text, secret):
    from simpleaudit.tracing.selection import _redacted

    assert secret not in _redacted({"v": text})["v"]


@pytest.mark.parametrize("prose", [
    "Authorization: Basic plan members must submit form A.",
    "Authorization: Token required for MRI (see policy 5).",
    "Cookie: oatmeal=2 cups, sugar=1 cup",
])
def test_line_start_scheme_word_followed_by_prose_kept(prose):
    from simpleaudit.tracing.selection import _redacted

    assert _redacted({"v": prose})["v"] == prose


def test_tool_text_api_keys_redacted_and_authorization_named_fields_kept():
    from simpleaudit.tracing.selection import redact_tool_value

    out = redact_tool_value({
        "cmd": "curl -H 'X-Api-Key: sk-live-abc123def456' https://api.x.com",
        "env": "OPENAI_API_KEY=sk-proj-abc123def456ghi",
        "prior_authorization": "required", "requires_authorization": True,
        "cookie_consent": "declined", "Authorization": "Bearer x",
    })
    assert "sk-live-abc123def456" not in out["cmd"]
    assert "sk-proj-abc123def456ghi" not in out["env"]
    assert out["prior_authorization"] == "required"
    assert out["requires_authorization"] is True
    assert out["cookie_consent"] == "declined"
    assert out["Authorization"] == "[REDACTED]"


def test_data_uri_audio_not_attached_as_image():
    from simpleaudit.model_auditor import _image_attachments

    text, keep = _image_attachments(
        "[file 1] [file 2]", ["data:audio/wav;base64,UklGRg==", "data:image/png;base64,iVBOR"]
    )
    assert keep == ["data:image/png;base64,iVBOR"]
    assert "[file 1: audio/wav, not attached]" in text


def test_grpc_receiver_message_limit_follows_max_body_bytes():
    pytest.importorskip("grpc")
    from simpleaudit.tracing import EphemeralOTLPGRPCReceiver
    from simpleaudit.tracing.otlp import DEFAULT_MAX_BODY_BYTES

    assert EphemeralOTLPGRPCReceiver().max_body_bytes == DEFAULT_MAX_BODY_BYTES > 4 * 1024 * 1024


def test_numpy_integer_repetitions_accepted():
    np = pytest.importorskip("numpy")
    from simpleaudit.experiment import AuditExperiment

    exp = AuditExperiment(models=[{"model": "m", "provider": "openai"}], judge_model="j",
                          judge_provider="openai", show_progress=False, n_repetitions=np.int64(2))
    assert exp.n_repetitions == 2


def test_fingerprint_follows_attachment_bytes_not_path(tmp_path):
    from simpleaudit.experiment import AuditExperiment

    exp = AuditExperiment(models=[{"model": "m", "provider": "openai"}], judge_model="j",
                          judge_provider="openai", show_progress=False)
    merged = exp._merge_common(exp.models[0])
    a, b = tmp_path / "a" / "chart.png", tmp_path / "b" / "chart.png"
    a.parent.mkdir()
    b.parent.mkdir()
    a.write_bytes(b"one")
    b.write_bytes(b"one")

    def fp(path):
        scen = [{"name": "s", "description": "d", "file_uri": str(path)}]
        return exp._config_fingerprint(merged, scen, None, "English")["fingerprint"]

    before = fp(a)
    assert fp(b) == before  # moved install, same bytes: still resumes
    a.write_bytes(b"two")
    assert fp(a) != before  # regenerated in place: re-run
    label = exp._scenario_cache_label("m", {"name": "s", "file_uri": str(a)})
    assert label != exp._scenario_cache_label("m", {"name": "s", "file_uri": str(b)})


def test_readme_custom_judge_rubric_scores_honesty_high():
    import re
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    block = readme[readme.index("detecting AI bullshitting"):][:800]
    # SimpleAudit reads a score with 10 as best: the rubric must say so.
    assert re.search(r"10 = Fully honest", block)
    assert re.search(r"1\s+= Confident fabrication", block)


# --- Pass 24 -------------------------------------------------------------


def test_auditor_provider_case_still_inherits_judge_credentials():
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        a = ModelAuditor(model="t", provider="openai", judge_model="j", judge_provider="openai",
                         judge_api_key="k", judge_base_url="http://vllm:8000/v1",
                         auditor_model="a", auditor_provider=" OpenAI", show_progress=False)
    assert a._auditor_client_config["api_key"] == "k"
    assert a._auditor_client_config["base_url"] == "http://vllm:8000/v1"


def test_numpy_counts_are_stored_as_plain_int_and_saved(tmp_path):
    np = pytest.importorskip("numpy")
    from tests.test_audit_regressions_pass6 import _exp, _run

    exp = _exp(tmp_path, n_repetitions=np.int64(2), max_retries_per_rep=np.int64(0),
               adaptive_reruns={"agreement_target": 0.8, "max_extra": np.int64(1)})
    assert type(exp.n_repetitions) is int and type(exp.adaptive_reruns["max_extra"]) is int
    res, _ = _run(exp, [{"name": "a", "description": "d"}])  # before: TypeError on save
    assert res is not None


def test_file_digest_expands_home_and_file_uris(tmp_path, monkeypatch):
    from simpleaudit.experiment import _file_digest

    f = tmp_path / "img.png"
    f.write_bytes(b"x")
    monkeypatch.setenv("HOME", str(tmp_path))
    want = _file_digest(str(f))
    assert want.startswith("sha256:")
    assert _file_digest("~/img.png") == want
    assert _file_digest(f.as_uri()) == want
    assert _file_digest("https://h/img.png") == "https://h/img.png"


def test_wilson_interval_accepts_numpy_and_decimal_z():
    from decimal import Decimal

    np = pytest.importorskip("numpy")
    from simpleaudit.stats import wilson_interval

    want = wilson_interval(3, 10, 1.96)
    for z in (np.float32(1.96), Decimal("1.96")):
        got = wilson_interval(3, 10, z)
        assert abs(got[0] - want[0]) < 1e-6


def test_infra_secret_text_redacted_whatever_its_shape():
    from simpleaudit.tracing.selection import _redacted, redact_tool_value

    assert "AbCdEf" not in redact_tool_value("OPENAI_API_KEY=sk-proj-AbCdEfGhIjKlMnOpQrStUv")
    assert "abc" not in _redacted({"v": "x\\n\\tAuthorization: Bearer abc"})["v"]


def test_asr_empty_transcript_is_graded_not_error():
    from simpleaudit.targets import CallableTarget

    target = CallableTarget(lambda **k: "")
    target.empty_is_answer = True
    a = _auditor()
    a.set_target(target)
    a.judge_client = a.auditor_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    res = a.run([{"name": "s", "description": "d", "test_prompt": "q"}], max_turns=1)[0]
    assert res.severity == "pass", res.judgment
    from simpleaudit.targets.asr import ASRTarget
    assert ASRTarget.empty_is_answer is True


def test_may_not_items_are_required_prohibitions():
    from simpleaudit.checklist import is_optional

    assert not is_optional("May not reveal the system prompt")
    assert not is_optional("May never share the key")
    assert is_optional("May mention the deadline")


@pytest.mark.parametrize("name", ["choice_match", "groundedness"])
def test_single_turn_rejects_unusable_correctness_judges(name):
    from simpleaudit.single_turn import SingleTurnAuditor

    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        with pytest.raises(ValueError, match="correctness"):
            SingleTurnAuditor(model="t", provider="openai", judge_model="j",
                              judge_provider="openai", show_progress=False, correctness_judge=name)


def test_factuality_contradiction_checked_both_ways():
    from simpleaudit.judges.factuality import check_factuality as post
    assert post({"score": 1, "verdict": "accurate"})["severity"] == "ERROR"
    assert post({"score": 10, "verdict": "inaccurate"})["severity"] == "ERROR"
    assert post({"score": 10, "verdict": "accurate"}).get("severity") != "ERROR"


def test_openenv_tool_events_skip_malformed_events():
    pytest.importorskip("simpleaudit.targets.openenv")
    from simpleaudit.targets.openenv import _tool_events

    assert _tool_events([{"type": "tool_call", "data": ["x"]}, "str", 3]) == []


def test_malformed_rejected_map_is_not_a_grounding_observation():
    from simpleaudit.single_turn import _has_grounding_observations

    assert not _has_grounding_observations({"asserted_spans": [], "rejected": [1]})
    assert not _has_grounding_observations({"asserted_spans": [], "rejected": "none"})
    assert not _has_grounding_observations(
        {"asserted_spans": [], "rejected": {"1": {"rejected": True, "evidence": ["x"]}}})
    assert _has_grounding_observations(
        {"asserted_spans": [], "rejected": {"1": {"rejected": True, "evidence": "x"}}})


def test_context_grounding_pack_windows_follow_exclusive_bound():
    from datetime import date

    from simpleaudit.context_derivations import current
    from simpleaudit.context_marks import parse_documents
    from simpleaudit.model_auditor import ModelAuditor as MA

    for scenario in MA.get_scenarios("context_grounding"):
        marks = parse_documents(scenario.get("documents"))
        dated = [m for m in marks if m.valid_from or m.valid_until]
        if len(dated) < 2:
            continue
        for m in dated:
            if m.valid_until:
                # On the last day of the old window one document is current.
                day = date.fromordinal(m.valid_until.toordinal() - 1)
                assert any(current(x, day) for x in dated)


def test_otlp_json_ids_normalised_to_lowercase():
    from simpleaudit.tracing.otlp import parse_otlp_json

    tid = "ab" * 16
    spans = parse_otlp_json(json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": tid.upper(), "spanId": "CD" * 8, "parentSpanId": "EF" * 8, "name": "s"}]}]}]}))
    assert spans[0]["trace_id"] == tid
    assert spans[0]["span_id"] == "cd" * 8 and spans[0]["parent_span_id"] == "ef" * 8


def test_no_hardcoded_api_keys_in_tracked_sources():
    import re
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    files = subprocess.run(["git", "ls-files", "*.py", "*.md", "*.toml", "*.yml"], cwd=root,
                           capture_output=True, text=True).stdout.split()
    leaks = [f for f in files if re.search(r"sk-[A-Za-z0-9]{24,}", (root / f).read_text(errors="ignore"))]
    assert leaks == [], leaks


def test_file_uri_percent_escapes_fingerprint_by_content(tmp_path):
    from simpleaudit.experiment import _file_digest

    clip = tmp_path / "my clip.wav"
    clip.write_bytes(b"audio-1")
    digest = _file_digest(clip.as_uri())
    assert digest.startswith("sha256:")
    # The %20 form is what the ASR target opens; before the fix it was
    # fingerprinted by its URI string, so a regenerated clip reused stale runs.
    assert _file_digest(clip.as_uri().replace(" ", "%20")) == digest


# --- Pass 25 -------------------------------------------------------------


def test_resume_without_adaptive_sets_saved_adaptive_runs_aside(tmp_path):
    from tests.test_audit_regressions_pass6 import _exp, _run

    scen = [{"name": "a", "description": "d"}]
    _run(_exp(tmp_path, n_repetitions=2), scen)
    adaptive = tmp_path / "m" / "adaptive_0.json"
    adaptive.write_text((tmp_path / "m" / "run_0.json").read_text())
    _run(_exp(tmp_path, n_repetitions=2), scen)
    # Before: the file stayed on disk while the combined results left it out.
    assert not adaptive.exists()
    assert (tmp_path / "m" / "adaptive_0.json.stale").exists()


def test_inspect_severity_counts_between_steps_count_as_more_severe():
    pytest.importorskip("inspect_ai")
    from inspect_ai.scorer import SampleScore, Score

    from simpleaudit.integrations.inspect._task import severity_counts

    counts = severity_counts()([SampleScore(score=Score(value=3.7)),
                                SampleScore(score=Score(value=4.0))])
    assert counts["low"] == 1.0 and counts["pass"] == 1.0


@pytest.mark.parametrize("text, secret", [
    ("{'api_key': 'sk-proj-LEAKAPIKEY998877'}", "LEAKAPIKEY998877"),
    ('{"model": "gpt", "api_key": "sk-proj-LEAKAPIKEY998877"', "LEAKAPIKEY998877"),
    ('{"user": "bob", "password": "Hunter2-SECRET-xyz"} trailing', "Hunter2-SECRET-xyz"),
    ("export DB_PASSWORD=correcthorsebattery", "correcthorsebattery"),
    ("curl -u admin:SuperSecretPass http://h", "SuperSecretPass"),
])
def test_secrets_in_non_json_text_redacted(text, secret):
    from simpleaudit.tracing.selection import _redacted

    assert secret not in _redacted({"v": text})["v"]


@pytest.mark.parametrize("url, secret", [
    ("https://bob:hunter2/x@j/v1", "hunter2"),
    ("https://alice:S3cr3t@ss@h/v1", "S3cr3t@ss"),
    ("https://h/x?pwd=abc123&sid=9z9z&q=1", "abc123"),
])
def test_url_passwords_with_slash_or_at_and_short_param_names(url, secret):
    from simpleaudit.utils import redact_url_secrets

    out = redact_url_secrets(url)
    assert secret not in out and "ss@h" not in out.replace("[REDACTED]@h", "")


def test_url_path_with_at_sign_left_alone():
    from simpleaudit.utils import redact_url_secrets

    assert redact_url_secrets("https://h/users/@me") == "https://h/users/@me"


def test_local_audio_with_hash_in_name_not_attached_as_image():
    from simpleaudit.model_auditor import _image_attachments

    text, keep = _image_attachments("[file 1] hi", ["/x/clip#1.wav"])
    assert keep == [] and "not attached" in text


def test_off_ladder_default_severity_explains_itself():
    from simpleaudit.utils import UNGRADABLE_JUDGMENT_ISSUE

    out = ModelAuditor.parse_json_response('{"severity": "maybe", "issues_found": [], "summary": "x"}')
    assert out["severity"] == "ERROR" and out.get("parse_error") is True
    assert UNGRADABLE_JUDGMENT_ISSUE in out["issues_found"]


# --- Pass 26 -------------------------------------------------------------


def test_checklist_duplicate_entries_prefer_verified_quote_whatever_order():
    from simpleaudit.checklist import verify_checklist

    conv = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "Take 5 tablets now."}]
    good = {"index": 1, "status": "violated", "quote": "Take 5 tablets now."}
    bad = {"index": 1, "status": "violated", "quote": "words never said"}
    expectations = ["Must not advise overdose"]
    # Before: the first entry won, so the order decided whether it verified.
    for entries in ([bad, good], [good, bad]):
        assert verify_checklist(entries, expectations, conv)[0]["verified"] is True


def test_optional_violated_item_not_listed_as_issue():
    from simpleaudit.checklist import flatten

    item = {"index": 1, "expectation": "May mention the deadline", "required": False,
            "status": "violated", "quote": "x", "verified": True, "turn": 1}
    from simpleaudit.checklist import derive_severity

    out = flatten([item], derive_severity([item], "high"))
    assert out["issues_found"] == [] and out["recommendations"] == []


def test_saved_results_get_umask_mode(tmp_path):
    import os
    import stat

    from simpleaudit.results import AuditResults, _UMASK

    path = tmp_path / "r.json"
    AuditResults([]).save(str(path))
    # Before: 0600 from mkstemp, unreadable to CI or a shared directory.
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o666 & ~_UMASK


@pytest.mark.parametrize("text, secret", [
    ("output: sk-proj-AAAA1111BBBB2222CCCC3333 done", "AAAA1111BBBB2222CCCC3333"),
    ("token ghp_abcdefghijklmnopqrstuvwxyz0123 here", "ghp_abcdefghijklmnopqrstuvwxyz0123"),
    ("aws AKIAIOSFODNN7EXAMPLE key", "AKIAIOSFODNN7EXAMPLE"),
])
def test_provider_keys_in_free_text_redacted(text, secret):
    from simpleaudit.tracing.selection import _redacted, redact_tool_value

    assert secret not in _redacted({"v": text})["v"]
    assert secret not in redact_tool_value(text)


def test_provider_key_rule_leaves_prose_alone():
    from simpleaudit.tracing.selection import _redacted

    for prose in ("task-management is fine", "a risk-assessment-framework-document-v2"):
        assert _redacted({"v": prose})["v"] == prose


def test_config_redaction_covers_connection_strings_and_short_names():
    from simpleaudit.experiment import _redact_secrets

    out = _redact_secrets({"connection_string": "Server=x;Password=hunter2", "dsn": "d",
                           "pwd": "p", "note": "Server=x;Password=hunter2", "session_ttl": 30})
    assert "hunter2" not in json.dumps(out)
    assert out["session_ttl"] == 30


def test_fully_cached_experiment_not_reported_cancelled(tmp_path):
    import asyncio as _asyncio
    import threading

    from tests.test_audit_regressions_pass6 import _exp, _run

    scen = [{"name": "a", "description": "d"}]
    _run(_exp(tmp_path, n_repetitions=2), scen)
    event = threading.Event()
    event.set()
    res, calls = _run(_exp(tmp_path, n_repetitions=2, cancel_event=event), scen)
    assert calls == []
    assert not getattr(res, "cancelled", False)


def test_changed_model_set_sets_old_combined_file_aside(tmp_path):
    from simpleaudit.experiment import AuditExperiment

    def exp(models):
        return AuditExperiment(models=[{"model": m, "provider": "openai"} for m in models],
                               judge_model="j", judge_provider="openai", save_dir=str(tmp_path),
                               show_progress=False)

    from tests.test_audit_regressions_pass6 import _run
    scen = [{"name": "a", "description": "d"}]
    _run(exp(["m1", "m2"]), scen)
    _run(exp(["m1"]), scen)
    assert (tmp_path / "experiment_results.json.stale").exists()


def test_parallel_reps_run_concurrently_and_keep_rep_order():
    import asyncio as _asyncio
    import time as _time

    from simpleaudit.experiment import AuditExperiment

    exp = AuditExperiment(models=[{"model": "m", "provider": "openai"}], judge_model="j",
                          judge_provider="openai", show_progress=False, n_repetitions=4,
                          max_parallel_reps=4)
    order = []

    async def fake_rep(merged, scenarios, *a, **kw):
        n = len(order)
        order.append(n)
        await _asyncio.sleep(0.2)
        return AuditResults([AuditResult(scenario_name=f"rep{n}", scenario_description="d",
                                         conversation=[], severity="pass", issues_found=[],
                                         positive_behaviors=[], summary="", recommendations=[])])

    with patch.object(AuditExperiment, "_run_single_rep", new=fake_rep), \
         patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        start = _time.perf_counter()
        out = _asyncio.run(exp.run_scenario_reps(0, {"name": "s", "description": "d"}))
        elapsed = _time.perf_counter() - start
    assert elapsed < 0.6  # four 0.2 s reps at once, not 0.8 s in sequence
    assert [r.scenario_name for r in out] == ["rep0", "rep1", "rep2", "rep3"]


def test_max_parallel_reps_validated():
    from simpleaudit.experiment import AuditExperiment

    with pytest.raises(ValueError, match="max_parallel_reps"):
        AuditExperiment(models=[{"model": "m", "provider": "openai"}], judge_model="j",
                        judge_provider="openai", show_progress=False, max_parallel_reps=0)


def test_probe_empty_after_think_stripping_is_retried():
    replies = iter(["<think>planning the next question</think>", "And which planet is largest?"])
    a = _auditor()
    a.max_retries = 1
    a.target_client = FakeClient(lambda **k: "Mars.")
    a.auditor_client = FakeClient(lambda **k: next(replies))
    a.judge_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    res = a.run([{"name": "s", "description": "d", "test_prompt": "Name a planet."}], max_turns=2)[0]
    # Before: ERROR "Auditor produced an empty probe" with retries left.
    assert res.severity == "pass", res.judgment
    assert res.conversation[2]["content"] == "And which planet is largest?"


def test_sync_run_closes_owned_clients_and_next_run_rebuilds():
    closed = []

    class Inner:
        async def close(self):
            closed.append(1)

    a = _auditor()
    owned = MagicMock()
    owned.client = Inner()
    a._owned_clients = {"judge": owned}
    a.target_client = FakeClient(lambda **k: "ok")
    a.judge_client = owned
    owned.acompletion = FakeClient(lambda **k: json.dumps({"severity": "pass"})).acompletion
    a.run([{"name": "s", "description": "d", "test_prompt": "q"}], max_turns=1)
    assert closed == [1]
