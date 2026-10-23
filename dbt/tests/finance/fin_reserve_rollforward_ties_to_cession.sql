-- The funded reserve reconciliation must tie with no plug, every tenant x period: book-wide closing
-- funded reserve (roll-forward) = participation reserve balance (cession) + rest of book recomputed
-- from the core facts, and closing funded - closing unearned = earned surplus recomputed from the
-- facts. Tolerance five cents (the surplus spread over tenant x category is a prorated split).
select tenant_key, posting_period_key, difference, earned_surplus_difference
from {{ ref('mart_funded_reserve_reconciliation') }}
where abs(difference) > 0.05 or abs(earned_surplus_difference) > 0.05
