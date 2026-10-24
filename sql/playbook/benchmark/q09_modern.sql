-- database: modern
-- timeout: 30s
-- question: RV chassis make per contract, next to the contract's state, in one query.
-- fix:      the custom fields are pivoted in staging into typed columns on dim_contract_vehicle, one row per
--           contract, joinable on contract_key like any other attribute.
set statement_timeout = '30s';

select count(v.chassis_make) as contracts_with_chassis_make, count(distinct c.reportable_state) as states
from core.dim_contract c
join core.dim_contract_vehicle v on v.contract_key = c.contract_key
where v.chassis_make is not null;
