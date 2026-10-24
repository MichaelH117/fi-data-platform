-- pattern:  coverage-cost lineage, re-implemented scoped to a population
-- question: Coverage cost per product type for one provider's book in one state (provider 3, CA), computed the same way
--           the reporting contract view computes it (sale + reinstate + amend disbursements over the
--           deduped transaction bridge), and checked against the finance fact.
-- trap:     the reporting contract view builds its coverage cost in materialized CTEs over EVERY
--           disbursement before any outer where clause applies, so filtering it by provider runs the whole
--           book. Re-implement the lineage with the population first: pick the contract ids, then dedupe
--           and sum only their transactions. Same number, a fraction of the work. explain shows it.

set statement_timeout = '60s';

explain with pop as (
    select "ContractId"
    from legacy_ops.tbl_contract
    where "fxProviderId" = 3
        and "Reportable State" = 'CA'
), txn as (
    select "TransactionId",
           max("fxCashTransactionId") as cash_id
    from legacy_finance.cube_transaction
    where "fxContractId" in (
        select "ContractId"
        from pop
    )
    group by 1
)
select d."fxContractId",
       sum(d."Amount")
from legacy_finance.cube_disbursement d
join txn t on t."TransactionId" = d."fxTransactionId"
where d."fxTransactionTypeId" in (1, 3, 4)
    and d."fxContractId" in (
        select "ContractId"
        from pop
    )
group by 1;

with pop as (
    select "ContractId",
           "ProductType"
    from legacy_ops.tbl_contract
    where "fxProviderId" = 3
        and "Reportable State" = 'CA'
), txn as (
    select "TransactionId",
           max("fxCashTransactionId") as cash_id
    from legacy_finance.cube_transaction
    where "fxContractId" in (
        select "ContractId"
        from pop
    )
    group by 1
), cost as (
    select d."fxContractId",
           sum(d."Amount") as "CoverageCost"
    from legacy_finance.cube_disbursement d
    join txn t on t."TransactionId" = d."fxTransactionId"
    where d."fxTransactionTypeId" in (1, 3, 4)
        and d."fxContractId" in (
            select "ContractId"
            from pop
        )
    group by 1
), fin as (
    select "fxContractId",
           "CoverageCost"
    from legacy_finance.vw_fact_contracts
    where "fxContractId" in (
        select "ContractId"
        from pop
    )
)
select pop."ProductType",
       count(*) as contracts,
       count(cost."fxContractId") as with_cost,
       sum(cost."CoverageCost") as coverage_cost_lineage,
       sum(fin."CoverageCost") as coverage_cost_finance_fact,
       sum(cost."CoverageCost") - sum(fin."CoverageCost") as diff
from pop
left join cost on cost."fxContractId" = pop."ContractId"
left join fin on fin."fxContractId" = pop."ContractId"
group by 1
order by 1;
