-- Call /internal/tick every 10 minutes from Supabase (pg_cron + pg_net).
-- Read docs/ops/tick-scheduler.md first. Run in the Supabase SQL editor, once.
-- Replace the two placeholders. Do NOT commit the filled-in file: it contains the secret.

create extension if not exists pg_cron;
create extension if not exists pg_net;

-- The URL and the shared secret live in Vault, not in the job text.
-- The secret travels in a header, so anything but https is refused here.
do $$
declare
    tick_url text := 'https://<your-host>/internal/tick/';
begin
    if tick_url not like 'https://%' then
        raise exception 'The tick URL must start with https://';
    end if;
    perform vault.create_secret(tick_url, 'bountyboard_tick_url');
end $$;
select vault.create_secret('<same value as TICK_SECRET on the web service>', 'bountyboard_tick_secret');

select cron.schedule(
    'bountyboard-tick',
    '*/10 * * * *',
    $job$
    select net.http_post(
        url := (select decrypted_secret from vault.decrypted_secrets where name = 'bountyboard_tick_url'),
        headers := jsonb_build_object(
            'Content-Type', 'application/json',
            'X-Tick-Secret',
            (select decrypted_secret from vault.decrypted_secrets where name = 'bountyboard_tick_secret')
        ),
        -- The default is 2000 ms. A sleeping Render instance takes about a minute to wake,
        -- and the tick itself may then use up to ~30 s.
        timeout_milliseconds := 120000
    );
    $job$
);
