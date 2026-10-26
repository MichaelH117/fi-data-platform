-- One row per claim, DERIVED from fct_claim_payment_line (the only claims fact) plus dim_claim.
-- Adds the dealer & claims analyst's operating fields: turnaround (reported -> closed), days from
-- contract effective to loss (early-claim window, seed dealer_claim_kpi_rules), open-claim aging
-- (seed dealer_age_bands, CLAIM_OPEN) and the top component by cost.
with lines as (
    select
        claim_key,
        count(*) as payment_lines,
        sum(authorized_change) as authorized_amount,
        sum(inspection_change) as inspection_amount,
        sum(paid_amount) as paid_amount,
        sum(pending_amount) as pending_amount,
        sum(paid_amount + pending_amount) as incurred_amount,
        min(posted_at) as first_posted_at,
        max(posted_at) filter (where paid_amount > 0) as last_paid_at
    from {{ ref('fct_claim_payment_line') }}
    group by 1
),

closed as (
    select claim_key, min(changed_at) as closed_at
    from {{ ref('fct_claim_history') }}
    where to_status in ('CLOSED_FULLY_PAID', 'CLOSED_PARTIALLY_PAID', 'DENIED')
    group by 1
),

components as (
    select
        b.claim_key,
        upper(cc.component_code) as component_code,
        coalesce(cc.parts_amount, 0) + coalesce(cc.labor_amount, 0) as component_cost,
        row_number() over (
            partition by b.claim_key
            order by coalesce(cc.parts_amount, 0) + coalesce(cc.labor_amount, 0) desc, cc.component_code
        ) as rn,
        count(*) over (partition by b.claim_key) as component_count
    from {{ ref('stg_admin_core__claim_component') }} as cc
    inner join {{ ref('int_claim_number_bridge') }} as b on b.admin_claim_id = cc.claim_id
    where not coalesce(cc.is_deleted, false)
),

windows as (
    select cast(unnest(string_split(rule_value, '|')) as integer) as window_days
    from {{ ref('dealer_claim_kpi_rules') }}
    where rule_id = 'early_claim_windows'
),

threshold as (
    select cast(rule_value as integer) as aging_days
    from {{ ref('dealer_claim_kpi_rules') }}
    where rule_id = 'open_aging_threshold_days'
),

as_of as (select cast(as_of_date as date) as as_of_date from {{ ref('dim_reporting_date') }}),

base as (
    select
        c.claim_key,
        c.claim_number,
        c.source_system,
        c.contract_key,
        k.contract_number,
        k.product_code,
        k.dealer_number,
        c.has_contract_key,
        coalesce(k.tenant_key, c.tenant_key) as tenant_key,
        coalesce(k.dealer_key, '-1') as dealer_key,
        coalesce(k.product_key, '-1') as product_key,
        coalesce(k.subcategory_code, 'UNKNOWN') as subcategory_code,
        coalesce(k.category_code, 'UNKNOWN') as category_code,
        c.status,
        c.substatus,
        c.status_group,
        c.denied_reason,
        c.denied_category,
        c.repair_category,
        c.requested_amount,
        c.deductible_amount,
        c.odometer_at_loss,
        c.loss_date,
        c.reported_date,
        cast(date_trunc('month', c.loss_date) as date) as loss_month,
        cast(date_trunc('month', c.reported_date) as date) as reported_month,
        k.effective_date as contract_effective_date,
        -- Matched = has a contract and the loss is on or after the sale (the V1 actuarial validity rule).
        coalesce(c.has_contract_key and c.loss_date >= k.entered_date, false) as is_contract_matched,
        datediff('day', k.effective_date, c.loss_date) as days_from_effective,
        coalesce(comp.component_code, upper(c.repair_category), 'UNKNOWN') as top_component,
        case when comp.component_code is not null then 'claim_component' else 'repair_category' end
            as component_source,
        coalesce(comp.component_count, 0) as component_count,
        coalesce(l.payment_lines, 0) as payment_lines,
        round(coalesce(l.authorized_amount, 0), 2) as authorized_amount,
        round(coalesce(l.inspection_amount, 0), 2) as inspection_amount,
        round(coalesce(l.paid_amount, 0), 2) as paid_amount,
        round(coalesce(l.pending_amount, 0), 2) as pending_amount,
        round(coalesce(l.incurred_amount, 0), 2) as incurred_amount,
        case when c.status_group <> 'open' then coalesce(cl.closed_at, l.last_paid_at) end as closed_at,
        a.as_of_date
    from {{ ref('dim_claim') }} as c
    cross join as_of as a
    left join {{ ref('dim_contract') }} as k on k.contract_key = nullif(c.contract_key, '-1')  -- equi-join (hash), not a nested loop
    left join lines as l on l.claim_key = c.claim_key
    left join closed as cl on cl.claim_key = c.claim_key
    left join components as comp on comp.claim_key = c.claim_key and comp.rn = 1
)

select
    b.*,
    case when b.closed_at is not null
         then greatest(datediff('day', b.reported_date, cast(b.closed_at as date)), 0) end as turnaround_days,
    (
        select min(w.window_days) from windows as w
        where b.status_group <> 'denied' and b.days_from_effective between 0 and w.window_days
    ) as early_claim_window_days,
    case when b.status_group = 'open' then datediff('day', b.reported_date, b.as_of_date) end as open_age_days,
    coalesce(
        b.status_group = 'open' and datediff('day', b.reported_date, b.as_of_date) > t.aging_days, false
    ) as is_aged_open,
    ob.band_label as open_age_band,
    {{ audit_columns() }}
from base as b
cross join threshold as t
left join {{ ref('dealer_age_bands') }} as ob
    on ob.band_set = 'CLAIM_OPEN' and b.status_group = 'open'
    and greatest(datediff('day', b.reported_date, b.as_of_date), 0) between ob.min_days and ob.max_days
