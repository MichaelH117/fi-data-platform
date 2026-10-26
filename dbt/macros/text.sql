{#- Proper Case for names and mailing addresses on dealer-facing exports (V6 edit 10).
    Each space-separated word is capitalized, and so is each part after a hyphen or apostrophe
    ("smith-jones" -> "Smith-Jones", "o'neil" -> "O'Neil"). A whole word listed in the exceptions
    seed keeps the seed's spelling (LLC, II, NW, ...); a lower-case joining word from the seed
    ("of", "and") is still capitalized when it is the first word. Runs of spaces collapse to one.
    `exceptions` is a MAP(varchar, varchar) column from proper_case_exceptions_map(). NULL in, NULL out. -#}
{% macro proper_case(expr, exceptions='pc.exceptions') -%}
    nullif(array_to_string(list_transform(
        string_split(regexp_replace(lower(trim({{ expr }})), '\s+', ' ', 'g'), ' '),
        (pc_w, pc_i) -> case
            when {{ exceptions }}[pc_w] is not null and not (pc_i = 1 and {{ exceptions }}[pc_w] = lower({{ exceptions }}[pc_w]))
                then {{ exceptions }}[pc_w]
            else array_to_string(list_transform(string_split(pc_w, '-'),
                     pc_h -> array_to_string(list_transform(string_split(pc_h, ''''),
                              pc_a -> upper(left(pc_a, 1)) || substr(pc_a, 2)), '''')), '-')
        end
    ), ' '), '')
{%- endmacro %}

{#- One-row relation with the exceptions map; cross join it as `pc`. -#}
{% macro proper_case_exceptions_map() -%}
    select map(list(lower(token)), list(replacement)) as exceptions from {{ ref('proper_case_exceptions') }}
{%- endmacro %}
