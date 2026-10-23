-- Metric rule (docs/V3_DESIGN.md 3): the scorecard ratios must equal their metrics.yml formulas
-- (loss_ratio = incurred / earned claims reserve, cancel_rate = cancelled / sold) recomputed from the row's own
-- additive columns, and each T12 column must equal the sum of the dealer's last 12 monthly rows.
with card as (select * from {{ ref('mart_dealer_scorecard') }}),

ratio_fail as (
    select tenant_key, dealer_key, month_key, 'ratio' as check_name
    from card
    where abs(coalesce(loss_ratio, 0) - coalesce(incurred_claims / nullif(earned_claims_reserve, 0), 0)) > 1e-6
       or abs(coalesce(cancel_rate, 0) - coalesce(contracts_cancelled / nullif(contracts_written, 0), 0)) > 1e-9
       or abs(coalesce(t12_loss_ratio, 0)
              - coalesce(t12_incurred_claims / nullif(t12_earned_claims_reserve, 0), 0)) > 1e-6
       or abs(coalesce(early_claim_rate, 0) - coalesce(early_claims / nullif(claims_reported, 0), 0)) > 1e-9
),

t12 as (
    select a.tenant_key, a.dealer_key, a.month_key, a.t12_contracts_written, a.t12_late_fees,
        sum(b.contracts_written) as w, sum(b.late_fees) as f
    from card as a
    join card as b
        on b.tenant_key = a.tenant_key and b.dealer_key = a.dealer_key
        and b.month_start between a.month_start - interval 11 months and a.month_start
    group by all
)

select * from ratio_fail
union all
select tenant_key, dealer_key, month_key, 't12_window'
from t12 where t12_contracts_written <> w or abs(t12_late_fees - f) > 0.05
