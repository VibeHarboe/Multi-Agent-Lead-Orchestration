with source as (
    select * from {{ ref('subscriptions') }}
),

-- Normalise the optional date columns once. The DuckDB CSV sniffer may type
-- these as DATE or VARCHAR depending on the data; cast-to-varchar first makes
-- the parse robust either way, and try_cast turns blanks/bad values into null.
cast_dates as (
    select
        subscription_id,
        customer_id,
        plan_type,
        cast(start_date as date)                                        as start_date,
        try_cast(nullif(trim(cast(end_date   as varchar)), '') as date) as end_date,
        try_cast(nullif(trim(cast(churn_date as varchar)), '') as date) as churn_date,
        nullif(trim(churn_reason), '')                                  as churn_reason,
        mrr,
        country
    from source
),

renamed as (
    select
        subscription_id,
        customer_id,
        plan_type,
        start_date,
        end_date,
        churn_date,
        churn_reason,
        mrr,
        country,

        -- is active = no churn date and no end date
        (churn_date is null and end_date is null)   as is_active,

        -- churned flag
        (churn_date is not null)                    as is_churned,

        -- days since churn (null if not churned)
        case
            when churn_date is not null
            then date_diff('day', churn_date, current_date)
            else null
        end                                         as days_since_churn

    from cast_dates
)

select * from renamed
