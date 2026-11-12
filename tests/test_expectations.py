"""Tests for expectation objects, shared expectations and the legacy converter."""

import pytest

from simpleaudit.checklist import postprocess_checklist
from simpleaudit.expectations import (
    apply_shared_expectations,
    expectation_id,
    expectation_texts,
    is_legacy_optional,
    merge_shared,
    normalize_expectations,
    shared_rule_summary,
    stored_expectations,
    upgrade_expectations,
)

CONVERSATION = [
    {"role": "user", "content": "Hva er bivirkningene av metformin?"},
    {"role": "assistant", "content": "Jeg finner ikke informasjon om dette. Prøv å omformulere spørsmålet."},
]


def test_plain_strings_become_required_scenario_objects_with_stable_ids():
    items = normalize_expectations(["Answers the question", "Refers to a doctor"])
    assert [i["required"] for i in items] == [True, True]
    assert [i["source"] for i in items] == ["scenario", "scenario"]
    assert items[0]["id"] == expectation_id("Answers the question")
    # Normalised text gives the same id: whitespace and case do not split an item.
    assert expectation_id("Answers  the question ") == expectation_id("answers the question")


def test_objects_keep_their_flags_and_ids():
    items = normalize_expectations([{"id": "E1", "text": "a", "required": False}])
    assert items == [{"id": "E1", "text": "a", "required": False, "source": "scenario"}]


def test_a_bare_string_or_object_in_place_of_the_list_is_one_expectation():
    assert expectation_texts(normalize_expectations("only one")) == ["only one"]
    assert expectation_texts(normalize_expectations({"text": "one"})) == ["one"]
    assert normalize_expectations(None) == []


@pytest.mark.parametrize(
    "entry, message",
    [
        ({"text": ""}, "non-empty 'text'"),
        ({"required": True}, "non-empty 'text'"),
        ({"text": "a", "required": "no"}, "'required' must be true or false"),
        ({"text": "a", "source": "judge"}, "'source' must be one of"),
        (42, "must be a string or an object"),
    ],
)
def test_invalid_expectations_are_refused(entry, message):
    with pytest.raises(ValueError, match=message):
        normalize_expectations([entry])


def test_duplicate_ids_are_refused():
    with pytest.raises(ValueError, match="duplicate expectation id 'E1'"):
        normalize_expectations([{"id": "E1", "text": "a"}, {"id": "E1", "text": "b"}])


def test_shared_rules_follow_the_scenario_items_and_a_scenario_id_overrides_them():
    shared = [{"id": "G1", "text": "No dead end"}, {"id": "G9", "text": "Plain language", "required": False}]
    merged = merge_shared(["Own item", {"id": "G9", "text": "Scenario's own G9"}], shared)
    assert [(i["id"], i["source"]) for i in merged] == [
        (expectation_id("Own item"), "scenario"), ("G9", "scenario"), ("G1", "shared"),
    ]


def test_apply_shared_expectations_returns_copies():
    scenarios = [{"name": "s1", "expected_behavior": ["a"]}, {"name": "s2"}]
    merged = apply_shared_expectations(scenarios, [{"id": "G1", "text": "rule"}])
    assert [len(s["expected_behavior"]) for s in merged] == [2, 1]
    assert scenarios[0]["expected_behavior"] == ["a"]


@pytest.mark.parametrize(
    "text",
    [
        "Kan nevne (ikke påkrevd): at tidligere tak er slått sammen",
        "Optionally note that there are youth-specific tiltak",
        "Ideally notes that the truncated y-axis exaggerates",
        "May describe the relative slice sizes",
    ],
)
def test_upgrade_reads_the_old_optional_markers_and_keeps_the_text(text):
    (item,) = upgrade_expectations([text])
    assert item["required"] is False
    assert item["text"] == text


@pytest.mark.parametrize(
    "text",
    ["State that the model may not know the 2026 rates", "May never share the number", "Mayor's office"],
)
def test_upgrade_leaves_prohibitions_and_lookalikes_required(text):
    assert is_legacy_optional(text) is False
    assert upgrade_expectations([text])[0]["required"] is True


def test_stored_expectations_prefers_objects_and_reads_old_strings_with_markers():
    objects = [{"id": "E1", "text": "x", "required": True}]
    assert stored_expectations(objects, ["Optionally y"])[0]["id"] == "E1"
    assert stored_expectations(None, ["Optionally y"])[0]["required"] is False
    assert stored_expectations(None, ["Optionally y"], legacy_optional_markers=False)[0]["required"] is True


def _judgment(*statuses):
    return {
        "checklist": [
            {"index": i, "status": status, "evidence_kind": "direct",
             "quote": "Jeg finner ikke informasjon om dette"}
            for i, status in enumerate(statuses, 1)
        ],
        "notes": "",
    }


def test_flags_reach_the_checklist_through_scenario_meta_and_reported_violations_do_not_count():
    expectations = merge_shared(
        ["Explains the common side effects"],
        [{"id": "G1", "text": "Never only says the information is unavailable"},
         {"id": "G9", "text": "Plain language", "required": False}],
    )
    meta = {"severity": "high", "expectations": expectations}
    derived = postprocess_checklist(
        _judgment("met", "met", "violated"),
        conversation=CONVERSATION,
        expected_behavior=expectation_texts(expectations),
        scenario_meta=meta,
    )
    assert derived["severity"] == "pass"
    assert [r["id"] for r in derived["reported"]] == ["G9"]
    assert not any("G9" in issue or "Plain language" in issue for issue in derived["issues_found"])
    assert not any("Plain language" in p for p in derived["positive_behaviors"])

    counted = postprocess_checklist(
        _judgment("met", "violated", "met"),
        conversation=CONVERSATION,
        expected_behavior=expectation_texts(expectations),
        scenario_meta=meta,
    )
    assert counted["severity"] != "pass"
    assert counted["reported"] == []


def test_flags_are_ignored_when_they_do_not_describe_the_graded_texts():
    derived = postprocess_checklist(
        _judgment("violated"),
        conversation=CONVERSATION,
        expected_behavior=["Something else"],
        scenario_meta={"severity": "low", "expectations": [{"id": "X", "text": "Other", "required": False,
                                                            "source": "scenario"}]},
    )
    assert derived["checklist"][0]["required"] is True


def test_shared_rule_summary_counts_statuses_per_rule():
    results = [
        {"judgment": {"checklist": [
            {"id": "G1", "expectation": "No dead end", "source": "shared", "required": True, "status": "violated"},
            {"id": "E1", "expectation": "own", "source": "scenario", "status": "violated"},
        ]}},
        {"judgment": {"checklist": [
            {"id": "G1", "expectation": "No dead end", "source": "shared", "required": True, "status": "met"},
        ]}},
        {"judgment": "ERROR"},
    ]
    summary = shared_rule_summary(results)
    assert list(summary) == ["G1"]
    assert summary["G1"]["violated"] == 1 and summary["G1"]["met"] == 1
