-- V7 edit 25: Current Month Activations. "This month: what is still pending, what is active."
-- Grain: tenant x dealer x product family (VSC | GAP | OTHER) x entered month, for the month that
-- holds the reporting date and the three months before it. Pending / active use the dealer status
-- sets DEALER_PROD_PENDING and DEALER_PROD_IN_FORCE, the same split as the production pivot.
-- Product family comes from dim_product.external_product_line (VSC and GAP; everything else OTHER).
-- is_current_month marks the month still in progress (display rule 2).
with as_of as (
    select cast(as_of_date as date) as as_of_date, cast(date_trunc('month', as_of_date) as date) as as_of_month
    from {{ ref('dim_reporting_date') }}
),

buckets as (
    select status, report_id
    from {{ ref('dealer_report_status_sets') }}
    where report_id in ('DEALER_PROD_PENDING', 'DEALER_PROD_IN_FORCE')
),

contracts as (
    select
        c.tenant_key,
        c.dealer_key,
        case p.external_product_line when 'VSC' then 'VSC' when 'GAP' then 'GAP' else 'OTHER' end
            as product_family,
        cast(date_trunc('month', c.entered_date) as date) as month_start,
        b.report_id
    from {{ ref('dim_contract') }} as c
    cross join as_of as a
    inner join buckets as b on b.status = c.status
    left join {{ ref('dim_product') }} as p on p.product_key = c.product_key
    where c.contract_key <> '-1'
      and c.entered_date >= a.as_of_month - interval 3 month
      and c.entered_date <= a.as_of_date
)

select
    c.tenant_key,
    c.dealer_key,
    d.dealer_number,
    d.dealer_name,
    c.product_family,
    c.month_start,
    c.month_start = a.as_of_month as is_current_month,
    a.as_of_date,
    count(*) filter (where c.report_id = 'DEALER_PROD_PENDING') as contracts_pending,
    count(*) filter (where c.report_id = 'DEALER_PROD_IN_FORCE') as contracts_active,
    {{ audit_columns() }}
from contracts as c
cross join as_of as a
left join {{ ref('dim_dealer') }} as d on d.dealer_key = c.dealer_key
group by all
