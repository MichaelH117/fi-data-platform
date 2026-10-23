-- The legacy book never recorded an obligor. int_contracts_conformed maps it at conversion
-- (obligor_basis = CONVERSION_STATE_MAP), so no legacy contract in a state that Admin Core
-- writes is left without one and the regulatory marts need no fallback.
select k.contract_key, k.reportable_state, k.obligor_basis
from {{ ref('dim_contract') }} as k
where k.source_system = 'legacy_admin'
  and k.current_obligor_key = '-1'
  and k.reportable_state in (
      select reportable_state from {{ ref('dim_contract') }}
      where source_system = 'admin_core' and current_obligor_key <> '-1'
  )
