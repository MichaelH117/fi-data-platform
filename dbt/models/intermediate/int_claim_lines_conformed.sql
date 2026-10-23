{{ config(materialized='table') }}
-- Claim payment lines from both systems, sign normalized (paid is always >= 0).
-- Admin Core: one row per claim_payment_line. Legacy has no line table, so each legacy claim
-- becomes one line: PAYMENT (paid claims), REQUEST (open: case reserve as pending) or DENIAL.
-- Converted claims come only from Admin Core (int_claim_number_bridge), so nothing is counted twice.
with core as (
    select
        md5('admin_core|line|' || cast(l.line_id as varchar)) as line_key,
        b.claim_key,
        b.contract_key,
        'admin_core' as source_system,
        cast(l.line_id as varchar) as source_line_id,
        l.line_type,
        l.posted_at,
        l.created_at as recorded_at,
        coalesce(l.authorized_change, 0) as authorized_change,
        coalesce(l.inspection_change, 0) as inspection_change,
        abs(coalesce(l.paid_amount, 0)) as paid_amount,
        coalesce(l.pending_amount, 0) as pending_amount,
        l.payee_account_holder_id,
        coalesce(l.paid_amount, 0) < 0 as is_sign_fixed
    from {{ ref('stg_admin_core__claim_payment_line') }} as l
    inner join {{ ref('int_claim_number_bridge') }} as b
        on b.admin_claim_id = l.claim_id and b.source_system = 'admin_core'
    where not l.is_deleted
),

legacy as (
    select
        md5('legacy_admin|line|' || c.claim_number) as line_key,
        b.claim_key,
        b.contract_key,
        'legacy_admin' as source_system,
        c.claim_number as source_line_id,
        case c.claim_status when 'paid' then 'PAYMENT' when 'denied' then 'DENIAL' else 'REQUEST' end as line_type,
        cast(coalesce(case when c.claim_status = 'paid' then c.paid_date end, c.reported_date) as timestamp)
            as posted_at,
        cast(coalesce(case when c.claim_status = 'paid' then c.paid_date end, c.reported_date) as timestamp)
            as recorded_at,
        case when c.claim_status = 'paid' then coalesce(c.amount_paid, 0) else 0 end as authorized_change,
        0.0 as inspection_change,
        case when c.claim_status = 'paid' then coalesce(c.amount_paid, 0) else 0 end as paid_amount,
        case when c.claim_status = 'open' then coalesce(c.amount_requested, 0) else 0 end as pending_amount,
        cast(null as bigint) as payee_account_holder_id,
        c.is_amount_sign_fixed as is_sign_fixed
    from {{ ref('stg_legacy_admin__claims') }} as c
    inner join {{ ref('int_claim_number_bridge') }} as b
        on b.source_claim_id = c.claim_number and b.source_system = 'legacy_admin'
)

select * from core
union all
select * from legacy
