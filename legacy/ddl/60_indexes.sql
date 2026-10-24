-- Indexes only on contract-id columns (never on dates: the earnings date has none on purpose).
create index on legacy_ops.tbl_contract ("ContractId");
create index on legacy_ops.tbl_claim ("fxContractId");
create index on legacy_ops.tbl_cancel ("fxContractId");
create index on legacy_finance.cube_transaction ("fxContractId");
create index on legacy_finance.cube_disbursement ("fxContractId");
create index on legacy_finance.cube_earned_disbursement ("fxContractId");
create index on legacy_reporting."FactContracts" ("fxContractId");
create index on legacy_reporting."FactCancels" ("fxContractId");
create index on legacy_reporting."FactClaims" ("fxContractId");
create index on legacy_reporting."FactEarnings" ("fxContractId");
create index on tenant_b.tbl_contract ("ContractId");
