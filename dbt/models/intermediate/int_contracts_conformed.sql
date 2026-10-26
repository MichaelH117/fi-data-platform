{{ config(materialized='table') }}
-- One row per contract from BOTH source systems, on the conformed contract_key, with one column
-- vocabulary. No PII (hashes only). Deleted source rows are dropped here.
--
-- Legacy contracts take their product from the complete Admin Core catalog (product_code +
-- plan_year), never from the stale legacy product table, so recent vintages are not lost on a
-- join (the "stale product dim" incident). Dealers resolve by cleaned dealer number.

with catalog as (
    select
        pl.plan_id,
        pl.plan_code,
        pl.deductible_amount as plan_deductible,
        p.product_id,
        p.product_code,
        p.plan_year,
        p.claim_wait_days,
        p.claim_wait_miles,
        p.coverage_limit,
        p.program_type,
        s.subcategory_code,
        c.category_code
    from {{ ref('stg_admin_core__plan') }} as pl
    inner join {{ ref('stg_admin_core__product') }} as p using (product_id)
    left join {{ ref('stg_admin_core__product_subcategory') }} as s using (subcategory_id)
    left join {{ ref('stg_admin_core__product_category') }} as c on s.category_id = c.category_id
    where not pl.is_deleted
),

dealers as (
    select * from {{ ref('stg_admin_core__dealer') }} where not is_deleted
),

sale_split as (
    -- Dealer-cost split booked on the SALE transaction.
    select
        d.contract_id,
        sum(d.amount) filter (where d.disbursement_type = 'ADMIN_FEE') as admin_fee,
        sum(d.amount) filter (where d.disbursement_type = 'CLAIMS_RESERVE') as reserve_amount,
        sum(d.amount) filter (where d.disbursement_type = 'COMMISSION') as commission,
        sum(d.amount) filter (where d.disbursement_type = 'INSURER_FEE') as insurer_fee,
        sum(d.amount) filter (where d.disbursement_type = 'PREMIUM_TAX') as premium_tax,
        sum(d.amount) filter (where d.disbursement_type = 'PROFIT_SHARE_RESERVE') as profit_share_reserve
    from {{ ref('stg_admin_core__disbursement') }} as d
    inner join {{ ref('stg_admin_core__transaction') }} as t using (txn_id)
    where t.txn_type = 'SALE' and not d.is_deleted and not t.is_deleted
    group by 1
),

-- V6 edit 9: the e-contracting feed carries a co-buyer; the admin contract row never does. The flag
-- (no name or address) travels on the conformed contract; the packet reads the names from staging.
feed_cobuyer as (
    select deal_id, true as has_feed_cobuyer
    from {{ ref('stg_network_feed__deals') }}
    where cobuyer_name is not null
),

core as (
    select
        {{ contract_key("'admin_core'", 'c.contract_id') }} as contract_key,
        'admin_core' as source_system,
        cast(c.contract_id as varchar) as source_contract_id,
        c.contract_number_clean as contract_number,
        c.tenant_id,
        c.dealer_id,
        d.dealer_number,
        false as is_dealer_code_missing,
        c.agent_id,
        cast(null as varchar) as servicing_agent_code,
        c.plan_id,
        c.program_id,
        c.rate_id,
        cat.product_id,
        cat.product_code,
        cat.subcategory_code,
        cat.category_code,
        cat.plan_year,
        c.status,
        c.entered_date,
        c.effective_date,
        c.expiration_date,
        c.expiration_mileage,
        c.term_months,
        c.term_miles,
        datediff('day', c.effective_date, c.expiration_date) as term_days,
        c.odometer,
        c.deductible_amount,
        c.deductible_name,
        c.claim_wait_days,
        c.claim_wait_miles,
        c.coverage_limit,
        c.purchase_price,
        c.coverage_cost,
        c.retail_price,
        c.msrp,
        c.finance_type,
        c.finance_amount,
        c.apr,
        c.lienholder_id,
        c.new_used,
        c.vin_hash,
        c.chassis_vin_hash,
        c.is_valid_vin,
        c.has_chassis_vin,
        c.vehicle_year,
        c.vehicle_make,
        c.vehicle_model,
        c.fuel_type,
        c.cylinders,
        c.drive_type,
        coalesce(c.is_commercial, 0) = 1 as is_commercial,
        c.title_brand,
        c.class_code,
        c.reportable_state,
        c.original_obligor_id,
        c.current_obligor_id,
        case when c.current_obligor_id is not null then 'SOURCE' else 'MISSING' end as obligor_basis,
        c.insurer_id,
        c.program_type,
        coalesce(fc.has_feed_cobuyer, false) as has_feed_cobuyer,
        c.customer_key,
        c.customer_zip3,
        c.deal_id,
        s.admin_fee,
        s.reserve_amount,
        s.commission,
        s.insurer_fee,
        s.premium_tax,
        s.profit_share_reserve,
        false as is_amount_sign_fixed,
        false as is_recovered_from_quarantine,
        c.created_at,
        c.updated_at,
        c._loaded_at
    from {{ ref('stg_admin_core__contract') }} as c
    left join dealers as d using (dealer_id)
    left join catalog as cat using (plan_id)
    left join sale_split as s using (contract_id)
    left join feed_cobuyer as fc on fc.deal_id = c.deal_id
    where not c.is_deleted
),

-- Conversion rule for the legacy book, which never recorded an obligor: the contract takes the
-- obligor that writes its reportable state on Admin Core (the most contracts; lowest id on a tie).
-- obligor_basis = 'CONVERSION_STATE_MAP' marks these rows, so filings can footnote them.
legacy_obligor_map as (
    select reportable_state, current_obligor_id as obligor_id
    from (
        select reportable_state, current_obligor_id,
               row_number() over (
                   partition by reportable_state order by count(*) desc, current_obligor_id
               ) as rn
        from core
        where current_obligor_id is not null and reportable_state is not null
        group by 1, 2
    )
    where rn = 1
),

legacy_base as (
    select
        l.*,
        d.dealer_id,
        d.tenant_id,
        d.program as dealer_program,
        a.agent_id
    from {{ ref('stg_legacy_admin__contracts') }} as l
    left join dealers as d on d.dealer_number = l.dealer_code
    left join {{ ref('stg_admin_core__agent') }} as a on a.agent_code = l.writing_agent_code
    where not l.is_deleted
),

legacy_plan as (
    -- Plan within the product year: same deductible if one exists, else the first plan.
    select
        l.legacy_contract_id,
        cat.*
    from legacy_base as l
    inner join catalog as cat
        on cat.product_code = l.product_code and cat.plan_year = l.plan_year
    qualify row_number() over (
        partition by l.legacy_contract_id
        order by (cat.plan_deductible = l.deductible) desc nulls last, cat.plan_id
    ) = 1
),

legacy as (
    select
        {{ contract_key("'legacy_admin'", 'l.legacy_contract_id') }} as contract_key,
        'legacy_admin' as source_system,
        cast(l.legacy_contract_id as varchar) as source_contract_id,
        l.contract_no as contract_number,
        coalesce(l.tenant_id, 1) as tenant_id,
        l.dealer_id,
        l.dealer_code as dealer_number,
        l.is_dealer_code_missing,
        l.agent_id,
        l.servicing_agent_code,
        lp.plan_id,
        pg.program_id,
        cast(null as bigint) as rate_id,
        lp.product_id,
        l.product_code,
        lp.subcategory_code,
        lp.category_code,
        l.plan_year,
        l.status,
        l.sale_date as entered_date,
        l.effective_date,
        l.expiration_date,
        l.odometer_at_sale + l.term_miles as expiration_mileage,
        l.term_months,
        l.term_miles,
        l.term_days,
        l.odometer_at_sale as odometer,
        l.deductible as deductible_amount,
        cast(null as varchar) as deductible_name,
        lp.claim_wait_days,
        lp.claim_wait_miles,
        lp.coverage_limit,
        cast(null as double) as purchase_price,
        l.dealer_cost as coverage_cost,
        l.retail_price,
        cast(null as double) as msrp,
        cast(null as varchar) as finance_type,
        cast(null as double) as finance_amount,
        cast(null as double) as apr,
        cast(null as bigint) as lienholder_id,
        l.new_used,
        l.vin_hash,
        cast(null as varchar) as chassis_vin_hash,
        l.is_valid_vin,
        false as has_chassis_vin,
        l.vehicle_year,
        l.vehicle_make,
        l.vehicle_model,
        cast(null as varchar) as fuel_type,
        cast(null as bigint) as cylinders,
        cast(null as varchar) as drive_type,
        false as is_commercial,
        cast(null as varchar) as title_brand,
        cast(null as varchar) as class_code,
        l.customer_state as reportable_state,
        om.obligor_id as original_obligor_id,
        om.obligor_id as current_obligor_id,
        case when om.obligor_id is not null then 'CONVERSION_STATE_MAP' else 'MISSING' end as obligor_basis,
        cast(null as bigint) as insurer_id,
        lp.program_type,  -- legacy rows take their catalog product's program type
        false as has_feed_cobuyer,
        l.customer_key,
        l.customer_zip3,
        cast(null as varchar) as deal_id,
        l.admin_fee,
        l.reserve_amount,
        l.commission,
        cast(null as double) as insurer_fee,
        cast(null as double) as premium_tax,
        cast(null as double) as profit_share_reserve,
        l.is_amount_sign_fixed,
        l.is_recovered_from_quarantine,
        l.created_at,
        l.updated_at,
        l._loaded_at
    from legacy_base as l
    left join legacy_plan as lp using (legacy_contract_id)
    left join {{ ref('stg_admin_core__program') }} as pg
        on pg.plan_id = lp.plan_id and pg.program_code = coalesce(l.dealer_program, 'RETAIL')
    left join legacy_obligor_map as om on om.reportable_state = l.customer_state
)

select
    *,
    year(entered_date) as entered_year,
    {{ odometer_band('odometer') }} as odometer_band,
    {{ term_band('term_months') }} as term_band
from (
    select * from core
    union all by name
    select * from legacy
)
