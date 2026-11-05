# OTLP Receiver Authentication — Design

**Date:** 2026-10-01
**Scope:** `simpleaudit` library (`simpleaudit/tracing/`)
**Status:** Approved for implementation

## Problem

SimpleAudit's OTLP receiver (`OTLPTraceReceiver` / `EphemeralOTLPReceiver`)
accepts any `POST /v1/traces` with no authentication. For a **shared,
always-on** receiver that many instrumented targets (e.g. multiple OpenWebUI
instances) push to, we need to answer two orthogonal questions:

- **WHO** is allowed to submit?  → *authentication* (this spec)
- **WHICH** audit the spans belong to?  → *correlation* via `trace_id`
  (already handled by the existing `TraceCorrelation` / `traceparent` flow)

This spec adds the **WHO** layer. It is deliberately framework-agnostic: the
auth lives in the `simpleaudit` library with **no Django dependency**, so both
the standalone library and the SimpleAuditStudio app can use it.

## Design decisions

### Two auth modes (plus `none`)

```
auth_mode:  none | basic | bearer
```

- **`none`** — the current behavior. Local / ephemeral receiver, trusted
  network. No credential store, no header check.
- **`basic`** — `Authorization: Basic base64(username:password)`. Chosen for
  OpenWebUI because it already supports `OTEL_BASIC_AUTH_USERNAME` /
  `OTEL_BASIC_AUTH_PASSWORD` natively and converts them into a Basic header.
- **`bearer`** — `Authorization: Bearer <token>`. Chosen for generic OTel
  exporters / agents / frameworks that can set arbitrary OTLP headers
  (`OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer <token>`).

**Out of scope for v1:** mTLS, OIDC, per-IP allowlists, rate limiting. These
can be layered on later without changing the interface.

### Credential model

A credential maps a secret to a **`target_id`** (a stable, caller-chosen
identifier for the submitting target). The receiver tags every ingested span
with the authenticated `target_id` so downstream consumers can attribute
spans to the target that sent them.

- **Basic:** one credential per `(username)`. The password is stored as a
  salted hash (SHA-256, `hashlib.scrypt`-free to keep stdlib-only); comparison
  is constant-time via `hmac.compare_digest`.
- **Bearer:** one credential per `(token)`. The token is stored as a salted
  hash for the same reason.

Credentials are **revocable** (disable without deleting) and **rotatable**
(re-issue a new secret for the same `target_id`).

### Interface

```python
class OTLPAuth:
    mode: str  # "none" | "basic" | "bearer"

    # Credential management
    def add_basic(self, username: str, password: str, target_id: str) -> None
    def add_bearer(self, token: str, target_id: str) -> None
    def revoke(self, username_or_token: str) -> bool
    def rotate_basic(self, username: str, new_password: str) -> None
    def rotate_bearer(self, old_token: str, new_token: str) -> None

    # Verification
    def verify(self, authorization_header: str | None) -> AuthResult
```

`AuthResult` is a small dataclass:

```python
@dataclass
class AuthResult:
    ok: bool
    target_id: str | None = None   # set when ok
    reason: str | None = None      # set when not ok
```

`verify` is the single entry point the receiver calls. It:
1. Returns `ok=True, target_id=None` when `mode == "none"` (no header needed).
2. Parses the `Authorization` header.
3. Dispatches to the basic or bearer check.
4. Returns `ok=False` with a reason on any failure (missing header, wrong
   scheme, unknown credential, revoked, bad secret).

All secret comparisons use `hmac.compare_digest` to avoid timing side-channels.

### Receiver wiring

`OTLPTraceReceiver` and `EphemeralOTLPReceiver` gain an optional `auth`
parameter (default `None` → `none` mode, preserving existing behavior).

- `OTLPTraceReceiver.handle(body, authorization=None)`: when `auth` is set and
  not `none`, it calls `auth.verify(authorization)` **before** decoding. On
  failure it returns an OTLP ack with a non-200 signal (the receiver's public
  contract is to return a dict; the HTTP layer maps `ok=False` to 401). On
  success it tags each span's attributes with `simpleaudit.target_id` before
  storing.
- `EphemeralOTLPReceiver._handle_traces(request)`: reads
  `request.headers.get("Authorization")`, calls `auth.verify`, and returns
  `web.json_response(..., status=401)` on failure. On success it tags spans
  with the target id and stores them as today.

Tagging is done by injecting `simpleaudit.target_id` into each raw span's
`attributes` before `parse_otlp_json`/`store.add_many`, so it flows through
the existing normalization and is queryable via `store.by_attribute`.

## What is NOT in this spec

- **Persistence** of credentials or spans (the library stays in-memory;
  Studio adds its own DB-backed credential store on top).
- **Protobuf** decoding (OpenWebUI can force `http/json`; a protobuf path is a
  separate follow-up).
- **Studio UI / Django model / migration** (separate work in the Studio repo;
  it will construct an `OTLPAuth` and pass it to the receiver).

## Testing

Engine tests (`tests/test_tracing.py` or a new `tests/test_otlp_auth.py`):
- `none` mode: `verify` returns ok with no header; receiver stores spans.
- `basic` mode: correct username/password → ok + target_id; wrong password →
  not ok; unknown username → not ok; revoked → not ok; rotate works.
- `bearer` mode: correct token → ok + target_id; wrong token → not ok;
  revoked → not ok; rotate works.
- Header parsing: missing header, wrong scheme, malformed base64.
- Receiver integration: 401 on bad auth, 200 + tagged span on good auth,
  target id present in stored span attributes.
