-- Dealer-integration network remittance lines: typed, minus rejected files and rows, one row per
-- batch x contract (a file re-sent after a row-count mismatch keeps the accepted copy).
with accepted as (
    {{ feed_accepted('remittance') }}
),

latest as (
    select *
    from accepted
    qualify row_number() over (
        partition by remit_batch_id, upper(trim(contract_number))
        order by _feed_date, _source_file, _row_number
    ) = 1
)

select
    remit_batch_id,
    {{ clean_code('dealer_number') }} as dealer_number,
    upper(trim(contract_number)) as contract_number,
    try_cast(remit_amount as double) as remit_amount,
    try_cast(remit_date as date) as remit_date,
    try_cast(entered_date as date) as entered_date,
    _feed_date as feed_date,
    _source_file,
    _row_number,
    _source_system,
    _loaded_at
from latest
