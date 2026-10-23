-- Provider assignment is checkable at contract level: the state provider map is used only for
-- legacy contracts with no obligor, and the filing note count of mapped contracts equals the
-- contract detail (provider_basis = 'STATE_PROVIDER_MAP').
select contract_key, source_system, provider_basis, 'state map used on a non-legacy contract' as problem
from {{ ref('mart_funded_reserve_contracts') }}
where provider_basis = 'STATE_PROVIDER_MAP' and source_system <> 'legacy_admin'

union all

select s.state || '/' || s.provider_key, null, null, 'mapped_contracts note does not match the contract detail'
from (select distinct state, provider_key, mapped_contracts from {{ ref('mart_funded_reserve_summary') }}) as s
left join (
    select state, provider_key, count(*) as n
    from {{ ref('mart_funded_reserve_contracts') }}
    where provider_basis = 'STATE_PROVIDER_MAP' and is_filing_product_scope and is_written_itd
    group by all
) as d using (state, provider_key)
where s.mapped_contracts <> coalesce(d.n, 0)
