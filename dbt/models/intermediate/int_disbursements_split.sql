{{ config(materialized='table') }}
-- Dealer-cost split: one row per transaction x disbursement type x account holder. Amounts tie
-- to the parent transaction (calibration check in tests/reconciliation).
-- Legacy contracts carried admin fee, claims reserve and commission only; the rest of the dealer
-- cost is booked as INSURER_FEE so the split still sums to the transaction. Legacy cancels
-- reverse each refundable bucket by the refund % (the admin fee is retained, as in Admin Core), with
-- INSURER_FEE as the rounding plug.
with core as (
    select
        md5('admin_core|disb|' || cast(d.disbursement_id as varchar)) as disbursement_key,
        t.txn_key,
        t.contract_key,
        t.source_system,
        t.txn_type,
        d.disbursement_type,
        d.account_holder_id,
        d.amount,
        coalesce(d.is_refundable, 0) = 1 as is_refundable,
        coalesce(d.is_renewable, 0) = 1 as is_renewable
    from {{ ref('stg_admin_core__disbursement') }} as d
    inner join {{ ref('int_transactions_posted') }} as t
        on t.source_system = 'admin_core' and t.source_txn_id = cast(d.txn_id as varchar)
    where not d.is_deleted
),

legacy_sale_parts as (
    select
        c.contract_key,
        t.txn_key,
        coalesce(c.admin_fee, 0) as admin_fee,
        coalesce(c.reserve_amount, 0) as reserve_amount,
        coalesce(c.commission, 0) as commission,
        round(coalesce(c.coverage_cost, 0) - coalesce(c.admin_fee, 0) - coalesce(c.reserve_amount, 0)
              - coalesce(c.commission, 0), 2) as insurer_fee
    from {{ ref('int_contracts_conformed') }} as c
    inner join {{ ref('int_transactions_posted') }} as t
        on t.contract_key = c.contract_key and t.source_system = 'legacy_admin' and t.txn_type = 'SALE'
),

legacy_cancel_parts as (
    select
        x.contract_key,
        t.txn_key,
        t.amount as txn_amount,
        -- the admin fee is not refundable (Admin Core rule; the refund base is dealer cost - admin fee)
        0.0 as admin_fee,
        round(-coalesce(s.reserve_amount, 0) * coalesce(x.refund_pct, 0), 2) as reserve_amount,
        round(-coalesce(s.commission, 0) * coalesce(x.refund_pct, 0), 2) as commission
    from {{ ref('int_cancellations_conformed') }} as x
    inner join {{ ref('int_transactions_posted') }} as t
        on t.source_system = 'legacy_admin' and t.source_txn_id = 'CANCEL-' || x.source_cancel_id
    left join legacy_sale_parts as s on s.contract_key = x.contract_key
),

legacy_long as (
    select txn_key, contract_key, 'SALE' as txn_type, 'ADMIN_FEE' as disbursement_type, admin_fee as amount from legacy_sale_parts
    union all select txn_key, contract_key, 'SALE', 'CLAIMS_RESERVE', reserve_amount from legacy_sale_parts
    union all select txn_key, contract_key, 'SALE', 'COMMISSION', commission from legacy_sale_parts
    union all select txn_key, contract_key, 'SALE', 'INSURER_FEE', insurer_fee from legacy_sale_parts
    union all select txn_key, contract_key, 'CANCEL', 'ADMIN_FEE', admin_fee from legacy_cancel_parts
    union all select txn_key, contract_key, 'CANCEL', 'CLAIMS_RESERVE', reserve_amount from legacy_cancel_parts
    union all select txn_key, contract_key, 'CANCEL', 'COMMISSION', commission from legacy_cancel_parts
    union all
    select txn_key, contract_key, 'CANCEL', 'INSURER_FEE',
           round(txn_amount - admin_fee - reserve_amount - commission, 2)
    from legacy_cancel_parts
),

legacy as (
    select
        md5('legacy_admin|disb|' || txn_key || '|' || disbursement_type) as disbursement_key,
        txn_key,
        contract_key,
        'legacy_admin' as source_system,
        txn_type,
        disbursement_type,
        cast(null as bigint) as account_holder_id,
        amount,
        true as is_refundable,
        false as is_renewable
    from legacy_long
)

select
    s.*,
    t.posted_at,
    t.posting_period_key,
    case when s.disbursement_type = 'CLAIMS_RESERVE' then s.amount else 0 end as reserve_amount,
    case when s.disbursement_type = 'PROFIT_SHARE_RESERVE' then s.amount else 0 end as profit_share_reserve
from (
    select * from core
    union all
    select * from legacy
) as s
inner join {{ ref('int_transactions_posted') }} as t using (txn_key)
