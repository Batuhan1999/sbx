-- Limits for Embed's one team, applied to Embed's own Postgres by install.sh,
-- which then restarts the api to drop its cached limits. Embed seeds base_v1
-- for a hosted free tier: 512 MiB of build working space (too little for apt
-- or pnpm install) and one-hour sandboxes. sbx-control decides how many
-- sandboxes fit (hugepages, core.capacity) and when they pause (the keeper),
-- so Embed's own limits only need to stay out of the way.
UPDATE public.tiers SET
  disk_mb                   = 20480, -- build working space before template steps
  default_free_disk_size_mb = 4096,  -- free space a template gets after its steps
  max_free_disk_size_mb     = 20480,
  max_disk_size_mb          = 40960,
  max_length_hours          = 24,    -- sandbox timeout ceiling; the keeper extends it
  concurrent_instances      = 1000   -- above any host's capacity
WHERE id = 'base_v1';
