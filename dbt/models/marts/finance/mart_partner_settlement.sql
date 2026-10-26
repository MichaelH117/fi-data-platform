-- Partner settlement statement (V6 edit 17): the monthly statement a white-label partner pays from.
-- Grain: tenant (the partner) x dealer x posting period. Signs are as they move the amount due:
--   admin_premium_due        cost split posted in the period (sale, amendment, cancel, reinstatement),
--                            every bucket except the insurer-program fee; cancels reduce it
--   program_fee_adjustment   the insurer-program fee (INSURER_FEE) on the same postings, on its own line
--                            because it funds incentives, not admin / premium
--   cancel_fees              cancellation fees on cancels posted in the period
--   transient_claims         claims paid in the period on contracts not yet remitted (no cash receipt
--                            posted by the end of the period): the partner funds them
--   obligor_claims_deducted  claims paid in the period on remitted contracts whose claims reserve the
--                            obligor holds: deducted from what the partner owes
--   net_due                  admin_premium_due + program_fee_adjustment + cancel_fees + transient_claims
--                            - obligor_claims_deducted
-- Admin Core business only: legacy contracts are not remitted through the partner (no cash ledger).
-- FINAL once the posting period is closed, PRELIMINARY otherwise.
with postings as (
    select
        d.tenant_key,
        d.dealer_key,
        d.posting_period_key,
        coalesce(sum(d.amount) filter (where d.disbursement_type <> 'INSURER_FEE'), 0) as admin_premium_due,
        coalesce(sum(d.amount) filter (where d.disbursement_type = 'INSURER_FEE'), 0) as program_fee_adjustment
    from {{ ref('fct_disbursement') }} as d
    where d.txn_type in ('SALE', 'AMEND', 'CANCEL', 'REINSTATE') and d.source_system = 'admin_core'
    group by 1, 2, 3
),

cancel_fees as (
    select k.tenant_key, k.dealer_key, x.posting_period_key, sum(coalesce(x.cancel_fee, 0)) as cancel_fees
    from {{ ref('fct_cancellation') }} as x
    inner join {{ ref('dim_contract') }} as k using (contract_key)
    where k.source_system = 'admin_core'
    group by 1, 2, 3
),

first_cash as (
    select contract_key, min(posting_period_key) as first_cash_period
    from {{ ref('fct_cash_transaction') }}
    group by 1
),

obligor_held as (
    select distinct d.contract_key
    from {{ ref('fct_disbursement') }} as d
    inner join {{ ref('dim_account_holder') }} as h using (account_holder_key)
    where d.disbursement_type = 'CLAIMS_RESERVE' and d.txn_type = 'SALE' and h.holder_type = 'OBLIGOR'
),

claims as (
    select
        k.tenant_key,
        k.dealer_key,
        l.posting_period_key,
        coalesce(sum(l.paid_amount) filter (
            where fc.first_cash_period is null or fc.first_cash_period > l.posting_period_key), 0)
            as transient_claims,
        coalesce(sum(l.paid_amount) filter (
            where fc.first_cash_period <= l.posting_period_key and o.contract_key is not null), 0)
            as obligor_claims_deducted
    from {{ ref('fct_claim_payment_line') }} as l
    inner join {{ ref('dim_contract') }} as k using (contract_key)
    left join first_cash as fc using (contract_key)
    left join obligor_held as o using (contract_key)
    where k.source_system = 'admin_core'
    group by 1, 2, 3
),

keys as (
    select tenant_key, dealer_key, posting_period_key from postings
    union select tenant_key, dealer_key, posting_period_key from cancel_fees
    union select tenant_key, dealer_key, posting_period_key from claims
),

lines as (
    select
        k.tenant_key,
        k.dealer_key,
        k.posting_period_key,
        coalesce(p.admin_premium_due, 0) as admin_premium_due,
        coalesce(p.program_fee_adjustment, 0) as program_fee_adjustment,
        coalesce(c.cancel_fees, 0) as cancel_fees,
        coalesce(cl.transient_claims, 0) as transient_claims,
        coalesce(cl.obligor_claims_deducted, 0) as obligor_claims_deducted
    from keys as k
    left join postings as p using (tenant_key, dealer_key, posting_period_key)
    left join cancel_fees as c using (tenant_key, dealer_key, posting_period_key)
    left join claims as cl using (tenant_key, dealer_key, posting_period_key)
)

select
    l.tenant_key,
    l.dealer_key,
    d.dealer_number,
    d.dealer_name,
    l.posting_period_key,
    case when pp.is_closed then 'FINAL' else 'PRELIMINARY' end as statement_status,
    l.admin_premium_due,
    l.program_fee_adjustment,
    l.cancel_fees,
    l.transient_claims,
    l.obligor_claims_deducted,
    l.admin_premium_due + l.program_fee_adjustment + l.cancel_fees + l.transient_claims
        - l.obligor_claims_deducted as net_due,
    {{ audit_columns() }}
from lines as l
left join {{ ref('dim_dealer') }} as d using (dealer_key)
left join {{ ref('dim_posting_period') }} as pp using (posting_period_key)
