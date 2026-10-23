-- Actuarial rating class per reportable contract: the cell a pricing or reserving model groups on.
with c as (
    select c.*
    from {{ ref('int_contracts_conformed') }} as c
    inner join {{ ref('int_reportable_scope') }} as s using (contract_key)
    where s.is_reportable
),

banded as (
    select
        contract_key,
        coalesce(class_code, 'UNRATED') as class_code,
        odometer_band,
        case
            when vehicle_year is null then 'unknown'
            when year(entered_date) - vehicle_year <= 1 then '0-1'
            when year(entered_date) - vehicle_year <= 4 then '2-4'
            when year(entered_date) - vehicle_year <= 7 then '5-7'
            else '8+'
        end as vehicle_age_band,
        term_band,
        new_used,
        subcategory_code
    from c
)

select
    *,
    concat_ws('|', subcategory_code, class_code, new_used, odometer_band, vehicle_age_band, term_band) as rating_cell,
    {{ audit_columns() }}
from banded
