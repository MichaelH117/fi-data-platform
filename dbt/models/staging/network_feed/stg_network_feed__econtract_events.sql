-- Dealer-integration network eContract events (RATED, CONTRACTED, VOIDED): typed, minus rejected
-- files and rows, one row per event_id.
with accepted as (
    {{ feed_accepted('econtract_events') }}
),

latest as (
    select *
    from accepted
    qualify row_number() over (partition by event_id order by _feed_date, _source_file, _row_number) = 1
)

select
    event_id,
    upper(trim(contract_number)) as contract_number,
    upper(trim(event_type)) as event_type,
    try_cast(event_at as timestamp) as event_at,
    payload::json ->> 'deal_id' as deal_id,
    {{ clean_code("payload::json ->> 'dealer_number'") }} as dealer_number,
    try_cast(payload::json ->> 'coverage_cost' as double) as coverage_cost,
    _feed_date as feed_date,
    _source_file,
    _row_number,
    _source_system,
    _loaded_at
from latest
