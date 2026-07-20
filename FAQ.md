# FAQ

## API / Errors

### Why do I get `openai.PermissionDeniedError: Your request was blocked` with my custom LLM endpoint?
This usually happens if you are using a proxy (e.g., Cloudflare or a corporate firewall) and it blocks requests that look like automated agents.

#### Solution:
`ModelAuditor` creates its clients through [any-llm](https://mozilla-ai.github.io/any-llm/) and does not expose per-request HTTP header overrides (there are no `extra_kwargs` / `judge_extra_kwargs` parameters). Options that work today:

- Ask your proxy/firewall administrator to allowlist the endpoint or your machine's traffic.
- Point `base_url` (target), `judge_base_url` (judge), or `auditor_base_url` (auditor) at a local gateway or reverse proxy that injects the headers your infrastructure requires (e.g. nginx with `proxy_set_header User-Agent "SimpleAudit-test/1.0";`).
- Switch `provider` / `judge_provider` to a provider whose requests your proxy accepts — any provider supported by any-llm works.

If you need native header overrides, please [open an issue](https://github.com/kelkalot/simpleaudit/issues).

## Customization

### What extension points does `ModelAuditor` offer?

- `probe_prompt` — replace the built-in red-team persona used to generate probes. Include a literal `{language}` placeholder to opt into the `language` parameter (it is substituted verbatim, so JSON braces elsewhere in the prompt are untouched).
- `judge_prompt` — replace the judge's system prompt, including your own output schema; the framework returns whatever JSON the judge produces.
- `judge_response_schema` — supply a custom JSON schema for judge output enforcement (named judge configs with non-default shapes declare their own automatically).
- `judge` — select a named judge config (`safety`, `abstention`, `helpfulness`, `factuality`, `harm`, `binary_abstention`, ...); explicit `probe_prompt` / `judge_prompt` / `judge_response_schema` always override the config's values.
- `base_url` / `judge_base_url` / `auditor_base_url` — point the target, judge, or auditor at custom OpenAI-compatible endpoints (vLLM, LM Studio, gateways).
- `provider` / `judge_provider` / `auditor_provider` — use any provider supported by any-llm, independently per role.
