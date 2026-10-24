-- database: legacy_admin
-- timeout: 30s
-- question: Step two: pivot the key-value store in the admin database on its own (text values, missing rows
--           for blank fields), then hand the id list to the warehouse side by export and re-import.
set statement_timeout = '30s';

with p as (
    select v."fxContractId",
           max(v."Value") filter (where f."FieldName" = 'Chassis Make') as chassis_make,
           max(v."Value") filter (where f."FieldName" = 'Coach Model') as coach_model,
           count(distinct f."FieldName") as fields_present
    from admin_eav.contract_field v
    join admin_eav.field_definition f on f."FieldId" = v."fxFieldId"
    group by 1
)
select count(*) filter (where chassis_make is not null) as contracts_with_chassis_make,
       count(*) as contracts_with_any_field,
       count(*) filter (where fields_present < 5) as contracts_missing_a_field
from p;
