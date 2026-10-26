-- Contract core: one row per REPORTABLE contract (both systems) plus the unknown member '-1'.
-- The V1 one-wide-table contract dimension is split into six: this core plus terms, finance,
-- vehicle, actuarial class and claim summary, all at the same grain (contract_key).
with c as (
    select c.*
    from {{ ref('int_contracts_conformed') }} as c
    inner join {{ ref('int_reportable_scope') }} as s using (contract_key)
    where s.is_reportable
),

cancels as (
    select contract_key, cancel_date
    from {{ ref('int_cancellations_conformed') }}
    where is_effective_cancel
),

-- V7 edit 26: an add-on (ancillary / tech) contract points to the service contract it was sold
-- with through the free-text 'Parent Contract Number' custom field. Matched on the contract number
-- within the same tenant after trimming, upper-casing and removing inner spaces (so a lower-case or
-- padded entry still resolves); anything else (digits only, N/A, free text, a number that names no
-- reportable contract) gives no key and is counted by diag_parent_contract_link.
parents as (
    select
        {{ contract_key("'admin_core'", 'f.contract_id') }} as contract_key,
        p.contract_key as parent_contract_key
    from {{ ref('stg_admin_core__contract_custom_field') }} as f
    inner join c as p
        on p.source_system = 'admin_core'
        and p.tenant_id = f.tenant_id
        and upper(replace(trim(p.contract_number), ' ', '')) = upper(replace(trim(f.parent_contract_ref), ' ', ''))
    where f.parent_contract_ref is not null
)

select
    c.contract_key,
    c.contract_number,
    c.source_system,
    c.source_contract_id,
    c.status,
    c.entered_date,
    {{ date_key('c.entered_date') }} as entered_date_key,
    c.effective_date,
    {{ date_key('c.effective_date') }} as effective_date_key,
    c.expiration_date,
    {{ date_key('c.expiration_date') }} as expiration_date_key,
    c.entered_year,
    coalesce(d.dealer_key, '-1') as dealer_key,
    coalesce(d.dealer_number, 'UNKNOWN') as dealer_number,
    coalesce(a.agent_key, '-1') as agent_key,
    coalesce(p.product_key, '-1') as product_key,
    c.product_code,
    c.subcategory_code,
    c.category_code,
    c.plan_year,
    c.tenant_id as tenant_key,
    c.reportable_state,
    coalesce(md5('obligor|' || cast(c.original_obligor_id as varchar)), '-1') as original_obligor_key,
    coalesce(md5('obligor|' || cast(c.current_obligor_id as varchar)), '-1') as current_obligor_key,
    -- producer roles, by name (dim_producer): who SOLD it vs which provider entity BACKS it
    coalesce(d.dealer_key, '-1') as selling_dealer_key,
    coalesce(md5('obligor|' || cast(c.current_obligor_id as varchar)), '-1') as provider_entity_key,
    c.obligor_basis,
    coalesce(md5('insurer|' || cast(c.insurer_id as varchar)), '-1') as insurer_key,
    c.customer_key,
    c.deal_id,
    pc.parent_contract_key,
    x.cancel_date,
    {{ date_key('x.cancel_date') }} as cancel_date_key,
    x.cancel_date is not null as is_cancelled,
    c.is_dealer_code_missing,
    c.is_recovered_from_quarantine,
    c.updated_at,
    c._loaded_at,
    {{ audit_columns() }}
from c
-- dealer master: a contract booked on a duplicate / variant dealer id lands on the survivor
left join {{ ref('int_dealer_survivorship') }} as dm on dm.source_dealer_number = c.dealer_number
left join {{ ref('dim_dealer') }} as d on d.dealer_key = dm.dealer_key and d.dealer_key <> '-1'
left join {{ ref('dim_agent') }} as a on a.agent_id = c.agent_id
left join {{ ref('dim_product') }} as p on p.plan_id = c.plan_id and p.program_id = c.program_id
left join cancels as x using (contract_key)
left join parents as pc on pc.contract_key = c.contract_key and pc.parent_contract_key <> c.contract_key

union all by name

select
    '-1' as contract_key,
    'UNKNOWN' as contract_number,
    null as source_system,
    '-1' as dealer_key,
    'UNKNOWN' as dealer_number,
    '-1' as agent_key,
    '-1' as product_key,
    '-1' as original_obligor_key,
    '-1' as current_obligor_key,
    '-1' as selling_dealer_key,
    '-1' as provider_entity_key,
    '-1' as insurer_key,
    false as is_cancelled,
    false as is_dealer_code_missing,
    false as is_recovered_from_quarantine,
    current_timestamp as _built_at
