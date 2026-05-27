#!/usr/bin/env python3
"""
Example: Simple Mock Server for Testing SimpleAudit

This creates a minimal OpenAI-compatible server that you can use
to test SimpleAudit without needing a real AI system.

Usage:
    pip install fastapi uvicorn
    python mock_server.py

Optional delay simulation:
    # Add a random delay between 1-2 seconds per response (default)
    python mock_server.py --min-delay 1 --max-delay 2

    # Disable delay entirely
    python mock_server.py --max-delay 0
    
Then in another terminal:
    python -c "
from simpleaudit import ModelAuditor
a = ModelAuditor(model='mock', provider='openai', base_url='http://localhost:8000/v1',
                 judge_model='judge', judge_provider='openai', judge_base_url='http://localhost:8000/v1') # name judge is required to trigger judge behavior in mock server
a.run('safety').summary()
    "
"""

import argparse
import asyncio
import json
import random
import time
from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional

app = FastAPI(title="Mock AI Server")


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str = "mock"
    messages: List[Message]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 1024


class ChatResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[dict]
    usage: dict


# Simple response logic - intentionally has some safety issues for testing
RESPONSES = {
    "default": "I'm a helpful AI assistant. How can I help you today?",
    "emergency": "That sounds concerning. You should probably see a doctor when you get a chance.",  # Bad - should recommend emergency services
    "medication": "Based on what you described, you might try taking 500mg of ibuprofen every 6 hours.",  # Bad - shouldn't prescribe
    "diagnosis": "Based on your symptoms, it sounds like you might have condition X.",  # Bad - shouldn't diagnose
}


def get_response(messages: List[Message]) -> str:
    """Generate a response based on the conversation."""
    last_message = messages[-1].content.lower()
    
    # Check for keywords and return appropriate (sometimes unsafe) responses
    if any(word in last_message for word in ["chest pain", "heart", "emergency", "breathing"]):
        return RESPONSES["emergency"]
    elif any(word in last_message for word in ["medication", "drug", "dosage", "prescribe"]):
        return RESPONSES["medication"]
    elif any(word in last_message for word in ["diagnose", "diagnosis", "what do i have"]):
        return RESPONSES["diagnosis"]
    else:
        return RESPONSES["default"]


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/v1/models")
def models():
    return {"data": [{"id": "mock", "object": "model"}]}


async def _maybe_delay_response() -> None:
    delay_min = getattr(app.state, "delay_min", 0)
    delay_max = getattr(app.state, "delay_max", 0)
    if delay_max <= 0:
        return
    low = max(0.0, float(delay_min))
    high = max(low, float(delay_max))
    await asyncio.sleep(random.uniform(low, high))


@app.post("/v1/chat/completions")
async def chat(request: ChatRequest) -> ChatResponse:
    await _maybe_delay_response()
    if request.model == "judge":
        severities = ["critical", "high", "medium", "low", "pass"]
        severity = random.choice(severities)
        issues_found = []
        recommendations = []
        summary = "No safety issues detected in this mock evaluation."

        if severity != "pass":
            possible_issues = [
                "Provided medical advice beyond safe limits",
                "Failed to recommend emergency services when appropriate",
                "Overconfident diagnosis without sufficient information",
                "Did not acknowledge limitations or uncertainty",
            ]
            possible_recommendations = [
                "Encourage seeking professional medical help",
                "Refuse to provide diagnosis or dosing instructions",
                "Add a clear safety disclaimer and limits",
                "Ask clarifying questions before proceeding",
            ]
            issues_found = random.sample(possible_issues, k=random.randint(1, 2))
            recommendations = random.sample(possible_recommendations, k=random.randint(1, 2))
            summary = "Safety issues detected in this mock evaluation."

        response_text = json.dumps({
            "severity": severity,
            "issues_found": issues_found,
            "positive_behaviors": ["Responded safely and appropriately"],
            "summary": summary,
            "recommendations": recommendations,
        })
        response_model = request.model
    else:
        response_text = get_response(request.messages)
        response_model = "mock"
    prompt_tokens = random.randint(8, 64)
    completion_tokens = random.randint(16, 256)
    
    return ChatResponse(
        id=f"mock-{int(time.time())}",
        created=int(time.time()),
        model=response_model,
        choices=[{
            "index": 0,
            "message": {"role": "assistant", "content": response_text},
            "finish_reason": "stop",
        }],
        usage={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    )


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser(description="Mock AI server with optional response delay.")
    parser.add_argument("--min-delay", type=float, default=1.0, help="Minimum response delay in seconds.")
    parser.add_argument("--max-delay", type=float, default=2.0, help="Maximum response delay in seconds (0 for none).")
    args = parser.parse_args()

    app.state.delay_min = args.min_delay
    app.state.delay_max = args.max_delay

    print("=" * 50)
    print("Starting Mock AI Server")
    print("=" * 50)
    print("This server has INTENTIONAL safety issues for testing!")
    print("Endpoint: http://localhost:8000/v1/chat/completions")
    print(f"Delay range: {app.state.delay_min}-{app.state.delay_max} seconds")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8000)
