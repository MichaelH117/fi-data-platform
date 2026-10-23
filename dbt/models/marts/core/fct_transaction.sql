-- Grain: one accounting transaction (SALE, CANCEL, REINSTATE, AMEND, CASH), coverage-cost basis.
-- Reportable contracts only. Ties to the ledger by posting_period_key.
select
    t.txn_key,
    t.source_system,
    t.source_txn_id,
    t.contract_key,
    k.dealer_key,
    k.product_key,
    k.tenant_key,
    t.txn_type,
    t.posted_at,
    {{ date_key('t.posted_at') }} as posted_date_key,
    t.posting_period_key,
    {{ period_start_date("t.posting_period_key") }} as posting_period_start_date,
    t.amount,
    t.source_ref,
    {{ audit_columns() }}
from {{ ref('int_transactions_posted') }} as t
inner join {{ ref('dim_contract') }} as k using (contract_key)
