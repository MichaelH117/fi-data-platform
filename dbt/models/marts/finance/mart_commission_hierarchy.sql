-- Commission by district, region and nation, against volume targets (V6 edit 12).
-- Grain: level (DISTRICT | REGIONAL | NATIONAL) x node x product type x posting period.
-- District and region come from dim_agent (DE Manager); the monthly contract target per district x
-- product type (product category) from seed commission_volume_targets. A district's region is the
-- one its agents roll up to ('UNASSIGNED' when no agent sits in it).
--   contracts         contracts ENTERED in the month, by the selling agent's district
--   target_contracts  seed monthly_target_contracts, summed up the hierarchy (NULL when no target)
--   over_under        contracts - target (Over(+) / Under(-)); NULL when there is no target
--   commission        fct_commission posted in the period (paid, advances, chargebacks), so a level's
--                     commission sums to the commission ledger
-- V7 edit 28: every row carries its commission period (posting_period_key + commission_period_label),
-- the latest posting period in the ledger (as_of_period_key) and when the commission data was last
-- posted (data_as_of_ts), so the page header can say "Commission period <month>, data as of <ts>".
-- Contracts or commission with no district land in an explicit UNASSIGNED node (is_unassigned),
-- counted by diagnostic diag_commission_unassigned, never left blank.
-- Every month from the first activity to the latest posting period is kept for each node that has a
-- target, so a month with no sales still shows how far under target it was.
with agent_node as (
    select
        agent_key,
        coalesce(nullif(trim(district), ''), 'UNASSIGNED') as district,
        coalesce(nullif(trim(region), ''), 'UNASSIGNED') as region
    from {{ ref('dim_agent') }}
),

district_region as (
    select district, min(region) as region from agent_node group by 1
),

targets as (
    select t.district, coalesce(r.region, 'UNASSIGNED') as region, t.product_type, t.monthly_target_contracts
    from {{ ref('commission_volume_targets') }} as t
    left join district_region as r using (district)
),

sold as (
    select
        coalesce(n.district, 'UNASSIGNED') as district,
        coalesce(n.region, 'UNASSIGNED') as region,
        p.category_code as product_type,
        year(k.entered_date) * 100 + month(k.entered_date) as posting_period_key,
        count(*) as contracts
    from {{ ref('dim_contract') }} as k
    left join agent_node as n using (agent_key)
    left join {{ ref('dim_product') }} as p using (product_key)
    where k.contract_key::varchar <> '-1'
    group by all
),

paid as (
    select
        coalesce(n.district, 'UNASSIGNED') as district,
        coalesce(n.region, 'UNASSIGNED') as region,
        p.category_code as product_type,
        m.posting_period_key,
        sum(m.amount) as commission
    from {{ ref('fct_commission') }} as m
    left join agent_node as n using (agent_key)
    left join {{ ref('dim_contract') }} as k using (contract_key)
    left join {{ ref('dim_product') }} as p on p.product_key = k.product_key
    group by all
),

as_of as (
    select max(posting_period_key) as as_of_period from {{ ref('fct_transaction') }}
),

stamp as (
    select max(posted_at) as data_as_of_ts from {{ ref('fct_commission') }}
),

first_period as (
    select min(posting_period_key) as first_period from (
        select posting_period_key from sold union all select posting_period_key from paid
    )
),

spine as (
    select t.district, t.region, t.product_type, p.posting_period_key
    from targets as t
    cross join first_period as f
    cross join as_of as a
    inner join {{ ref('dim_posting_period') }} as p
        on p.posting_period_key between f.first_period and a.as_of_period
    union
    select district, region, product_type, posting_period_key from sold
    union
    select district, region, product_type, posting_period_key from paid
),

district_rows as (
    select
        s.district,
        s.region,
        s.product_type,
        s.posting_period_key,
        coalesce(so.contracts, 0) as contracts,
        t.monthly_target_contracts as target_contracts,
        coalesce(pd.commission, 0) as commission
    from spine as s
    left join sold as so using (district, region, product_type, posting_period_key)
    left join paid as pd using (district, region, product_type, posting_period_key)
    left join targets as t using (district, region, product_type)
),

levels as (
    select 'DISTRICT' as level, 1 as level_sort, district as node, region as parent_node, product_type,
        posting_period_key, contracts, target_contracts, commission
    from district_rows
    union all
    select 'REGIONAL', 2, region, 'NATIONAL', product_type, posting_period_key, sum(contracts),
        sum(target_contracts), sum(commission)
    from district_rows
    group by all
    union all
    select 'NATIONAL', 3, 'NATIONAL', null, product_type, posting_period_key, sum(contracts),
        sum(target_contracts), sum(commission)
    from district_rows
    group by all
)

select
    l.level,
    l.level_sort,
    l.node,
    l.parent_node,
    coalesce(l.product_type, 'UNKNOWN') as product_type,
    l.posting_period_key,
    strftime(p.period_start, '%b %Y') as commission_period_label,
    p.is_closed,
    a.as_of_period as as_of_period_key,
    st.data_as_of_ts,
    l.node = 'UNASSIGNED' as is_unassigned,
    l.contracts,
    l.target_contracts,
    l.contracts - l.target_contracts as over_under_contracts,
    l.commission,
    {{ audit_columns() }}
from levels as l
left join {{ ref('dim_posting_period') }} as p using (posting_period_key)
cross join as_of as a
cross join stamp as st
