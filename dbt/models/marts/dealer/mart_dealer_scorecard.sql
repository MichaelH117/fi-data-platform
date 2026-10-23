-- Dealer scorecard. Grain: tenant x dealer x month (month_key = yyyymm). One row per dealer-month
-- with any activity. Each block keeps the basis of its source, stated in the column name:
--   production   mart_dealer_production, by contract ENTERED month (ties out to that mart)
--   cancel rate  cancelled / written for the contracts entered that month (metric cancel_rate, cohort basis)
--   loss ratio   fct_contract_monthly incurred (loss month) / EARNED CLAIMS RESERVE by earned month
--                ("Loss ratio on claims reserve"; the posted P&L ratio uses posting periods instead)
--   claims       mart_claim_summary by LOSS month; early = loss within the headline window of the
--                contract effective date (seed dealer_claim_kpi_rules early_claim_headline_days)
--   remittance   fct_cash_transaction by POSTING period; days late = days past dbt var remit_late_days
--   cancels      fct_cancellation effective cancels by POSTING period (activity, not the rate)
-- T12 columns are trailing 12 months ending in the row month. Watch thresholds and minimum volumes
-- are counting rules in seed dealer_claim_kpi_rules (scorecard_*). The unknown dealer ('-1') is excluded.
-- Early claims carry two different numbers, never one label for both:
--   t12_early_claim_rate   "early-claim share of claims": non-denied claims with a loss within the headline
--                          window / all claims reported (denied included), T12 by loss month. Descriptive only.
--   is_watch_early_claims  the watch of record: mart_dealer_early_claim_ae (early-claim incidence per
--                          contract, denied included, actual vs expected for product mix, seed window), the
--                          same test as the Actuarial page dealer list.
with rules as (
    select
        max(case when rule_id = 'early_claim_headline_days' then cast(rule_value as integer) end) as early_days,
        max(case when rule_id = 'scorecard_watch_loss_ratio' then cast(rule_value as double) end) as watch_lr,
        max(case when rule_id = 'scorecard_min_t12_earned' then cast(rule_value as double) end) as min_earned,
        max(case when rule_id = 'scorecard_watch_late_rate' then cast(rule_value as double) end) as watch_late
    from {{ ref('dealer_claim_kpi_rules') }}
),

as_of as (
    select cast(date_trunc('month', as_of_date) as date) as as_of_month from {{ ref('dim_reporting_date') }}
),

production as (
    select tenant_key, dealer_key, month_start,
        sum(contracts_written) as contracts_written,
        sum(contracts_cancelled) as contracts_cancelled,
        sum(coverage_cost) as coverage_cost,
        sum(retail_price) as retail_price
    from {{ ref('mart_dealer_production') }}
    group by all
),

earned as (
    select tenant_key, dealer_key, cast(month_start as date) as month_start,
        -- fct_contract_monthly.earned_premium is the earned CLAIMS_RESERVE bucket, not earned gross premium
        sum(earned_premium) as earned_claims_reserve,
        sum(incurred_claims) as incurred_claims
    from {{ ref('fct_contract_monthly') }}
    group by all
),

claims as (
    select s.tenant_key, s.dealer_key, s.loss_month as month_start,
        count(*) as claims_reported,
        count(*) filter (where s.early_claim_window_days <= r.early_days) as early_claims
    from {{ ref('mart_claim_summary') }} as s
    cross join rules as r
    group by all
),

remit as (
    select tenant_key, dealer_key,
        make_date(cast(posting_period_key // 100 as integer), cast(posting_period_key % 100 as integer), 1)
            as month_start,
        count(*) as remittances,
        count(*) filter (where is_late) as late_remittances,
        sum(late_fee) as late_fees,
        sum(remit_age_days) as remit_age_days_total,
        sum(greatest(remit_age_days - {{ var('remit_late_days', 30) }}, 0)) filter (where is_late)
            as days_late_total
    from {{ ref('fct_cash_transaction') }}
    group by all
),

cancels as (
    select tenant_key, dealer_key,
        make_date(cast(posting_period_key // 100 as integer), cast(posting_period_key % 100 as integer), 1)
            as month_start,
        count(*) filter (where is_effective_cancel) as cancellations_posted,
        sum(dealer_portion) filter (where is_effective_cancel) as dealer_chargebacks
    from {{ ref('fct_cancellation') }}
    group by all
),

spine as (
    select tenant_key, dealer_key, month_start from production
    union select tenant_key, dealer_key, month_start from earned
    union select tenant_key, dealer_key, month_start from claims
    union select tenant_key, dealer_key, month_start from remit
    union select tenant_key, dealer_key, month_start from cancels
    -- a dealer flagged on early claims is scored even in a month with no other activity
    union select tenant_key, dealer_key, month_start from {{ ref('mart_dealer_early_claim_ae') }} where is_flagged
),

monthly as (
    select
        s.tenant_key,
        s.dealer_key,
        s.month_start,
        coalesce(p.contracts_written, 0) as contracts_written,
        coalesce(p.contracts_cancelled, 0) as contracts_cancelled,
        round(coalesce(p.coverage_cost, 0), 2) as coverage_cost,
        round(coalesce(p.retail_price, 0), 2) as retail_price,
        round(coalesce(e.earned_claims_reserve, 0), 2) as earned_claims_reserve,
        round(coalesce(e.incurred_claims, 0), 2) as incurred_claims,
        coalesce(c.claims_reported, 0) as claims_reported,
        coalesce(c.early_claims, 0) as early_claims,
        coalesce(r.remittances, 0) as remittances,
        coalesce(r.late_remittances, 0) as late_remittances,
        round(coalesce(r.late_fees, 0), 2) as late_fees,
        coalesce(r.remit_age_days_total, 0) as remit_age_days_total,
        coalesce(r.days_late_total, 0) as days_late_total,
        coalesce(x.cancellations_posted, 0) as cancellations_posted,
        round(coalesce(x.dealer_chargebacks, 0), 2) as dealer_chargebacks
    from spine as s
    cross join as_of as a
    left join production as p using (tenant_key, dealer_key, month_start)
    left join earned as e using (tenant_key, dealer_key, month_start)
    left join claims as c using (tenant_key, dealer_key, month_start)
    left join remit as r using (tenant_key, dealer_key, month_start)
    left join cancels as x using (tenant_key, dealer_key, month_start)
    where s.dealer_key is not null and s.dealer_key <> '-1' and s.month_start <= a.as_of_month
),

{% set t12_cols = ['contracts_written', 'contracts_cancelled', 'earned_claims_reserve', 'incurred_claims',
                   'claims_reported', 'early_claims', 'remittances', 'late_remittances', 'late_fees', 'days_late_total'] %}
t12_window as (
    select
        m.*,
        {% for col in t12_cols %}
        sum(m.{{ col }}) over (
            partition by m.tenant_key, m.dealer_key order by m.month_start
            range between interval 11 months preceding and current row
        ) as t12_{{ col }}{{ "," if not loop.last }}
        {% endfor %}
    from monthly as m
),

rated as (
    select
        t.*,
        {{ safe_divide('t.contracts_cancelled', 't.contracts_written') }} as cancel_rate,
        {{ safe_divide('t.incurred_claims', 't.earned_claims_reserve') }} as loss_ratio,
        {{ safe_divide('t.early_claims', 't.claims_reported') }} as early_claim_rate,
        {{ safe_divide('t.late_remittances', 't.remittances') }} as late_remit_rate,
        {{ safe_divide('t.remit_age_days_total', 't.remittances') }} as avg_remit_age_days,
        {{ safe_divide('t.days_late_total', 't.late_remittances') }} as avg_days_late,
        {{ safe_divide('t.t12_contracts_cancelled', 't.t12_contracts_written') }} as t12_cancel_rate,
        {{ safe_divide('t.t12_incurred_claims', 't.t12_earned_claims_reserve') }} as t12_loss_ratio,
        {{ safe_divide('t.t12_early_claims', 't.t12_claims_reported') }} as t12_early_claim_rate,
        {{ safe_divide('t.t12_late_remittances', 't.t12_remittances') }} as t12_late_remit_rate,
        {{ safe_divide('t.t12_days_late_total', 't.t12_late_remittances') }} as t12_avg_days_late
    from t12_window as t
),

flagged as (
    select
        x.*,
        x.t12_earned_claims_reserve >= r.min_earned and x.t12_loss_ratio > r.watch_lr as is_watch_loss_ratio,
        x.t12_remittances > 0 and x.t12_late_remit_rate > r.watch_late as is_watch_late_remit,
        coalesce(ae.is_flagged, false) as is_watch_early_claims,
        ae.window_days as early_claim_ae_window_days,
        ae.contracts_exposed as early_claim_ae_contracts_exposed,
        ae.early_claim_contracts as early_claim_ae_early_contracts,
        ae.expected_early_claims as early_claim_ae_expected,
        ae.early_claim_incidence as early_claim_ae_incidence,
        ae.actual_to_expected as early_claim_actual_to_expected,
        ae.z_score as early_claim_ae_z_score,
        -- Peer percentile of the T12 loss ratio among dealers with enough earned claims reserve that month
        -- (0 = best, 1 = worst). Null when the dealer is below the minimum.
        case when x.t12_earned_claims_reserve >= r.min_earned then
            percent_rank() over (
                partition by x.month_start, (x.t12_earned_claims_reserve >= r.min_earned)
                order by x.t12_loss_ratio
            )
        end as t12_loss_ratio_peer_pct
    from rated as x
    cross join rules as r
    left join {{ ref('mart_dealer_early_claim_ae') }} as ae
        on ae.tenant_key = x.tenant_key and ae.dealer_key = x.dealer_key and ae.month_start = x.month_start
)

select
    f.tenant_key,
    f.dealer_key,
    d.dealer_number,
    d.dealer_name,
    d.state as dealer_state,
    f.month_start,
    {{ yyyymm('f.month_start') }} as month_key,
    f.contracts_written,
    f.contracts_cancelled,
    f.coverage_cost,
    f.retail_price,
    f.cancel_rate,
    f.earned_claims_reserve,
    f.incurred_claims,
    f.loss_ratio,
    f.claims_reported,
    f.early_claims,
    f.early_claim_rate,
    f.remittances,
    f.late_remittances,
    f.late_remit_rate,
    f.late_fees,
    f.remit_age_days_total,
    f.avg_remit_age_days,
    f.days_late_total,
    f.avg_days_late,
    f.cancellations_posted,
    f.dealer_chargebacks,
    f.t12_contracts_written,
    f.t12_contracts_cancelled,
    f.t12_cancel_rate,
    round(f.t12_earned_claims_reserve, 2) as t12_earned_claims_reserve,
    round(f.t12_incurred_claims, 2) as t12_incurred_claims,
    f.t12_loss_ratio,
    f.t12_loss_ratio_peer_pct,
    f.t12_claims_reported,
    f.t12_early_claims,
    f.t12_early_claim_rate,
    f.early_claim_ae_window_days,
    f.early_claim_ae_contracts_exposed,
    f.early_claim_ae_early_contracts,
    round(f.early_claim_ae_expected, 4) as early_claim_ae_expected,
    f.early_claim_ae_incidence,
    f.early_claim_actual_to_expected,
    f.early_claim_ae_z_score,
    f.t12_remittances,
    f.t12_late_remittances,
    f.t12_late_remit_rate,
    round(f.t12_late_fees, 2) as t12_late_fees,
    f.t12_avg_days_late,
    coalesce(f.is_watch_loss_ratio, false) as is_watch_loss_ratio,
    coalesce(f.is_watch_late_remit, false) as is_watch_late_remit,
    coalesce(f.is_watch_early_claims, false) as is_watch_early_claims,
    coalesce(f.is_watch_loss_ratio or f.is_watch_late_remit or f.is_watch_early_claims, false) as is_watch,
    {{ audit_columns() }}
from flagged as f
left join {{ ref('dim_dealer') }} as d on d.dealer_key = f.dealer_key
