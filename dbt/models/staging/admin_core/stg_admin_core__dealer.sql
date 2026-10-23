-- Admin Core dealers: latest version per dealer_id. Codes trimmed and upper-cased.
with latest as (
    {{ stg_admin_core('dealer', 'dealer_id') }}
)

select
    * replace (
        {{ clean_code('dealer_number') }} as dealer_number,
        upper(trim(state)) as state
    ),
    coalesce(is_demo, 0) = 1 as is_demo_dealer
from latest
