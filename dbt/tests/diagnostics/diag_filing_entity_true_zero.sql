-- Diagnostic (V7 edit 29): a filing entity with zero in-force must be a true zero. Every row of
-- seed filing_entity_map must resolve to an obligor, and an entity whose obligor has contracts in
-- core.dim_contract must also have rows in the filing mart; otherwise the zero on the state form
-- comes from a broken join, not from the book. The measured value (pipeline diagnostics) is how many
-- mapped entities show zero in-force contracts; that count never fails, it is the confirm-list.
with mapped as (
    select fm.provider_code, o.obligor_key
    from {{ ref('filing_entity_map') }} as fm
    left join {{ ref('dim_obligor') }} as o
        on o.obligor_kind = fm.obligor_kind
        and fm.source_id = case o.obligor_kind when 'dealer_owned' then o.participation_entity_id
                                               else o.obligor_id end
),

core_counts as (
    select current_obligor_key as obligor_key, count(*) as core_contracts
    from {{ ref('dim_contract') }}
    group by 1
),

filing_counts as (
    select provider_key as obligor_key, count(*) as filing_contracts
    from {{ ref('mart_funded_reserve_contracts') }}
    group by 1
)

select m.provider_code, m.obligor_key, coalesce(c.core_contracts, 0) as core_contracts,
    coalesce(f.filing_contracts, 0) as filing_contracts
from mapped as m
left join core_counts as c using (obligor_key)
left join filing_counts as f using (obligor_key)
where m.obligor_key is null
    or (coalesce(c.core_contracts, 0) > 0 and coalesce(f.filing_contracts, 0) = 0)
