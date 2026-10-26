-- V7 edits 34 + 42: portfolio facts pack. "The numbers a buyer asks for, with the method written next
-- to them." Grain: window x tenant scope (each tenant, plus COMBINED) x metric x product line.
-- Windows end at the last day of the latest CLOSED posting period (as_of_date):
--   ITD (inception to date), TTM (trailing twelve months), YTD (calendar year to date) and one full
--   calendar year CY<yyyy> for every earlier year with sales.
-- Contract population: every contract entered in the window except VOIDED and DENIED (they never went
-- live). Product line = dim_product.external_product_line, plus ALL.
-- Metrics (numerator / denominator kept, value = numerator / denominator for rates, blank with no
-- denominator, display rule 1):
--   ACTIVE_DEALERS               dealers with at least one non-voided contract in the window
--   DEALER_GROUPS                dealer groups those dealers belong to
--   CONTRACTS                    non-voided contracts entered in the window
--   OF_WHICH_BUNDLES             ... that are bundles (dim_product.is_bundle)
--   OF_WHICH_LIMITED_WARRANTY    ... that are limited warranties (category LIMITED_WARRANTY)
--   CANCEL_RATE_COHORT           contracts entered in the window that have cancelled by as_of_date /
--                                contracts entered in the window
--   CANCEL_RATE_FLOW             cancels dated in the window / contracts entered in the window
--   *_EXCL_ADMIN                 the same two rates leaving out admin-correction cancels (edit 41)
-- Cancels are effective cancels only (a reversed cancel is not a cancel, edit 23).
with as_of as (
    select cast(max(period_end) as date) as as_of_date
    from {{ ref('dim_posting_period') }}
    where is_closed
),

years as (
    select distinct year(k.entered_date) as yr
    from {{ ref('dim_contract') }} as k
    cross join as_of as a
    where k.contract_key <> '-1' and year(k.entered_date) < year(a.as_of_date)
),

windows as (
    select 'ITD' as window_id, 'Inception to date' as window_label, 1 as window_sort,
        date '1900-01-01' as window_start, a.as_of_date as window_end
    from as_of as a
    union all
    select 'TTM', 'Trailing twelve months', 2,
        cast(a.as_of_date - interval 12 month + interval 1 day as date), a.as_of_date
    from as_of as a
    union all
    select 'YTD', 'Year to date ' || year(a.as_of_date), 3,
        make_date(year(a.as_of_date), 1, 1), a.as_of_date
    from as_of as a
    union all
    select 'CY' || y.yr, 'Calendar year ' || y.yr, 10000 - y.yr, make_date(y.yr, 1, 1), make_date(y.yr, 12, 31)
    from years as y
),

cancels as (
    select contract_key, is_admin_correction, cancel_date
    from {{ ref('fct_cancellation') }}
    where is_effective_cancel
),

contract_cancel as (
    select
        x.contract_key,
        bool_or(true) as has_cancel,
        bool_or(not coalesce(x.is_admin_correction, false)) as has_cancel_excl_admin
    from cancels as x
    cross join as_of as a
    where x.cancel_date <= a.as_of_date
    group by 1
),

-- every contract counts once in its own tenant and once in COMBINED, and once in its product line and
-- once in ALL (a cross join, so each tenant has an ALL row and COMBINED has a row per product line)
scopes as (
    select * from (values ('TENANT', 'LINE'), ('TENANT', 'ALL'), ('COMBINED', 'LINE'), ('COMBINED', 'ALL'))
        as t(tenant_level, line_level)
),

contracts_base as (
    select
        k.contract_key,
        k.tenant_key,
        coalesce(p.external_product_line, 'UNMAPPED') as line,
        k.dealer_key,
        d.dealer_group_key,
        k.entered_date,
        coalesce(p.is_bundle, false) as is_bundle,
        k.category_code = 'LIMITED_WARRANTY' as is_limited_warranty,
        coalesce(cc.has_cancel, false) as has_cancel,
        coalesce(cc.has_cancel_excl_admin, false) as has_cancel_excl_admin
    from {{ ref('dim_contract') }} as k
    left join {{ ref('dim_product') }} as p on p.product_key = k.product_key
    left join {{ ref('dim_dealer') }} as d on d.dealer_key = k.dealer_key
    left join contract_cancel as cc on cc.contract_key = k.contract_key
    where k.contract_key <> '-1' and k.status not in ('VOIDED', 'DENIED') and k.tenant_key is not null
),

contracts as (
    select
        b.*,
        case s.tenant_level when 'TENANT' then cast(b.tenant_key as varchar) else 'COMBINED' end as tenant_scope,
        case s.line_level when 'LINE' then b.line else 'ALL' end as product_line
    from contracts_base as b
    cross join scopes as s
),

cancel_base as (
    select
        x.cancel_date,
        coalesce(x.is_admin_correction, false) as is_admin_correction,
        k.tenant_key,
        coalesce(p.external_product_line, 'UNMAPPED') as line
    from cancels as x
    inner join {{ ref('dim_contract') }} as k on k.contract_key = x.contract_key
    left join {{ ref('dim_product') }} as p on p.product_key = k.product_key
    where k.status not in ('VOIDED', 'DENIED') and k.tenant_key is not null
),

cancel_events as (
    select
        b.cancel_date,
        b.is_admin_correction,
        case s.tenant_level when 'TENANT' then cast(b.tenant_key as varchar) else 'COMBINED' end as tenant_scope,
        case s.line_level when 'LINE' then b.line else 'ALL' end as product_line
    from cancel_base as b
    cross join scopes as s
),

per_window as (
    select
        w.window_id, c.tenant_scope, c.product_line,
        count(*) as contracts,
        count(distinct c.dealer_key) as active_dealers,
        count(distinct c.dealer_group_key) as dealer_groups,
        count(*) filter (where c.is_bundle) as bundles,
        count(*) filter (where c.is_limited_warranty) as limited_warranty,
        count(*) filter (where c.has_cancel) as cohort_cancelled,
        count(*) filter (where c.has_cancel_excl_admin) as cohort_cancelled_excl_admin
    from windows as w
    inner join contracts as c on c.entered_date between w.window_start and w.window_end
    group by all
),

flow as (
    select
        w.window_id, e.tenant_scope, e.product_line,
        count(*) as flow_cancels,
        count(*) filter (where not e.is_admin_correction) as flow_cancels_excl_admin
    from windows as w
    inner join cancel_events as e on e.cancel_date between w.window_start and w.window_end
    group by all
),

facts as (
    select
        window_id, tenant_scope, product_line,
        coalesce(p.contracts, 0) as contracts,
        coalesce(p.active_dealers, 0) as active_dealers,
        coalesce(p.dealer_groups, 0) as dealer_groups,
        coalesce(p.bundles, 0) as bundles,
        coalesce(p.limited_warranty, 0) as limited_warranty,
        coalesce(p.cohort_cancelled, 0) as cohort_cancelled,
        coalesce(p.cohort_cancelled_excl_admin, 0) as cohort_cancelled_excl_admin,
        coalesce(f.flow_cancels, 0) as flow_cancels,
        coalesce(f.flow_cancels_excl_admin, 0) as flow_cancels_excl_admin
    from per_window as p
    full outer join flow as f using (window_id, tenant_scope, product_line)
),

long as (
    select window_id, tenant_scope, product_line, 'ACTIVE_DEALERS' as metric_id, 1 as metric_sort,
        active_dealers as numerator, null::bigint as denominator from facts where product_line = 'ALL'
    union all
    select window_id, tenant_scope, product_line, 'DEALER_GROUPS', 2, dealer_groups, null
    from facts where product_line = 'ALL'
    union all
    select window_id, tenant_scope, product_line, 'CONTRACTS', 3, contracts, null from facts
    union all
    select window_id, tenant_scope, product_line, 'OF_WHICH_BUNDLES', 4, bundles, null from facts
    union all
    select window_id, tenant_scope, product_line, 'OF_WHICH_LIMITED_WARRANTY', 5, limited_warranty, null
    from facts
    union all
    select window_id, tenant_scope, product_line, 'CANCEL_RATE_COHORT', 6, cohort_cancelled, contracts from facts
    union all
    select window_id, tenant_scope, product_line, 'CANCEL_RATE_COHORT_EXCL_ADMIN', 7,
        cohort_cancelled_excl_admin, contracts from facts
    union all
    select window_id, tenant_scope, product_line, 'CANCEL_RATE_FLOW', 8, flow_cancels, contracts from facts
    union all
    select window_id, tenant_scope, product_line, 'CANCEL_RATE_FLOW_EXCL_ADMIN', 9,
        flow_cancels_excl_admin, contracts from facts
)

select
    l.window_id,
    w.window_label,
    w.window_sort,
    w.window_start,
    w.window_end,
    a.as_of_date,
    l.tenant_scope,
    case l.tenant_scope when 'COMBINED' then 'Combined'
         else coalesce(t.reportable_name, 'Tenant ' || l.tenant_scope) end as tenant_label,
    l.product_line,
    l.metric_id,
    l.metric_sort,
    l.metric_id like 'CANCEL_RATE%' as is_rate,
    l.numerator,
    l.denominator,
    case when l.metric_id like 'CANCEL_RATE%' then round({{ ratio_or_blank('l.numerator', 'l.denominator') }}, 6)
         else l.numerator end as value,
    {{ audit_columns() }}
from long as l
inner join windows as w using (window_id)
cross join as_of as a
left join {{ ref('dim_tenant') }} as t on cast(t.tenant_key as varchar) = l.tenant_scope
