-- The regulatory line engine (grain: report_id x state x window x provider x line_item x basis).
-- Every non-DERIVED row of seeds/regulatory/report_definitions becomes one value per window and
-- provider. The seed decides the measure, the status set, the date rule, the product scope and the
-- dollar basis, so a counting rule changes in the seed, not in this SQL. Missing combinations are
-- filled with 0 (a genuine zero is reported, never dropped).
--   contract measures: status set and date rule apply to the contract
--   CLAIM_COUNT:       status set applies to the claim; date rule REPORTED_ / CLOSED_IN_WINDOW
--   CLAIM_PAID_AMOUNT: status set and date rule pick the contracts; payments posted in the window
--   COMPLAINT_COUNT:   complaints on in-scope contracts received in the window
--   RESERVE_HELD:      remaining reserve balance = reserve funded at sale on the picked contracts minus
--                      claims paid on them posted on or before the window end (not floored)
with defs as (
    select * from {{ ref('report_definitions') }} where measure <> 'DERIVED'
),

lines as (
    select
        w.report_id, w.state, w.scope, w.window_id, w.window_label, w.window_start, w.window_end, w.is_final,
        coalesce(w.window_start, date '1900-01-01') as win_from,
        d.section, d.line_no, d.line_item, d.label, d.measure, d.date_rule, d.status_set_id, d.basis,
        d.category_filter, d.subcategory_exclude, d.aggregation
    from {{ ref('mart_regulatory_windows') }} as w
    inner join defs as d on d.report_id = w.report_id and d.scope = w.scope
),

sets as (
    select status_set_id, status from {{ ref('report_status_sets') }}
),

contracts as (
    select * from {{ ref('mart_funded_reserve_contracts') }}
),

claims as (
    select * from {{ ref('mart_funded_reserve_claims') }}
),

-- line x contract pairs that pass the product scope, the status set and the contract date rule
contract_hits as (
    select l.*, c.contract_key, c.provider_key, c.coverage_cost, c.retail_price, c.reserve_amount
    from lines as l
    inner join contracts as c on c.state = l.state
    inner join sets as s on s.status_set_id = l.status_set_id and s.status = c.status
    where l.measure in ('CONTRACT_COUNT', 'CONTRACT_AMOUNT', 'RESERVE_HELD', 'CLAIM_PAID_AMOUNT')
      and (l.category_filter is null or list_contains(string_split(l.category_filter, '|'), c.category_code))
      and (l.subcategory_exclude is null
           or not list_contains(string_split(l.subcategory_exclude, '|'), c.subcategory_code))
      and case l.date_rule
            when 'IN_FORCE_AT_END' then
                c.effective_date <= l.window_end
                and coalesce(c.expiration_date, date '9999-12-31') > l.window_end
                and coalesce(c.cancel_date, date '9999-12-31') > l.window_end
            when 'ENTERED_IN_WINDOW' then c.entered_date between l.win_from and l.window_end
            when 'IN_FORCE_ENTERED_IN_WINDOW' then
                c.entered_date between l.win_from and l.window_end
                and c.effective_date <= l.window_end
                and coalesce(c.expiration_date, date '9999-12-31') > l.window_end
                and coalesce(c.cancel_date, date '9999-12-31') > l.window_end
            when 'PAID_IN_WINDOW' then c.entered_date <= l.window_end
            else false
          end
),

contract_values as (
    select
        report_id, state, window_id, line_item, basis, provider_key,
        sum(case measure
                when 'CONTRACT_COUNT' then 1
                when 'CONTRACT_AMOUNT' then case basis when 'COVERAGE_COST' then coverage_cost
                                                       when 'RETAIL' then retail_price end
                when 'RESERVE_HELD' then reserve_amount
            end) as value
    from contract_hits
    where measure <> 'CLAIM_PAID_AMOUNT'
    group by all
),

-- claims paid out of the funded reserve: taken off RESERVE_HELD so the line is the balance still held
reserve_used as (
    select h.report_id, h.state, h.window_id, h.line_item, h.basis, h.provider_key, -sum(p.paid_amount) as value
    from contract_hits as h
    inner join {{ ref('fct_claim_payment_line') }} as p on p.contract_key = h.contract_key
    where h.measure = 'RESERVE_HELD'
      and cast(p.posted_at as date) <= h.window_end
    group by all
),

paid_values as (
    select h.report_id, h.state, h.window_id, h.line_item, h.basis, h.provider_key, sum(p.paid_amount) as value
    from contract_hits as h
    inner join {{ ref('fct_claim_payment_line') }} as p on p.contract_key = h.contract_key
    where h.measure = 'CLAIM_PAID_AMOUNT'
      and cast(p.posted_at as date) between h.win_from and h.window_end
    group by all
),

claim_values as (
    select l.report_id, l.state, l.window_id, l.line_item, l.basis, k.provider_key, count(*) as value
    from lines as l
    inner join claims as k on k.state = l.state
    inner join sets as s on s.status_set_id = l.status_set_id and s.status = k.status
    where l.measure = 'CLAIM_COUNT'
      and (l.category_filter is null or list_contains(string_split(l.category_filter, '|'), k.category_code))
      and (l.subcategory_exclude is null
           or not list_contains(string_split(l.subcategory_exclude, '|'), k.subcategory_code))
      and case l.date_rule
            when 'REPORTED_IN_WINDOW' then k.reported_date between l.win_from and l.window_end
            when 'CLOSED_IN_WINDOW' then k.closed_date between l.win_from and l.window_end
            else false
          end
    group by all
),

complaints as (
    select x.complaint_id, x.received_date, c.state, c.provider_key, c.category_code, c.subcategory_code
    from {{ ref('stg_admin_core__complaint') }} as x
    inner join contracts as c on c.contract_key = {{ contract_key("'admin_core'", 'x.contract_id') }}
    where not x.is_deleted
),

complaint_values as (
    select l.report_id, l.state, l.window_id, l.line_item, l.basis, x.provider_key, count(*) as value
    from lines as l
    inner join complaints as x on x.state = l.state
    where l.measure = 'COMPLAINT_COUNT'
      and (l.category_filter is null or list_contains(string_split(l.category_filter, '|'), x.category_code))
      and (l.subcategory_exclude is null
           or not list_contains(string_split(l.subcategory_exclude, '|'), x.subcategory_code))
      and x.received_date between l.win_from and l.window_end
    group by all
),

all_values as (
    select * from contract_values
    union all by name select * from paid_values
    union all by name select * from reserve_used
    union all by name select * from claim_values
    union all by name select * from complaint_values
),

providers as (
    select distinct state, provider_key, provider_name from contracts
)

select
    -- one non-null key for the grain (basis is NULL on count lines, so the 6 grain columns cannot
    -- be a primary key on their own; surrogate_key spells NULL as '~null~')
    {{ surrogate_key(['l.report_id', 'l.state', 'l.window_id', 'p.provider_key', 'l.line_item', 'l.basis']) }}
        as line_value_key,
    l.report_id,
    l.state,
    l.scope,
    l.window_id,
    l.window_label,
    l.window_start,
    l.window_end,
    l.is_final,
    p.provider_key,
    p.provider_name,
    l.section,
    l.line_no,
    l.line_item,
    l.label,
    l.measure,
    l.basis,
    l.aggregation,
    round(cast(coalesce(v.value, 0) as double), 2) as value,
    {{ audit_columns() }}
from lines as l
inner join providers as p on p.state = l.state
left join (
    select report_id, state, window_id, line_item, basis, provider_key, sum(value) as value
    from all_values group by all
) as v
    on v.report_id = l.report_id
    and v.state = l.state
    and v.window_id = l.window_id
    and v.line_item = l.line_item
    and v.basis is not distinct from l.basis
    and v.provider_key = p.provider_key
