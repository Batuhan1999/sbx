import { useState, type ReactNode } from 'react'
import { api } from '@/api/endpoints'
import { useAvailableRuntimes, useMe, useRuntimes } from '@/api/queries'
import type { AvailableRuntime, ConnectionState, RuntimeChange, SandboxRuntime } from '@/api/types'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { EmptyState, ErrorState, QueryView, SkeletonRows } from '@/components/states'
import { StateBadge } from '@/components/StatusBadge'
import { Button } from '@/components/ui/button'
import { Badge, Panel } from '@/components/ui/primitives'
import { limitsText } from '@/lib/format'
import type { Tone } from '@/lib/status'
import { cn } from '@/lib/utils'
import { CONNECTION_STATE } from '../extensions/ConnectionCard'
import { ChangeStatus, RuntimeSettingsForm, useRuntimeChangeFlow, type ChangeFlow } from './RuntimeForms'

/** Where sandboxes run. Admins add a runtime, change one's settings, make one the default or remove one. */
export function Runtimes() {
  const admin = !!useMe().data?.admin
  const runtimes = useRuntimes()
  const available = useAvailableRuntimes(admin)
  const flow = useRuntimeChangeFlow(admin)
  const [adding, setAdding] = useState(false)
  const byKind = Object.fromEntries((available.data ?? []).map(a => [a.kind, a]))
  const addable = (available.data ?? []).filter(a => !a.enabled && !a.host && a.can_run)
  const title = (kind: string) => byKind[kind]?.title ?? kind
  return <Panel id="runtimes" title="Sandbox runtimes" bodyClassName="p-0"
    description="Where sandboxes run. Each sandbox runs on one, chosen when it is created; repositories without a choice of their own use the default."
    actions={admin && addable.length > 0 && !adding ? <Button size="sm" disabled={flow.busy} onClick={() => setAdding(true)}>Add a runtime</Button> : undefined}>
    {admin && (flow.busy || flow.change?.state === 'failed' || (flow.change?.state === 'done' && flow.mine)) && <div className="border-b border-line px-4 py-3"><ChangeStatus flow={flow} title={title} /></div>}
    {adding && <AddRuntime choices={addable} flow={flow} onClose={() => setAdding(false)} />}
    <QueryView query={runtimes} inline loading={<SkeletonRows rows={1} />}>{d => d.runtimes.length === 0
      ? <EmptyState title="No sandbox runtime yet">Sandboxes need one to run on.{admin && addable.length > 0 ? ' Add one above.' : ''}</EmptyState>
      : <RuntimeList runtimes={d.runtimes}>{r => admin && byKind[r.kind] ? <RuntimeActions r={r} a={byKind[r.kind]} flow={flow} /> : null}</RuntimeList>}
    </QueryView>
  </Panel>
}

function AddRuntime({ choices, flow, onClose }: { choices: AvailableRuntime[]; flow: ChangeFlow; onClose: () => void }) {
  const [kind, setKind] = useState(choices[0]?.kind ?? '')
  const chosen = choices.find(c => c.kind === kind)
  return <div className="space-y-3 border-b border-line px-4 py-4">
    <h3 className="font-semibold">Add a runtime</h3>
    {choices.length > 1 && <RuntimeChoices choices={choices} value={kind} onChange={setKind} />}
    {chosen && (chosen.not_editable
      ? <><ErrorState compact error={chosen.not_editable} /><div className="flex justify-end"><Button variant="ghost" onClick={onClose}>Close</Button></div></>
      : <RuntimeSettingsForm key={chosen.kind} runtime={chosen} mode="add" flow={flow} onDone={onClose} onCancel={onClose} />)}
  </div>
}

/** The runtimes to choose from: one this server can't run, or install.sh's, is listed with why and can't be chosen. */
export function RuntimeChoices({ choices, value, onChange }: { choices: AvailableRuntime[]; value: string; onChange: (kind: string) => void }) {
  return <fieldset className="grid gap-2 sm:grid-cols-2">
    <legend className="sr-only">Runtime</legend>
    {choices.map(c => {
      const selectable = !c.host && c.can_run
      const note = !c.can_run ? `Can't run here: ${c.why_not}` : c.host ? 'Set up by install.sh: run it again on this server to enable it' : 'Runs at the provider'
      return <label key={c.kind} className={cn('flex items-start gap-3 rounded-lg border bg-panel p-3', value === c.kind ? 'border-accent ring-1 ring-accent' : 'border-line', selectable ? 'cursor-pointer' : 'opacity-60')}>
        <input type="radio" name="runtime" value={c.kind} checked={value === c.kind} disabled={!selectable} onChange={() => onChange(c.kind)} className="mt-1 shrink-0 accent-[var(--accent)]" />
        <span className="min-w-0"><span className="block font-medium">{c.title}</span><span className="block text-[12.5px] text-muted">{note}</span></span>
      </label>
    })}
  </fieldset>
}

function RuntimeActions({ r, a, flow }: { r: SandboxRuntime; a: AvailableRuntime; flow: ChangeFlow }) {
  const [editing, setEditing] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const run = async (call: () => Promise<RuntimeChange>) => {
    setError(null)
    try { await flow.submit(call) } catch (e) { setError(e) }
  }
  const editable = !a.host && !a.not_editable && a.settings.length > 0
  return <div className="mt-2 space-y-2">
    {a.host && <p className="text-[12.5px] text-muted">Set up by install.sh on this server.</p>}
    {!a.host && a.not_editable && <p className="text-[12.5px] text-muted">{a.not_editable}</p>}
    <div className="flex flex-wrap gap-2">
      {editable && !editing && <Button size="sm" disabled={flow.busy} onClick={() => setEditing(true)}>Change settings</Button>}
      {!r.default && <Button size="sm" disabled={flow.busy} onClick={() => run(() => api.runtimeSettings.setDefault(r.kind))}>Make default</Button>}
      {!a.host && (a.removable
        ? <Button size="sm" variant="ghost" disabled title={a.removable}>Remove</Button>
        : <ConfirmDialog title={`Remove ${r.title}?`} description="New sandboxes can no longer run on it; its key leaves this server." confirm="Remove"
          trigger={<Button size="sm" variant="ghost" disabled={flow.busy}>Remove</Button>} onConfirm={() => flow.submit(() => api.runtimeSettings.remove(r.kind))} />)}
    </div>
    {!a.host && a.removable && <p className="text-[12px] text-muted">Can't remove: {a.removable}.</p>}
    <ErrorState compact error={error} />
    {editing && <div className="rounded-lg border border-line p-3"><RuntimeSettingsForm runtime={a} mode="change" flow={flow} onDone={() => setEditing(false)} onCancel={() => setEditing(false)} /></div>}
  </div>
}

/** One row per enabled runtime: title, default, where it runs, its status and limits, and a limit its provider refused. */
export function RuntimeList({ runtimes, className, children }: { runtimes: SandboxRuntime[]; className?: string; children?: (r: SandboxRuntime) => ReactNode }) {
  return <ul aria-label="Sandbox runtimes" className={cn('divide-y divide-line', className)}>{runtimes.map(r => <RuntimeRow key={r.kind} r={r}>{children?.(r)}</RuntimeRow>)}</ul>
}

function RuntimeRow({ r, children }: { r: SandboxRuntime; children?: ReactNode }) {
  const state = CONNECTION_STATE[r.status.state as ConnectionState] ?? { label: r.status.state, tone: 'idle' as Tone }
  const limits = limitsText(r.limits)
  return <li className="px-4 py-3">
    <div className="flex flex-wrap items-center gap-2">
      <h3 className="font-semibold">{r.title}</h3>
      {r.default && <Badge tone="accent">Default</Badge>}
      <StateBadge tone={state.tone} label={state.label} />
    </div>
    <p className="mt-0.5 break-words text-[13px] text-muted">{r.host ? 'Runs on this server' : 'Runs at the provider'}{r.status.summary ? `. ${r.status.summary}` : ''}</p>
    {limits && <p className="mt-0.5 text-[12px] text-muted">{limits}</p>}
    {r.limit_hit && <p role="note" className="mt-1 break-words text-[12.5px] text-warn">{r.limit_hit.message}</p>}
    {children}
  </li>
}
