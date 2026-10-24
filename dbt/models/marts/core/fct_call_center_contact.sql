-- Call-center contacts (CRM export). Grain: one customer contact (call, chat or e-mail), call_id.
-- Contract resolved by int_call_contract_bridge (contract number, else a unique phone hash);
-- unresolved contacts keep contract_key / dealer_key '-1'. Contacts on demo / test contracts are
-- excluded (int_reportable_scope), like every other mart. A CLAIM_STATUS contact is attributed to
-- the contract's latest claim reported on or before the contact day (claim_key; NULL when none).
-- Not a financial fact, so it carries a date key and no posting period.
with calls as (
    select c.*, b.tenant_key, b.contract_key, b.contract_match_method
    from {{ ref('stg_crm__calls') }} as c
    inner join {{ ref('int_call_contract_bridge') }} as b using (call_id)
),

excluded as (
    select contract_key from {{ ref('int_reportable_scope') }} where not is_reportable
),

claims as (
    select claim_key, contract_key, reported_date
    from {{ ref('dim_claim') }}
    where contract_key <> '-1'
),

claim_link as (
    select c.call_id, cl.claim_key
    from calls as c
    inner join claims as cl on cl.contract_key = c.contract_key and cl.reported_date <= c.call_date
    where c.reason = 'CLAIM_STATUS'
    qualify row_number() over (partition by c.call_id order by cl.reported_date desc, cl.claim_key) = 1
)

select
    md5('call|' || c.call_id) as call_key,
    c.call_id,
    c.call_at,
    c.call_date,
    {{ date_key('c.call_date') }} as call_date_key,
    c.channel,
    c.queue,
    c.reason,
    c.disposition,
    coalesce(k.tenant_key, c.tenant_key) as tenant_key,
    c.contract_key,
    coalesce(k.dealer_key, '-1') as dealer_key,
    coalesce(k.product_key, '-1') as product_key,
    l.claim_key,
    c.contract_match_method,
    c.reason = 'COMPLAINT' as is_complaint,
    c.complaint_category,
    c.is_regulator_referral,
    c.handle_seconds,
    'crm' as source_system,
    {{ audit_columns() }}
from calls as c
left join {{ ref('dim_contract') }} as k on k.contract_key = nullif(c.contract_key, '-1')  -- equi-join (hash), not a nested loop
left join claim_link as l using (call_id)
where c.contract_key not in (select contract_key from excluded)
