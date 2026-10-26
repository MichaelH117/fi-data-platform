-- Regulatory contract detail (grain: one row per reportable contract, every state).
-- Feeds the filing's ITD / Period Contracts tabs, the market survey contract tab and the
-- regulatory line engine (mart_regulatory_line_values).
--
-- Provider = the contract's current obligor. Legacy contracts carry no obligor, so they get the
-- obligor that serves their reportable state on Admin Core (the state provider map, derived from
-- the data, one obligor per state). provider_basis says which rule applied; the filing notes count
-- the mapped contracts so a reviewer can see it.
-- Window flags use the state's FUNDED_RESERVE windows; they are null for states with no requirement.
with contracts as (
    select * from {{ ref('dim_contract') }}
    where contract_key <> '-1'
),

state_provider_map as (
    select reportable_state, arg_max(current_obligor_key, n) as obligor_key
    from (
        select reportable_state, current_obligor_key, count(*) as n
        from contracts
        where current_obligor_key <> '-1'
        group by all
    )
    group by 1
),

refunds as (
    select contract_key, sum(coverage_cost_refund) as coverage_cost_refund
    from {{ ref('fct_cancellation') }}
    where is_effective_cancel
    group by 1
),

scope_rule as (
    -- The filing's product scope (the same on every FUNDED_RESERVE line; tested).
    select any_value(category_filter) as category_filter, any_value(subcategory_exclude) as subcategory_exclude
    from {{ ref('report_definitions') }}
    where report_id = 'FUNDED_RESERVE' and measure <> 'DERIVED'
),

periods as (
    select
        state,
        max(window_start) filter (where scope = 'PERIOD') as period_start,
        max(window_end) filter (where scope = 'PERIOD') as period_end,
        bool_and(is_final) as is_final
    from {{ ref('mart_regulatory_windows') }}
    where report_id = 'FUNDED_RESERVE'
    group by 1
),

written as (
    select status from {{ ref('report_status_sets') }} where status_set_id = 'WRITTEN'
),

in_force as (
    select status from {{ ref('report_status_sets') }} where status_set_id = 'IN_FORCE'
),

base as (
    select
        c.contract_key,
        c.contract_number,
        c.source_system,
        c.tenant_key,
        c.reportable_state as state,
        case
            when c.current_obligor_key <> '-1' then c.current_obligor_key
            when m.obligor_key is not null then m.obligor_key
            else '-1'
        end as provider_key,
        case
            when c.current_obligor_key <> '-1' then 'CONTRACT_OBLIGOR'
            when m.obligor_key is not null then 'STATE_PROVIDER_MAP'
            else 'UNRESOLVED'
        end as provider_basis,
        c.original_obligor_key,
        c.dealer_key,
        c.dealer_number,
        c.insurer_key,
        c.product_key,
        c.status,
        c.category_code,
        c.subcategory_code,
        c.entered_date,
        c.effective_date,
        c.expiration_date,
        c.cancel_date,
        f.coverage_cost,
        f.retail_price,
        f.purchase_price,
        f.reserve_amount,
        coalesce(r.coverage_cost_refund, 0) as coverage_cost_refund
    from contracts as c
    left join state_provider_map as m on m.reportable_state = c.reportable_state
    left join {{ ref('dim_contract_finance') }} as f using (contract_key)
    left join refunds as r using (contract_key)
)

select
    b.contract_key,
    b.contract_number,
    b.source_system,
    b.tenant_key,
    b.state,
    b.provider_key,
    coalesce(fm.legal_entity_name, o.obligor_name, 'Unknown obligor') as provider_name,
    fm.provider_code,
    fm.filing_provider_code,
    fm.financial_security,
    b.provider_basis,
    b.original_obligor_key,
    b.dealer_key,
    b.dealer_number,
    d.dealer_name,
    b.insurer_key,
    coalesce(i.insurer_name, 'Unknown insurer') as insurer_name,
    b.product_key,
    b.status,
    b.category_code,
    coalesce(p.category, b.category_code) as category,
    b.subcategory_code,
    coalesce(p.subcategory, b.subcategory_code) as subcategory,
    p.product,
    b.entered_date,
    b.effective_date,
    b.expiration_date,
    b.cancel_date,
    b.coverage_cost,
    b.retail_price,
    b.purchase_price,
    b.reserve_amount,
    b.coverage_cost_refund,
    1 as contract_count,
    (sr.category_filter is null or list_contains(string_split(sr.category_filter, '|'), b.category_code))
    and (sr.subcategory_exclude is null
         or not list_contains(string_split(sr.subcategory_exclude, '|'), b.subcategory_code))
        as is_filing_product_scope,
    coalesce(s.has_requirement, false) as has_requirement,
    w.period_start,
    w.period_end,
    w.is_final,
    case when w.period_end is not null then
        b.status in (select status from written) and b.entered_date <= w.period_end
    end as is_written_itd,
    case when w.period_end is not null then
        b.status in (select status from written) and b.entered_date between w.period_start and w.period_end
    end as is_written_in_period,
    case when w.period_end is not null then
        b.status in (select status from in_force)
        and b.effective_date <= w.period_end
        and coalesce(b.expiration_date, date '9999-12-31') > w.period_end
        and coalesce(b.cancel_date, date '9999-12-31') > w.period_end
    end as is_in_force_at_period_end,
    {{ audit_columns() }}
from base as b
cross join scope_rule as sr
left join {{ ref('state_reserve_method') }} as s on s.state = b.state
left join periods as w on w.state = b.state
left join {{ ref('dim_obligor') }} as o on o.obligor_key = b.provider_key
-- V7 edit 29: the state form uses the legal name from seed filing_entity_map (internal providers by
-- obligor_id, dealer-owned obligors by participation_entity_id); the obligor dim name is the fallback.
left join {{ ref('filing_entity_map') }} as fm
    on fm.obligor_kind = o.obligor_kind
    and fm.source_id = case o.obligor_kind when 'dealer_owned' then o.participation_entity_id else o.obligor_id end
left join {{ ref('dim_dealer') }} as d on d.dealer_key = b.dealer_key
left join {{ ref('dim_insurer') }} as i on i.insurer_key = b.insurer_key
left join {{ ref('dim_product') }} as p on p.product_key = b.product_key
