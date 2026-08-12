{% macro safe_divide(numerator, denominator, default=0) %}
    case
        when {{ denominator }} = 0 or {{ denominator }} is null
        then {{ default }}
        else {{ numerator }} / {{ denominator }}
    end
{% endmacro %}


{% macro pct(numerator, denominator, decimals=2) %}
    round(
        {{ safe_divide(numerator, denominator) }} * 100,
        {{ decimals }}
    )
{% endmacro %}


{% macro nps_score(promoter_col, detractor_col, total_col) %}
    round(
        ({{ promoter_col }} - {{ detractor_col }})
        / nullif({{ total_col }}, 0) * 100,
        1
    )
{% endmacro %}


{% macro date_spine_months(start_date, end_date) %}
    /*
      Generates a series of months between two dates.
      Usage: {{ date_spine_months('2023-01-01', '2024-12-01') }}
    */
    with months as (
        {{ dbt_utils.date_spine(
            datepart="month",
            start_date="cast('" ~ start_date ~ "' as date)",
            end_date="cast('" ~ end_date ~ "' as date)"
        ) }}
    )
    select date_month from months
{% endmacro %}


{% macro aging_bucket(days_overdue_col) %}
    case
        when {{ days_overdue_col }} between 1  and 30  then '1-30 days'
        when {{ days_overdue_col }} between 31 and 60  then '31-60 days'
        when {{ days_overdue_col }} between 61 and 90  then '61-90 days'
        when {{ days_overdue_col }} > 90               then '90+ days'
        else 'current'
    end
{% endmacro %}


{% macro classify_nps(score_col) %}
    case
        when {{ score_col }} >= {{ var('nps_high_score') }} then 'promoter'
        when {{ score_col }} >= 7                           then 'passive'
        when {{ score_col }} <= {{ var('nps_low_score') }}  then 'detractor'
    end
{% endmacro %}
