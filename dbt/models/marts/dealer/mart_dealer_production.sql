-- Dealer production: contracts written per dealer x product subcategory x entered month.
-- Grain: tenant x dealer x subcategory x month_start. Status set: seed dealer_report_status_sets
-- (DEALER_PRODUCTION). Reportable contracts only (core is already scoped by int_reportable_scope).
-- The pending / in force / cancelled split also comes from the seed (DEALER_PROD_PENDING,
-- DEALER_PROD_IN_FORCE, DEALER_PROD_CANCELLED), so a hold status (SUSPENDED, REINSTATEMENT_*) is
-- counted where its rule says, never dropped by a hard-coded status list.
with written as (
    select c.*
    from {{ ref('dim_contract') }} as c
    inner join {{ ref('dealer_report_status_sets') }} as s
        on s.report_id = 'DEALER_PRODUCTION' and s.status = c.status
    where c.contract_key <> '-1'
),

buckets as (
    select status, report_id
    from {{ ref('dealer_report_status_sets') }}
    where report_id in ('DEALER_PROD_PENDING', 'DEALER_PROD_IN_FORCE', 'DEALER_PROD_CANCELLED')
),

subcats as (
    select subcategory_code, min(category) as category, min(subcategory) as subcategory
    from {{ ref('dim_product') }}
    where subcategory_code is not null
    group by 1
)

select
    w.tenant_key,
    w.dealer_key,
    coalesce(d.dealer_number, w.dealer_number) as dealer_number,
    d.dealer_name,
    d.state as dealer_state,
    w.category_code,
    w.subcategory_code,
    coalesce(p.subcategory, w.subcategory_code) as subcategory,
    cast(date_trunc('month', w.entered_date) as date) as month_start,
    {{ yyyymm('w.entered_date') }} as entered_month_key,
    count(*) as contracts_written,
    count(*) filter (where bk.report_id = 'DEALER_PROD_PENDING') as contracts_pending,
    count(*) filter (where bk.report_id = 'DEALER_PROD_IN_FORCE') as contracts_in_force,
    count(*) filter (where bk.report_id = 'DEALER_PROD_CANCELLED') as contracts_cancelled,
    round(sum(coalesce(f.coverage_cost, 0)), 2) as coverage_cost,
    round(sum(coalesce(f.retail_price, 0)), 2) as retail_price,
    round(sum(coalesce(f.dealer_markup, 0)), 2) as dealer_markup,
    {{ audit_columns() }}
from written as w
left join {{ ref('dim_dealer') }} as d on d.dealer_key = w.dealer_key
left join {{ ref('dim_contract_finance') }} as f on f.contract_key = w.contract_key
left join buckets as bk on bk.status = w.status
left join subcats as p on p.subcategory_code = w.subcategory_code
group by all
