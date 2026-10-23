-- Call-center contacts per claim, by dealer and claim-reported month. Grain: dealer_key x
-- reported_month, for claims reported on or after the CRM go-live (the first contact day), so a
-- month before the CRM existed never shows a false 0% contact rate.
--   claims_reported        claims reported in the month (dim_claim, contract resolved)
--   claims_contacted       of those, claims with >= 1 CLAIM_STATUS contact (fct_call_center_contact)
--   repeat_contact_claims  of those, claims with >= 2 CLAIM_STATUS contacts
--   claim_status_contacts  CLAIM_STATUS contacts attributed to those claims
--   claim_contact_rate     = claims_contacted / claims_reported
--   repeat_contact_rate    = repeat_contact_claims / claims_contacted
--   contacts_per_claim     = claim_status_contacts / claims_reported
-- Every number is derived from fct_call_center_contact and dim_claim (tests/assert in
-- dbt/tests/core/assert_claim_contact_by_dealer_ties.sql).
with crm_start as (
    select min(call_date) as first_contact_date from {{ ref('fct_call_center_contact') }}
),

per_claim as (
    select claim_key, count(*) as contacts
    from {{ ref('fct_call_center_contact') }}
    where claim_key is not null
    group by 1
),

claims as (
    select
        coalesce(k.dealer_key, '-1') as dealer_key,
        coalesce(k.tenant_key, c.tenant_key) as tenant_key,
        date_trunc('month', c.reported_date)::date as reported_month,
        c.claim_key,
        coalesce(p.contacts, 0) as contacts
    from {{ ref('dim_claim') }} as c
    inner join {{ ref('dim_contract') }} as k on k.contract_key = c.contract_key
    cross join crm_start as s
    left join per_claim as p on p.claim_key = c.claim_key
    where c.claim_key <> '-1' and c.contract_key <> '-1' and c.reported_date >= s.first_contact_date
)

select
    dealer_key,
    tenant_key,
    reported_month,
    {{ yyyymm('reported_month') }} as reported_month_key,
    count(*) as claims_reported,
    count(*) filter (where contacts >= 1) as claims_contacted,
    count(*) filter (where contacts >= 2) as repeat_contact_claims,
    sum(contacts) as claim_status_contacts,
    {{ safe_divide('count(*) filter (where contacts >= 1)', 'count(*)') }} as claim_contact_rate,
    {{ safe_divide('count(*) filter (where contacts >= 2)', 'count(*) filter (where contacts >= 1)') }}
        as repeat_contact_rate,
    {{ safe_divide('sum(contacts)', 'count(*)') }} as contacts_per_claim,
    {{ audit_columns() }}
from claims
group by 1, 2, 3
