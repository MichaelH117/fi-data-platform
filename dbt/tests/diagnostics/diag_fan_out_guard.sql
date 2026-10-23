-- Diagnostic: fan-out guard. Every contract-grain model has count(*) = count(distinct contract_key),
-- so no join quietly multiplies contracts (the transaction-grain trap). One row per failing model.
{% set contract_grain = [
    'int_contracts_conformed', 'int_reportable_scope', 'int_contract_key_bridge',
    'dim_contract', 'dim_contract_terms', 'dim_contract_finance', 'dim_contract_vehicle',
    'dim_contract_actuarial_class', 'dim_contract_claim_summary', 'fct_contract_sale'] %}

with counts as (
    {% for m in contract_grain %}
    select '{{ m }}' as model_name, count(*) as row_count, count(distinct contract_key) as contracts
    from {{ ref(m) }}
    {% if not loop.last %}union all{% endif %}
    {% endfor %}
)

select * from counts where row_count <> contracts
