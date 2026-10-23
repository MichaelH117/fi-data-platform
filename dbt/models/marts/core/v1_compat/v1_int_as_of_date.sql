-- DEPRECATED V1 name (silver.int_as_of_date). Use intermediate.int_as_of_date.
{{ config(schema='silver', alias='int_as_of_date') }}
select * from {{ ref('int_as_of_date') }}
