-- Single-row reporting date. Earning, in-force status, claim aging and period closing all hang
-- off this. Default = latest source activity over both contract systems, so reruns on the same
-- data are deterministic. Override with --vars '{as_of_date: "YYYY-MM-DD"}'.
select
    coalesce(
        {% if var('as_of_date') %}cast('{{ var("as_of_date") }}' as date){% else %}null{% endif %},
        (
            select max(updated_at)::date
            from (
                select updated_at from {{ ref('stg_admin_core__contract') }}
                union all
                select updated_at from {{ ref('stg_legacy_admin__contracts') }}
            )
        )
    ) as as_of_date,
    cast('{{ var("cutover_date") }}' as date) as cutover_date
