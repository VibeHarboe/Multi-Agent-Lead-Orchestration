with source as (
    select * from {{ ref('invoices') }}
),

-- Normalise the optional paid_date column once (see stg_subscriptions for the
-- cast-to-varchar rationale — robust to either DuckDB seed-sniffer typing).
cast_dates as (
    select
        invoice_id,
        customer_id,
        cast(issue_date as date)                                        as issue_date,
        cast(due_date as date)                                          as due_date,
        try_cast(nullif(trim(cast(paid_date as varchar)), '') as date)  as paid_date,
        amount,
        currency,
        status,
        country
    from source
),

renamed as (
    select
        invoice_id,
        customer_id,
        issue_date,
        due_date,
        paid_date,
        amount,
        currency,
        status,
        country,

        -- days overdue (positive = overdue, 0 if paid on time)
        case
            when trim(status) in ('overdue', 'paid_late')
            then date_diff('day', due_date, coalesce(paid_date, current_date))
            else 0
        end                                         as days_overdue,

        -- binary overdue flag
        (trim(status) = 'overdue')                  as is_currently_overdue

    from cast_dates
)

select * from renamed
