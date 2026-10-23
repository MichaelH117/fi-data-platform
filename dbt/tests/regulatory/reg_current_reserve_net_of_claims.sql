-- Line 3.6 is the reserve still held at the period end: the reserve funded at sale on the in-force
-- block minus the claims already paid on that same block (line 3.2). The funded reserve is recomputed
-- from contract detail (status set and product scope from the seeds); returns any state x provider x
-- scope x basis line where current_reserve <> funded - claims_paid_amount by more than a cent.
with d as (
    select * from {{ ref('report_definitions') }} where line_item = 'CURRENT_RESERVE'
),

w as (
    select distinct state, scope, period_start, period_end from {{ ref('mart_funded_reserve_summary') }}
),

funded as (
    select w.state, w.scope, c.provider_key, sum(coalesce(c.reserve_amount, 0)) as funded_reserve
    from w
    inner join d on d.scope = w.scope
    inner join {{ ref('mart_funded_reserve_contracts') }} as c on c.state = w.state
    inner join {{ ref('report_status_sets') }} as s on s.status_set_id = d.status_set_id and s.status = c.status
    where list_contains(string_split(d.category_filter, '|'), c.category_code)
      and (d.subcategory_exclude is null
           or not list_contains(string_split(d.subcategory_exclude, '|'), c.subcategory_code))
      and c.entered_date between coalesce(w.period_start, date '1900-01-01') and w.period_end
      and c.effective_date <= w.period_end
      and coalesce(c.expiration_date, date '9999-12-31') > w.period_end
      and coalesce(c.cancel_date, date '9999-12-31') > w.period_end
    group by all
)

select s.state, s.provider_key, s.scope, s.basis, s.current_reserve, s.claims_paid_amount,
       coalesce(f.funded_reserve, 0) as funded_reserve
from {{ ref('mart_funded_reserve_summary') }} as s
left join funded as f on f.state = s.state and f.provider_key = s.provider_key and f.scope = s.scope
where abs(s.current_reserve - (coalesce(f.funded_reserve, 0) - s.claims_paid_amount)) > 0.01
