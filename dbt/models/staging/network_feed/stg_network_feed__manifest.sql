-- One row per landing file the ingest step copied, with the manifest checks (checksum, row count).
-- Feeds the freshness / manifest diagnostic.
select
    feed_date,
    sequence_no,
    file_name,
    feed_date || '/' || file_name as source_file,
    expected_rows,
    actual_rows,
    checksum_ok,
    row_count_ok,
    generated_at,
    _source_system,
    _loaded_at
from {{ source('network_feed', '_manifest') }}
qualify row_number() over (partition by feed_date, file_name order by _loaded_at desc) = 1
