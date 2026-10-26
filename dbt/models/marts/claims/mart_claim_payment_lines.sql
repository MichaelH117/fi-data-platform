-- Row-level claim payment lines (V6 edit 18: the "claims detail" export). Grain: one line of
-- core.fct_claim_payment_line (the only claims fact) with the claim and contract numbers a reader
-- searches by. A view: it adds no rows and no logic, only labels.
{{ config(materialized='view') }}
select
    l.line_key,
    l.claim_key,
    s.claim_number,
    s.contract_number,
    l.source_system,
    l.source_line_id,
    l.line_type,
    l.posted_at,
    l.posting_period_key,
    l.payee_account_holder_key,
    l.authorized_change,
    l.inspection_change,
    l.paid_amount,
    l.pending_amount,
    l.incurred_change,
    l.process_days,
    l.age_bin,
    l.is_sign_fixed
from {{ ref('fct_claim_payment_line') }} as l
inner join {{ ref('mart_claim_summary') }} as s on s.claim_key = l.claim_key
