-- database: legacy
-- timeout: 60s
-- question: The book (contract count) for one provider entity, "Obligor 1".
-- trap:     in the ops contract view "Producers" is the SELLING DEALER's name. Asked for a "producer's"
--           book, the analyst filters that column by the provider's name and gets 0, which reads as
--           "no business" instead of "wrong column".
set statement_timeout = '60s';

select count(*) as contracts,
       (select count(*) from legacy_ops.tbl_contract c join legacy_ops.tbl_provider p
               on p."ProviderId" = c."fxProviderId" where p."ProviderName" ilike 'obligor 1 %') as right_column
from legacy_ops.vw_dim_contracts
where "Producers" ilike 'obligor 1 %';
