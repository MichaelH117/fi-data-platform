-- Metric rule (docs/V3_DESIGN.md 3): the cession mart's ITD loss ratio must equal the metrics.yml
-- posted_loss_ratio definition (incurred / earned reserve) recomputed straight from the core
-- facts at the as-of period.
with ec as (
    select participation_entity_key, contract_key
    from {{ ref('mart_participation_contract') }}
    where is_ceded
),

earned as (
    select ec.participation_entity_key, sum(e.earned_reserve) as earned
    from {{ ref('fct_earned_disbursement') }} as e
    inner join ec using (contract_key)
    where e.disbursement_type = 'CLAIMS_RESERVE'
    group by 1
),

incurred as (
    select ec.participation_entity_key, sum(l.paid_amount + l.pending_amount) as incurred
    from {{ ref('fct_claim_payment_line') }} as l
    inner join ec using (contract_key)
    group by 1
),

latest as (
    select * from {{ ref('mart_cession_experience') }}
    qualify posting_period_key = max(posting_period_key) over (partition by participation_entity_key)
)

select m.participation_entity_key, m.itd_loss_ratio, coalesce(i.incurred, 0) / nullif(e.earned, 0) as expected
from latest as m
left join earned as e using (participation_entity_key)
left join incurred as i using (participation_entity_key)
where abs(coalesce(m.itd_loss_ratio, -1) - coalesce(coalesce(i.incurred, 0) / nullif(e.earned, 0), -1)) > 1e-9
