import { useQueryClient } from '@tanstack/react-query'
import { ArrowUpRight, Check, Circle, FileText, GitBranch, Pause, Play, RotateCw, Trash2, X } from 'lucide-react'
import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { api } from '@/api/endpoints'
import { keys, useAction, useAgents, useResources, useSandbox } from '@/api/queries'
import type { SandboxDetail, SandboxInspection } from '@/api/types'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { CopyCommand } from '@/components/CopyCommand'
import { PageHeader } from '@/components/PageHeader'
import { RuntimeLabel } from '@/components/RuntimeLabel'
import { EmptyState, ErrorState, Loading, QueryView } from '@/components/states'
import { StateBadge, StatusBadge } from '@/components/StatusBadge'
import { AnchorButton, Button } from '@/components/ui/button'
import { Badge, Code, Facts, Panel } from '@/components/ui/primitives'
import { ago, dateTime, memory, plural } from '@/lib/format'
import { lifecycleSteps, sandboxStatus, type LifecycleStep } from '@/lib/status'
import { cn } from '@/lib/utils'
import { mainPreview } from './SandboxTable'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'

export default function SandboxPage() {
  const { name = '' } = useParams()
  const query = useSandbox(name)
  return <QueryView query={query} loading={<Loading label="Loading sandbox…" />}>{s => <Sandbox s={s} />}</QueryView>
}

function Sandbox({ s }: { s: SandboxDetail }) {
  const status = sandboxStatus(s)
  const preview = mainPreview(s)
  return <>
    <PageHeader title={s.name} crumbs={[{ label: 'Sandboxes', to: '/app/sandboxes' }]}
      meta={<>
        <StatusBadge sandbox={s} />
        <RuntimeLabel kind={s.runtime} />
        {s.app && <Link to={`/app/repos/${s.app}`} className="hover:text-ink">{s.app}</Link>}
        <span className="inline-flex items-center gap-1"><GitBranch className="size-3.5" />{s.branch || 'default branch'}</span>
        <span>{s.origin.href ? <a href={s.origin.href} target="_blank" rel="noreferrer" className="hover:text-ink">{s.origin.label}</a> : s.origin.label}{s.owner && s.origin.kind === 'person' ? ` · ${s.owner}` : ''}</span>
        <span title={dateTime(s.created_at)}>Created {ago(s.created_at)}</span>
      </>}
      actions={<Actions s={s} preview={preview} />} />

    {status.key === 'failed' && <FailedNotice s={s} />}
    {(status.busy || status.key === 'failed' || status.key === 'unverified' || status.key === 'workspace') && <Progress s={s} />}

    <div className="grid gap-4 lg:grid-cols-[minmax(0,1.55fr)_minmax(0,1fr)]">
      <div className="min-w-0 space-y-4">
        <Previews s={s} />
        <Services s={s} />
        <Logs s={s} />
        <Activity s={s} />
      </div>
      <div className="min-w-0 space-y-4">
        <Details s={s} />
        <Connect s={s} />
        <Resources s={s} />
      </div>
    </div>
  </>
}

function Actions({ s, preview }: { s: SandboxDetail; preview?: string }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const can = s.actions
  const refresh = [keys.sandbox(s.name), keys.sandboxes]
  const pause = useAction(() => api.sandboxes.pause(s.name), ...refresh)
  const resume = useAction(() => api.sandboxes.resume(s.name), ...refresh)
  const restart = useAction(() => api.sandboxes.restart(s.name), ...refresh)
  const error = pause.error || resume.error || restart.error
  return <div className="flex flex-col items-end gap-2">
    <div className="flex flex-wrap items-center justify-end gap-2">
      {can.pause && <Button busy={pause.isPending} onClick={() => pause.mutate()}><Pause />Pause</Button>}
      {can.resume && <Button busy={resume.isPending} onClick={() => resume.mutate()}><Play />Resume</Button>}
      {can.restart && <Button busy={restart.isPending} onClick={() => restart.mutate()} title="Run setup again and restart the app"><RotateCw />Restart</Button>}
      {can.remove && <ConfirmDialog trigger={<Button variant="danger" aria-label="Delete sandbox"><Trash2 /><span className="hidden sm:inline">Delete</span></Button>}
        title={`Delete ${s.name}?`} description="Its databases and unpushed work are lost."
        confirm="Delete sandbox" onConfirm={async () => {
          await api.sandboxes.remove(s.name)
          await qc.invalidateQueries({ queryKey: keys.sandboxes })
          navigate('/app/sandboxes')
        }} />}
      {preview && <AnchorButton variant={['ready', 'unverified', 'paused'].includes(sandboxStatus(s).key) ? 'primary' : 'secondary'} href={preview} target="_blank" rel="noreferrer">Open preview<ArrowUpRight /></AnchorButton>}
    </div>
    {error && <ErrorState error={error} compact />}
  </div>
}

function FailedNotice({ s }: { s: SandboxDetail }) {
  const error = s.error || s.lifecycle?.error
  return <div role="alert" className="mb-4 rounded-lg border border-bad/25 bg-bad-soft px-4 py-3 text-[13px]">
    <p className="font-medium text-bad">{error || 'The last operation failed.'}</p>
    <p className="mt-1 text-muted">See the logs below; Restart retries setup.</p>
  </div>
}

const STEP_ICON: Record<LifecycleStep['state'], React.ReactNode> = {
  done: <Check className="size-3" />,
  current: <span className="size-1.5 rounded-full bg-current pulse-dot" />,
  pending: <Circle className="size-2 opacity-0" />,
  failed: <X className="size-3" />,
  skipped: <span className="text-[10px]">–</span>,
}

function Progress({ s }: { s: SandboxDetail }) {
  const steps = lifecycleSteps(s.lifecycle)
  const status = sandboxStatus(s)
  return <section aria-label="Lifecycle" className="mb-4 rounded-lg border border-line bg-panel px-4 py-3">
    <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2 text-[12.5px]">
      <span className="font-medium">{status.busy ? `${status.label}: ${status.detail ?? ''}`
        : status.key === 'failed' ? `Stopped at ${steps.find(x => x.state === 'failed')?.label.toLowerCase() ?? 'an unknown step'}`
        : status.detail ?? status.label}</span>
      {s.lifecycle?.updated_at && <span className="text-muted">updated {ago(s.lifecycle.updated_at)}</span>}
    </div>
    <ol className="grid grid-cols-3 gap-y-3 sm:grid-cols-5">
      {steps.map((step, i) => <li key={step.key} className="relative flex flex-col items-center gap-1.5 text-center" aria-current={step.state === 'current' ? 'step' : undefined}>
        {i > 0 && <span aria-hidden className={cn('absolute right-1/2 top-2.5 hidden h-px w-full -translate-x-3 sm:block', step.state === 'done' || steps[i - 1].state === 'done' ? 'bg-ok/40' : 'bg-line')} />}
        <span className={cn('relative grid size-5 place-items-center rounded-full border text-[10px]',
          step.state === 'done' && 'border-transparent bg-ok text-white dark:text-black',
          step.state === 'current' && 'border-info bg-info-soft text-info',
          step.state === 'failed' && 'border-transparent bg-bad text-white dark:text-black',
          step.state === 'pending' && 'border-line-strong bg-panel',
          step.state === 'skipped' && 'border-warn/50 bg-warn-soft text-warn')}>{STEP_ICON[step.state]}</span>
        <span className={cn('text-[12px]', step.state === 'pending' ? 'text-faint' : 'text-ink')}>{step.label}</span>
        <span className="sr-only">{step.state}</span>
      </li>)}
    </ol>
  </section>
}

function Previews({ s }: { s: SandboxDetail }) {
  const declared = new Set(s.previews.map(p => p.name))
  const detected = Object.entries(s.urls).filter(([k, v]) => !declared.has(k) && /^https?:/.test(v))
  return <Panel title="Previews" bodyClassName="p-0">
    {s.previews.length === 0 && detected.length === 0
      ? <EmptyState title="No previews" className="py-8" />
      : <ul className="divide-y divide-line">
        {s.previews.map(p => <PreviewRow key={p.name} name={p.name} url={p.url} port={p.port} access={p.access} />)}
        {detected.map(([k, url]) => <PreviewRow key={k} name={k} url={url} detected />)}
      </ul>}
  </Panel>
}

function PreviewRow({ name, url, port, access, detected }: { name: string; url: string; port?: number; access?: string; detected?: boolean }) {
  return <li className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5">
    <span className="w-20 shrink-0 font-medium">{name}</span>
    {url ? <a href={url} target="_blank" rel="noreferrer" className="min-w-0 grow truncate font-mono text-[12.5px] text-accent hover:underline">{url}</a>
      : <span className="grow text-[12.5px] text-muted">No link yet</span>}
    <span className="flex items-center gap-1.5">
      {port && <Badge>:{port}</Badge>}
      {access && access !== 'private' && <Badge tone="warn">{access}</Badge>}
      {detected && <Badge tone="info">detected</Badge>}
    </span>
  </li>
}

const SERVICE_TONE: Record<string, 'ok' | 'progress' | 'bad' | 'idle'> = { ready: 'ok', created: 'ok', creating: 'progress', failed: 'bad', deleted: 'idle' }

function Services({ s }: { s: SandboxDetail }) {
  if (!s.services.length) return null
  return <Panel title="Services" bodyClassName="p-0">
    <ul className="divide-y divide-line">
      {s.services.map(svc => <li key={svc.key} className="px-4 py-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{svc.key}</span><span className="text-[12.5px] text-muted">{svc.uses}</span>
          <span className="grow" /><StateBadge tone={SERVICE_TONE[svc.status] ?? 'idle'} label={svc.status} />
        </div>
        {Object.keys(svc.outputs).length > 0 && <div className="mt-1.5 flex flex-wrap gap-1.5">
          {Object.entries(svc.outputs).map(([k, v]) => <span key={k} className="rounded bg-sunken px-1.5 py-0.5 font-mono text-[11.5px] text-muted">{k}={v}</span>)}
        </div>}
      </li>)}
    </ul>
  </Panel>
}

function Activity({ s }: { s: SandboxDetail }) {
  const events = s.events.filter(e => !e.internal).slice(0, 30).reverse()
  return <Panel title="Activity" bodyClassName="p-0">
    {events.length === 0 ? <EmptyState title="No activity yet" className="py-8" /> : <ol className="divide-y divide-line">
      {events.map((e, i) => <li key={i} className="grid grid-cols-[88px_minmax(0,1fr)] gap-3 px-4 py-2 text-[13px]">
        <time dateTime={e.at} title={dateTime(e.at)} className="text-[12px] text-muted">{ago(e.at)}</time>
        <div className="min-w-0"><span className="font-medium">{e.kind}</span>{e.detail && <span className="break-words text-muted"> · {e.detail}</span>}</div>
      </li>)}
    </ol>}
  </Panel>
}

function Logs({ s }: { s: SandboxDetail }) {
  const [data, setData] = useState<SandboxInspection | null>(null)
  const status = sandboxStatus(s)
  const inspect = useAction(() => api.sandboxes.inspect(s.name).then(setData))
  const allowed = s.actions.logs
  return <Panel title="Logs" description={data ? `Last ${data.lines} lines of each. Known secret values are masked.` : 'The app logs the recipe names.'}
    actions={allowed && <Button size="sm" busy={inspect.isPending} onClick={() => inspect.mutate()}><FileText />{data ? 'Reload' : 'Load logs'}</Button>}>
    {!data && !inspect.error && (status.key === 'paused' || !allowed) && <p className="text-[13px] text-muted">
      {status.key === 'paused' ? 'Loading logs resumes this paused sandbox.' : 'No logs to read yet.'}
    </p>}
    <ErrorState error={inspect.error} compact />
    {data && <div className="space-y-3">
      {data.commit && <p className="text-[12.5px] text-muted">At <Code>{data.commit.slice(0, 10)}</Code> {data.subject}</p>}
      {data.logs.length === 0 && <p className="text-[13px] text-muted">The recipe names no readable logs.</p>}
      {data.logs.map(l => <Collapsible key={l.path} defaultOpen={data.logs.length === 1} className="rounded-md border border-line">
        <CollapsibleTrigger className="px-3 py-2 font-mono text-[12px] text-ink">{l.path}</CollapsibleTrigger>
        <CollapsibleContent><pre className="max-h-96 overflow-auto border-t border-line bg-sunken p-3 text-[11.5px] leading-relaxed">{l.text || '(empty)'}</pre></CollapsibleContent>
      </Collapsible>)}
      {data.env.length > 0 && <Collapsible>
        <CollapsibleTrigger className="w-auto text-[12.5px]">{plural(data.env.length, 'environment variable')} set (names only)</CollapsibleTrigger>
        <CollapsibleContent><p className="mt-2 break-words font-mono text-[11.5px] text-muted">{data.env.join('  ')}</p></CollapsibleContent>
      </Collapsible>}
    </div>}
  </Panel>
}

function Details({ s }: { s: SandboxDetail }) {
  return <Panel title="Details">
    <Facts items={[
      ['Owner', s.owner ?? '—'],
      ['Cleanup', s.paused_days > 0 ? `Deleted after ${plural(s.paused_days, 'day')} paused` : 'Kept while paused'],
    ]} />
  </Panel>
}

export function Connect({ s }: { s: Pick<SandboxDetail, 'name'> }) {
  const agents = useAgents()
  return <Panel title="Work in it" description="From a checkout, with the sbx CLI.">
    <div className="space-y-2">
      <CopyCommand command={`sbx open ${s.name} code`} label="open in VS Code" />
      {agents.map(a => <CopyCommand key={a.command} command={`sbx open ${s.name} ${a.command}`} label={`open ${a.title} inside`} />)}
      <CopyCommand command={`sbx logs ${s.name}`} label="follow logs" />
    </div>
  </Panel>
}

function Resources({ s }: { s: SandboxDetail }) {
  const res = useResources(s.name, !!s.sandbox_id)
  if (!s.sandbox_id) return null
  return <Panel title="Resources">
    {res.isPending ? <Loading className="py-4" /> : res.error ? <ErrorState error={res.error} compact retry={() => res.refetch()} /> : res.data && <div className="space-y-3">
      <Facts items={[
        ['Recipe asks', res.data.recipe.cpu || res.data.recipe.memory_mb ? `${res.data.recipe.cpu ?? '—'} vCPU · ${memory(res.data.recipe.memory_mb)}${res.data.recipe.swap_mb ? ` · ${memory(res.data.recipe.swap_mb)} swap` : ''}` : 'platform defaults'],
        ['Allocated', `${res.data.allocated.cpu} vCPU · ${memory(res.data.allocated.memory_mb)}`],
      ]} />
      {res.data.issues.map(i => <p key={i.code + (i.resource ?? '')} className="rounded-md bg-warn-soft px-3 py-2 text-[12.5px] text-warn">{i.message}</p>)}
    </div>}
  </Panel>
}
