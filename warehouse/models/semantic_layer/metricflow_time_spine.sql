{{
  config(materialized = 'table')
}}

-- Daily date spine required by MetricFlow for all time-based metric
-- aggregation (metric_time, monthly/quarterly grouping, time filters).
-- Range is deliberately wide so it covers the seed data and any
-- current_date()-based logic in the marts.

with days as (
    {{ dbt_utils.date_spine(
        datepart = "day",
        start_date = "cast('2022-01-01' as date)",
        end_date   = "cast('2027-01-01' as date)"
    ) }}
)

select
    cast(date_day as date) as date_day
from days
