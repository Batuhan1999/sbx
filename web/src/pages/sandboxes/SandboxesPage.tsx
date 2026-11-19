import { Boxes, Plus, Search } from 'lucide-react'
import { Fragment, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router'
import { useMe, useRepos, useRuntimeTitle, useSandboxes } from '@/api/queries'
import type { Capacity, SandboxList, SandboxScope } from '@/api/types'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/states'
import { Segmented } from '@/components/Tabs'
import { Button, LinkButton } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { memory } from '@/lib/format'
import { sandboxStatus } from '@/lib/status'
import { NewSandboxForm } from './NewSandboxForm'
import { SandboxTable } from './SandboxTable'


/** The room for sandboxes: the default runtime's, or with several runtimes one line per runtime that knows its room. */
export function CapacityNote({ list }: { list?: Pick<SandboxList, 'capacity' | 'capacities'> }) {
  const title = useRuntimeTitle()
  if (!list?.capacity) return null
  if (!title) return <Room capacity={list.capacity} />
  const known = Object.values(list.capacities ?? { [list.capacity.runtime ?? '']: list.capacity }).filter(c => c.known)
  return <span>{known.map((c, i) => <Fragment key={c.runtime}>{i > 0 && ' · '}<Room capacity={c} title={title(c.runtime)} /></Fragment>)}</span>
}

function Room({ capacity, title }: { capacity: Capacity; title?: string }) {
  if (!capacity.known) return <LimitsNote limits={capacity.limits} />
  const low = (capacity.slots_free ?? 0) <= 1
  return <span className={low ? 'text-warn' : undefined}>
    {title ? `${title}: room` : 'Room'} for {capacity.slots_free} more of {capacity.slots_total} at {memory(capacity.memory_mb)} each
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

/** An empty list says what would fill it. A server with no repository ready has nothing a sandbox could run,
 * so the first step there is the repository, not "New sandbox". */
function NoSandboxes({ scope, admin, onNew }: { scope: SandboxScope; admin: boolean; onNew: () => void }) {
  const repos = useRepos()
  if (scope === 'previews') return <EmptyState icon={Boxes} title="No PR previews">A pull request’s preview shows up here while it runs.</EmptyState>
  if (scope === 'sessions') return <EmptyState icon={Boxes} title="No agent sessions">A coding agent’s session that runs in a sandbox here shows up while it runs.</EmptyState>
  if (scope === 'all') return <EmptyState icon={Boxes} title="No sandboxes">Nobody has a sandbox right now.</EmptyState>
  if (repos.isPending) return <EmptyState icon={Boxes} title="No sandboxes yet" />   // what to say next depends on the repositories
  const ready = (repos.data ?? []).filter(r => r.configured)
  if (repos.data && !ready.length) return <EmptyState icon={Boxes} title="No sandboxes yet"
    action={admin && <LinkButton variant="primary" size="sm" to={repos.data.length ? '/app/repos' : '/app/repos/new'}><Plus />{repos.data.length ? 'Finish a repository' : 'Connect a repository'}</LinkButton>}>
    {repos.data.length ? 'A sandbox runs one of your repositories, and none has its recipe yet. Its coding agent writes the recipe and starts the first sandbox.'
      : admin ? 'A sandbox runs one of your repositories. Connect one first: its coding agent writes the recipe and starts the first sandbox.'
      : 'A sandbox runs one of this server’s repositories, and none is connected yet. Ask an admin to connect one.'}
  </EmptyState>
  return <EmptyState icon={Boxes} title="No sandboxes yet"
    action={<><Button variant="primary" size="sm" onClick={onNew}><Plus />New sandbox</Button><LinkButton size="sm" to="/app/cli">Use the CLI</LinkButton></>}>
    Start one here, or run <code>sbx up NAME</code> in a repository’s checkout. Each gets its own preview link.
  </EmptyState>
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
    <PageHeader title="Sandboxes"
      meta={<CapacityNote list={list.data} />}
      actions={<Dialog open={creating} onOpenChange={setCreating} title="New sandbox"
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
          : <NoSandboxes scope={scope} admin={!!me.data?.admin} onNew={() => setCreating(true)} />}
      </div>}
  </>
}
