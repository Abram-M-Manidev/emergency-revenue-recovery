# Data Processing Terms — DRAFT pending legal review

Between [CUSTOMER BUSINESS] ("Business", controller/fiduciary) and
[LEGAL ENTITY NAME] ("ESSR", processor). Annex to the service agreement.

1. **Subject and duration.** ESSR processes personal data only to provide
   the call-handling service for the term of the agreement plus the
   deletion period in §8.
2. **Nature and purpose.** Answering the Business's calls; transcribing
   them; extracting caller details; recording service requests,
   appointments and emergency tickets; sending alerts and pages the Business
   configures; usage metering; support.
3. **Data subjects and categories.** Callers: phone number (caller ID),
   name, callback number, service address, problem description, call
   transcript, call recording (held by the voice provider). Business staff:
   name, email, role, on-call phone numbers.
4. **Instructions.** ESSR acts on the Business's documented instructions,
   which are its configuration (services, hours, emergency rules, caller
   notice, alert and paging destinations) and this agreement.
5. **Confidentiality and access.** Personnel with access are bound by
   confidentiality. Tenant data is separated in the application; every
   request is scoped to the authenticated user's organization.
6. **Security.** HTTPS in transit; role-based access; per-tenant isolation
   tested in the code base; secrets held outside source control; daily
   encrypted off-site backups; audit logs of key events. [COUNSEL/OPERATOR:
   confirm encryption at rest of the production disk and backups.]
7. **Subprocessors.** Listed in `SUBPROCESSORS.md`. ESSR gives [30] days'
   notice of a new subprocessor; the Business may object and terminate.
8. **Return and deletion.** On termination ESSR exports the Business's
   tickets, appointments and customers on request (CSV), then deletes the
   Business's data within [30] days, except backups, which expire within
   [35] days. Transcripts are redacted on the agreed retention schedule
   during the term.
9. **Incidents.** ESSR notifies the Business without undue delay and within
   [72] hours of becoming aware of a personal-data breach affecting it.
10. **Assistance.** ESSR helps the Business answer data-subject requests
    about call data (find, export, correct, delete).
11. **International transfers.** Processing occurs in India and, through
    subprocessors, in the United States. COUNSEL: transfer mechanism.
12. **Audit.** Reasonable written information on request; on-site audits
    only as agreed.
