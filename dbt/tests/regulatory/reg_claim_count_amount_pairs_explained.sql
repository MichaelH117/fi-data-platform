-- Counting rule: a claim count and a claim dollar line of one report and scope either cover the same
-- contract population, or both carry a note that names the other line. FUNDED_RESERVE also needs a
-- like-for-like pair (an all-contracts amount line) so the count can be tied to dollars.
with defs as (
    select * from {{ ref('report_definitions') }}
    where report_id = 'FUNDED_RESERVE' and measure in ('CLAIM_COUNT', 'CLAIM_PAID_AMOUNT')
),

amounts as (
    select *, (date_rule = 'PAID_IN_WINDOW' and status_set_id = 'CONTRACT_ANY') as is_all_contracts
    from defs where measure = 'CLAIM_PAID_AMOUNT'
),

paid_counts as (
    select * from defs where line_item = 'CLAIMS_PAID_COUNT'
)

-- restricted amount lines without a comparability note
select a.scope, a.line_item, 'restricted amount line has no comparability note' as problem
from amounts as a
where not a.is_all_contracts
  and (a.notes is null or a.notes not ilike '%not comparable%')

union all

-- counts that do not point the reader to their dollar pair
select c.scope, c.line_item, 'claims paid count does not name its dollar line'
from paid_counts as c
where c.notes is null or c.notes not ilike '%line 2.6%'

union all

-- every scope needs a like-for-like all-contracts amount
select c.scope, c.line_item, 'no all-contracts amount line in this scope'
from paid_counts as c
where not exists (select 1 from amounts as a where a.scope = c.scope and a.is_all_contracts)
