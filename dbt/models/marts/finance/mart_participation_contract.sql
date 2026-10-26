-- Contract -> participation entity, with the run-off rule (V6 edit 13). Grain: one row per contract
-- whose dealer has a participation entity. Every finance mart that attributes a contract to an entity
-- reads this model, so the rule lives in one place.
-- A run-off entity takes no new business: a contract entered AFTER the entity's run-off date is not
-- ceded (is_ceded = false); its reserve stays with the obligor (the ledger routes it there too), so it
-- leaves the entity's experience and shows on the Transfer summary instead. Contracts entered on or
-- before the run-off date keep ceding: the entity still earns and pays claims on them.
-- Status, run-off date, cession type and GL class come from dim_participation_entity (DE Manager).
select
    k.contract_key,
    e.participation_entity_key,
    e.entity_id,
    k.entered_date,
    coalesce(e.status, 'ACTIVE') as entity_status,
    e.runoff_date,
    e.cession_type,
    e.gl_class_code,
    coalesce(k.entered_date > e.runoff_date, false) as is_transferred,
    not coalesce(k.entered_date > e.runoff_date, false) as is_ceded,
    {{ audit_columns() }}
from {{ ref('dim_contract') }} as k
inner join {{ ref('dim_participation_entity') }} as e using (dealer_key)
