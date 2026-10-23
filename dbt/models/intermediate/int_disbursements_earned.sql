{{ config(materialized='table') }}
-- Earned dealer-cost buckets: contract x disbursement type x posting period x earned month.
-- earned_month_key is the true month being earned; posting_period_key is where it is booked
-- (int_contract_months: a month already closed when the contract activated is caught up in the
-- first open period, and a VOID after activation posts a reversal there; closed periods are never
-- restated).
-- Written = SALE + AMEND disbursements of the type. Each written LAYER (the SALE / AMEND amounts of
-- one posting period) earns layer x earned_pct of every month (int_contract_months: pro-rata by
-- default, rule of 78 via var earning_method), booked no earlier than the layer's own posting
-- period: an AMEND recorded today catches up its share of past months in today's open period
-- instead of re-pricing closed months. Only catch-up, reversal and late-layer rows have
-- posting_period_key <> earned_month_key. Earning stops at the effective cancellation, whose
-- refund reverses part of the unearned balance.
--
-- Cancel true-up: the part of the bucket that is neither earned by the cancel date nor refunded
-- (cancel fees, an R78 refund below the pro-rata earning, a non-refundable bucket) is retained,
-- so it is earned in one true-up line in the posting period of the cancellation:
--     true_up = written - earned to cancel - net refund (CANCEL + REINSTATE disbursements)
-- After it, earned + refunded = written for every effectively cancelled contract and bucket
-- (test recon_cancelled_fully_earned_or_refunded). The true-up posts to the cancel's posting
-- period (never earlier than the last earned month), so closed periods are never restated.
with layers as (
    select contract_key, disbursement_type, posting_period_key as layer_period_key, sum(amount) as layer_amount
    from {{ ref('int_disbursements_split') }}
    where txn_type in ('SALE', 'AMEND')
    group by 1, 2, 3
),

written as (
    select contract_key, disbursement_type, sum(layer_amount) as written_amount
    from layers
    group by 1, 2
),

monthly as (
    select
        m.contract_key,
        l.disbursement_type,
        greatest(m.posting_period_key, l.layer_period_key) as posting_period_key,
        m.earned_month_key,
        w.written_amount,
        m.earned_pct * {{ safe_divide('l.layer_amount', 'w.written_amount') }} as earned_pct,
        l.layer_amount * m.earned_pct as earned_amount,
        0.0 as cancel_true_up_amount
    from {{ ref('int_contract_months') }} as m
    inner join layers as l using (contract_key)
    inner join written as w using (contract_key, disbursement_type)
),

effective_cancel as (
    select x.contract_key, max(x.cancel_date) as cancel_date
    from {{ ref('int_cancellations_conformed') }} as x
    where x.is_effective_cancel
    group by 1
),

cancel_period as (
    select contract_key, max(posting_period_key) as cancel_period_key
    from {{ ref('int_transactions_posted') }}
    where txn_type in ('CANCEL', 'REINSTATE')
    group by 1
),

refunded as (
    select contract_key, disbursement_type, -sum(amount) as refunded_amount
    from {{ ref('int_disbursements_split') }}
    where txn_type in ('CANCEL', 'REINSTATE')
    group by 1, 2
),

earned_to_cancel as (
    select contract_key, disbursement_type, sum(earned_amount) as earned_amount,
           max(posting_period_key) as last_earned_period_key
    from monthly
    group by 1, 2
),

true_up as (
    select
        w.contract_key,
        w.disbursement_type,
        greatest(
            coalesce(p.cancel_period_key, {{ yyyymm('x.cancel_date') }}),
            coalesce(e.last_earned_period_key, 0)
        ) as posting_period_key,
        w.written_amount,
        w.written_amount - coalesce(e.earned_amount, 0) - coalesce(r.refunded_amount, 0) as cancel_true_up_amount
    from written as w
    inner join effective_cancel as x using (contract_key)
    left join cancel_period as p using (contract_key)
    left join refunded as r using (contract_key, disbursement_type)
    left join earned_to_cancel as e using (contract_key, disbursement_type)
),

combined as (
    select contract_key, disbursement_type, posting_period_key, earned_month_key, written_amount, earned_pct,
           earned_amount, cancel_true_up_amount
    from monthly
    union all
    select contract_key, disbursement_type, posting_period_key, posting_period_key, written_amount,
           {{ safe_divide('cancel_true_up_amount', 'written_amount') }},
           cancel_true_up_amount, cancel_true_up_amount
    from true_up
    -- rounded first: the sum behind it carries float noise that differs between builds, and a
    -- true-up on the half-cent line must not flip in or out of an already-closed period
    where abs(round(cancel_true_up_amount, 6)) >= 0.005
),

by_period as (
    select
        contract_key,
        disbursement_type,
        posting_period_key,
        earned_month_key,
        max(written_amount) as written_amount,
        sum(coalesce(earned_pct, 0)) as earned_pct,
        sum(earned_amount) as earned_amount,
        sum(cancel_true_up_amount) as cancel_true_up_amount
    from combined
    group by 1, 2, 3, 4
)

select
    contract_key,
    disbursement_type,
    {{ period_start_date('posting_period_key') }} as month_start,
    posting_period_key,
    earned_month_key,
    written_amount,
    earned_pct,
    sum(earned_pct) over (
        partition by contract_key, disbursement_type order by posting_period_key, earned_month_key
        rows unbounded preceding
    ) as earned_pct_cumulative,
    earned_amount,
    cancel_true_up_amount,
    case when disbursement_type = 'CLAIMS_RESERVE' then earned_amount else 0 end as earned_reserve
from by_period
