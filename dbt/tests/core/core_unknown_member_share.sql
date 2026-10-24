-- Core test (runs in dbt build): unknown-member share. Orphan fact rows are kept on the '-1' member (never dropped);
-- it fails when more than var('max_unknown_member_share') of a fact's rows sit on '-1' for a
-- conformed key (contract, dealer, product): a broken join upstream, not a few late reference rows.
-- One row per failing fact x key (core.mart_unknown_member_share holds every count).
select model_name, key_column, row_count, unknown_rows, unknown_share
from {{ ref('mart_unknown_member_share') }}
where is_gated and unknown_share > {{ var('max_unknown_member_share') }}
