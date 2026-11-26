# Scheduling `/internal/tick`

The tick closes overdue bounties and lapsed verifications, sends queued email, purges abandoned
signups and, by being a request, keeps the free web instance awake.

## Why not only GitHub Actions

`.github/workflows/tick.yml` asks for every 10 minutes. The scheduled runs observed on 26 Nov 2026
were at 02:31, 11:29 and 20:29 UTC: about 9 hours apart. Render spins a free service down after 15
idle minutes, and email retries wait for a tick. Keep the workflow as a backup, not as the clock.

## Choice: Supabase `pg_cron` + `pg_net`

| | Supabase | cron-job.org |
|---|---|---|
| Cost / new account | none / none | none / one more |
| Request timeout | set per call (we use 120 s) | 30 s, shorter than a ~1 min cold start |
| Secret held by | the database (Vault) | a third party |
| Fails when | the database is down or paused | independent of the database |

## Set up

1. Make sure `TICK_SECRET` is set on the web service.
2. Fill the two placeholders in `tick-scheduler.sql` **in the SQL editor** and run it. Do not save
   the filled-in file in the repository.
3. After 10-20 minutes, check both halves:

   ```sql
   select status, return_message, start_time from cron.job_run_details order by start_time desc limit 5;
   select status_code, timed_out, error_msg, created from net._http_response order by created desc limit 5;
   ```

   `cron.job_run_details` only says the request was queued, because `pg_net` is asynchronous. The
   second query says what the app answered: expect `200`. `403` means the secrets differ, `404`
   means the app has no `TICK_SECRET`, and `500` means part of the tick failed (see the app log).
4. `pg_net` keeps responses for about 6 hours.

To stop it: `select cron.unschedule('bountyboard-tick');`
To rotate the secret: update it on the web service, then `vault.update_secret(...)` for
`bountyboard_tick_secret`.

## Limits and things to verify

- **Not yet run against Supabase.** The SQL follows the documentation; nothing here has executed it.
- Supabase's pages did not state that Cron is available on the free plan. Step 2 will show it.
- Free projects pause after a week of inactivity, and Supabase does not define "inactivity" or say
  whether a `pg_cron` job counts. The tick makes the app query the database from outside, which is
  the safer assumption, but check the dashboard in week two.
- Any role that can log in to the database can read the secret while a request is queued. Here that
  is the app's own role and the dashboard.
- `pg_net` is documented as beta.
- `cron.unschedule` comes from `pg_cron` itself; the Supabase pages read did not mention it.

Sources: Supabase docs for [pg_net](https://supabase.com/docs/guides/database/extensions/pg_net),
[Cron](https://supabase.com/docs/guides/cron) and [Vault](https://supabase.com/docs/guides/database/vault),
[Supabase pricing](https://supabase.com/pricing), [cron-job.org FAQ](https://cron-job.org/en/faq/),
[Render free tier](https://render.com/docs/free). Read 26 Nov 2026.
