# Subprocessors — DRAFT

What each provider receives in the code as it stands (2026-11-08). Locations
and certifications must be confirmed against each provider's current terms
before publication; nothing here claims a provider's compliance status.

| Provider | Purpose | Data it receives | Where configured | Status |
|---|---|---|---|---|
| Vapi | Answers calls, speech-to-text/text-to-speech orchestration, recording | Call audio, transcript, caller ID, recording (14-day retention on current plan) | Vapi dashboard; `VAPI_*` | In use (dev) |
| Deepgram (via Vapi) | Speech-to-text | Call audio | Vapi assistant transcriber | In use via Vapi |
| Twilio | Phone number / PSTN carrier; on-call SMS paging | Call audio in transit; paging SMS text and on-call numbers | Twilio console; `TWILIO_*`, `PAGING_PROVIDER=twilio` | Number in use; paging not verified live |
| OpenAI | Language model for replies and decisions | Transcript text, business configuration, caller details stated in the call | `OPENAI_*` | In use (dev) |
| Backblaze B2 | Off-site encrypted database backups | Encrypted database dumps | `docker/backup`, B2 bucket | In use (verified drill 2026-09-28) |
| Hosting provider [AWS EC2 planned] | Servers, disks | Everything at rest | — | Not yet provisioned |
| Webhook destination chosen by each Business (e.g. Slack) | Emergency alerts | Ticket summary, callback number, address | Per-tenant settings | Business-controlled |
| Email provider [TBD] | Contacting leads / customers | Email addresses | — | Not chosen |

TTS voice provider: as configured on the Vapi assistant [CONFIRM which].
