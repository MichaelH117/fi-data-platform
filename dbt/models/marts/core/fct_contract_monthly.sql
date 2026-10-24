-- Grain: contract x calendar month x row_type, from the effective month to the month earning stops
-- (expiry, effective cancellation, void or the as-of date). Kept for actuarial/ and the loss-ratio marts.
--   row_type         EARN (one per contract x month)
--                    VOID_REVERSAL (reverses all earning of a contract voided after activation, in
--                    the period open on the void date)
--                    WRITTEN_CATCH_UP (an AMEND posted after the contract's first earning period:
--                    its written reserve plus its share of the months booked before it, in the
--                    AMEND's own posting period; month_start / earned_month_key = that period)
--   month_start / earned_month_key = the month being earned; posting_period_key = where it is
--                    booked (catch-up into the first open period when the month was already closed
--                    at activation, int_contract_months), so closed periods are never restated.
--   written_premium  = claims reserve written (SALE + AMEND): layers posted by the first earning
--                      period on the contract's first month, later AMENDs on their catch-up row
--   earned_premium   = reserve booked by the row's posting period x earned_pct of the month
--                      (+ the catch-up rows), so an AMEND never re-prices a closed period
--   incurred/paid    = from fct_claim_payment_line, by LOSS month (accident basis); a loss dated
--                      outside the earning window is placed in the nearest window month, so claim
--                      totals always tie to the claims fact.
with reserve_layers as (
    -- same "written" rule as int_disbursements_earned (SALE + AMEND, layered by posting period)
    select contract_key, posting_period_key as layer_period_key, sum(amount) as layer_amount
    from {{ ref('int_disbursements_split') }}
    where disbursement_type = 'CLAIMS_RESERVE' and txn_type in ('SALE', 'AMEND')
    group by 1, 2
),

months as (
    select
        m.*,
        min(case when m.row_type = 'EARN' then m.month_start end) over (partition by m.contract_key) as first_month,
        max(case when m.row_type = 'EARN' then m.month_start end) over (partition by m.contract_key) as last_month,
        min(case when m.row_type = 'EARN' then m.posting_period_key end) over (partition by m.contract_key)
            as first_posting_period_key
    from {{ ref('int_contract_months') }} as m
    inner join {{ ref('dim_contract') }} as k using (contract_key)
),

layer_class as (
    select
        l.*,
        f.first_posting_period_key is null or l.layer_period_key <= f.first_posting_period_key as is_base
    from reserve_layers as l
    left join (select distinct contract_key, first_posting_period_key from months) as f using (contract_key)
),

booked as (
    -- reserve booked by each spine row's posting period (a VOID reversal reverses the whole reserve)
    select
        m.contract_key,
        m.month_start,
        m.row_type,
        sum(l.layer_amount) filter (
            where m.row_type <> 'EARN' or l.is_base or l.layer_period_key <= m.posting_period_key
        ) as booked_reserve,
        sum(l.layer_amount) filter (where l.is_base) as base_reserve
    from months as m
    inner join layer_class as l using (contract_key)
    group by 1, 2, 3
),

catch_up as (
    -- a later AMEND layer: its share of every EARN month booked before its posting period
    select
        l.contract_key,
        l.layer_period_key,
        any_value(l.layer_amount) as layer_amount,
        coalesce(sum(m.earned_pct) filter (where m.posting_period_key < l.layer_period_key), 0)
            * any_value(l.layer_amount) as earned_premium
    from layer_class as l
    inner join months as m on m.contract_key = l.contract_key and m.row_type = 'EARN'
    where not l.is_base
    group by 1, 2
),

windows as (
    select distinct contract_key, first_month, last_month from months
),

claims_by_month as (
    select
        c.contract_key,
        greatest(w.first_month, least(w.last_month, cast(date_trunc('month', c.loss_date) as date))) as month_start,
        count(distinct c.claim_key) filter (where c.status_group <> 'denied') as claim_count,
        sum(f.paid_amount + f.pending_amount) as incurred_claims,
        sum(f.paid_amount) as paid_claims
    from {{ ref('dim_claim') }} as c
    inner join windows as w using (contract_key)
    left join {{ ref('fct_claim_payment_line') }} as f using (claim_key)
    group by all
),

spine_rows as (
    select
        m.contract_key,
        m.row_type,
        m.month_start,
        m.earned_month_key,
        m.posting_period_key,
        m.period_end,
        case when m.row_type = 'EARN' and m.month_start = m.first_month then coalesce(b.base_reserve, 0) else 0 end
            as written_premium,
        coalesce(b.booked_reserve, 0) * m.earned_pct as earned_premium,
        coalesce(b.booked_reserve, 0) as booked_reserve,
        m.earned_pct,
        m.earned_pct_cumulative,
        m.earned_days,
        m.exposure_years,
        m.is_in_force_eom
    from months as m
    left join booked as b using (contract_key, month_start, row_type)
),

catch_up_rows as (
    select
        contract_key,
        'WRITTEN_CATCH_UP' as row_type,
        {{ period_start_date('layer_period_key') }} as month_start,
        layer_period_key as earned_month_key,
        layer_period_key as posting_period_key,
        cast(null as date) as period_end,
        layer_amount as written_premium,
        earned_premium,
        cast(0 as double) as booked_reserve,
        cast(0 as double) as earned_pct,
        cast(null as double) as earned_pct_cumulative,
        0 as earned_days,
        cast(0 as double) as exposure_years,
        false as is_in_force_eom
    from catch_up
),

all_rows as (
    select * from spine_rows
    union all
    select * from catch_up_rows
)

select
    case when m.row_type = 'EARN' then {{ surrogate_key(['m.contract_key', 'm.month_start']) }}
         else {{ surrogate_key(['m.contract_key', 'm.month_start', 'm.row_type']) }} end as contract_month_key,
    m.row_type,
    m.contract_key,
    m.contract_key as contract_id,
    k.dealer_key,
    k.dealer_number as dealer_code,
    k.product_key,
    k.product_code,
    k.subcategory_code,
    k.tenant_key,
    v.new_used,
    m.month_start,
    m.earned_month_key,
    {{ date_key('m.month_start') }} as month_date_key,
    m.posting_period_key,
    {{ period_start_date("m.posting_period_key") }} as posting_period_start_date,
    cast(date_trunc('month', k.entered_date) as date) as sale_month,
    datediff('month', date_trunc('month', k.effective_date), m.month_start) as months_since_effective,
    m.written_premium,
    m.earned_premium,
    m.earned_pct,
    m.earned_pct_cumulative,
    case
        when m.row_type <> 'EARN' then 0
        when k.cancel_date is not null and k.cancel_date <= m.period_end then 0
        else m.booked_reserve * (1 - m.earned_pct_cumulative)
    end as unearned_premium_eom,
    m.earned_days,
    m.exposure_years,
    m.is_in_force_eom,
    coalesce(cl.claim_count, 0) as claim_count,
    coalesce(cl.incurred_claims, 0) as incurred_claims,
    coalesce(cl.paid_claims, 0) as paid_claims,
    {{ audit_columns() }}
from all_rows as m
inner join {{ ref('dim_contract') }} as k using (contract_key)
left join {{ ref('dim_contract_vehicle') }} as v using (contract_key)
-- Claims land on EARN rows only. The row-type filter sits inside the equality (NULL never matches) so
-- this stays a hash join; `and m.row_type = 'EARN'` as a separate ON term made DuckDB fall back to a
-- block-wise nested loop over 1.6M x 14k rows (about 45 s of the build).
left join claims_by_month as cl
    on cl.contract_key = m.contract_key
    and cl.month_start = case when m.row_type = 'EARN' then m.month_start end
