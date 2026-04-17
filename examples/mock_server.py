#!/usr/bin/env python3
"""
Example: Simple Mock Server for Testing SimpleAudit

This creates a minimal OpenAI-compatible server that you can use
to test SimpleAudit without needing a real AI system.

Usage:
    pip install fastapi uvicorn
    python mock_server.py
    
Then in another terminal:
    python -c "
from simpleaudit import ModelAuditor
a = ModelAuditor(model='mock', provider='openai', base_url='http://localhost:8000/v1',
                 judge_model='mock', judge_provider='openai', judge_base_url='http://localhost:8000/v1')
a.run('safety').summary()
    "
"""

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


@app.post("/v1/chat/completions")
def chat(request: ChatRequest) -> ChatResponse:
    response_text = get_response(request.messages)
    
    return ChatResponse(
        id=f"mock-{int(time.time())}",
        created=int(time.time()),
        model="mock",
        choices=[{
            "index": 0,
            "message": {"role": "assistant", "content": response_text},
            "finish_reason": "stop",
        }],
    )


if __name__ == "__main__":
    import uvicorn
    print("=" * 50)
    print("Starting Mock AI Server")
    print("=" * 50)
    print("This server has INTENTIONAL safety issues for testing!")
    print("Endpoint: http://localhost:8000/v1/chat/completions")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8000)
