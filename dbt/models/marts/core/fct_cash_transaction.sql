-- Grain: one cash receipt per contract, with its remittance batch lifecycle
-- (entered -> sent -> received), remit_age_days and late_fee (vars remit_late_days, remit_late_fee_pct).
select
    r.cash_txn_key,
    r.cash_txn_id,
    r.remit_batch_id,
    r.contract_key,
    k.dealer_key,
    k.product_key,
    k.tenant_key,
    r.remit_status,
    r.remit_source,
    r.entered_at,
    r.sent_at,
    r.received_at,
    r.posted_at,
    {{ date_key('r.posted_at') }} as posted_date_key,
    r.posting_period_key,
    {{ period_start_date("r.posting_period_key") }} as posting_period_start_date,
    r.contract_entered_date,
    r.amount,
    r.remit_age_days,
    r.is_late,
    r.late_fee,
    {{ audit_columns() }}
from {{ ref('int_remittance_lifecycle') }} as r
inner join {{ ref('dim_contract') }} as k using (contract_key)
