-- State in-force contract list (grain: one row per contract in force on the list date, every state).
-- Compliance asks for "every service contract in force in <state> on <date>" with the form code and
-- the lienholder. Built on mart_funded_reserve_contracts, so the product scope (is_filing_product_scope)
-- and the resolved provider are the same ones the funded-reserve filing uses.
--
-- In force on the list date: status in the IN_FORCE status set (seeds/regulatory), effective on or
-- before the date, not expired and not cancelled at that date (the same point-in-time rule as line 2.1).
-- The list date is the reporting as-of date (dim_reporting_date).
--
-- Derived here until the core dims carry them (docs/V3_DESIGN.md section 9, edit 4):
--   current_form_code / form_code_source: the latest non-deleted CONTRACT document of the contract.
--     Never inferred from product x state: no document or a document without a form code = 'missing'.
--   obligor_kind comes from dim_obligor (internal_provider / dealer_owned); NULL when the provider is
--     unresolved.
-- program_type comes from dim_contract_finance (V6 edit 3); legacy contracts have none.
-- lienholder_address / lienholder_state are not in any source yet; the columns are kept (NULL) so the
-- workbook layout does not change when they arrive.
with as_of as (
    select cast(as_of_date as date) as list_as_of_date from {{ ref('dim_reporting_date') }}
),

in_force as (
    select status from {{ ref('report_status_sets') }} where status_set_id = 'IN_FORCE'
),

contract_doc as (
    select
        {{ contract_key("'admin_core'", 'contract_id') }} as contract_key,
        form_code,
        document_name,
        generated_at
    from {{ ref('stg_admin_core__contract_document') }}
    where document_type = 'CONTRACT'
      and not is_deleted
      and not is_document_deleted
    qualify row_number() over (partition by contract_id order by generated_at desc, document_id desc) = 1
)

select
    c.contract_key,
    c.state,
    a.list_as_of_date,
    c.entered_date as contract_date,
    c.contract_number,
    c.product as product_name,
    c.category,
    c.subcategory,
    f.program_type,
    c.dealer_number,
    c.dealer_name,
    f.lienholder_name,
    cast(null as varchar) as lienholder_address,
    cast(null as varchar) as lienholder_state,
    d.form_code as current_form_code,
    case when d.form_code is not null then 'document' else 'missing' end as form_code_source,
    d.document_name as form_document_name,
    c.provider_key as obligor_key,
    c.provider_name as obligor_name,
    case when c.provider_key <> '-1' then o.obligor_kind end as obligor_kind,
    c.provider_basis,
    c.status,
    c.effective_date,
    c.expiration_date,
    1 as contract_count,
    {{ audit_columns() }}
from {{ ref('mart_funded_reserve_contracts') }} as c
cross join as_of as a
left join {{ ref('dim_contract_finance') }} as f using (contract_key)
left join contract_doc as d using (contract_key)
left join {{ ref('dim_obligor') }} as o on o.obligor_key = c.provider_key
where c.is_filing_product_scope
  and c.status in (select status from in_force)
  and c.effective_date <= a.list_as_of_date
  and coalesce(c.expiration_date, date '9999-12-31') > a.list_as_of_date
  and coalesce(c.cancel_date, date '9999-12-31') > a.list_as_of_date
