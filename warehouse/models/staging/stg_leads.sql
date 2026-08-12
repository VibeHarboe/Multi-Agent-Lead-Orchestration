with source as (
    select * from {{ ref('leads') }}
),

-- Normalise the optional date columns once (see stg_subscriptions for the
-- cast-to-varchar rationale — robust to either DuckDB seed-sniffer typing).
cast_dates as (
    select
        lead_id,
        customer_id,
        country,
        service_type,
        cast(lead_date as date)                                          as lead_date,
        try_cast(nullif(trim(cast(assigned_at  as varchar)), '') as date) as assigned_at,
        try_cast(nullif(trim(cast(converted_at as varchar)), '') as date) as converted_at,
        trim(lead_status)                                                as lead_status,
        lead_source
    from source
),

renamed as (
    select
        lead_id,
        customer_id,
        country,
        service_type,
        lead_date,
        assigned_at,
        converted_at,
        lead_status,
        lead_source,

        -- time-to-assign in hours (business metric)
        case
            when assigned_at is not null
            then date_diff('hour', lead_date, assigned_at)
            else null
        end                                             as hours_to_assign,

        -- time-to-convert in days
        case
            when converted_at is not null
            then date_diff('day', lead_date, converted_at)
            else null
        end                                             as days_to_convert,

        -- conversion flag
        (lead_status = 'converted')                     as is_converted

    from cast_dates
)

select * from renamed
