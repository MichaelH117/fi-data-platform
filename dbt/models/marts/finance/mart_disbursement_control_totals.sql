-- Close control: does every posted transaction's cost split add back to the transaction?
-- Grain: posting period x source system x transaction type (CASH is remittance, not a cost split,
-- so it is excluded). One column per cost-split bucket; difference = txn amount - split total.
-- Each bucket is rounded to the cent on its own, so a prorated cancel can be off by up to half a
-- cent per split line (seed control_tolerances). That residual is shown, never hidden:
-- rounding_txns / rounding_residual. is_balanced = every transaction has a split and ties within
-- its tolerance.
with tol as (
    select tolerance from {{ ref('control_tolerances') }} where control_id = 'split_rounding_per_line'
),

split_txn as (
    select
        txn_key,
        count(*) as split_lines,
        sum(amount) as split_amount,
        sum(amount) filter (where disbursement_type = 'ADMIN_FEE') as admin_fee,
        sum(amount) filter (where disbursement_type = 'INSURER_FEE') as insurer_fee,
        sum(amount) filter (where disbursement_type = 'PREMIUM_TAX') as premium_tax,
        sum(amount) filter (where disbursement_type = 'COMMISSION') as commission,
        sum(amount) filter (where disbursement_type = 'CLAIMS_RESERVE') as claims_reserve,
        sum(amount) filter (where disbursement_type = 'PROFIT_SHARE_RESERVE') as profit_share_reserve
    from {{ ref('fct_disbursement') }}
    group by 1
),

per_txn as (
    select
        t.posting_period_key,
        t.source_system,
        t.txn_type,
        t.amount as txn_amount,
        s.*,
        s.txn_key is not null as has_split,
        round(t.amount - coalesce(s.split_amount, 0), 6) as txn_difference,
        abs(t.amount - coalesce(s.split_amount, 0)) <= coalesce(s.split_lines, 0) * tol.tolerance + 1e-9
            as within_tolerance
    from {{ ref('fct_transaction') }} as t
    cross join tol
    left join split_txn as s using (txn_key)
    where t.txn_type <> 'CASH'
)

select
    posting_period_key,
    source_system,
    txn_type,
    count(*) as txn_count,
    count(*) filter (where has_split) as split_txn_count,
    coalesce(sum(split_lines), 0) as split_lines,
    round(sum(txn_amount), 2) as txn_amount,
    round(coalesce(sum(split_amount), 0), 2) as split_amount,
    round(coalesce(sum(admin_fee), 0), 2) as admin_fee,
    round(coalesce(sum(insurer_fee), 0), 2) as insurer_fee,
    round(coalesce(sum(premium_tax), 0), 2) as premium_tax,
    round(coalesce(sum(commission), 0), 2) as commission,
    round(coalesce(sum(claims_reserve), 0), 2) as claims_reserve,
    round(coalesce(sum(profit_share_reserve), 0), 2) as profit_share_reserve,
    round(sum(txn_difference), 2) as difference,
    count(*) filter (where abs(txn_difference) >= 0.005) as rounding_txns,
    round(sum(txn_difference) filter (where abs(txn_difference) >= 0.005), 2) as rounding_residual,
    max(abs(txn_difference)) as max_txn_difference,
    count(*) filter (where not within_tolerance) as txns_out_of_tolerance,
    bool_and(has_split and within_tolerance) as is_balanced,
    {{ audit_columns() }}
from per_txn
group by 1, 2, 3
