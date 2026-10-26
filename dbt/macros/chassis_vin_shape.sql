{#- V7 edit 27: how a typed chassis VIN looks, decided before the value is hashed (the shape is not PII).
    missing     nothing typed
    placeholder a stand-in word or all zeros
    bad_length  not 17 characters once spaces are removed
    lowercase_fixed  17 characters typed in lower case (staging upper-cases it before hashing)
    valid       17 characters, upper case
  duplicate_across_contracts is decided later, in mart_specialty_line, by comparing hashes. -#}
{% macro chassis_vin_shape(expr) -%}
case
    when nullif(trim({{ expr }}), '') is null then 'missing'
    when upper(trim({{ expr }})) in ('UNKNOWN', 'TBD', 'N/A', 'NA', 'NONE', 'PENDING')
        or regexp_full_match(trim({{ expr }}), '0+') then 'placeholder'
    when length(replace(trim({{ expr }}), ' ', '')) <> 17 then 'bad_length'
    when trim({{ expr }}) <> upper(trim({{ expr }})) then 'lowercase_fixed'
    else 'valid'
end
{%- endmacro %}
