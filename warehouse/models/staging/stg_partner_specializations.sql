with source as (
    select * from {{ ref('partner_specializations') }}
),

renamed as (
    select
        partner_id,
        industry,
        service_type,
        strength_score,

        -- discrete tier for the matching agent's rationale
        case
            when strength_score >= 80 then 'expert'
            when strength_score >= 60 then 'strong'
            when strength_score >= 40 then 'competent'
            else 'basic'
        end                             as strength_tier

    from source
)

select * from renamed
