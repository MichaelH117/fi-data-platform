-- Profit share earned ITD = dealer share x max(0, ITD earned reserve - ITD incurred - ceding fee), and
-- zero until the entity is seasoned. Recomputed from the seed terms, independent of the mart math.
with t as (
    select * from {{ ref('participation_structure_terms') }}
),

pe as (
    select
        p.participation_entity_key,
        coalesce(p.profit_share_pct, t.default_dealer_share_pct) as share_pct,
        t.ceding_fee_pct,
        t.min_seasoning_months
    from {{ ref('dim_participation_entity') }} as p
    inner join t on t.structure_type = p.entity_type
)

select s.participation_entity_key, s.posting_period_key, s.itd_profit_share_earned
from {{ ref('mart_profit_share_statement') }} as s
inner join pe using (participation_entity_key)
where abs(
    s.itd_profit_share_earned
    - case
        when s.months_seasoned >= pe.min_seasoning_months
            then greatest(s.itd_earned_reserve * (1 - pe.ceding_fee_pct) - s.itd_incurred_claims, 0) * pe.share_pct
        else 0
    end
) > 0.005
