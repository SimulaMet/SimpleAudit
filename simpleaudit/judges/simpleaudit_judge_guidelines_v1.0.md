# SimpleAudit Judge Configuration Guidelines

Standards and best practices for creating judge configurations for [simpleaudit](https://github.com/kelkalot/simpleaudit).

---

## What is a Judge Config?

A judge config defines two things:

1. **`probe_prompt`** — the system prompt that controls how adversarial probes are generated (the "attacker" persona)
2. **`judge_prompt`** — the system prompt that controls how conversations are evaluated (the "judge" criteria and output format)

Together they let you swap out the entire evaluation logic without changing any code.

---

## Schema

Each judge config is a Python dict with the following structure:

```python
{
    "name": "Human-readable name",
    "description": "One-sentence description of what this judge evaluates",

    # Optional — if omitted, the default red-team probe is used
    "probe_prompt": "System prompt for the probe generator...",

    # Required — system prompt for the judge LLM
    "judge_prompt": "System prompt for the judge LLM...",

    # Documents the expected JSON output fields
    "output_schema": {
        "field_name": "type and description",
        ...
    },

    # Academic or technical source this config is based on
    "source": {
        "paper": "Full paper title",
        "authors": "Author et al.",
        "year": 2024,
        "url": "https://arxiv.org/abs/...",
        "notes": "How the source maps to this config",
    },

    # Provenance
    "metadata": {
        "author": "your-name",
        "version": "1.0",
        "date_created": "YYYY-MM-DD",
    },
}
```

---

## Output Schema Design

### Always request JSON output

The judge prompt must always end with an instruction to return valid JSON only:

```
Output valid JSON only, no markdown code blocks.
```

### Numeric scores

Use integer or float scores on a consistent scale. Include the range in the prompt and output_schema:

```python
"score": "int (1–10) — overall quality score"
```

### Categorical fields

Define the exact allowed values in the prompt and output_schema:

```python
"verdict": "str — one of: accurate | partially_accurate | inaccurate"
```

### Compatibility with the visualiser

The SimpleAudit visualiser detects the schema type automatically:

- **Default schema** (severity-based): include a `"severity"` field with values `critical | high | medium | low | pass`. The visualiser renders pass rates and severity bars.
- **Custom schema**: any output that does **not** include a `"severity"` field. The visualiser renders a purple-themed key-value panel and uses the first numeric field as the score.

If you want the default visualiser rendering, include `"severity"` in your output schema.

---

## Probe Prompt Guidelines

The probe prompt replaces the default red-team adversary persona. It controls how the judge model generates test messages sent to the target model.

**Good probe prompts:**
- Define a realistic attacker persona relevant to the evaluation goal
- Specify what kinds of inputs to generate (e.g. factual questions, harmful requests, edge cases)
- Instruct the model to behave naturally, not obviously as a tester
- End with: `Output ONLY the next user message, nothing else.`

**What to avoid:**
- Vague instructions that don't align with the judge criteria
- Prompts that telegraph the test to the target model
- Overly aggressive tone that produces unrealistic probes

---

## Judge Prompt Guidelines

The judge prompt replaces the default safety evaluation criteria. It controls how conversations are evaluated.

**Good judge prompts:**
- Clearly define the evaluation criteria with named dimensions
- Specify a scoring rubric with anchors at key points (e.g. 1, 5, 10)
- Define the exact output schema in the prompt body
- Reference the source methodology if applicable
- End with: `Output valid JSON only, no markdown code blocks.`

**What to avoid:**
- Vague criteria without clear anchors ("rate the quality of the response")
- Mixing multiple unrelated evaluation goals in one judge
- Output schemas with ambiguous or overlapping field definitions

---

## Built-in Judges

| Name | Basis | Output fields |
|------|-------|---------------|
| `safety` | Constitutional AI (Bai et al., 2022) | severity, issues_found, positive_behaviors, summary, recommendations |
| `abstention` | AbstentionBench (Kirichenko et al., 2025) | score, abstained, appropriate, category, reasoning |
| `helpfulness` | MT-Bench (Zheng et al., 2023) | score, relevance, accuracy, clarity, completeness, feedback |
| `factuality` | G-Eval (Liu et al., 2023) | score, verdict, factual_errors, unsupported_claims, reasoning |
| `harm` | HELM Safety (Liang et al., 2022) | harmful, category, severity, explanation |

---

## Creating a Custom Judge

### Option 1: Inline (one-off)

Pass prompts directly to `ModelAuditor`:

```python
auditor = ModelAuditor(
    ...,
    probe_prompt="You are probing for X...",
    judge_prompt="You are evaluating Y. Output JSON only.",
)
```

### Option 2: Named config (reusable)

Create a file in `simpleaudit/judges/` and register it:

```python
# simpleaudit/judges/my_judge.py
MY_JUDGE = {
    "name": "My Custom Judge",
    "description": "Evaluates ...",
    "probe_prompt": "...",
    "judge_prompt": "...",
    "output_schema": {...},
    "source": {...},
    "metadata": {"author": "you", "version": "1.0", "date_created": "2026-01-01"},
}
```

Then register it in `simpleaudit/judges/__init__.py`:

```python
from .my_judge import MY_JUDGE

JUDGE_CONFIGS = {
    ...,
    "my_judge": MY_JUDGE,
}
```

Use it:

```python
auditor = ModelAuditor(..., judge="my_judge")
```

---

## Precedence Rules

When multiple judge-related parameters are set, the following precedence applies:

```
explicit judge_prompt / probe_prompt  >  named judge config  >  default behaviour
```

This means:
- If you pass `judge_prompt="..."` directly, it always wins — even if you also pass `judge="helpfulness"`
- If you pass `judge="factuality"` with no `judge_prompt`, the factuality config is used
- If neither is set, the default safety evaluation behaviour is used

---

## Source and Citation

Judge configs are based on published research. Always include a `source` block with:
- Full paper title
- Authors and year
- arXiv or DOI URL
- Notes explaining how the source maps to the config

This helps users understand the evaluation methodology and cite the correct papers in their work.
