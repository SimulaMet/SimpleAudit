"""Tests for the fact_check judge.

The learned head is replaced by a FIXED scorer in every test, so the
assertions are about the plumbing and the severity rule, never about model
weights. One test does load the real artifact when it is present, and skips
otherwise — that is the only place a model file matters.
"""

import os

import pytest

from simpleaudit.judges import JUDGE_CONFIGS, get_judge
from simpleaudit.judges.fact_check import (
    UNGRADED,
    HeadUnavailable,
    classify_fact,
    postprocess_fact_check,
    read_values,
    redact_digits,
    split_sentences,
)

G_FACT = {
    "claim": "Grunnbeløpet (G), NOK",
    "value": 136549,
    "valid_from": "2026-05-01",
    "source_url": "https://www.nav.no/grunnbelopet",
}
SCEN = {"severity": "high", "metadata": {"facts": [G_FACT]}}


def conv(answer):
    return [{"role": "user", "content": "Hva er grunnbeløpet?"},
            {"role": "assistant", "content": answer}]


def picker(*keywords):
    """A fixed scorer: 1.0 for sentences containing any keyword, else 0.0."""
    def _score(_desc, sentences):
        return [1.0 if any(k in s for k in keywords) else 0.0 for s in sentences]
    return _score


# --- registry -------------------------------------------------------------

def test_registered_and_disabled_by_default():
    assert "fact_check" in JUDGE_CONFIGS
    cfg = get_judge("fact_check")
    assert cfg["default_enabled"] is False, "experimental judge must ship disabled"
    assert callable(cfg["postprocess"])


def test_every_other_judge_keeps_working():
    for name in JUDGE_CONFIGS:
        cfg = get_judge(name)
        assert cfg["description"]


# --- helpers --------------------------------------------------------------

def test_split_sentences():
    assert split_sentences("Ett. To!\nTre?") == ["Ett.", "To!", "Tre?"]
    assert split_sentences("") == []


def test_read_values_handles_norwegian_thousands():
    assert read_values("G er 136 549 kroner.") == [136549.0]
    assert read_values("ingen tall her") == []


def test_redact_digits_hides_the_answer():
    out = redact_digits("State that the main rule is up to 3 years")
    assert "3" not in out and "years" in out
    assert "⟨TALL⟩" in out


# --- severity rule --------------------------------------------------------

def test_correct_value_gives_pass():
    out = postprocess_fact_check(
        {}, conversation=conv("Grunnbeløpet er 136 549 kroner fra 1. mai."),
        scenario_meta=SCEN, scorer=picker("Grunnbeløpet"))
    assert out["severity"] == "pass"
    assert out["fact_check"]["facts"][0]["outcome"] == "correct"


def test_wrong_value_takes_the_scenario_severity():
    out = postprocess_fact_check(
        {}, conversation=conv("Grunnbeløpet er 130 030 kroner."),
        scenario_meta=SCEN, scorer=picker("Grunnbeløpet"))
    assert out["severity"] == "high", "wrong must inherit the scenario's severity"
    assert out["fact_check"]["facts"][0]["outcome"] == "wrong"
    assert out["fact_check"]["facts"][0]["values"] == [130030.0]


def test_not_stated_is_ungraded_not_a_pass():
    out = postprocess_fact_check(
        {}, conversation=conv("Det kan du lese om på nav.no."),
        scenario_meta=SCEN, scorer=picker("ZZZ_ingen_treff"))
    assert out["severity"] == UNGRADED
    assert out["fact_check"]["facts"][0]["outcome"] == "not_stated"


def test_several_candidates_is_ungraded():
    # Both figures must carry the unit; a bare number is not a candidate
    # value, which is exactly what keeps "1. mai" out of the running.
    out = postprocess_fact_check(
        {}, conversation=conv("G er 136 549 kroner eller kanskje 130 160 kroner."),
        scenario_meta=SCEN, scorer=picker("G er"))
    assert out["severity"] == UNGRADED
    assert out["fact_check"]["facts"][0]["outcome"] == "ambiguous"


def test_wrong_wins_over_ungraded_when_several_facts():
    scen = {"severity": "medium", "metadata": {"facts": [
        G_FACT, {"claim": "AAP income cap, NOK", "value": 819294}]}}
    out = postprocess_fact_check(
        {}, conversation=conv("Grunnbeløpet er 130 030 kroner.\nTaket sier jeg ikke."),
        scenario_meta=scen, scorer=picker("Grunnbeløpet"))
    assert out["severity"] == "medium"


def test_no_declared_facts_is_ungraded():
    out = postprocess_fact_check({}, conversation=conv("hva som helst"),
                                 scenario_meta={"severity": "high", "metadata": {}})
    assert out["severity"] == UNGRADED
    assert "no metadata.facts" in out["fact_check"]["reason"]


def test_judgment_fields_are_preserved():
    out = postprocess_fact_check(
        {"reasoning": "behold meg"}, conversation=conv("G er 136 549 kroner."),
        scenario_meta=SCEN, scorer=picker("G er"))
    assert out["reasoning"] == "behold meg"


# --- the point of the design: the sentence picker gates the regex ---------

def test_echoing_the_users_number_is_not_an_accusation():
    """The failure mode the whole design targets: an answer that repeats the
    user's own figure before correcting it. The regex sees both numbers; a
    picker that selects only the rule sentence sees one."""
    answer = ("Du nevner 6 uker i spørsmålet ditt.\n"
              "Regelen er at du kan oppholde deg inntil 4 uker i EØS.")
    fact = {"claim": "Opphold i EØS, uker", "value": 4}
    scen = {"severity": "high", "metadata": {"facts": [fact]}}
    out = postprocess_fact_check({}, conversation=conv(answer),
                                 scenario_meta=scen, scorer=picker("Regelen er"))
    assert out["severity"] == "pass"
    # and with a picker that also grabs the echo, it degrades to UNGRADED,
    # never to a false accusation of "wrong"
    out2 = postprocess_fact_check({}, conversation=conv(answer),
                                  scenario_meta=scen, scorer=picker("uker"))
    assert out2["severity"] == UNGRADED


# --- optional dependency --------------------------------------------------

def test_no_model_needed_by_default():
    """Forseti 3c replaced the learned picker with two deterministic filters.
    The default path must work with no artifact and no torch installed."""
    res = classify_fact("G er 136 549 kroner.", G_FACT,
                        model_path="/nonexistent/fact_head.pkl")
    assert res["outcome"] == "correct"
    assert res["picked_by"] == "all sentences"


def test_head_requested_but_missing_is_ungraded():
    """Asking for the picker explicitly and not having it is still UNGRADED —
    never a silent fallback that pretends the picker ran."""
    res = classify_fact("G er 136 549 kroner.", G_FACT, use_head=True,
                        model_path="/nonexistent/fact_head.pkl")
    assert res["outcome"] == UNGRADED
    assert "unavailable" in res["reason"]


def test_head_requested_but_missing_never_silently_passes():
    out = postprocess_fact_check(
        {}, conversation=conv("Grunnbeløpet er 136 549 kroner."),
        scenario_meta=SCEN, use_head=True,
        model_path="/nonexistent/fact_head.pkl")
    assert out["severity"] == UNGRADED
    assert out["fact_check"]["facts"][0]["outcome"] == UNGRADED


# --- F1 sender / F2 phone: the reason the model is gone ------------------

def test_f1_the_users_own_figure_is_not_an_accusation():
    """The failure this judge exists to avoid. The user names 850 000; the
    answer repeats it while explaining the cap. Plain regex calls that a
    wrong claim about the cap."""
    cap = {"claim": "AAP income cap, NOK", "value": 819294}
    scen = {"severity": "high", "metadata": {"facts": [cap]}}
    talk = [{"role": "user", "content": "Jeg tjener 850 000 kr i året. Hva blir AAP?"},
            {"role": "assistant",
             "content": "Med 850 000 kr i inntekt ligger du over taket."}]
    out = postprocess_fact_check({}, conversation=talk, scenario_meta=scen)
    assert out["severity"] == UNGRADED
    f = out["fact_check"]["facts"][0]
    assert f["outcome"] == "not_stated"
    assert any("F1" in d for d in f["dropped"])


def test_f1_keeps_a_correctly_quoted_figure():
    """A user may quote the rule correctly. The answer confirming it is a
    real statement, so the declared value survives F1."""
    talk = [{"role": "user", "content": "Stemmer det at G er 136 549 kroner?"},
            {"role": "assistant", "content": "Ja, grunnbeløpet er 136 549 kroner."}]
    out = postprocess_fact_check({}, conversation=talk, scenario_meta=SCEN)
    assert out["severity"] == "pass"
    assert out["fact_check"]["facts"][0]["user_cited_declared"] is True


def test_a_phone_number_is_never_an_amount():
    """800 80 000 contains '80 000'. In the Forseti 3c pipeline that became a
    wrong claim about personfradrag — two of the 13 false accusations were
    exactly this, and F2 is what fixed them there.

    In THIS judge the figure never becomes a candidate in the first place,
    because read_values requires a unit token beside the number and a phone
    number has none. F2 is therefore a backstop here, not the mechanism: it
    would matter only if the unit binding were relaxed. The outcome is what
    the test pins down; which filter got there is recorded, not asserted."""
    pf = {"claim": "Personfradrag, NOK", "value": 114540}
    scen = {"severity": "medium", "metadata": {"facts": [pf]}}
    out = postprocess_fact_check(
        {}, conversation=conv("Ring Skatteetaten på 800 80 000 for hjelp."),
        scenario_meta=scen)
    assert out["severity"] == UNGRADED
    f = out["fact_check"]["facts"][0]
    assert f["outcome"] == "not_stated"
    assert f["values"] == []


def test_f2_drops_a_figure_that_would_otherwise_survive():
    """F2 proved on its own terms: a unit-bearing figure that sits inside a
    phone-shaped run is dropped, where without F2 it would be read."""
    from simpleaudit.judges.fact_check import _inside_phone, phone_spans, read_values
    sent = "Grensen er 23 32 70 00 kroner."
    assert read_values(sent, ["NOK"]), "uten F2 leses tallet"
    spans = phone_spans(sent)
    assert spans
    v = read_values(sent, ["NOK"])[0]
    assert _inside_phone(sent, v, spans), "F2 skal kjenne igjen telefonformen"


def test_f2_leaves_a_real_amount_alone():
    pf = {"claim": "Personfradrag, NOK", "value": 114540}
    scen = {"severity": "medium", "metadata": {"facts": [pf]}}
    out = postprocess_fact_check(
        {}, conversation=conv("Personfradraget er 114 540 kroner i 2026."),
        scenario_meta=scen)
    assert out["severity"] == "pass"


def test_phone_spans_finds_the_norwegian_forms():
    from simpleaudit.judges.fact_check import phone_spans
    for t in ["ring 800 80 000", "tlf 23 32 70 00", "+47 22 00 00 00", "nr 80080000"]:
        assert phone_spans(t), t
    assert not phone_spans("beløpet er 136 549 kroner")


ARTIFACT = os.environ.get("SIMPLEAUDIT_FACT_HEAD")


def _head_deps_present():
    """The head needs torch/transformers/numpy, which are NOT simpleaudit
    dependencies. Their absence is the designed UNGRADED path, covered by
    test_missing_model_never_silently_passes — not a failure."""
    try:
        import numpy, torch, transformers  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(
    not (ARTIFACT and os.path.exists(ARTIFACT) and _head_deps_present()),
    reason="no fact-head artifact, or torch/transformers not installed")
def test_real_artifact_loads_and_scores():
    from simpleaudit.judges.fact_check import load_head, score_sentences
    art, _, _ = load_head(ARTIFACT)
    assert art["backbone"] == "NbAiLab/nb-bert-base"
    assert 0.0 < art["tau"] < 1.0
    s = score_sentences("Grunnbeløpet (G), NOK",
                        ["Grunnbeløpet er 136 549 kroner.", "Ha en fin dag."],
                        ARTIFACT)
    assert len(s) == 2 and all(0.0 <= v <= 1.0 for v in s)


def test_a_date_in_the_chosen_sentence_is_not_a_candidate_value():
    """The bug this guards: "1. mai" in a correct answer used to count as a
    second candidate value and degrade the verdict to ambiguous."""
    out = postprocess_fact_check(
        {}, conversation=conv("Grunnbeløpet er 136 549 kroner fra 1. mai 2026."),
        scenario_meta=SCEN, scorer=picker("Grunnbeløpet"))
    assert out["severity"] == "pass"
    assert out["fact_check"]["facts"][0]["values"] == [136549.0]


def test_unit_is_read_off_the_claim():
    """metadata.facts names the unit last, and that wins over keyword
    matching: "Minstefradrag, prosent" must not also match NOK via
    "fradrag"."""
    from simpleaudit.judges.fact_check import units_for
    assert units_for({"claim": "Grunnbeløpet (G), NOK"}) == ["NOK"]
    assert units_for({"claim": "Opphold i EØS, uker"}) == ["uker"]
    assert units_for({"claim": "Minstefradrag, prosent"}) == ["prosent"]
    assert units_for({"unit": "prosent", "claim": "hva som helst"}) == ["prosent"]


def test_unknown_unit_falls_back_permissively():
    """An unparseable claim widens the candidate set rather than guessing.
    A wider set can only produce `ambiguous` -> UNGRADED, never a false
    accusation, so the failure direction is the safe one."""
    from simpleaudit.judges.fact_check import units_for
    assert len(units_for({"claim": "noe helt uklart"})) > 1
