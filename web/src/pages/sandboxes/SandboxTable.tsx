import { ArrowUpRight, Bot, GitBranch, GitPullRequest, User } from 'lucide-react'
import { Link } from 'react-router'
import type { Sandbox } from '@/api/types'
import { RuntimeLabel } from '@/components/RuntimeLabel'
import { StatusBadge } from '@/components/StatusBadge'
import { ago, host } from '@/lib/format'
import { sandboxStatus } from '@/lib/status'
import { cn } from '@/lib/utils'

const ORIGIN_ICON = { pr: GitPullRequest, session: Bot, person: User }

export function mainPreview(s: Pick<Sandbox, 'urls'>): string | undefined {
  return s.urls.web ?? Object.values(s.urls).find(u => /^https?:/.test(u))
}

export function SandboxTable({ sandboxes, showRepo = true }: { sandboxes: Sandbox[]; showRepo?: boolean }) {
  const cols = showRepo ? 'md:grid-cols-[minmax(0,1.6fr)_minmax(0,1.2fr)_minmax(0,1.3fr)_minmax(0,1fr)_112px]' : 'md:grid-cols-[minmax(0,1.6fr)_minmax(0,1.3fr)_minmax(0,1fr)_112px]'
  return <div role="table" aria-label="Sandboxes" className="overflow-hidden rounded-lg border border-line bg-panel">
    <div role="row" className={cn('hidden gap-4 border-b border-line bg-sunken/60 px-4 py-2 text-[11.5px] font-medium uppercase tracking-wide text-muted md:grid', cols)}>
      <span role="columnheader">Sandbox</span>{showRepo && <span role="columnheader">Repository</span>}<span role="columnheader">Status</span>
      <span role="columnheader">Origin</span><span role="columnheader" className="text-right">Created</span>
    </div>
    <div className="divide-y divide-line">
      {sandboxes.map(s => {
        const preview = mainPreview(s)
        const Icon = ORIGIN_ICON[s.origin?.kind] ?? User
        const status = sandboxStatus(s)
        return <div role="row" key={s.name} className={cn('relative grid grid-cols-1 gap-x-4 gap-y-1.5 px-4 py-3 transition-colors hover:bg-hover/60 md:items-center', cols)}>
          <div role="cell" className="min-w-0">
            <div className="flex min-w-0 items-center gap-1.5">
              <Link to={`/app/sandboxes/${encodeURIComponent(s.name)}`} className="truncate font-medium after:absolute after:inset-0 hover:underline">{s.name}</Link>
              <RuntimeLabel kind={s.runtime} />
            </div>
            <div className="mt-0.5 flex min-w-0 items-center gap-1 text-[12px] text-muted"><GitBranch className="size-3 shrink-0" /><span className="truncate">{s.branch || 'default branch'}</span></div>
          </div>
          {showRepo && <div role="cell" className="min-w-0 truncate text-[13px] text-muted">{s.app || '—'}</div>}
          <div role="cell" className="flex min-w-0 flex-col items-start gap-0.5">
            <StatusBadge sandbox={s} />
            {(status.busy || status.key === 'failed') && status.detail && <span className="max-w-full truncate text-[12px] text-muted" title={status.detail}>{status.detail}</span>}
          </div>
          <div role="cell" className="flex min-w-0 items-center gap-1.5 text-[12.5px] text-muted">
            <Icon className="size-3.5 shrink-0" /><span className="truncate">{s.origin?.label}{s.origin?.kind === 'person' && s.owner ? ` · ${s.owner.split('@')[0]}` : ''}</span>
          </div>
          <div role="cell" className="flex items-center justify-between gap-2 text-[12.5px] text-muted md:justify-end">
            {preview && status.key !== 'failed' && <a href={preview} target="_blank" rel="noreferrer" className="relative z-10 inline-flex items-center gap-0.5 text-ink hover:underline md:hidden" title={preview}>{host(preview)}<ArrowUpRight className="size-3" /></a>}
            <span title={s.created_at ?? undefined}>{ago(s.created_at)}</span>
          </div>
        </div>
      })}
    </div>
  </div>
}
