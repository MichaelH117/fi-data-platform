-- Earned facts are never restated after close (docs/V3_DESIGN.md 2.5). A rebuild may only change a
-- closed period if a row could post to a period that was already closed when the event that
-- created it was recorded. The events are activation (first earning status) and void, so:
--   1. every EARN row posts no earlier than its earned month and the first period open on the
--      activation date (late remittance catch-up lands in the open period);
--   2. every VOID_REVERSAL posts no earlier than the first period open on the void date and nets the
--      contract's earning to zero;
--   3. fct_earned_disbursement and fct_contract_monthly carry the same posting keys as the spine.
with spine as (
    select m.*, pa.first_open_period_key as open_at_activation, pv.first_open_period_key as open_at_void
    from {{ ref('int_contract_months') }} as m
    left join {{ ref('int_posting_days') }} as pa on pa.date_day = cast(m.activated_at as date)
    left join {{ ref('int_posting_days') }} as pv on pv.date_day = cast(m.voided_at as date)
),

bad_spine as (
    select contract_key, posting_period_key, 'earn_row_before_activation_open_period' as reason
    from spine
    where row_type = 'EARN'
      and (activated_at is null
           or posting_period_key < earned_month_key
           or posting_period_key < coalesce(open_at_activation, {{ yyyymm('activated_at') }}))
    union all
    select contract_key, posting_period_key, 'void_reversal_before_void_open_period'
    from spine
    where row_type = 'VOID_REVERSAL'
      and posting_period_key < coalesce(open_at_void, {{ yyyymm('voided_at') }})
    union all
    select contract_key, null, 'voided_contract_not_netted_to_zero'
    from spine
    where voided_at is not null
    group by contract_key
    having abs(sum(earned_pct)) > 0.000001
),

bad_earned as (
    -- every earned-disbursement row maps to a spine (or cancel true-up) posting period no earlier
    -- than the contract's activation open period
    select e.contract_key, e.posting_period_key, 'earned_disbursement_before_activation_open_period' as reason
    from {{ ref('fct_earned_disbursement') }} as e
    inner join (
        select contract_key, min(coalesce(open_at_activation, {{ yyyymm('activated_at') }})) as first_post
        from spine group by 1
    ) as s using (contract_key)
    where e.posting_period_key < s.first_post
),

bad_monthly as (
    select f.contract_key, f.posting_period_key, 'contract_monthly_key_differs_from_spine' as reason
    from {{ ref('fct_contract_monthly') }} as f
    inner join spine as s
        on s.contract_key = f.contract_key and s.month_start = f.month_start and s.row_type = f.row_type
    where f.posting_period_key <> s.posting_period_key or f.earned_month_key <> s.earned_month_key
)

select * from bad_spine
union all
select * from bad_earned
union all
select * from bad_monthly
