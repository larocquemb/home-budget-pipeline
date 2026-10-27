# Project working instructions

## Paul's time records

Paul requires prospective Jira worklogs for his BrownRook project work. Apply
the durable time-recording agreement in `~/.codex/AGENTS.md` at the beginning
and end of substantive work. Jira is the primary record; keep private exports
and unresolved durations under `.validation/time-tracking/`.

Log Paul's actual hands-on work, including directing and reviewing AI. Keep
unattended AI/server runtime separate. Do not infer hours from elapsed chat
time, commits, or automated execution. When duration is unknown, record it as
pending rather than inventing hours. Preserve actual, estimated and
reconstructed time as distinct bases. Check existing entries for duplicates.

Use one dated worklog per issue and activity block, with evidence and a session
ID. Separate experiments, direct experimental support, routine development or
operations, claim preparation, administration and client work. Eligibility is a
separate review decision; project work is not automatically SR&ED.

Before reporting substantive work complete or moving a story to Done, reconcile
its worklogs or explicitly report pending time. Do not claim a Jira workflow
validator or background timer exists unless one has actually been installed.
The FY2027 recordkeeping/claim index is maintained on BrownRook Jira KAN-139.
KAN-138 is the completed FY2026 preparation story.

## Evidence on every story

At creation or the next substantive work on any BrownRook Jira story, include
the standard evidence/time checklist. Preserve existing acceptance criteria.
The reusable checklist is `.validation/time-tracking/STORY-EVIDENCE-CHECKLIST.txt`
and is also maintained on KAN-139. Capture work date, separate experimental and
support time, the problem/hypothesis or support purpose, versioned inputs and
configuration, code/PRs, raw successful and failed outputs, measurements,
conclusions and the resulting approach changes. Link actual artifacts through a
private per-story index; do not put sensitive source data in public Git or Jira.

Before completion, verify retained artifacts and time records or explicitly
record what is pending, not applicable or unavailable. Preserve relevant
experimental records before databases, caches or logs are rebuilt/expired.
For routine work, document the ordinary purpose; do not invent an SR&ED
uncertainty. Evidence capture does not imply tax eligibility.

KAN-139 stays To Do until annual claim preparation starts or Paul directs
otherwise. Its status does not suspend time/evidence capture on working stories.

## Command interface

Use `make` for builds, tests, documentation and infrastructure setup. Use `ledger`
for application operations (receipts, cache, database, receipt service lifecycle).
Select local settings explicitly with `ledger --env-file .env.dev ...`; do not mix
Make operation wrappers into application workflows. Existing wrappers are legacy
compatibility entry points. Never imply cache deletion clears database history.

KAN-137 is the agreed development architecture: Python, workers and HTTPS/Entra
on the Mac; isolated PostgreSQL, RabbitMQ and Neo4j on Arsene Podman. Native Mac
databases/brokers are legacy migration state, not a fourth environment to add.
Verify remote services before changing active connections; preserve legacy data.
