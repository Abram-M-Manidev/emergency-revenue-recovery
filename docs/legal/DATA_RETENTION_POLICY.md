# Data retention and deletion — DRAFT

Periods in brackets are proposals; agree them per customer in the contract.

| Data | Where | Proposed retention | How it is removed | Implemented? |
|---|---|---|---|---|
| Call recordings | Vapi | 14 days (Vapi plan) | Vapi deletes automatically | Provider-side; ESSR clears its link with `app.cli.retention` |
| Transcripts (conversation messages) | ESSR DB | [90] days after the call | `python -m app.cli.retention --days 90 --apply` (redacts text) | Yes — operator-run, not scheduled |
| Tickets, appointments, outcomes | ESSR DB | Term of contract | Deleted on the Business's request / at termination | Manual (SQL), no self-serve |
| Customers (CRM records) | ESSR DB | Term of contract | Same | Manual |
| Contact-correction proposals | ESSR DB | With the conversation | Cascade on conversation deletion | Yes |
| Usage and reconciliation records | ESSR DB | [7 years] (billing evidence) | Manual | No PII beyond call ids/numbers ids |
| Early-access leads | ESSR DB | [24] months after last contact | Manual delete by email (RUNBOOK) | Manual |
| Application logs | Host / log store | [30] days | Log rotation | Configure on host |
| Database backups | Backblaze B2 | [35] days | Bucket lifecycle rule | Configured per RUNBOOK |

Access control: tenant staff see only their organization's data (enforced
on every API route by the authenticated user's organization). Operator
access is via the production host only (SSH + database shell); there is no
cross-tenant admin UI. Every operator CLI is read-only except
`voice_lines` (routing) and `retention --apply`.

Deletion on request: run the retention CLI or targeted SQL; record the
request, date and scope in the operator log. Gap: no self-serve erasure.
