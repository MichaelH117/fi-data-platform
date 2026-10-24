-- Orphans made visible (docs/V3_DESIGN.md 8.4): for every core fact x foreign key, how many rows
-- point at the unknown member '-1' instead of a real dimension row. A fact never drops an orphan
-- (it is coalesced to '-1'), so this table is where the orphans are counted.
-- is_gated rows are checked in dbt build (tests/core/core_unknown_member_share, var max_unknown_member_share);
-- the rest are known, documented unknowns (e.g. legacy rows with no payee split) and are reported only.
{%- set gated = ['contract_key', 'dealer_key', 'product_key'] %}
{%- set pairs = [
    ('fct_cancellation', ['contract_key', 'dealer_key', 'product_key']),
    ('fct_cash_transaction', ['contract_key', 'dealer_key', 'product_key']),
    ('fct_claim_history', ['contract_key', 'dealer_key', 'product_key']),
    ('fct_claim_payment_line', ['contract_key', 'dealer_key', 'product_key', 'payee_account_holder_key']),
    ('fct_commission', ['contract_key', 'dealer_key', 'product_key', 'agent_key']),
    ('fct_contract_monthly', ['contract_key', 'dealer_key', 'product_key']),
    ('fct_disbursement', ['contract_key', 'dealer_key', 'product_key', 'account_holder_key']),
    ('fct_earned_disbursement', ['contract_key', 'dealer_key', 'product_key']),
    ('fct_gross_premium', ['contract_key', 'dealer_key', 'product_key']),
    ('fct_reinstatement', ['contract_key', 'dealer_key', 'product_key']),
    ('fct_transaction', ['contract_key', 'dealer_key', 'product_key']),
    ('fct_call_center_contact', ['contract_key', 'dealer_key', 'product_key']),
    ('dim_contract', ['dealer_key', 'product_key', 'agent_key', 'insurer_key', 'selling_dealer_key',
                      'provider_entity_key']),
] %}
{%- set dims = {
    'contract_key': 'dim_contract', 'dealer_key': 'dim_dealer', 'selling_dealer_key': 'dim_producer',
    'provider_entity_key': 'dim_producer', 'product_key': 'dim_product', 'agent_key': 'dim_agent',
    'insurer_key': 'dim_insurer', 'account_holder_key': 'dim_account_holder',
    'payee_account_holder_key': 'dim_account_holder',
} %}

with counts as (
    {%- for model, keys in pairs %}
    select
        '{{ model }}' as model_name,
        count(*) as row_count,
        {%- for k in keys %}
        count(*) filter (where {{ k }} = '-1') as {{ k }}__unknown{% if not loop.last %},{% endif %}
        {%- endfor %}
    from {{ ref(model) }}
    {%- if model == 'dim_contract' %}
    where contract_key <> '-1'
    {%- endif %}
    {%- if not loop.last %}
    union all by name
    {%- endif %}
    {%- endfor %}
),

unpivoted as (
    {%- for model, keys in pairs %}
    {%- set outer = loop %}
    {%- for k in keys %}
    select model_name, '{{ k }}' as key_column, '{{ dims[k] }}' as dimension, row_count,
           {{ k }}__unknown as unknown_rows
    from counts where model_name = '{{ model }}'
    {%- if not (outer.last and loop.last) %}
    union all
    {%- endif %}
    {%- endfor %}
    {%- endfor %}
)

select
    md5(model_name || '|' || key_column) as unknown_member_share_key,
    model_name,
    key_column,
    dimension,
    row_count,
    unknown_rows,
    case when row_count > 0 then unknown_rows / row_count else 0 end as unknown_share,
    key_column in ({% for g in gated %}'{{ g }}'{% if not loop.last %}, {% endif %}{% endfor %})
        and model_name <> 'dim_contract' as is_gated,
    {{ audit_columns() }}
from unpivoted
