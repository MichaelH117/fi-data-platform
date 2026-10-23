-- Dealer-integration network deals: typed, PII hashed, minus every rejected file or row
-- (anti-join on _quarantine), one row per deal_id (a duplicate resend keeps the first file).
with accepted as (
    {{ feed_accepted('deals') }}
),

latest as (
    select *
    from accepted
    qualify row_number() over (partition by deal_id order by _feed_date, _source_file, _row_number) = 1
)

select
    deal_id,
    {{ clean_code('dealer_number') }} as dealer_number,
    upper(trim(contract_number)) as contract_number,
    try_cast(sale_date as date) as sale_date,
    upper(trim(product_code)) as product_code,
    upper(trim(plan_code)) as plan_code,
    try_cast(term_months as integer) as term_months,
    try_cast(term_miles as integer) as term_miles,
    try_cast(odometer as integer) as odometer,
    try_cast(vehicle_year as integer) as vehicle_year,
    trim(vehicle_make) as vehicle_make,
    trim(vehicle_model) as vehicle_model,
    upper(trim(new_used)) as new_used,
    try_cast(msrp as double) as msrp,
    try_cast(purchase_price as double) as purchase_price,
    try_cast(coverage_cost as double) as coverage_cost,
    try_cast(retail_price as double) as retail_price,
    nullif(trim(surcharge_codes), '') as surcharge_codes,
    nullif(trim(option_codes), '') as option_codes,
    nullif(upper(trim(finance_type)), '') as finance_type,
    try_cast(finance_amount as double) as finance_amount,
    try_cast(apr as double) as apr,
    nullif(upper(trim(lienholder_code)), '') as lienholder_code,
    {{ hash_pii('customer_email') }} as customer_key,
    left(trim(customer_zip), 3) as customer_zip3,
    upper(trim(customer_state)) as customer_state,
    {{ hash_pii('upper(vin)') }} as vin_hash,
    {{ is_valid_vin('vin') }} as is_valid_vin,
    _feed_date as feed_date,
    _source_file,
    _row_number,
    _source_system,
    _loaded_at
from latest
