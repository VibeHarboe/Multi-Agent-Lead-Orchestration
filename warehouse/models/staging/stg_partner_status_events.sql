with source as (
    select * from {{ ref('partner_status_events') }}
),

renamed as (
    select
        event_id,
        partner_id,
        prior_status,
        new_status,
        cast(event_at as date)                          as event_at,
        reason,
        changed_by,

        -- classification for the audit log + weekly report
        case
            when prior_status = 'inactive' and new_status = 'active' then 'reactivation'
            when prior_status = 'active'   and new_status = 'inactive' then 'deactivation'
            else 'other'
        end                                             as status_transition

    from source
)

select * from renamed
