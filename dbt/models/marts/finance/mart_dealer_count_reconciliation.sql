-- V7 edit 43: dealer-count reconciliation. "Several ways to count dealers, side by side, so two
-- analysts agree." Grain: basis x tenant scope (each tenant, plus COMBINED). Windows end at the last
-- day of the latest closed posting period, the same cut-off as mart_portfolio_facts.
--   TTM_NONVOIDED       dealers with a non-voided contract (not VOIDED / DENIED) in the last 12 months;
--                       the portfolio-facts "active dealers" definition
--   TTM_INCL_VOIDED     the same, counting voided and denied contracts too
--   ITD_NONVOIDED       dealers with a non-voided contract ever
--   ITD_ALL             dealers with any contract ever
--   DEALER_DIM_TOTAL    every current row in dim_dealer, sold or not
--   CROSS_TENANT_DEDUP  ITD_NONVOIDED with a dealer that sells for more than one tenant counted once
--                       (same name, city and state); only differs from ITD_NONVOIDED on COMBINED
-- difference_vs_active = count - the TTM_NONVOIDED count of the same scope, so each row says how far
-- it sits from the definition the pack uses.
with as_of as (
    select cast(max(period_end) as date) as as_of_date
    from {{ ref('dim_posting_period') }}
    where is_closed
),

dealers as (
    select
        dealer_key,
        tenant_key,
        upper(trim(dealer_name)) || '|' || upper(trim(coalesce(city, ''))) || '|' || upper(trim(coalesce(state, '')))
            as dealer_identity
    from {{ ref('dim_dealer') }}
    where is_current and dealer_key <> '-1' and tenant_key is not null
),

sold as (
    select
        k.dealer_key,
        d.dealer_identity,
        k.status not in ('VOIDED', 'DENIED') as is_nonvoided,
        k.entered_date > a.as_of_date - interval 12 month and k.entered_date <= a.as_of_date as is_ttm,
        k.entered_date <= a.as_of_date as is_itd,
        unnest([cast(k.tenant_key as varchar), 'COMBINED']) as tenant_scope  -- the only unnest: one row per scope
    from {{ ref('dim_contract') }} as k
    cross join as_of as a
    left join dealers as d on d.dealer_key = k.dealer_key
    where k.contract_key <> '-1' and k.tenant_key is not null
),

dim_rows as (
    select unnest([cast(tenant_key as varchar), 'COMBINED']) as tenant_scope, dealer_key from dealers
),

counts as (
    select tenant_scope, 'TTM_NONVOIDED' as basis, 1 as basis_sort,
        count(distinct dealer_key) filter (where is_ttm and is_nonvoided) as dealer_count
    from sold group by 1
    union all
    select tenant_scope, 'TTM_INCL_VOIDED', 2, count(distinct dealer_key) filter (where is_ttm) from sold group by 1
    union all
    select tenant_scope, 'ITD_NONVOIDED', 3, count(distinct dealer_key) filter (where is_itd and is_nonvoided)
    from sold group by 1
    union all
    select tenant_scope, 'ITD_ALL', 4, count(distinct dealer_key) filter (where is_itd) from sold group by 1
    union all
    select tenant_scope, 'DEALER_DIM_TOTAL', 5, count(distinct dealer_key) from dim_rows group by 1
    union all
    select tenant_scope, 'CROSS_TENANT_DEDUP', 6,
        count(distinct coalesce(dealer_identity, dealer_key)) filter (where is_itd and is_nonvoided)
    from sold group by 1
)

select
    c.basis,
    c.basis_sort,
    c.tenant_scope,
    case c.tenant_scope when 'COMBINED' then 'Combined'
         else coalesce(t.reportable_name, 'Tenant ' || c.tenant_scope) end as tenant_label,
    a.as_of_date,
    c.dealer_count,
    c.dealer_count - x.dealer_count as difference_vs_active,
    {{ audit_columns() }}
from counts as c
cross join as_of as a
left join counts as x on x.tenant_scope = c.tenant_scope and x.basis = 'TTM_NONVOIDED'
left join {{ ref('dim_tenant') }} as t on cast(t.tenant_key as varchar) = c.tenant_scope
