# Friday AI KPI reporting — source mapping pending

Scope authorized: KPIs for leads, new customers and onboarding. Later phases
will build workflows generating leads, appointments and purchases in Friday/GHL.
No lead-generation, outbound-message, appointment or payment workflow is enabled
by this reporting foundation.

## Owner-confirmed definitions

| KPI | Proposed event | Required evidence |
| --- | --- | --- |
| New leads | A warm conversation | Approved warmth signal, canonical prospect identity, first-warm timestamp |
| New customers | A customer's first successful purchase | Approved product scope and first-payment evidence; renewals must not count again |
| Onboarding completed | A2P completed AND SMS usable in the platform | Customer-account mapping and verified timestamps for both readiness conditions |

The owner confirmed these definitions and the Friday Ai location. Machine-level
signal mapping remains pending: what tag/stage identifies warmth, and which
source records the customer’s A2P/SMS readiness? The owner's
Business Brain KPI-FRAMEWORK separates onboarding completion, launched campaigns,
first conversations and first appointments. Those are distinct milestones.
A current tag or pipeline stage does not establish when a person entered it.

Initial output should be an on-demand Slack report for today, week-to-date or
month-to-date, with exact bounds, source, freshness and missing-data warnings.
Do not claim conversion rates by dividing unrelated same-period totals. A
scheduled report requires the owner's cadence/time decision.

## Counting foundation

`crew/friday_kpis.py` is pure deterministic software, not an agent prompt or a
live adapter. It accepts verified, tenant-scoped business events and explicit
coverage attestations. Events carry only source/opaque IDs, kind, timestamp and
test flag; no names, email addresses, phone numbers or message bodies are needed.

- Replayed source event IDs are idempotent; conflicting payloads fail closed.
- Subjects count once per event kind using their first authoritative transition.
- Tests, other locations and unverified event sources do not count.
- Reporting uses half-open time bounds and timezone-aware calendar periods.
- Missing coverage returns `unavailable`/null; verified empty data returns zero.
- Distinct metric definitions cannot silently mix.

Source adapters must establish the approved business meaning before creating
events. In particular, customer_started must represent first-ever customer
status, not every payment. Incomplete payment history cannot prove that a payment
is the first. Coverage must span the actual reporting interval, ending at the
observation time for to-date reports. Historical backfills require equivalent
verified evidence. Current tags/stages cannot reconstruct absent history.

No adapter, HTTP endpoint, webhook, model tool, scheduler or database migration
has been enabled. The accepted live-vault Slack deployment remains intact.

## Current connection blocker

Recovered older documents identify sub-account Friday Ai with location
`EB7boaJBjH7hnj4Mp2VZ`. The current managed GHL_API_KEY exists, but a read-only
contact search against that location returned HTTP 403: "The token does not have
access to this location." Location/pipeline/custom-field metadata reads were
also denied. Agency location discovery returned Forbidden resource.

The owner has confirmed this sub-account/location and the definitions above.
Verify a matching least-privilege GHL integration,
inspect only required source schemas/aggregate data, and configure its actual
credential securely on Render. Do not copy a Codex proxy credential into Render
or paste tokens into chat. Do not reset keys or broaden scopes speculatively.

## Next implementation steps

1. Map explicit warm-conversation and A2P/SMS-readiness signals/timestamps.
2. Add read-only adapters with complete pagination, cross-location checks,
   source freshness and coverage tracking. Do not send raw contacts to the model.
3. Wire deterministic KPI reports into Friday's owner-only Slack path, behind an
   opt-in setting. Report missing sources rather than guessing.
4. Validate with synthetic cases, then real aggregates, before enabling on Render.
5. Build the separately requested lead, appointment and purchase workflows after
   the KPI baseline is accepted.

Rollback today requires no production action: this is an isolated source-only
foundation. Do not enable CRM write tools from the inherited general GHL module.

Official API references inspected:
https://marketplace.gohighlevel.com/docs/ghl/contacts/search-contacts-advanced/
https://marketplace.gohighlevel.com/docs/ghl/payments/list-transactions/
https://marketplace.gohighlevel.com/docs/ghl/opportunities/get-pipelines/


## A2P interpretation

The owner's completion rule is specifically A2P completed plus usable SMS. A
first workflow success, payment, account creation, active phone number alone,
or Friday Ai's own approved campaign does not establish every customer's
readiness. Each customer's CRM record must map to their own account or to an
explicitly verified readiness marker with a completion date. The public active
numbers endpoint does not by itself prove A2P approval. No customer SMS will be
sent as a test to infer readiness.
