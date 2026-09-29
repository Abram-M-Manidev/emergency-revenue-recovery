"""Human fallback: handing a live call to a person.

A caller must always have a way out of the assistant — to the office during
business hours, to the on-call line after hours. This package holds the pure
rules for that: where a call may go (`settings`), which state a transfer
attempt is in (`attempt`), whether the business is open (`hours`), and the
port a provider implements to actually move the call (`port`).

The single truthfulness rule that shapes all of it: the assistant may say it
is connecting the caller only after the provider has accepted the transfer,
and nothing in this system ever claims that a person answered — no provider
we use can confirm that.
"""
