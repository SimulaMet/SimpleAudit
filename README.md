# 🔍 SimpleAudit

**Lightweight AI Safety Auditing Framework**

Contributors: \
Michael A. Riegler (Simula) \
Sushant Gautam (Simula)\
Maja Gran Erke (The Norwegian Directorate of Health)\
Hilde Lovett (The Norwegian Directorate of Health)\
Sunniva Bjørklund (The Norwegian Directorate of Health)\

SimpleAudit uses Claude to red-team your AI systems through adversarial probing. It's simple, effective, and requires minimal setup.

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Results from example [notebook](https://colab.research.google.com/drive/1uY2pL7WdAO21vRxI0bxEj7PtB45c6x_4?usp=sharing)

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

## Scenario Packs

SimpleAudit includes pre-built scenario packs:

| Pack | Scenarios | Description |
|------|-----------|-------------|
| `safety` | 8 | General AI safety (hallucination, manipulation, boundaries) |
| `rag` | 8 | RAG-specific (source attribution, retrieval boundaries) |
| `health` | 8 | Healthcare domain (emergency, diagnosis, prescriptions) |
| `all` | 24 | All scenarios combined |

```python
# List available packs
from simpleaudit import list_scenario_packs
print(list_scenario_packs())
# {'safety': 8, 'rag': 8, 'health': 8, 'all': 24}

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

SimpleAudit uses Claude for probe generation and judging:

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

## License

MIT License - see [LICENSE](LICENSE) for details.

## Acknowledgments

Built with [Anthropic Claude](https://www.anthropic.com/) for intelligent probing and safety evaluation. Can be extened to OpenAI or any other model.
