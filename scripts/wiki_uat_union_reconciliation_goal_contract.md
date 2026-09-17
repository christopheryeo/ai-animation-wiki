---
type: goal-contract
name: AI Animation UAT reconciliation
status: disabled-until-configured
---

# AI Animation UAT Reconciliation

Markdown remains canonical. Database projection is disabled in this starter until an explicit
AI Animation UAT target, read-only production source, credentials, expected population, approval
record, rollback plan, and acceptance thresholds are supplied.

When configured, the workflow must snapshot Markdown, calculate a deterministic union, validate
all parent and child rows, write only to the exact UAT target inside a rollback-on-failure
transaction, and prove that production was unchanged. It must never infer counts from the
reference wiki or reuse a historical approval bundle.
