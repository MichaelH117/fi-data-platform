-- Every surcharge dollar on an audited contract shows up in the audit as charged.
with scope as (
    select distinct contract_key from {{ ref('mart_surcharge_audit') }}
),

terms as (
    select round(sum(t.surcharge_amount), 2) as charged
    from {{ ref('dim_contract_terms') }} as t
    inner join {{ ref('dim_contract') }} as c using (contract_key)
    inner join {{ ref('dealer_surcharge_subcategories') }} as sc using (subcategory_code)
    inner join {{ ref('dealer_report_status_sets') }} as s
        on s.report_id = 'SURCHARGE_AUDIT' and s.status = c.status
    where c.source_system = 'admin_core'
),

audit as (select round(sum(charged_amount), 2) as charged from {{ ref('mart_surcharge_audit') }})

select t.charged as terms_charged, a.charged as audit_charged
from terms as t cross join audit as a
where abs(coalesce(t.charged, 0) - coalesce(a.charged, 0)) > 1.0
