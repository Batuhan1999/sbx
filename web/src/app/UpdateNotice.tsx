import { useQueryClient } from '@tanstack/react-query'
import { ArrowUpCircle, CircleAlert, CircleCheck } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { Link } from 'react-router'
import { keys, useUpdates } from '@/api/queries'
import { Spinner } from '@/components/ui/primitives'
import { cn } from '@/lib/utils'

export const UPDATES_PAGE = '/app/settings/updates'

/** The admins' view of updates, and of the upgrade this page saw start: its
 * end is news (the version in /api/me changes), an older one's is not. While
 * sbx-control restarts, polls fail and the last answer stays. */
export function useUpgradeFollow() {
  const qc = useQueryClient()
  const updates = useUpdates(true)
  const upgrade = updates.data?.upgrade ?? null
  const [watched, setWatched] = useState('')
  useEffect(() => { if (upgrade?.state === 'running') setWatched(upgrade.tag) }, [upgrade?.state, upgrade?.tag])
  const finished = !!watched && upgrade?.state !== 'running'
  useEffect(() => { if (finished) qc.invalidateQueries({ queryKey: keys.me }) }, [finished, qc])
  return { updates, upgrade, watched, finished, reconnecting: upgrade?.state === 'running' && updates.isError }
}

export function ProgressBar({ value, label, className }: { value: number; label: string; className?: string }) {
  const percent = Math.round(Math.min(1, Math.max(0, value)) * 100)
  return <div role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}
    className={cn('h-1.5 overflow-hidden rounded-full bg-line', className)}>
    <div className="h-full rounded-full bg-accent transition-[width] duration-500 ease-out" style={{ width: `${percent}%` }} />
  </div>
}

/** The sidebar's notice for admins: a newer release, the upgrade while it
 * runs, then its outcome; the Updates page has the rest. */
export function UpdateNotice() {
  const { updates, upgrade, watched, finished, reconnecting } = useUpgradeFollow()
  if (!updates.data) return null
  const { available } = updates.data
  if (upgrade?.state === 'running') {
    const percent = Math.round((upgrade.progress ?? 0) * 100)
    return <Box to={UPDATES_PAGE}>
      <Spinner className="size-3.5" />
      <div className="min-w-0 grow space-y-1.5" role="status">
        <div className="flex justify-between gap-2"><span className="truncate font-medium">Updating to {upgrade.tag}</span><span className="tabular-nums text-muted">{percent}%</span></div>
        <ProgressBar value={upgrade.progress ?? 0} label={`Update to ${upgrade.tag}`} />
        <div className="truncate text-muted">{reconnecting ? 'sbx-control restarts; reconnecting…' : 'Sandboxes keep running.'}</div>
      </div>
    </Box>
  }
  if (finished && upgrade?.state === 'done' && upgrade.tag === watched && !available) {
    return <Box tone="ok">
      <CircleCheck className="size-3.5 shrink-0 text-ok" />
      <div className="min-w-0 grow" role="status">
        <div className="font-medium">Updated to {upgrade.tag}</div>
        <button className="text-accent hover:underline" onClick={() => window.location.reload()}>Reload the page</button>
      </div>
    </Box>
  }
  if (upgrade?.state === 'failed') {
    return <Box tone="bad" to={UPDATES_PAGE}>
      <CircleAlert className="size-3.5 shrink-0 text-bad" />
      <div className="min-w-0 grow" role="alert">
        <div className="font-medium text-bad">Update to {upgrade.tag} failed</div>
        <div className="text-muted">See what happened</div>
      </div>
    </Box>
  }
  if (!available) return null
  return <Box tone="accent" to={UPDATES_PAGE}>
    <ArrowUpCircle className="size-3.5 shrink-0 text-accent" />
    <div className="min-w-0 grow">
      <div className="font-medium">{available.latest} available</div>
      <div className="text-muted">{available.breaking ? 'Breaking changes: read first' : 'See what changed'}</div>
    </div>
  </Box>
}

function Box({ tone = 'neutral', to, children }: { tone?: 'neutral' | 'accent' | 'ok' | 'bad'; to?: string; children: ReactNode }) {
  const tones = { neutral: 'border-line bg-sunken', accent: 'border-accent/25 bg-accent-soft', ok: 'border-ok/25 bg-ok-soft', bad: 'border-bad/25 bg-bad-soft' }
  const className = cn('flex items-start gap-2 rounded-md border px-2.5 py-2 text-[12px]', tones[tone], to && 'transition-colors hover:border-line-strong')
  return to ? <Link to={to} className={className}>{children}</Link> : <div className={className}>{children}</div>
}
