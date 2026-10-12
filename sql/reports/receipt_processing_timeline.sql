-- One row per recorded worker attempt started in the past 24 hours.
-- NULL finished_at means no terminal event was recorded, not proof of liveness.
WITH attempts AS (
    SELECT payload->>'attempt_id' AS attempt_id,
           source_reference AS receipt,
           payload->>'worker_host' AS worker_host,
           payload->>'worker_pid' AS worker_pid,
           min(occurred_at) FILTER (WHERE payload->>'status' = 'active') AS started_at,
           min(occurred_at) FILTER (WHERE payload->>'status' IN (
               'results_published', 'review_results_published',
               'cache_results_published', 'cache_results_republished', 'retried', 'failed'
           )) AS finished_at,
           max(occurred_at) AS last_seen_at,
           (array_agg(payload->>'status' ORDER BY occurred_at DESC, id DESC)
               FILTER (WHERE payload->>'status' <> 'heartbeat'))[1] AS status,
           (array_agg(payload->>'cache_hit' ORDER BY occurred_at DESC, id DESC)
               FILTER (WHERE payload ? 'cache_hit'))[1]::boolean AS cache_hit
      FROM lineage.receipt_events
     WHERE occurred_at >= now() - interval '24 hours'
       AND payload->>'attempt_id' IS NOT NULL
     GROUP BY payload->>'attempt_id', source_reference,
              payload->>'worker_host', payload->>'worker_pid'
)
SELECT receipt, worker_host, worker_pid, started_at, finished_at,
       round(extract(epoch FROM (coalesce(finished_at, now()) - started_at)) / 60, 2)
           AS elapsed_minutes,
       status, last_seen_at,
       CASE WHEN finished_at IS NOT NULL THEN status
            WHEN last_seen_at >= now() - interval '45 seconds' THEN 'processing'
            ELSE 'heartbeat_stale' END AS live_state,
       cache_hit, attempt_id
  FROM attempts
 WHERE started_at IS NOT NULL
 ORDER BY started_at, worker_host, worker_pid;
