-- Reconciliation: call-center roll-ups tie to the call-contact fact.
--   * every accepted CRM contact is in fct_call_center_contact or on a demo / test contract;
--   * mart_claim_contact_by_dealer claim_status_contacts / claims_contacted / repeat_contact_claims
--     recompute exactly from fct_call_center_contact + dim_claim (the metric definitions in the mart);
--   * every contact attributed to a claim is a CLAIM_STATUS contact on that claim's contract.
with staged as (
    select count(*) as n from {{ ref('stg_crm__calls') }}
),

fact as (
    select count(*) as n from {{ ref('fct_call_center_contact') }}
),

excluded as (
    select count(*) as n
    from {{ ref('int_call_contract_bridge') }}
    where contract_key in (select contract_key from {{ ref('int_reportable_scope') }} where not is_reportable)
),

mart as (
    select sum(claim_status_contacts) as contacts, sum(claims_contacted) as contacted,
           sum(repeat_contact_claims) as repeats, sum(claims_reported) as reported
    from {{ ref('mart_claim_contact_by_dealer') }}
),

recomputed as (
    select sum(n) as contacts, count(*) as contacted, count(*) filter (where n >= 2) as repeats
    from (
        select f.claim_key, count(*) as n
        from {{ ref('fct_call_center_contact') }} as f
        inner join {{ ref('dim_claim') }} as c using (claim_key)
        where c.reported_date >= (select min(call_date) from {{ ref('fct_call_center_contact') }})
        group by 1
    )
),

bad_links as (
    select count(*) as n
    from {{ ref('fct_call_center_contact') }} as f
    inner join {{ ref('dim_claim') }} as c using (claim_key)
    where f.reason <> 'CLAIM_STATUS' or c.contract_key <> f.contract_key or c.reported_date > f.call_date
)

select 'staged contacts != fact + excluded' as check_name, s.n - f.n - e.n as diff
from staged as s, fact as f, excluded as e
where s.n <> f.n + e.n
union all
select 'claim_status_contacts', m.contacts - r.contacts from mart as m, recomputed as r
where m.contacts is distinct from r.contacts
union all
select 'claims_contacted', m.contacted - r.contacted from mart as m, recomputed as r
where m.contacted is distinct from r.contacted
union all
select 'repeat_contact_claims', m.repeats - r.repeats from mart as m, recomputed as r
where m.repeats is distinct from r.repeats
union all
select 'claim link not a status contact on that contract', n from bad_links where n > 0
