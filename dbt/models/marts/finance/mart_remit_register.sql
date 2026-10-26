-- Remit register (V6 edit 18, finance part): row-level export, one row per cash receipt, the same
-- columns every month. Grain: cash transaction. Batch lifecycle (entered -> sent -> received), age and
-- late fee come from fct_cash_transaction, so the register ties to the cash ledger and to the late-fee
-- bill (mart_remittance_late_fees).
select
    c.cash_txn_key,
    c.remit_batch_id,
    c.tenant_key,
    c.dealer_key,
    d.dealer_number,
    d.dealer_name,
    k.contract_number,
    c.posting_period_key,
    case when pp.is_closed then 'FINAL' else 'PRELIMINARY' end as register_status,
    c.contract_entered_date,
    c.entered_at,
    c.sent_at,
    c.received_at,
    c.posted_at,
    c.amount,
    c.remit_age_days,
    c.is_late,
    c.late_fee,
    {{ audit_columns() }}
from {{ ref('fct_cash_transaction') }} as c
left join {{ ref('dim_contract') }} as k using (contract_key)
left join {{ ref('dim_dealer') }} as d on d.dealer_key = c.dealer_key
left join {{ ref('dim_posting_period') }} as pp on pp.posting_period_key = c.posting_period_key
