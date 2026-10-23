-- Grain: transaction x disbursement type x account holder (the dealer-cost split).
-- reserve_amount = the CLAIMS_RESERVE bucket, profit_share_reserve = the PROFIT_SHARE_RESERVE bucket.
select
    d.disbursement_key,
    d.txn_key,
    d.contract_key,
    k.dealer_key,
    k.product_key,
    k.tenant_key,
    coalesce(md5('account_holder|' || cast(d.account_holder_id as varchar)), '-1') as account_holder_key,
    d.source_system,
    d.txn_type,
    d.disbursement_type,
    d.posted_at,
    {{ date_key('d.posted_at') }} as posted_date_key,
    d.posting_period_key,
    {{ period_start_date("d.posting_period_key") }} as posting_period_start_date,
    d.amount,
    d.reserve_amount,
    d.profit_share_reserve,
    d.is_refundable,
    d.is_renewable,
    {{ audit_columns() }}
from {{ ref('int_disbursements_split') }} as d
inner join {{ ref('dim_contract') }} as k using (contract_key)
