{{ config(materialized='table') }}
-- Cancellations from both systems with the Admin Core refund vocabulary.
-- Legacy kept only the customer refund (net of the cancel fee). At conversion the refund is put on
-- the Admin Core basis: refund_pct = the gross (pre-fee) refund over the refundable price, which is
-- the method's unexpired share (refund_share macro: pro rata, rule of 78 or full), coverage_cost_refund = admin_portion = (dealer_cost - admin_fee) x pct,
-- dealer_portion = (retail - dealer_cost) x pct, and the cancel fee is deducted afterwards from the
-- customer refund, exactly as Admin Core does (test core_refund_pct_matches_method).
-- is_reinstated: an APPROVED reinstatement reversed this cancellation (earning resumes).
-- V7 edit 41: reason_group and is_admin_correction come from seeds/core/cancel_reason_crosswalk.csv
-- (exact match on the normalized reason first, else the longest matching prefix). A cancel that
-- fixes a data-entry mistake is not a customer cancel: cancel_rate_excl_admin leaves it out.
-- is_reason_mapped = false fails test core_cancel_reason_mapped.
-- V7 edit 23: is_reversed = a later cancel row on the same contract nets this one to zero (the
-- source undid the cancel with an offsetting row); reversal rows themselves are flagged too.
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
        false as is_amount_sign_fixed,
        -- V7 edit 23 column contract: raw cancellation.reverses_cancel_id is set on the row that undoes
        -- an earlier cancel (NULL until the generator sends it).
        cast({{ column_or(ref('stg_admin_core__cancellation'), 'reverses_cancel_id', 'cast(null as bigint)', 'x') }} as varchar)
            as reverses_source_cancel_id
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
        is_amount_sign_fixed,
        cast(null as varchar) as reverses_source_cancel_id
    from legacy
),

unioned as (
    select * from core
    union all
    select * from legacy_split
),

xwalk as (
    select
        source_system,
        {{ cancel_reason_norm('source_reason') }} as reason_norm,
        match_type,
        reason_group,
        is_admin_correction
    from {{ ref('cancel_reason_crosswalk') }}
),

mapped as (
    select
        u.*,
        {{ cancel_reason_norm('u.reason') }} as reason_norm
    from unioned as u
),

matched as (
    select
        m.cancel_key,
        x.reason_group,
        x.is_admin_correction
    from mapped as m
    inner join xwalk as x
        on x.source_system = m.source_system
        and (
            (x.match_type = 'exact' and x.reason_norm = m.reason_norm)
            or (x.match_type = 'prefix' and starts_with(m.reason_norm, x.reason_norm))
        )
    qualify row_number() over (
        partition by m.cancel_key
        order by case when x.match_type = 'exact' then 0 else 1 end, length(x.reason_norm) desc
    ) = 1
),

-- Edit 23: a cancel undone in the source. Admin Core names the cancel it undoes
-- (reverses_cancel_id); a source without that link (legacy) is matched as an offsetting pair on one
-- contract: same customer refund with the opposite sign, the second row dated on or after the first.
reversals as (
    select a.cancel_key as original_key, b.cancel_key as reversal_key
    from unioned as a
    inner join unioned as b
        on b.source_system = a.source_system
        and b.reverses_source_cancel_id = a.source_cancel_id
    union
    select a.cancel_key, b.cancel_key
    from unioned as a
    inner join unioned as b
        on b.contract_key = a.contract_key
        and b.cancel_key <> a.cancel_key
        and b.source_system = 'legacy_admin'
        and a.customer_refund > 0
        and b.customer_refund = -a.customer_refund
        and b.cancel_date >= a.cancel_date
    qualify row_number() over (partition by a.cancel_key order by b.cancel_date, b.cancel_key) = 1
),

flagged as (
    select
        m.* exclude (reason_norm, reverses_source_cancel_id),
        coalesce(t.reason_group, case when m.reason_norm is null then 'NOT_GIVEN' end, 'UNMAPPED') as reason_group,
        coalesce(t.is_admin_correction, false) as is_admin_correction,
        t.cancel_key is not null or m.reason_norm is null as is_reason_mapped,
        exists (select 1 from reversals as r where r.original_key = m.cancel_key) as is_reversed,
        exists (select 1 from reversals as r where r.reversal_key = m.cancel_key) as is_reversal_row
    from mapped as m
    left join matched as t using (cancel_key)
)

select
    *,
    -- The cancellation that stops earning: the first one that was not reinstated or reversed.
    not (is_reinstated or is_reversed or is_reversal_row) and row_number() over (
        partition by contract_key, (is_reinstated or is_reversed or is_reversal_row) order by cancel_date, cancel_key
    ) = 1 as is_effective_cancel
from flagged
