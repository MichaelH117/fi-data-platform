-- Regulatory claim detail (grain: one row per claim that resolves to a reportable contract).
-- Derived from fct_claim_payment_line (the only claims fact). closed_date = the posted date of the
-- last PAYMENT / DENIAL / CLOSE_ADJUST line of a claim whose status is closed; open claims have none.
-- paid_itd / paid_in_period use the state's FUNDED_RESERVE windows (null where no requirement).
with lines as (
    select
        claim_key,
        cast(posted_at as date) as posted_date,
        line_type,
        paid_amount
    from {{ ref('fct_claim_payment_line') }}
),

contracts as (
    select * from {{ ref('mart_funded_reserve_contracts') }}
),

claim_dates as (
    select
        claim_key,
        max(posted_date) filter (where line_type in ('PAYMENT', 'PAYMENT_2', 'DENIAL', 'CLOSE_ADJUST'))
            as last_close_line_date,
        sum(paid_amount) as paid_to_date
    from lines
    group by 1
),

windowed as (
    select
        l.claim_key,
        sum(l.paid_amount) filter (where l.posted_date <= c.period_end) as paid_itd,
        sum(l.paid_amount) filter (where l.posted_date between c.period_start and c.period_end) as paid_in_period
    from lines as l
    inner join {{ ref('dim_claim') }} as k using (claim_key)
    inner join contracts as c on c.contract_key = k.contract_key
    where c.period_end is not null
    group by 1
)

select
    k.claim_key,
    k.claim_number,
    k.source_system,
    c.contract_key,
    c.contract_number,
    c.tenant_key,
    c.state,
    c.provider_key,
    c.provider_name,
    c.provider_basis,
    c.dealer_number,
    c.dealer_name,
    c.insurer_name,
    c.status as contract_status,
    c.category_code,
    c.category,
    c.subcategory_code,
    c.subcategory,
    c.product,
    c.entered_date,
    k.status,
    k.substatus,
    k.status_group,
    k.denied_reason,
    k.loss_date,
    k.reported_date,
    case when k.status_group in ('paid', 'denied') then d.last_close_line_date end as closed_date,
    coalesce(d.paid_to_date, 0) as paid_to_date,
    1 as claim_count,
    c.is_filing_product_scope,
    c.has_requirement,
    c.period_start,
    c.period_end,
    case when c.period_end is not null then k.reported_date <= c.period_end end as is_reported_itd,
    case when c.period_end is not null then k.reported_date between c.period_start and c.period_end end
        as is_reported_in_period,
    case when c.period_end is not null then coalesce(w.paid_itd, 0) end as paid_itd,
    case when c.period_end is not null then coalesce(w.paid_in_period, 0) end as paid_in_period,
    {{ audit_columns() }}
from {{ ref('dim_claim') }} as k
inner join contracts as c on c.contract_key = k.contract_key
left join claim_dates as d on d.claim_key = k.claim_key
left join windowed as w on w.claim_key = k.claim_key
