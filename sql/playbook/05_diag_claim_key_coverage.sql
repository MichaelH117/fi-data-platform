-- pattern:  diagnostics round 3, claim-key coverage
-- question: What share of claims carry a contract key that actually resolves, per source system? And do
--           claim numbers from the two systems collide once the zero padding is stripped?
-- trap:     converted claims are zero-padded and core claims are not. Bridging on ltrim(number, '0')
--           alone merges two different claims, so the bridge key must include the source system.

set statement_timeout = '60s';

with cl as (
    select case when "ClaimNumber" ~ '^0' then 'CONVERSION' else 'CORE' end as source,
           ltrim("ClaimNumber", '0') as claim_key,
           "fxContractId"
    from legacy_ops.tbl_claim
), other as (
    select case when "ClaimNumber" ~ '^0' then 'CORE' else 'CONVERSION' end as source,
           ltrim("ClaimNumber", '0') as claim_key
    from legacy_ops.tbl_claim
)
select cl.source,
       count(*) as claims,
       count(cl."fxContractId") as with_key,
       count(c."ContractId") as key_resolves,
       round(100.0 * count(c."ContractId") / count(*), 2) as coverage_pct,
       count(*) filter (where exists (
           select 1
           from other o
           where o.source = cl.source
               and o.claim_key = cl.claim_key
       )) as number_collisions
from cl
left join legacy_ops.tbl_contract c on c."ContractId" = cl."fxContractId"
group by 1
order by 1;
