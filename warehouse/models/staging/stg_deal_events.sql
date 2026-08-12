with source as (
    select * from {{ ref('deal_events') }}
),

renamed as (
    select
        event_id,
        referral_lead_id,
        nullif(trim(cast(partner_id as varchar)), '')       as partner_id,
        event_type,
        cast(event_at as date)                              as event_at,
        agent_name,
        nullif(trim(cast(rationale as varchar)), '')        as rationale,

        -- event classification for the monitor
        case
            when event_type in ('booked','resolved') then 'terminal_success'
            when event_type in ('lost','escalated_to_hitl') then 'terminal_review'
            else 'in_flight'
        end                                                 as event_class

    from source
)

select * from renamed
