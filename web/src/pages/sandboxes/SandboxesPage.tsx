import { Boxes, Plus, Search } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router'
import { useMe, useSandboxes } from '@/api/queries'
import type { Capacity, SandboxScope } from '@/api/types'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/states'
import { Segmented } from '@/components/Tabs'
import { Button, LinkButton } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { memory } from '@/lib/format'
import { sandboxStatus } from '@/lib/status'
import { NewSandboxForm } from './NewSandboxForm'
import { SandboxTable } from './SandboxTable'

const EMPTY: Record<SandboxScope, string> = {
  mine: 'Sandboxes you start appear here: from this page, from sbx up on your machine, or from a repository.',
  previews: 'Pull requests on repositories with previews get a sandbox each; none are running.',
  sessions: 'Sandboxes your coding agent sessions (Claude Code, Amp) started appear here.',
  all: 'Nobody has a sandbox on this server yet.',
}

export function CapacityNote({ capacity }: { capacity?: Capacity }) {
  if (!capacity) return null
  if (!capacity.known) return <LimitsNote limits={capacity.limits} />
  const low = (capacity.slots_free ?? 0) <= 1
  return <span className={low ? 'text-warn' : undefined}>
    Room for {capacity.slots_free} more of {capacity.slots_total} at {memory(capacity.memory_mb)} each
  </span>
}

// What the runtime allows when it can't say how much room is left.
function LimitsNote({ limits }: { limits?: Capacity['limits'] }) {
  if (!limits) return null
  const each = [
    limits.cpu != null && `${limits.cpu} vCPUs`,
    // Exact: a rounded maximum would let a request just above it look allowed.
    limits.memory_mb != null && `${limits.memory_mb} MB`,
    limits.disk_mb != null && `${limits.disk_mb} MB disk`,
  ].filter(Boolean)
  const parts = [
    limits.sandboxes != null && `Up to ${limits.sandboxes} running at once`,
    each.length > 0 && `each at most ${each.join(', ')}`,
  ].filter(Boolean) as string[]
  if (!parts.length) return null
  const text = parts.join(', ')
  return <span>{text.charAt(0).toUpperCase() + text.slice(1)}</span>
}

export default function SandboxesPage() {
  const me = useMe()
  const [params, setParams] = useSearchParams()
  const scope = (params.get('scope') as SandboxScope) || 'mine'
  const [query, setQuery] = useState('')
  const [creating, setCreating] = useState(false)
  const list = useSandboxes(scope)
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    const items = list.data?.sandboxes ?? []
    return q ? items.filter(s => [s.name, s.app, s.branch, s.owner ?? '', sandboxStatus(s).label].some(v => v.toLowerCase().includes(q))) : items
  }, [list.data, query])
  const scopes: { value: SandboxScope; label: string }[] = [
    { value: 'mine', label: 'Mine' }, { value: 'previews', label: 'PR previews' }, { value: 'sessions', label: 'Agent sessions' },
    ...(me.data?.admin ? [{ value: 'all' as const, label: 'Everyone' }] : []),
  ]
  return <>
    <PageHeader title="Sandboxes" description="Each sandbox is one branch running in its own VM: workspace, services, app and preview URLs."
      meta={<CapacityNote capacity={list.data?.capacity} />}
      actions={<Dialog open={creating} onOpenChange={setCreating} title="New sandbox" description="A development environment for one branch."
        trigger={<Button variant="primary"><Plus />New sandbox</Button>}>
        <NewSandboxForm onCancel={() => setCreating(false)} />
      </Dialog>} />
    <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
      <Segmented label="Whose sandboxes" value={scope} onChange={v => setParams(v === 'mine' ? {} : { scope: v }, { replace: true })} options={scopes} />
      <label className="relative w-full sm:w-64">
        <span className="sr-only">Filter sandboxes</span>
        <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-faint" />
        <input type="search" value={query} onChange={e => setQuery(e.target.value)} placeholder="Filter by name, repo, branch…" className="pl-8" />
      </label>
    </div>
    {list.error ? <ErrorState error={list.error} retry={() => list.refetch()} />
      : list.isPending ? <div className="rounded-lg border border-line bg-panel"><SkeletonRows /></div>
      : filtered.length ? <SandboxTable sandboxes={filtered} />
      : <div className="rounded-lg border border-dashed border-line">
        {query ? <EmptyState title="No sandboxes match" action={<Button size="sm" onClick={() => setQuery('')}>Clear filter</Button>} />
          : <EmptyState icon={Boxes} title="No sandboxes" action={scope === 'mine' && <><Button variant="primary" size="sm" onClick={() => setCreating(true)}><Plus />New sandbox</Button><LinkButton size="sm" to="/app/cli">Use the CLI</LinkButton></>}>{EMPTY[scope]}</EmptyState>}
      </div>}
  </>
}
