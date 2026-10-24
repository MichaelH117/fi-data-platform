-- Legacy contracts: latest version per legacy_contract_id, typed, cleaned, PII hashed or dropped.
-- Also replays contracts that ingest quarantined only because sale_date came as MM/DD/YYYY.
-- Sign errors (negative money) are stored as absolute values and flagged.

with recovered as (
    select
        cast(raw_json::json ->> 'legacy_contract_id' as bigint) as legacy_contract_id,
        raw_json::json as r,
        _source_system,
        _source_version,
        _loaded_at,
        _is_deleted
    from {{ source('legacy_admin', '_quarantine') }}
    where table_name = 'contracts' and reject_reason = 'bad_date:sale_date'
),

recovered_rows as (
    select
        legacy_contract_id,
        r ->> 'contract_no' as contract_no,
        r ->> 'dealer_code' as dealer_code,
        r ->> 'producer' as producer,
        r ->> 'producer_code' as producer_code,
        r ->> 'product_code' as product_code,
        try_cast(r ->> 'plan_year' as bigint) as plan_year,
        try_cast(r ->> 'term_months' as bigint) as term_months,
        try_cast(r ->> 'term_miles' as double) as term_miles,
        try_cast(r ->> 'deductible' as double) as deductible,
        try_strptime(r ->> 'sale_date', '%m/%d/%Y')::date as sale_date,
        try_cast(r ->> 'effective_date' as date) as effective_date,
        try_cast(r ->> 'expiration_date' as date) as expiration_date,
        r ->> 'customer_first_name' as customer_first_name,
        r ->> 'customer_last_name' as customer_last_name,
        r ->> 'customer_email' as customer_email,
        r ->> 'customer_zip' as customer_zip,
        r ->> 'customer_state' as customer_state,
        r ->> 'vin' as vin,
        r ->> 'vehicle_make' as vehicle_make,
        r ->> 'vehicle_model' as vehicle_model,
        try_cast(r ->> 'vehicle_year' as bigint) as vehicle_year,
        r ->> 'new_used' as new_used,
        try_cast(r ->> 'odometer_at_sale' as bigint) as odometer_at_sale,
        try_cast(r ->> 'retail_price' as double) as retail_price,
        try_cast(r ->> 'dealer_cost' as double) as dealer_cost,
        try_cast(r ->> 'admin_fee' as double) as admin_fee,
        try_cast(r ->> 'reserve_amount' as double) as reserve_amount,
        try_cast(r ->> 'commission' as double) as commission,
        r ->> 'status' as status,
        try_cast(r ->> 'created_at' as timestamp) as created_at,
        try_cast(r ->> 'updated_at' as timestamp) as updated_at,
        try_cast(r ->> 'removed_from_source' as bigint) as removed_from_source,
        true as is_recovered_from_quarantine,
        _source_system,
        _source_version,
        _loaded_at,
        _is_deleted
    from recovered
),

unioned as (
    select
        legacy_contract_id, contract_no, dealer_code, producer, producer_code, product_code, plan_year,
        term_months, cast(term_miles as double) as term_miles, deductible, sale_date, effective_date,
        expiration_date, customer_first_name, customer_last_name, customer_email, customer_zip,
        customer_state, vin, vehicle_make, vehicle_model, vehicle_year, new_used, odometer_at_sale,
        retail_price, dealer_cost, admin_fee, reserve_amount, commission, status, created_at, updated_at,
        removed_from_source, false as is_recovered_from_quarantine,
        _source_system, _source_version, _loaded_at, _is_deleted
    from {{ source('legacy_admin', 'contracts') }}
    union all by name
    select * from recovered_rows where sale_date is not null
),

latest as (
    select *
    from unioned
    qualify row_number() over (
        partition by legacy_contract_id
        order by _source_version desc nulls last, _loaded_at desc, is_recovered_from_quarantine
    ) = 1
)

select
    legacy_contract_id,
    upper(trim(contract_no)) as contract_no,
    {{ clean_code('dealer_code') }} as dealer_code,
    {{ clean_code('dealer_code') }} is null as is_dealer_code_missing,
    -- Two ambiguous producer columns: `producer` is the servicing agent, `producer_code` the
    -- writing agent (they differ for reassigned books). Commission follows the writing agent.
    {{ clean_code('producer_code') }} as writing_agent_code,
    {{ clean_code('producer') }} as servicing_agent_code,
    upper(trim(product_code)) as product_code,
    plan_year,
    term_months,
    cast(term_miles as bigint) as term_miles,
    abs(deductible) as deductible,
    -- Sentinel dates (1900-01-01 placeholders) become NULL here, once, flagged in is_sentinel_date.
    {{ sentinel_to_null('sale_date') }} as sale_date,
    coalesce({{ sentinel_to_null('effective_date') }}, {{ sentinel_to_null('sale_date') }}) as effective_date,
    {{ sentinel_to_null('expiration_date') }} as expiration_date,
    datediff('day', coalesce({{ sentinel_to_null('effective_date') }}, {{ sentinel_to_null('sale_date') }}),
             {{ sentinel_to_null('expiration_date') }}) as term_days,
    {{ any_sentinel_date(['sale_date', 'effective_date', 'expiration_date']) }} as is_sentinel_date,

    -- PII: hashed keys only. The fake name stays in staging; email, zip and VIN are hashed here.
    customer_first_name,
    customer_last_name,
    {{ hash_pii('customer_email') }} as customer_key,
    left(trim(customer_zip), 3) as customer_zip3,
    upper(trim(customer_state)) as customer_state,
    {{ hash_pii('upper(vin)') }} as vin_hash,
    {{ is_valid_vin('vin') }} as is_valid_vin,

    trim(vehicle_make) as vehicle_make,
    trim(vehicle_model) as vehicle_model,
    vehicle_year,
    upper(trim(new_used)) as new_used,
    odometer_at_sale,

    abs(retail_price) as retail_price,
    abs(dealer_cost) as dealer_cost,
    abs(admin_fee) as admin_fee,
    abs(reserve_amount) as reserve_amount,
    abs(commission) as commission,
    (retail_price < 0 or dealer_cost < 0 or admin_fee < 0 or reserve_amount < 0 or commission < 0)
        is true as is_amount_sign_fixed,

    {{ legacy_contract_status('status') }} as status,
    is_recovered_from_quarantine,
    created_at,
    updated_at,
    coalesce(_is_deleted, false) or coalesce(removed_from_source, 0) = 1 as is_deleted,
    _source_system,
    _loaded_at
from latest
