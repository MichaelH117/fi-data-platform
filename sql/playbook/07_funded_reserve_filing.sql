-- pattern:  funded-reserve filing pull (inception-to-date vs reporting period, in-force vs status set)
-- question: For one provider entity, by reportable state: contracts written ITD and in the Sep 1 - Aug 31
--           reporting period, contracts in force at period end, gross consideration (coverage cost basis
--           AND retail price basis), claims paid, net, and the 40% calculated reserve.
-- trap:     three of them. (1) "in force" by today's status is not "in force at period end": a contract
--           cancelled after Aug 31 was in force on Aug 31, so the as-of rule (effective, expiration,
--           cancel date) is computed next to the status count. (2) claims are stored NEGATIVE in the ops
--           layer, so net = coverage + claims, not coverage - claims. (3) demo dealers and API test
--           contracts must leave the population before anything is counted.

set statement_timeout = '60s';

explain with pop as (
    select c."ContractId"
    from legacy_ops.tbl_contract c
    join legacy_ops.tbl_provider p on p."ProviderId" = c."fxProviderId"
    where p."ProviderCode" = 'PRV02'
)
select count(*)
from pop;

with pop as (
    select c."ContractId",
           c."Reportable State",
           c."CurrentContractStatus",
           c."EnteredDate",
           c."EffectiveDate",
           c."ExpirationDate",
           c."ContractPurchasePrice"
    from legacy_ops.tbl_contract c
    join legacy_ops.tbl_provider p on p."ProviderId" = c."fxProviderId"
    where p."ProviderCode" = 'PRV02'
        and c."ProductType" in ('VSC', 'ANCILLARY')
        and c."fxDealerId" not in (
            select "Dealer Id"
            from legacy_ops.dim_demo_dealers
        )
        and c."EnteredByUser" <> 'svc_api'
        and upper(coalesce(c."FullName", '')) <> 'TEST CUSTOMER'
        and c."EnteredDate" <= date '2026-08-31'
), x as (
    select "fxContractId",
           min("CancelDate") as cancel_date
    from legacy_ops.tbl_cancel
    group by 1
), cc as (
    select "fxContractId",
           "CoverageCost"
    from legacy_finance.vw_fact_contracts
    where "fxContractId" in (
        select "ContractId"
        from pop
    )
), cl as (
    select "fxContractId",
           sum("ClaimAmount") as claims_itd,
           sum("ClaimAmount") filter (where "PaidDate" between date '2025-09-01' and date '2026-08-31') as claims_period
    from legacy_ops.vw_dim_claims
    where "Claim Status" in ('CLOSED_FULLY_PAID', 'CLOSED_PARTIALLY_PAID', 'PAID')
        and "PaidDate" <= date '2026-08-31'
        and "fxContractId" in (
            select "ContractId"
            from pop
        )
    group by 1
), f as (
    select pop.*,
           cc."CoverageCost",
           coalesce(cl.claims_itd, 0) as claims_itd,
           coalesce(cl.claims_period, 0) as claims_period,
           pop."CurrentContractStatus" in ('PENDING', 'INFORCE', 'CANCELLED', 'EXPIRED') as is_written,
           pop."CurrentContractStatus" not in ('VOIDED', 'DENIED', 'PENDING', 'REINSTATEMENT_VOIDED', 'REINSTATEMENT_DENIED')
        and pop."EffectiveDate" <= date '2026-08-31'
        and coalesce(pop."ExpirationDate", date '9999-12-31') > date '2026-08-31'
        and coalesce(x.cancel_date, date '9999-12-31') > date '2026-08-31' as is_in_force_asof
    from pop
    left join x on x."fxContractId" = pop."ContractId"
    left join cc on cc."fxContractId" = pop."ContractId"
    left join cl on cl."fxContractId" = pop."ContractId"
)
select "Reportable State",
       count(*) filter (where is_written) as q1_written_itd,
       count(*) filter (where is_written and "EnteredDate" >= date '2025-09-01') as q1_written_period,
       count(*) filter (where "CurrentContractStatus" = 'INFORCE') as q2_in_force_by_status,
       count(*) filter (where is_in_force_asof) as q2_in_force_at_period_end,
       sum("CoverageCost") filter (where is_in_force_asof) as q3_gross_consideration_cost,
       sum("ContractPurchasePrice") filter (where is_in_force_asof) as q3_gross_consideration_retail,
       -sum(claims_itd) as q4_claims_paid_itd,
       -sum(claims_period) as q4_claims_paid_period,
       sum("CoverageCost") filter (where is_in_force_asof) + sum(claims_itd) filter (where is_in_force_asof) as q5_net_itd,
       round(0.40 * (sum("CoverageCost") filter (where is_in_force_asof) + sum(claims_itd) filter (where is_in_force_asof)), 2) as q6_reserve_40pct
from f
group by 1
order by 1;
