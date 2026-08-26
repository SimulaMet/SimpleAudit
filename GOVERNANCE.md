# SimpleAudit Governance

This document describes how the SimpleAudit project is governed — who maintains
it, how decisions are made, and how to participate. It complements the
[Code of Conduct](./CODE_OF_CONDUCT.md), the [Security Policy](./SECURITY.md),
and the contribution guidance in the [README](./README.md).

SimpleAudit is an open-source (MIT) AI safety auditing framework and a
[verified Digital Public Good](https://www.digitalpublicgoods.net/r/simpleaudit).
It is stewarded by Simula Research Laboratory (Simula) and SimulaMet, and
developed in collaboration with the Norwegian Directorate of Health.

## Governance model

SimpleAudit follows a **maintainer-led model with institutional stewardship**.
Day-to-day direction rests with a small group of maintainers drawn from the
stewarding institutions; anyone may contribute. There is currently no separate
board or steering committee — decisions are made by the maintainers, in the
open, on GitHub. This document is the source of truth for that model and is
versioned with the project.

## Roles and responsibilities

### Lead maintainer / steward
Sets overall direction, has the final say where consensus cannot be reached,
and is the primary point of contact.

- **Michael A. Riegler** (Simula) — `@kelkalot` · michael@simula.no

### Maintainers
Review and merge contributions, triage issues, cut releases, and uphold the
Code of Conduct. Maintainers have write access to the repository.

- Sushant Gautam (SimulaMet)
- Finn Schwall (Simula)
- Annika Willoch Olstad (Simula)
- Klas H. Pettersen (SimulaMet)
- Sunniva Bjørklund (Hdir)

### Domain advisors
Provide expert review for specialised content — clinical/health scenarios,
Norwegian public-sector and youth domains, and safety policy — advising on
correctness and appropriateness within their domain.

- Sunniva Bjørklund, Maja Gran Erke, Hilde Lovett (Norwegian Directorate of
  Health) — health / clinical content
- Tor-Ståle Hansen (Ministry of Defence, Norway) — safety / public-sector

### Contributors
Anyone who opens an issue or pull request. Contributors do not need write
access; their changes are merged after maintainer review.

## Decision-making

- **Routine changes** (bug fixes, documentation, new scenario packs or judge
  configs, dependency bumps): handled through pull requests. A PR may be merged
  once it has at least one approving review from a maintainer (or the relevant
  code owner) and CI passes. Small, low-risk changes may use *lazy consensus* —
  if no maintainer objects within a reasonable window, the change proceeds.
- **Significant changes** (breaking API changes, methodology changes, new
  external dependencies, or anything affecting data provenance or safety
  posture): proposed and discussed in a GitHub Issue or PR before merging, so
  maintainers and affected domain advisors can weigh in. The goal is consensus
  among active maintainers.
- **Tie-breaking**: where maintainers cannot reach consensus, the lead
  maintainer makes the final decision, recorded in the relevant issue or PR.
- **Transparency**: substantive decisions are made and recorded in public
  issues and pull requests.

## Contribution & review process

1. For non-trivial changes, open an issue to discuss first (optional for small
   fixes).
2. Submit a pull request and ensure CI (tests) passes.
3. Reviews are requested automatically from the relevant owners via
   [`CODEOWNERS`](./.github/CODEOWNERS); at least one maintainer / code-owner
   approval is required to merge.
4. Health/clinical, Norwegian-domain, and other safety-sensitive changes should
   additionally be reviewed by a relevant domain advisor.

See the README "Contributing" section for current areas of interest.

## Becoming a maintainer

Contributors who have made sustained, high-quality contributions and shown good
judgement may be invited to become maintainers by consensus of the existing
maintainers. Maintainers who become inactive may move to emeritus status, with
write access adjusted accordingly. Changes to the maintainer list are made via a
pull request to this document.

## Releases

SimpleAudit uses semantic versioning, is published to
[PyPI](https://pypi.org/project/simpleaudit/), and tags releases on GitHub. Any
maintainer may cut a release once `main` is green.

## Code of Conduct & Security

All participation is governed by the [Code of Conduct](./CODE_OF_CONDUCT.md).
Security vulnerabilities are handled per the [Security Policy](./SECURITY.md).

## Amending this document

Changes to governance are proposed via pull request and require approval from
the lead maintainer plus at least one other maintainer. The change history is
tracked in version control.
