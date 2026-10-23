-- Data-quality warning (not a gate): RV contracts must carry a chassis VIN. The specialty-line
-- report lists offenders; this test surfaces the count in every dbt build.
{{ config(severity='warn') }}
select contract_key, contract_number, dealer_number
from {{ ref('mart_specialty_line') }}
where is_chassis_vin_missing
