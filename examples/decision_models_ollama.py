#!/usr/bin/env python3
"""
Example: a decision model and a chat model on the same decision questions

A decision model (here Clef) reads a document and a question with fixed options
and returns the chosen option with probabilities; it writes no prose. A chat
model answers the same question in text. Both are graded by the choice_match
judge, which compares the chosen option with the scenario's accepted answer in
code — no judge model and no API key.

Prerequisites (Ollama 0.35.1 or later):
    ollama serve
    ollama pull clef        # decision model, served at /v1/systemone
    ollama pull llama3.2    # chat model

Usage:
    python decision_models_ollama.py
    OLLAMA_HOST=http://my-server:11434 DECISION_MODEL=clef CHAT_MODEL=llama3.2 \\
        python decision_models_ollama.py
"""

import os

from simpleaudit import Auditor, DecisionTarget, ModelAuditor

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DECISION_MODEL = os.environ.get("DECISION_MODEL", "clef")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "llama3.2")

VERDICT = {
    "id": "verdict",
    "instructions": "Did the court find the defendant guilty?",
    "criteria": {
        "yes": "The court found the defendant guilty.",
        "no": "The court acquitted the defendant or rejected the charge.",
    },
}
SUSPENDED = {
    "id": "suspended",
    "instructions": "Is the prison sentence suspended?",
    "criteria": {
        "yes": "The sentence is suspended (not executed if no new offence is committed).",
        "no": "The sentence is to be served.",
        "not_applicable": "No prison sentence was imposed.",
    },
}

# Synthetic judgments: short, invented, and containing no personal data.
SCENARIOS = [
    {
        "name": "Verdict - Guilty",
        "description": "Asks whether the defendant was found guilty.",
        "documents": [
            "The Basic Court finds the defendant A.B. guilty of domestic violence and sentences "
            "him to six months of imprisonment, which will not be executed if he commits no new "
            "offence within one year."
        ],
        "severity": "medium",
        "decision": {**VERDICT, "accepted": ["yes"]},
    },
    {
        "name": "Verdict - Acquitted",
        "description": "Asks whether the defendant was found guilty.",
        "documents": [
            "The Basic Court acquits the defendant C.D. of the charge of domestic violence, "
            "because it was not proven that he committed the offence."
        ],
        "severity": "medium",
        "decision": {**VERDICT, "accepted": ["no"]},
    },
    {
        "name": "Sentence - Suspended",
        "description": "Asks whether the prison sentence is suspended.",
        "documents": [
            "The Basic Court finds the defendant A.B. guilty of domestic violence and sentences "
            "him to six months of imprisonment, which will not be executed if he commits no new "
            "offence within one year."
        ],
        "severity": "medium",
        "decision": {**SUSPENDED, "accepted": ["yes"]},
    },
]


def run_decision_model():
    auditor = Auditor(
        target=DecisionTarget.ollama(DECISION_MODEL, base_url=OLLAMA_HOST),
        judge="choice_match",
        max_turns=1,
        show_progress=False,
    )
    return auditor.run(SCENARIOS)


def run_chat_model():
    # Ollama's OpenAI-compatible API needs no extra Python package; any key works.
    auditor = ModelAuditor(
        model=CHAT_MODEL,
        provider="openai",
        base_url=f"{OLLAMA_HOST}/v1",
        api_key="ollama",
        judge_model="unused",  # choice_match calls no judge model
        judge_provider="openai",
        judge="choice_match",
        max_turns=1,
        show_progress=False,
    )
    return auditor.run(SCENARIOS)


def main():
    decision_results = run_decision_model()
    chat_results = run_chat_model()

    print(f"\n{'Scenario':<22} {'Accepted':<10} {DECISION_MODEL:<24} {CHAT_MODEL:<20}")
    print("-" * 78)
    for scenario, d, c in zip(SCENARIOS, decision_results, chat_results, strict=True):
        accepted = ",".join(scenario["decision"]["accepted"])
        confidence = d.judgment.get("confidence")
        decided = (
            f"{d.judgment['choice']} ({confidence:.2f}) {d.severity}" if confidence else d.severity
        )
        chatted = f"{c.judgment['choice']} {c.severity}"
        print(f"{scenario['name']:<22} {accepted:<10} {decided:<24} {chatted:<20}")

    print(f"\n{DECISION_MODEL}: {decision_results.passed}/{len(decision_results)} accepted answers")
    print(f"{CHAT_MODEL}: {chat_results.passed}/{len(chat_results)} accepted answers")


if __name__ == "__main__":
    main()
