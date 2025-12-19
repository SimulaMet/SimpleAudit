# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in SimpleAudit, please report it responsibly.

### For SimpleAudit Itself

If you find a security issue in the SimpleAudit codebase:

1. **Do not** open a public GitHub issue
2. Contact the maintainers privately through their affiliated organizations:
   - Simula Research Laboratory: https://www.simula.no/contact
   - Or open a private security advisory on GitHub

3. Include in your report:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

We will acknowledge receipt within 48 hours and provide a detailed response within 7 days.

### For Vulnerabilities Discovered Using SimpleAudit

If you use SimpleAudit to discover vulnerabilities in AI systems:

1. **Follow responsible disclosure practices**
2. Report findings to the affected system's owners first
3. Allow reasonable time for fixes before public disclosure
4. Do not exploit vulnerabilities beyond what's necessary to demonstrate them

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Security Considerations for Users

### API Keys

- Never commit API keys to version control
- Use environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.)
- Rotate keys if accidentally exposed

### Audit Results

- Audit results may contain sensitive information about AI system vulnerabilities
- Store results securely and limit access
- Redact sensitive content before sharing reports externally

### Custom Scenarios

- Review custom scenarios for unintended harmful content
- Do not include real PII in test scenarios
- Be cautious when testing scenarios that could generate harmful outputs

### Local vs. Cloud

- For sensitive testing, consider using local models (Ollama, HuggingFace)
- Be aware that cloud API providers may log requests
- Review provider data retention policies for compliance needs

## Known Limitations

SimpleAudit is a testing tool, not a comprehensive security solution:

- It tests for known vulnerability patterns, not unknown ones
- Passing all scenarios does not guarantee an AI system is "safe"
- Results require human interpretation and context
- The tool itself could theoretically be misused for malicious purposes

## Security Updates

Security updates will be released as patch versions (e.g., 0.1.1, 0.1.2).

Subscribe to GitHub releases to be notified of security updates.

---

*Last updated: December 2024*
