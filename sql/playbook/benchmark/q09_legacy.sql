-- database: legacy
-- timeout: 30s
-- expect_error: cross-database references are not implemented
-- question: RV chassis make and model per contract, next to the contract's dealer and state.
-- trap:     the custom fields live in the admin system's own database. Postgres cannot join across
--           databases, so the one-query version fails; the analyst has to export one side and re-import it.
set statement_timeout = '30s';

select c."ContractId", c."Reportable State", max(v."Value") as chassis_make
from legacy_ops.tbl_contract c
join vsc_legacy_admin.admin_eav.contract_field v on v."fxContractId" = c."ContractId"
group by 1, 2;
