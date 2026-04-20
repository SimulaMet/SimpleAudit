# FAQ

## API / Errors

### Why do I get `openai.PermissionDeniedError: Your request was blocked` with my custom LLM endpoint?
This usually happens if you are using a proxy (e.g., Cloudflare or a corporate firewall) and it blocks requests that look like automated agents.

#### Solution:
Override the default OpenAI request headers to include a valid `User-Agent` in ModelAuditor:

```python
extra_kwargs={"default_headers": {"User-Agent": "SimpleAudit-test/1.0"}} # for model calls
# or
judge_extra_kwargs={"default_headers": {"User-Agent": "SimpleAudit-test/1.0"}} # for judge calls
```