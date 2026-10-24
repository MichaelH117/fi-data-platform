-- pattern:  dealer duplicate-identity lookup (modern star schema)
-- question: The same lookup on the modern dealer dim: does any dealer number map to more than one
--           current dealer row?
-- trap:     none expected. The modern dealer dim is SCD2 with one current row per dealer, and survivorship
--           happens in the pipeline, so this returns zero rows; history lives in valid_from / valid_to.

set statement_timeout = '30s';

select regexp_replace(dealer_number, '[^0-9]', '', 'g') as number_key,
       count(*) as current_rows,
       string_agg(dealer_name, ', ') as names
from core.dim_dealer
where is_current
group by 1
having count(*) > 1
order by 1;
