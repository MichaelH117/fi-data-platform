-- Admin Core contract_document: latest version per document_id. Contract documents (CONTRACT,
-- CANCEL, OTHER), 1..n per contract. The source flag is_deleted (a regenerated document the
-- administrator deleted again) is renamed is_document_deleted here, because is_deleted is the
-- staging soft-delete column. "Latest non-deleted CONTRACT document wins" lives in dim_contract_terms.
with src as (
    select
        * exclude (is_deleted),
        coalesce(cast(is_deleted as integer), 0) = 1 as is_document_deleted
    from {{ source('admin_core', 'contract_document') }}
)

{{ latest_version('src', 'document_id') }}
