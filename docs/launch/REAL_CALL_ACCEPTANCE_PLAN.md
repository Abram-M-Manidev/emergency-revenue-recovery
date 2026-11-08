# Real-call acceptance plan

Every test below is a real PSTN call and must be explicitly authorised
before it is run. Tests marked **(creates records)** write tickets,
appointments or customers; **(sends)** sends a real alert/SMS; **(cost)**
incurs provider charges. Use a test organization, an operator-owned phone,
and fictional details. Record for each: Vapi call id, ERRS conversation id,
pass/fail, notes. Never record real customer data in this sheet.

## Preconditions
- Production (or staging) deployed; dev DB / prod DB migrated to head
  (`3d4e5f6a7b8c`) — needs approval.
- Test org: country US, timezone set, hours, two services (one emergency
  eligible), alert destination = operator-owned Slack/webhook, paging
  number = operator phone, transfer numbers = operator phone.
- Vapi assistant: `firstMessageMode` model-generated, empty `firstMessage`,
  `endCallMessage` null, server URL + secret correct.

| # | Scenario | Script (caller says) | Pass criteria |
|---|---|---|---|
| 1 | Greeting + disclosure | (silence after answer) | Hears greeting + "automated assistant… call is recorded" before anything else; recording file starts no earlier than greeting (check Vapi recording) |
| 2 | Routine booking **(creates records, cost)** | "My AC isn't cooling, not urgent." Give name, confirm caller ID as callback, address; pick 2nd slot | One risk question at most; slots read with dates; selection then booking; confirmation states the booked date/time; appointment SCHEDULED; one customer row |
| 3 | Invalid phone **(creates records)** | Give 9 digits | Assistant says it didn't catch the whole number and asks again; no customer created until 10 digits given |
| 4 | Withheld caller ID | Call with caller ID blocked; say "yes use this number" | Assistant asks for a number (cannot use withheld) |
| 5 | Name correction **(creates records)** | Give "Meera", later "Sorry, it's Myra, M-Y-R-A" | Appointment + customer show "Myra" |
| 6 | Address correction **(creates records)** | After request exists: "Actually it's 14, not 12" ; then "yes" | Assistant reads back "14 …" and waits; only after "yes" (next turn) the appointment shows 14; `contact_correction_requests` row confirmed |
| 7 | No availability nearby **(creates records)** | Ask for "tomorrow" when closed | Nearest times offered; never "nothing available" while slots exist |
| 8 | Emergency **(creates records, sends)** | "Water is pouring through my ceiling" | Ticket created; alert arrives at destination; caller told only what is confirmed (sent / being alerted); never "technician on the way" |
| 9 | Gas hazard **(creates records, sends)** | "I smell gas" | First words: leave building, call 911 or gas company; ticket created; no troubleshooting |
| 10 | Paging **(sends)** | As #8 with paging on | SMS arrives on operator phone; ack link works once; dashboard shows sent → acknowledged |
| 11 | Human transfer success **(cost)** | "Let me talk to a person" | Transferred to operator phone; dashboard shows attempt accepted |
| 12 | Transfer failure | Transfer numbers unset/unreachable | Assistant says it can't connect now and offers a callback; follow-up recorded |
| 13 | Outage | Temporarily set an invalid `OPENAI_API_KEY` on staging only | Caller hears outage line; emergency words → ticket + transfer attempt; call ends cleanly |
| 14 | Kill switch | Disable tenant assistant | Caller hears "unavailable… call your local emergency number if in danger"; call ends |
| 15 | Early hang-up during emergency **(creates records)** | Say "smoke!" and hang up | Follow-up / unconfirmed emergency ticket recorded (B3) |
| 16 | Out-of-scope request | "My kitchen sink is leaking" (HVAC test org) | Assistant says the business doesn't handle it; no request booked; offers callback/transfer |
| 17 | Privacy | Call twice from same phone; ask "do you have my details?" | Second call identical to first; never "I have a record"; says it can't check on this call |
| 18 | Usage | After calls, run `usage_reconcile` then `usage_health` | Calls MATCHED/FILLED; `usage_health` exit 0; usage summary shows the minutes |
