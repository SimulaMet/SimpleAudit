# 🔍 SimpleAudit

**Lightweight AI Safety Auditing Framework**

Contributors: \
Michael A. Riegler (Simula) \
Sushant Gautam (SimulaMet)\
Klas H. Pettersen (SimulaMet)\
Maja Gran Erke (The Norwegian Directorate of Health)\
Hilde Lovett (The Norwegian Directorate of Health)\
Sunniva Bjørklund (The Norwegian Directorate of Health)\
Tor-Ståle Hansen (Specialist Director, Ministry of Defense Norway)

SimpleAudit uses different models such as Claude for multilingual auditing/red-teaming your AI systems through adversarial probing. It is simple, easy to extend, and requires minimal setup. It supports models via API or locally running.

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

<img width="1362" height="590" alt="simpleaudit_example_gemma_model" src="https://github.com/user-attachments/assets/05c45a62-74e7-4aa3-a3cd-41bad0cc8233" />

## Why SimpleAudit?

| Tool | Complexity | Dependencies | Cost | Approach |
|------|------------|--------------|------|----------|
| **SimpleAudit** | ⭐ Simple | 2 packages | $ Low | Adversarial probing |
| Petri | ⭐⭐⭐ Complex | Many | $$$ High | Multi-agent framework |
| RAGAS | ⭐⭐ Medium | Several | Free | Metrics only |
| Custom | ⭐⭐⭐ Complex | Varies | Varies | Build from scratch |


## Installation

```bash
pip install simpleaudit

# With plotting support
pip install simpleaudit[plot]
```

**Or install from GitHub:**

```bash
pip install git+https://github.com/kelkalot/simpleaudit.git
```

## Quick Start

```python
from simpleaudit import Auditor

# Create auditor pointing to your AI system (default: Anthropic Claude)
auditor = Auditor(
    target="http://localhost:8000/v1/chat/completions",
    # Uses ANTHROPIC_API_KEY env var, or pass: api_key="sk-..."
)

# Run built-in safety scenarios
results = auditor.run("safety")

# View results
results.summary()
results.plot()
results.save("audit_results.json")
```

### Using Different Providers

```python
# OpenAI (requires: pip install simpleaudit[openai])
auditor = Auditor(
    target="http://localhost:8000/v1/chat/completions",
    provider="openai",  # Uses OPENAI_API_KEY env var
)

# Grok via xAI (requires: pip install simpleaudit[openai])
auditor = Auditor(
    target="http://localhost:8000/v1/chat/completions",
    provider="grok",  # Uses XAI_API_KEY env var
)
```

### Local Models (Free, No API Key Required)

```python
# Ollama - for locally served models
# First: ollama serve && ollama pull llama3.2
auditor = Auditor(
    target="http://localhost:8000/v1/chat/completions",
    provider="ollama",  # Uses local Ollama instance
    model="llama3.2",   # Or "mistral", "codellama", etc.
)

# HuggingFace - for direct transformers inference
auditor = Auditor(
    target="http://localhost:8000/v1/chat/completions",
    provider="huggingface",
    model="meta-llama/Llama-3.2-1B-Instruct",
)
```

## ModelAuditor - Direct API Testing

`ModelAuditor` audits models directly via their APIs without needing an external HTTP endpoint:

```python
from simpleaudit import ModelAuditor

# Basic usage - audit Claude with a system prompt
auditor = ModelAuditor(
    provider="anthropic",                          # Target model provider
    system_prompt="You are a helpful assistant.",  # Optional system prompt
)
results = auditor.run("system_prompt")
results.summary()
```

### Key Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `provider` | Target model: `"anthropic"`, `"openai"`, `"grok"`, `"huggingface"`, `"ollama"` | `"anthropic"` |
| `model` | Model name (e.g., `"gpt-4o"`, `"llama3.2"`) | Provider default |
| `system_prompt` | System prompt for target model (or `None`) | `None` |
| `judge_provider` | Provider for judging (can differ from target) | Same as `provider` |
| `judge_model` | Model for judging | Provider default |
| `max_turns` | Conversation turns per scenario | `5` |

### Cross-Provider Auditing

Use different providers for target and judge:

```python
# Test OpenAI, judged by Claude
auditor = ModelAuditor(
    provider="openai",           # Target: OpenAI
    model="gpt-4o",
    system_prompt="Be helpful and safe.",
    judge_provider="anthropic",  # Judge: Claude
)
```

### Local Model Auditing (Free)

Audit local models without any API keys:

```python
# Test a local Ollama model
auditor = ModelAuditor(
    provider="ollama",
    model="llama3.2",
    system_prompt="You are a helpful assistant.",
)
results = auditor.run("safety")

# Test a HuggingFace model (GPU required/recommended)
auditor = ModelAuditor(
    provider="huggingface",
    model="meta-llama/Llama-3.2-1B-Instruct",
)
results = auditor.run("system_prompt")
```

### Without System Prompt

Test model's default behavior:

```python
auditor = ModelAuditor(
    provider="openai",
    # system_prompt=None,  # Omit or set to None
)
results = auditor.run("safety")
```

## Scenario Packs

SimpleAudit includes pre-built scenario packs:

| Pack | Scenarios | Description |
|------|-----------|-------------|
| `safety` | 8 | General AI safety (hallucination, manipulation, boundaries) |
| `rag` | 8 | RAG-specific (source attribution, retrieval boundaries) |
| `health` | 8 | Healthcare domain (emergency, diagnosis, prescriptions) |
| `system_prompt` | 8 | System prompt adherence and bypass testing |
| `all` | 32 | All scenarios combined |

```python
# List available packs
from simpleaudit import list_scenario_packs
print(list_scenario_packs())
# {'safety': 8, 'rag': 8, 'health': 8, 'system_prompt': 8, 'all': 32}

# Run specific pack
results = auditor.run("rag")

# Run multiple packs
results = auditor.run("all")
```

## Custom Scenarios

Create your own scenarios:

```python
my_scenarios = [
    {
        "name": "Custom Test 1",
        "description": (
            "Test if the system does X when the user asks Y. "
            "The system should respond by doing Z."
        ),
    },
    {
        "name": "Custom Test 2", 
        "description": "Another test description...",
    },
]

results = auditor.run(my_scenarios)
```

## Configuration Options

```python
auditor = Auditor(
    # Required
    target="http://localhost:8000/v1/chat/completions",
    
    # Provider selection
    provider="anthropic",            # "anthropic" (default), "openai", or "grok"
    api_key="sk-...",                # Or use env vars (see below)
    model="claude-sonnet-4-20250514",           # Provider-specific model name
    
    # Other options
    target_model="my-model",          # Model name sent to target API
    max_turns=5,                      # Conversation turns per scenario
    timeout=120.0,                    # Request timeout (seconds)
    verbose=True,                     # Print progress
    prompt_for_key=True,              # Prompt for API key if not found
)

# Run with custom settings
results = auditor.run(
    "safety",
    max_turns=3,                      # Override default turns
    language="Norwegian",             # Probe language (default: English)
)
```

### Environment Variables

| Provider | Environment Variable | Default Model |
|----------|---------------------|---------------|
| Anthropic | `ANTHROPIC_API_KEY` | `claude-sonnet-4-20250514` |
| OpenAI | `OPENAI_API_KEY` | `gpt-4o` |
| Grok | `XAI_API_KEY` | `grok-3` |

## Understanding Results

```python
results = auditor.run("safety")

# Summary statistics
print(f"Score: {results.score}/100")
print(f"Passed: {results.passed}/{len(results)}")
print(f"Critical issues: {results.critical_count}")

# Severity distribution
print(results.severity_distribution)
# {'pass': 4, 'medium': 2, 'high': 1, 'critical': 1}

# Detailed inspection
for result in results:
    print(f"{result.scenario_name}: {result.severity}")
    print(f"  Issues: {result.issues_found}")
    print(f"  Summary: {result.summary}")

# Export
results.save("audit_results.json")
results.plot(save_path="audit_chart.png")
```

### Severity Levels

| Level | Score | Meaning |
|-------|-------|---------|
| 🟢 pass | 4 | Handled appropriately |
| 🔵 low | 3 | Minor issue |
| 🟡 medium | 2 | Moderate concern |
| 🟠 high | 1 | Significant issue |
| 🔴 critical | 0 | Dangerous behavior |

## Target API Requirements

Your target must be an OpenAI-compatible chat completions endpoint:

```
POST /v1/chat/completions
{
    "model": "your-model",
    "messages": [
        {"role": "user", "content": "Hello"}
    ]
}
```

**Works with:**
- OpenAI API
- Ollama (`ollama serve`)
- vLLM
- LiteLLM
- Any OpenAI-compatible server
- Custom RAG systems with chat wrapper

## Example: Auditing a RAG System

```python
# 1. Create an OpenAI-compatible wrapper for your RAG
#    (see examples/rag_server.py)

# 2. Start your RAG server
#    python rag_server.py  # Runs on localhost:8000

# 3. Audit it
from simpleaudit import Auditor

auditor = Auditor("http://localhost:8000/v1/chat/completions")
results = auditor.run("rag")  # RAG-specific scenarios

results.summary()
```

## Cost Estimation

SimpleAudit can use different models to probe generation and judging. This example is based on Claude:

| Scenarios | Turns | Estimated Cost |
|-----------|-------|----------------|
| 8 | 5 | ~$2-4 |
| 24 | 5 | ~$6-12 |
| 24 | 10 | ~$12-24 |

*Costs depend on response lengths and Claude model used.*

## Contributing

Contributions welcome! Areas of interest:

- New scenario packs (legal, finance, education, etc.)
- Additional judge criteria
- More target adapters
- Documentation improvements

## Governance & Compliance

- 📋 [Digital Public Good Compliance](DPG.md) — SDG alignment, ownership, standards
- 🤝 [Code of Conduct](CODE_OF_CONDUCT.md) — Community guidelines and responsible use
- 🔒 [Security Policy](SECURITY.md) — Vulnerability reporting and security considerations

## License

MIT License - see [LICENSE](LICENSE) for details.
