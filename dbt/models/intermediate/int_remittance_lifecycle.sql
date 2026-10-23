{{ config(materialized='table') }}
-- Cash received per contract with its remittance batch lifecycle (ENTERED -> SENT -> RECEIVED).
-- remit_age_days = days from contract entry to cash received; a remittance older than
-- var remit_late_days carries a late fee of var remit_late_fee_pct x amount.
with cash as (
    select
        md5('admin_core|cash|' || cast(ct.cash_txn_id as varchar)) as cash_txn_key,
        ct.cash_txn_id,
        {{ contract_key("'admin_core'", 'ct.contract_id') }} as contract_key,
        ct.tenant_id,
        ct.remit_batch_id,
        ct.amount,
        ct.posted_at,
        ct.created_at as recorded_at,
        rb.dealer_id,
        rb.status as remit_status,
        rb.source as remit_source,
        rb.entered_at,
        rb.sent_at,
        rb.received_at
    from {{ ref('stg_admin_core__cash_transaction') }} as ct
    left join {{ ref('stg_admin_core__remit_batch') }} as rb using (remit_batch_id)
    where not ct.is_deleted
)

select
    cash.*,
    c.entered_date as contract_entered_date,
    datediff('day', c.entered_date, cast(coalesce(cash.received_at, cash.posted_at) as date)) as remit_age_days,
    datediff('day', c.entered_date, cast(coalesce(cash.received_at, cash.posted_at) as date))
        > {{ var('remit_late_days') }} as is_late,
    case
        when datediff('day', c.entered_date, cast(coalesce(cash.received_at, cash.posted_at) as date))
             > {{ var('remit_late_days') }}
        then round(cash.amount * {{ var('remit_late_fee_pct') }}, 2)
        else 0
    end as late_fee,
    {{ posting_period_key('cash.posted_at', 'pp') }} as posting_period_key
from cash
left join {{ ref('int_contracts_conformed') }} as c using (contract_key)
{{ posting_period_join('cash.recorded_at', 'pp') }}
