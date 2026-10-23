-- The pending report lists every reportable PENDING contract and nothing else.
select 'count_mismatch' as problem
from (select count(*) as n from {{ ref('mart_dealer_pending_contracts') }}) as m
cross join (select count(*) as n from {{ ref('dim_contract') }} where status = 'PENDING' and contract_key <> '-1') as c
where m.n <> c.n
union all
select 'not_pending'
from {{ ref('mart_dealer_pending_contracts') }} as p
inner join {{ ref('dim_contract') }} as c using (contract_key)
where c.status <> 'PENDING'
