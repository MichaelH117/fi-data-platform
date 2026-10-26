-- Diagnostic (V6 edit 4): form-code coverage by entered year. Every Admin Core contract entered from
-- var form_code_from_year on must take its current_form_code from a document (form_code_source =
-- document); older years are inferred from the document name or missing, and are reported, never
-- failed. Returns the years under var min_form_code_coverage.
with years as (
    select
        k.entered_year,
        count(*) as contracts,
        count(*) filter (where t.form_code_source = 'document') as from_document,
        count(*) filter (where t.form_code_source = 'inferred') as inferred,
        count(*) filter (where t.form_code_source = 'missing') as missing
    from {{ ref('dim_contract_terms') }} as t
    inner join {{ ref('int_contracts_conformed') }} as k using (contract_key)
    where k.source_system = 'admin_core'
    group by 1
)

select *, from_document / contracts as coverage, {{ var('min_form_code_coverage') }} as threshold
from years
where entered_year >= {{ var('form_code_from_year') }}
  and from_document / contracts < {{ var('min_form_code_coverage') }}
