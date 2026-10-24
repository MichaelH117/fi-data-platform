-- pattern:  producer vs provider filter trap
-- question: How many contracts does provider entity "Obligor 3" have?
-- trap:     the word "Producer" means two different things. In the ops contract view "Producers" is the
--           SELLING DEALER's name; in the reporting layer "Producer" is the PROVIDER ENTITY. Filtering the
--           ops column by the provider's name returns 0 rows, which looks like "no business" instead of
--           "wrong column". Check both lenses side by side, and use the producer dim (provider entity)
--           for provider-level reports. Avoid the reporting contract view here: it is a union spine that
--           scans every fact before the filter applies.

set statement_timeout = '30s';

select "ProducerId", "Producer" from legacy_reporting.vw_dim_producers where "Producer" ilike 'obligor 3%';

select (
    select count(*)
    from legacy_ops.vw_dim_contracts
    where "Producers" ilike 'obligor 3%'
) as via_ops_producers_column,
       (
           select count(distinct f."fxContractId")
           from legacy_reporting.vw_fact_contracts f
           join legacy_reporting.vw_dim_producers p on p."ProducerId" = f."fxProducerId"
           where p."Producer" ilike 'obligor 3%'
       ) as via_reporting_producer_dim,
       (
           select count(*)
           from legacy_ops.tbl_contract c
           join legacy_ops.tbl_provider p on p."ProviderId" = c."fxProviderId"
           where p."ProviderName" ilike 'obligor 3%'
       ) as via_ops_provider_table;
