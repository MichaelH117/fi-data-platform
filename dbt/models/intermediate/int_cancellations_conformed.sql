{{ config(materialized='table') }}
-- Cancellations from both systems with the Admin Core refund vocabulary.
-- Legacy kept only the customer refund (net of the cancel fee). At conversion the refund is put on
-- the Admin Core basis: refund_pct = the gross (pre-fee) refund over the refundable price, which is
-- the method's unexpired share (refund_share macro: pro rata, rule of 78 or full), coverage_cost_refund = admin_portion = (dealer_cost - admin_fee) x pct,
-- dealer_portion = (retail - dealer_cost) x pct, and the cancel fee is deducted afterwards from the
-- customer refund, exactly as Admin Core does (test core_refund_pct_matches_method).
-- is_reinstated: an APPROVED reinstatement reversed this cancellation (earning resumes).
with reinstated as (
    select distinct cancel_id
    from {{ ref('stg_admin_core__reinstatement') }}
    where status = 'APPROVED' and not is_deleted
),

core as (
    select
        md5('admin_core|cancel|' || cast(x.cancel_id as varchar)) as cancel_key,
        {{ contract_key("'admin_core'", 'x.contract_id') }} as contract_key,
        'admin_core' as source_system,
        cast(x.cancel_id as varchar) as source_cancel_id,
        x.cancel_date,
        x.processed_at,
        x.processed_at as recorded_at,
        x.initiated_by,
        x.reason,
        x.method,
        x.odometer,
        x.contract_age_days,
        x.refund_pct,
        coalesce(x.cancel_fee, 0) as cancel_fee,
        coalesce(x.customer_refund, 0) as customer_refund,
        coalesce(x.admin_portion, 0) as admin_portion,
        coalesce(x.dealer_portion, 0) as dealer_portion,
        coalesce(x.coverage_cost_refund, 0) as coverage_cost_refund,
        coalesce(x.dealer_profit_refund, 0) as dealer_profit_refund,
        coalesce(x.admin_withheld_claims, 0) as admin_withheld_claims,
        r.cancel_id is not null as is_reinstated,
        false as is_amount_sign_fixed
    from {{ ref('stg_admin_core__cancellation') }} as x
    left join reinstated as r using (cancel_id)
    where not x.is_deleted
),

legacy as (
    select
        md5('legacy_admin|cancel|' || x.cancellation_id) as cancel_key,
        {{ contract_key("'legacy_admin'", 'x.legacy_contract_id') }} as contract_key,
        'legacy_admin' as source_system,
        x.cancellation_id as source_cancel_id,
        x.cancel_date,
        x.created_at as processed_at,
        x.created_at as recorded_at,
        cast(null as varchar) as initiated_by,
        x.cancel_reason as reason,
        x.cancel_method as method,
        x.odometer_at_cancel as odometer,
        datediff('day', c.effective_date, x.cancel_date) as contract_age_days,
        -- The share the source applied, recovered by grossing the refund up by the fee on the
        -- refundable price (retail - admin fee); the method's share when nothing was refunded.
        case
            when x.refund_amount > 0 and c.retail_price - coalesce(c.admin_fee, 0) > 0 then
                round(least(1, (x.refund_amount + coalesce(x.cancel_fee, 0))
                               / (c.retail_price - coalesce(c.admin_fee, 0))), 4)
            else {{ refund_share('x.cancel_method', "datediff('day', c.effective_date, x.cancel_date)", 'c.term_months') }}
        end as refund_pct,
        coalesce(x.cancel_fee, 0) as cancel_fee,
        x.refund_amount as customer_refund,
        c.dealer_cost,
        c.admin_fee,
        c.retail_price,
        x.is_amount_sign_fixed
    from {{ ref('stg_legacy_admin__cancellations') }} as x
    left join {{ ref('stg_legacy_admin__contracts') }} as c using (legacy_contract_id)
    where not x.is_deleted
),

legacy_split as (
    select
        cancel_key, contract_key, source_system, source_cancel_id, cancel_date, processed_at, recorded_at,
        initiated_by, reason, method, odometer, contract_age_days, refund_pct, cancel_fee, customer_refund,
        round(greatest(0, dealer_cost - coalesce(admin_fee, 0)) * refund_pct, 2) as admin_portion,
        round(greatest(0, retail_price - dealer_cost) * refund_pct, 2) as dealer_portion,
        round(greatest(0, dealer_cost - coalesce(admin_fee, 0)) * refund_pct, 2) as coverage_cost_refund,
        round(greatest(0, retail_price - dealer_cost) * refund_pct, 2) as dealer_profit_refund,
        0.0 as admin_withheld_claims,
        false as is_reinstated,
        is_amount_sign_fixed
    from legacy
)

select
    *,
    -- The cancellation that stops earning: the first one that was not reinstated.
    not is_reinstated and row_number() over (
        partition by contract_key, is_reinstated order by cancel_date, cancel_key
    ) = 1 as is_effective_cancel
from (
    select * from core
    union all
    select * from legacy_split
)
