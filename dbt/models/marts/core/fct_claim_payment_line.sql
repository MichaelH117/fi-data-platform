-- THE claims fact (the only one). Grain: one claim payment line, both systems.
-- paid_amount is always >= 0 (legacy negatives flipped in staging). pending_amount is the change
-- in outstanding case reserve, so incurred = sum(paid_amount) + sum(pending_amount).
-- posting_period_key = month of posted_at, subject to the period lock. Every per-claim or
-- per-contract roll-up (dim_contract_claim_summary, mart_claim_summary, ...) is derived from here.
select
    l.line_key,
    l.claim_key,
    c.contract_key,
    coalesce(k.dealer_key, '-1') as dealer_key,
    coalesce(k.product_key, '-1') as product_key,
    coalesce(k.tenant_key, c.tenant_key) as tenant_key,
    coalesce(md5('account_holder|' || cast(l.payee_account_holder_id as varchar)), '-1') as payee_account_holder_key,
    l.source_system,
    l.source_line_id,
    l.line_type,
    l.posted_at,
    {{ date_key('l.posted_at') }} as posted_date_key,
    {{ posting_period_key('l.posted_at', 'pp') }} as posting_period_key,
    {{ period_start_date(posting_period_key('l.posted_at', 'pp')) }} as posting_period_start_date,
    c.loss_date_key,
    c.reported_date_key,
    l.authorized_change,
    l.inspection_change,
    l.paid_amount,
    l.pending_amount,
    l.paid_amount + l.pending_amount as incurred_change,
    datediff('day', c.reported_date, cast(l.posted_at as date)) as process_days,
    case
        when datediff('day', c.reported_date, cast(l.posted_at as date)) <= 30 then '0-30'
        when datediff('day', c.reported_date, cast(l.posted_at as date)) <= 60 then '31-60'
        when datediff('day', c.reported_date, cast(l.posted_at as date)) <= 90 then '61-90'
        when datediff('day', c.reported_date, cast(l.posted_at as date)) <= 180 then '91-180'
        else '181+'
    end as age_bin,
    l.is_sign_fixed,
    {{ audit_columns() }}
from {{ ref('int_claim_lines_conformed') }} as l
inner join {{ ref('dim_claim') }} as c using (claim_key)
left join {{ ref('dim_contract') }} as k on k.contract_key = c.contract_key
{{ posting_period_join('l.recorded_at', 'pp') }}
