{{ config(materialized='table') }}
-- One claim header per claim_key, both systems, one status vocabulary (Admin Core's).
-- Legacy statuses map: paid -> CLOSED_FULLY_PAID, open -> OPEN, denied -> DENIED.
with first_component as (
    select claim_id, lower(component_code) as repair_category
    from {{ ref('stg_admin_core__claim_component') }}
    where not is_deleted
    qualify row_number() over (partition by claim_id order by parts_amount + labor_amount desc, claim_component_id) = 1
),

core as (
    select
        b.claim_key,
        b.claim_number,
        b.source_system,
        b.contract_key,
        b.has_contract_key,
        b.is_converted,
        c.tenant_id,
        c.status,
        c.substatus,
        c.denied_reason,
        c.denied_category,
        c.repair_facility_id,
        c.adjuster_user_id,
        c.loss_date,
        cast(c.reported_at as date) as reported_date,
        c.odometer as odometer_at_loss,
        c.repair_order,
        coalesce(c.towing_flag, 0) = 1 as is_towing,
        coalesce(c.goodwill_flag, 0) = 1 as is_goodwill,
        coalesce(c.authorization_required, 0) = 1 as is_authorization_required,
        c.requested_amount,
        c.deductible_amount,
        fc.repair_category,
        false as is_amount_sign_fixed,
        c.updated_at
    from {{ ref('int_claim_number_bridge') }} as b
    inner join {{ ref('stg_admin_core__claim') }} as c on c.claim_id = b.admin_claim_id
    left join first_component as fc on fc.claim_id = c.claim_id
    where b.source_system = 'admin_core'
),

legacy as (
    select
        b.claim_key,
        b.claim_number,
        b.source_system,
        b.contract_key,
        b.has_contract_key,
        b.is_converted,
        cast(null as bigint) as tenant_id,
        case l.claim_status
            when 'paid' then 'CLOSED_FULLY_PAID'
            when 'denied' then 'DENIED'
            else 'OPEN'
        end as status,
        case l.claim_status when 'paid' then 'PAID' when 'denied' then 'NOTIFIED' end as substatus,
        cast(null as varchar) as denied_reason,
        cast(null as varchar) as denied_category,
        cast(null as bigint) as repair_facility_id,
        cast(null as bigint) as adjuster_user_id,
        l.loss_date,
        l.reported_date,
        l.odometer_at_loss,
        cast(null as varchar) as repair_order,
        false as is_towing,
        false as is_goodwill,
        false as is_authorization_required,
        l.amount_requested as requested_amount,
        cast(null as double) as deductible_amount,
        l.repair_category,
        l.is_amount_sign_fixed,
        l.updated_at
    from {{ ref('int_claim_number_bridge') }} as b
    inner join {{ ref('stg_legacy_admin__claims') }} as l on l.claim_number = b.source_claim_id
    where b.source_system = 'legacy_admin'
)

select
    x.* replace (coalesce(x.tenant_id, k.tenant_id) as tenant_id)
from (
    select * from core
    union all by name
    select * from legacy
) as x
left join {{ ref('int_contracts_conformed') }} as k using (contract_key)
