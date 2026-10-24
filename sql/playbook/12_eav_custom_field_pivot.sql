-- pattern:  EAV custom-field pivot
-- question: For every RV contract, one row with coach make / model, chassis make / VIN and model year,
--           turned from the admin system's key-value custom-field store into columns.
-- trap:     the store is a separate database (no join to the warehouse in one query), every value is
--           text, and a blank chassis VIN is a MISSING ROW, not an empty string. Pivot with
--           max() filter (where field = ...), positional group by on the contract, guard the integer cast
--           with a regex, and count missing fields instead of trusting that each contract has five rows.

set statement_timeout = '30s';

select f."FieldId",
       f."FieldName",
       f."DataType",
       count(v."ContractFieldId") as value_rows,
       count(distinct v."fxContractId") as contracts
from admin_eav.field_definition f
left join admin_eav.contract_field v on v."fxFieldId" = f."FieldId"
group by 1, 2, 3
order by 1;

select v."fxContractId",
       max(v."Value") filter (where f."FieldName" = 'Coach Make') as "Coach Make",
       max(v."Value") filter (where f."FieldName" = 'Coach Model') as "Coach Model",
       max(v."Value") filter (where f."FieldName" = 'Chassis Make') as "Chassis Make",
       max(nullif(trim(v."Value"), '')) filter (where f."FieldName" = 'Chassis VIN') as "Chassis VIN",
       max(case when v."Value" ~ '^\d{4}$' then v."Value"::int end) filter (where f."FieldName" = 'Model Year') as "Model Year",
       5 - count(distinct f."FieldName") as missing_fields
from admin_eav.contract_field v
join admin_eav.field_definition f on f."FieldId" = v."fxFieldId"
group by 1
order by 1;
