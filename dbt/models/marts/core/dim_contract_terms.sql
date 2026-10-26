-- Contract terms (one row per reportable contract): term, mileage, waiting period, deductible,
-- coverage limit, options and surcharges.
-- V6 edit 4: current_form_code = the form code of the contract's latest non-deleted CONTRACT document
-- (generated_at, then document_id, descending). form_code_source says where it came from, so a form
-- code is never inferred silently:
--   document  the document row carries a form code
--   inferred  the document predates form codes (NULL); rebuilt from its name "<FORM>_STD - <yyyy>.<v> - ..."
--             as <FORM>-<yy>-<reportable state>, the layout later documents carry
--   missing   no usable document (every legacy contract) or a name that does not parse
with c as (
    select c.*
    from {{ ref('int_contracts_conformed') }} as c
    inner join {{ ref('int_reportable_scope') }} as s using (contract_key)
    where s.is_reportable
),

options as (
    select
        {{ contract_key("'admin_core'", 'contract_id') }} as contract_key,
        string_agg(option_code, ',' order by option_code) as option_codes,
        sum(amount) as options_amount
    from {{ ref('stg_admin_core__contract_option') }}
    where not is_deleted
    group by 1
),

current_doc as (
    select
        {{ contract_key("'admin_core'", 'contract_id') }} as contract_key,
        document_id,
        document_name,
        form_code,
        nullif(regexp_extract(document_name, '^([A-Z0-9]+)_STD', 1), '') as name_form,
        nullif(regexp_extract(document_name, ' - (?:19|20)(\d{2})\.\d+ - ', 1), '') as name_yy
    from {{ ref('stg_admin_core__contract_document') }}
    where document_type = 'CONTRACT' and not is_deleted and not is_document_deleted
    qualify row_number() over (partition by contract_id order by generated_at desc, document_id desc) = 1
),

surcharges as (
    select
        {{ contract_key("'admin_core'", 'contract_id') }} as contract_key,
        string_agg(surcharge_code, ',' order by surcharge_code) as surcharge_codes,
        count(*) as surcharge_count,
        sum(amount) as surcharge_amount
    from {{ ref('stg_admin_core__contract_surcharge') }}
    where not is_deleted
    group by 1
)

select
    c.contract_key,
    c.term_months,
    c.term_miles,
    c.term_band,
    c.term_days,
    c.expiration_mileage,
    c.claim_wait_days,
    c.claim_wait_miles,
    c.deductible_amount,
    c.deductible_name,
    c.coverage_limit,
    o.option_codes,
    coalesce(o.options_amount, 0) as options_amount,
    s.surcharge_codes,
    coalesce(s.surcharge_count, 0) as surcharge_count,
    coalesce(s.surcharge_amount, 0) as surcharge_amount,
    d.document_id as current_document_id,
    d.document_name as current_document_name,
    case
        when d.form_code is not null then d.form_code
        when d.name_form is not null and d.name_yy is not null and c.reportable_state is not null
            then d.name_form || '-' || d.name_yy || '-' || c.reportable_state
    end as current_form_code,
    case
        when d.form_code is not null then 'document'
        when d.name_form is not null and d.name_yy is not null and c.reportable_state is not null then 'inferred'
        else 'missing'
    end as form_code_source,
    {{ audit_columns() }}
from c
left join options as o using (contract_key)
left join surcharges as s using (contract_key)
left join current_doc as d using (contract_key)
