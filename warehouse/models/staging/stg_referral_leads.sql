with source as (
    select * from {{ ref('referral_leads') }}
),

-- Normalise the optional date columns once (see stg_subscriptions for the
-- cast-to-varchar rationale — robust to either DuckDB seed-sniffer typing).
cast_dates as (
    select
        referral_lead_id,
        referring_partner_id,
        nullif(trim(cast(customer_id as varchar)), '')                    as customer_id,
        country,
        service_type,
        industry,
        cast(referred_at as date)                                         as referred_at,
        try_cast(nullif(trim(cast(matched_at   as varchar)), '') as date) as matched_at,
        try_cast(nullif(trim(cast(booked_at    as varchar)), '') as date) as booked_at,
        try_cast(nullif(trim(cast(resolved_at  as varchar)), '') as date) as resolved_at,
        referral_status,
        deal_size_estimate,
        urgency
    from source
),

renamed as (
    select
        referral_lead_id,
        referring_partner_id,
        customer_id,
        country,
        service_type,
        industry,
        referred_at,
        matched_at,
        booked_at,
        resolved_at,
        referral_status,
        deal_size_estimate,
        urgency,

        -- days from referral to match (null if never matched)
        case
            when matched_at is not null
            then date_diff('day', referred_at, matched_at)
            else null
        end                                             as days_to_match,

        -- days from referral to booking (null if never booked)
        case
            when booked_at is not null
            then date_diff('day', referred_at, booked_at)
            else null
        end                                             as days_to_book,

        -- days from referral to final resolution
        case
            when resolved_at is not null
            then date_diff('day', referred_at, resolved_at)
            else null
        end                                             as days_to_resolve,

        -- lifecycle flags
        (referral_status = 'booked')                    as is_booked,
        (referral_status = 'lost')                      as is_lost,
        (referral_status in ('pending','matching','negotiating'))  as is_active_in_pipeline,
        (customer_id is not null)                       as is_converted

    from cast_dates
)

select * from renamed
