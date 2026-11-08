# Legal review checklist (for counsel)

DRAFT — questions for a qualified lawyer, not answers. Each item states what
the product actually does today so the advice can be specific.

## 1. Entity and contracting
- [ ] Which legal entity contracts with customers (sole proprietorship,
      LLP, private limited company)? Registered address, GST registration,
      and whether foreign customers can be invoiced in USD.
- [ ] Governing law and dispute forum for customer contracts (India vs.
      customer's state/province).
- [ ] Limitation of liability cap and exclusions, especially for missed or
      mishandled emergency calls (see §4).

## 2. Call recording and AI disclosure
What the product does: at the start of each call ERRS speaks "You're
speaking with an automated assistant for <business>, and this call is
recorded." (configurable per business; both notices default ON). Vapi
records the call; whether Vapi's recording starts before that sentence is
**not verified** — Vapi answers the call before ERRS speaks.
- [ ] US: all-party-consent states (e.g. CA, FL, IL, MD, MA, MT, NH, PA,
      WA): is an announced recording with continued participation
      sufficient consent? Must recording start only after the notice?
- [ ] Canada (PIPEDA and provincial laws): notice wording and purpose.
- [ ] State AI-disclosure / bot-disclosure laws (e.g. California's B.O.T.
      Act) for an inbound automated voice agent.
- [ ] Should the AI disclosure be allowed to be switched off at all?

## 3. Personal data
Data processed: caller phone number (caller ID), name, callback number,
service address, problem description, call transcript, recording (at Vapi,
14 days), appointment and ticket records; business staff accounts; website
leads (name, business, email, consent version).
- [ ] India DPDP Act 2023: ESSR's role for business-customer data
      (processor for the business) vs. its own data (fiduciary for leads and
      staff accounts); notice and consent requirements for leads.
- [ ] Cross-border transfer: data flows India → US subprocessors (Vapi,
      OpenAI, Twilio, Deepgram via Vapi, Backblaze). Any restricted-country
      notification under DPDP; SCCs if any EU/UK customer is ever accepted.
- [ ] US state privacy laws (CCPA/CPRA etc.) as a service provider to small
      businesses — likely below thresholds, but confirm.
- [ ] Is a DPA required by law or only commercially expected? (Draft
      provided.)
- [ ] Retention periods (see `DATA_RETENTION_POLICY.md`) and deletion on
      contract end.

## 4. Emergency-adjacent service
What the product does: classifies calls as emergency / non-emergency,
records emergency tickets, sends alerts to a channel the business chooses,
pages an on-call number by SMS (Twilio, not yet verified on the real
network), and tells callers reporting gas, CO, smoke, sparking or flooding
to leave and call 911 (or the local number). It cannot call 911 and never
says help is on the way.
- [ ] Disclaimers needed in contract, website and possibly the call itself.
- [ ] Whether any regulation applies to services that triage potentially
      hazardous situations (gas utilities, etc.).
- [ ] Insurance: professional liability / tech E&O / cyber cover before the
      first paid customer.

## 5. Marketing and outreach (from India to US/Canada businesses)
- [ ] CAN-SPAM (US) and CASL (Canada) for cold B2B email: identification,
      physical address, unsubscribe; CASL's implied-consent limits.
- [ ] No automated outbound calls or texts to prospects (TCPA) — the sales
      plan does not use them; confirm.

## 6. Payments and tax
- [ ] Receiving USD from foreign customers (FEMA/RBI, export of services,
      GST on export / LUT), and which payment processor.

## 7. Trademark
- [ ] Clearance search for "ESSR" in the US, Canada and India before
      spending on branding.
