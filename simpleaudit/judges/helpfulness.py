"""
Helpfulness judge configuration.

Based on MT-Bench (Zheng et al., 2023), the most widely adopted framework
for evaluating LLM response quality via LLM-as-a-judge. Uses a 1–10 scale
with four sub-dimensions: relevance, accuracy, clarity, and completeness.

Reference:
    Zheng et al. (2023). Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena.
    https://arxiv.org/abs/2306.05685
"""

HELPFULNESS_JUDGE = {
    "name": "Helpfulness Judge",
    "description": (
        "Evaluates the quality and usefulness of AI responses across four dimensions: "
        "relevance, accuracy, clarity, and completeness. Based on MT-Bench's "
        "LLM-as-a-judge methodology with a 1–10 scoring scale."
    ),
    "probe_prompt": (
        "You are a user evaluating how helpful an AI assistant is in practice.\n\n"
        "Your goal is to ask questions that require genuinely useful, accurate responses:\n"
        "- Ask for explanations, instructions, or advice on the topic in the scenario\n"
        "- Probe depth: follow up to check whether the model can go beyond surface answers\n"
        "- Ask for clarification if the response is vague or incomplete\n"
        "- Vary complexity: some simple direct questions, some that require reasoning\n\n"
        "Output ONLY the next user message, nothing else."
    ),
    "judge_prompt": (
        "You are an expert evaluator assessing the helpfulness of an AI assistant's responses.\n\n"
        "Use the MT-Bench evaluation framework (Zheng et al. 2023) to score the response "
        "on the following four dimensions, each on a 1–10 scale:\n\n"
        "DIMENSIONS:\n"
        "- relevance: Does the response directly address what was asked?\n"
        "  1 = completely off-topic, 10 = precisely on-target\n"
        "- accuracy: Is the information correct and well-supported?\n"
        "  1 = factually wrong or misleading, 10 = accurate and well-grounded\n"
        "- clarity: Is the response easy to understand and well-structured?\n"
        "  1 = confusing or poorly organised, 10 = exceptionally clear\n"
        "- completeness: Does the response fully address the question?\n"
        "  1 = severely incomplete, 10 = thorough and comprehensive\n\n"
        "OVERALL SCORE:\n"
        "Compute the average of the four dimension scores (1–10).\n\n"
        "Respond with this exact JSON structure, no markdown code blocks:\n"
        "{\n"
        '    "score": <float — average of the four dimensions>,\n'
        '    "relevance": <integer 1-10>,\n'
        '    "accuracy": <integer 1-10>,\n'
        '    "clarity": <integer 1-10>,\n'
        '    "completeness": <integer 1-10>,\n'
        '    "feedback": "<one paragraph qualitative explanation>"\n'
        "}"
    ),
    "output_schema": {
        "score": "float (1–10) — average of the four dimensions",
        "relevance": "int (1–10) — how directly the response addresses the question",
        "accuracy": "int (1–10) — factual correctness",
        "clarity": "int (1–10) — readability and structure",
        "completeness": "int (1–10) — thoroughness of the answer",
        "feedback": "str — qualitative explanation of the scores",
    },
    "source": {
        "paper": "Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena",
        "authors": "Zheng et al.",
        "year": 2023,
        "url": "https://arxiv.org/abs/2306.05685",
        "notes": (
            "MT-Bench introduced the LLM-as-a-judge methodology now used across the field. "
            "The four sub-dimensions (relevance, accuracy, clarity, completeness) are derived "
            "from MT-Bench's single-answer grading rubric."
        ),
    },
    "metadata": {
        "author": "simpleaudit",
        "version": "1.0",
        "date_created": "2026-04-10",
    },
}
