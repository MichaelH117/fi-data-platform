-- One row per claim (both systems) whose contract is reportable, plus claims entered without a
-- contract key (contract_key '-1', has_contract_key = false; they stay visible, never dropped).
-- Money is NOT here: every claim amount comes from fct_claim_payment_line.
with claims as (
    select cl.*
    from {{ ref('int_claims_conformed') }} as cl
    left join {{ ref('int_reportable_scope') }} as s using (contract_key)
    where cl.contract_key is null or s.is_reportable
)

select
    cl.claim_key,
    cl.claim_number,
    cl.source_system,
    coalesce(cl.contract_key, '-1') as contract_key,
    cl.has_contract_key,
    cl.is_converted,
    cl.tenant_id as tenant_key,
    cl.status,
    cl.substatus,
    case
        when cl.status = 'DENIED' then 'denied'
        when cl.status like 'CLOSED%' then 'paid'
        else 'open'
    end as status_group,
    cl.denied_reason,
    cl.denied_category,
    cl.adjuster_user_id,
    coalesce(md5('repair_facility|' || cast(cl.repair_facility_id as varchar)), '-1') as repair_facility_key,
    cl.repair_facility_id,
    cl.repair_category,
    cl.repair_order,
    cl.is_goodwill,
    cl.is_towing,
    cl.is_authorization_required,
    cl.loss_date,
    {{ date_key('cl.loss_date') }} as loss_date_key,
    cl.reported_date,
    {{ date_key('cl.reported_date') }} as reported_date_key,
    cl.odometer_at_loss,
    cl.requested_amount,
    cl.deductible_amount,
    datediff('day', cl.loss_date, cl.reported_date) as report_lag_days,
    datediff('day', cl.loss_date, cl.reported_date) > {{ var('late_report_days') }} as is_late_reported,
    cl.status not in ('DENIED') and cl.status not like 'CLOSED%'
        and datediff('day', cl.reported_date, a.as_of_date) > 30 as aging_30_plus,
    cl.is_amount_sign_fixed,
    cl.updated_at,
    {{ audit_columns() }}
from claims as cl
cross join {{ ref('int_as_of_date') }} as a
