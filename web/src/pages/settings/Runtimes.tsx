import { useState, type ReactNode } from 'react'
import { api } from '@/api/endpoints'
import { Link } from 'react-router'
import { useAvailableRuntimes, useEndpoints, useMe, useRuntimes } from '@/api/queries'
import type { AvailableRuntime, Blocker, Endpoints, RuntimeChange, SandboxRuntime } from '@/api/types'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { DetailText, EmptyState, ErrorState, QueryView, SkeletonRows } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Panel } from '@/components/ui/primitives'
import { limitsText } from '@/lib/format'
import { cn } from '@/lib/utils'
import { ConnectionMark } from '../extensions/ConnectionCard'
import { ENDPOINTS_LINK, QUIET_LINK } from './Endpoints'
import { ChangeStatus, RuntimeSettingsForm, useRuntimeChangeFlow, type ChangeFlow } from './RuntimeForms'

/** Where sandboxes run. Admins add a runtime, change one's settings, make one the default or remove one. */
export function Runtimes() {
  const admin = !!useMe().data?.admin
  const runtimes = useRuntimes()
  const available = useAvailableRuntimes(admin)
  const flow = useRuntimeChangeFlow(admin)
  const endpoints = useEndpoints(admin).data
  const [adding, setAdding] = useState(false)
  const byKind = Object.fromEntries((available.data ?? []).map(a => [a.kind, a]))
  const addable = (available.data ?? []).filter(a => !a.enabled && !a.host && a.can_run)
  return <Panel id="runtimes" title="Sandbox runtimes" bodyClassName="p-0"
    actions={admin && addable.length > 0 && !adding ? <Button size="sm" disabled={flow.busy} onClick={() => setAdding(true)}>Add runtime</Button> : undefined}>
    {admin && <ChangeStatus flow={flow} className="border-b border-line px-4 py-3" />}
    {adding && <AddRuntime choices={addable} flow={flow} onClose={() => setAdding(false)} />}
    <QueryView query={runtimes} inline loading={<SkeletonRows rows={1} />}>{d => d.runtimes.length === 0
      ? <EmptyState title="No runtime yet" />
      : <RuntimeList runtimes={d.runtimes}>{r => <>
        {endpoints && !r.capabilities.private_networks && <Reaches endpoints={endpoints} />}
        {admin && byKind[r.kind] && <RuntimeActions r={r} a={byKind[r.kind]} flow={flow} />}
      </>}</RuntimeList>}
    </QueryView>
  </Panel>
}

function AddRuntime({ choices, flow, onClose }: { choices: AvailableRuntime[]; flow: ChangeFlow; onClose: () => void }) {
  const [kind, setKind] = useState(choices[0]?.kind ?? '')
  const chosen = choices.find(c => c.kind === kind)
  return <div className="space-y-3 border-b border-line px-4 py-4">
    <h3 className="font-semibold">Add runtime</h3>
    {choices.length > 1 && <RuntimeChoices choices={choices} value={kind} onChange={setKind} />}
    {chosen && (chosen.edit_blocker
      ? <><ErrorState compact error={blockedText(chosen.edit_blocker, chosen.kind)} /><div className="flex justify-end"><Button variant="ghost" onClick={onClose}>Close</Button></div></>
      : <RuntimeSettingsForm key={chosen.kind} runtime={chosen} mode="add" flow={flow} onDone={onClose} onCancel={onClose} />)}
  </div>
}

/** Why the web app can't add or change a runtime, in a sentence with what to do. */
export function blockedText(b: Blocker, kind: string): string {
  if (b.code === 'host') return 'Managed by install.sh on this server.'
  if (b.code === 'needs_domain') return 'Needs a domain first.'
  if (b.code === 'local_store') return `Add it on the server instead: sbx-admin runtime add ${kind}`
  return b.message
}

/** "needs /dev/kvm: bare metal, ..." → "Needs /dev/kvm". */
const reasonHead = (text: string) => { const head = text.split(':')[0].trim(); return head.charAt(0).toUpperCase() + head.slice(1) }

/** The runtimes to choose from: one this server can't run, or install.sh's, is listed with why and can't be chosen. */
export function RuntimeChoices({ choices, value, onChange }: { choices: AvailableRuntime[]; value: string; onChange: (kind: string) => void }) {
  return <fieldset className="grid gap-2 sm:grid-cols-2">
    <legend className="sr-only">Runtime</legend>
    {choices.map(c => {
      const selectable = !c.host && c.can_run
      const note = !c.can_run ? reasonHead(c.why_not) : c.host ? 'Enable with install.sh' : 'Hosted'
      return <label key={c.kind} className={cn('flex items-start gap-3 rounded-lg border bg-panel p-3', value === c.kind ? 'border-ink ring-1 ring-ink' : 'border-line', selectable ? 'cursor-pointer' : 'opacity-60')}>
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
  const editable = !a.edit_blocker && a.settings.length > 0
  return <div className="mt-2 space-y-2">
    {a.edit_blocker && <p className="text-[12.5px] text-muted">{blockedText(a.edit_blocker, a.kind)}</p>}
    <div className="flex flex-wrap gap-2">
      {editable && !editing && <Button size="sm" disabled={flow.busy} onClick={() => setEditing(true)}>Edit</Button>}
      {!r.default && <Button size="sm" disabled={flow.busy} onClick={() => run(() => api.runtimeSettings.setDefault(r.kind))}>Make default</Button>}
      {!a.host && (a.remove_blocker
        ? <span title={removeBlocked(a.remove_blocker)}><Button size="sm" variant="ghost" disabled>Remove</Button></span>
        : <ConfirmDialog title={`Remove ${r.title}?`} description="New sandboxes can't run on it." confirm="Remove"
          trigger={<Button size="sm" variant="ghost" disabled={flow.busy}>Remove</Button>} onConfirm={() => flow.submit(() => api.runtimeSettings.remove(r.kind))} />)}
    </div>
    <ErrorState compact error={error} />
    {editing && <div className="rounded-lg border border-line p-3"><RuntimeSettingsForm runtime={a} mode="change" flow={flow} onDone={() => setEditing(false)} onCancel={() => setEditing(false)} /></div>}
  </div>
}

/** One row per enabled runtime: title, default, where it runs, its status and limits, and a limit its provider refused. */
export function RuntimeList({ runtimes, className, children }: { runtimes: SandboxRuntime[]; className?: string; children?: (r: SandboxRuntime) => ReactNode }) {
  return <ul aria-label="Sandbox runtimes" className={cn('divide-y divide-line', className)}>{runtimes.map(r => <RuntimeRow key={r.kind} r={r}>{children?.(r)}</RuntimeRow>)}</ul>
}

/** Why Remove is off, short: the last runtime, or the sandboxes still on it. */
function removeBlocked(b: Blocker): string {
  if (b.code === 'last_runtime') return 'Add another runtime first'
  if (b.code === 'has_sandboxes') return `Delete its sandboxes first (${b.sandboxes.join(', ')})`
  return b.message
}

/** A runtime's line: with a plan, "Hobby · 0 of 10 running"; on this server, "On this server · <its status>";
 * a runtime that needs attention says why instead. */
export function runtimeLine(r: SandboxRuntime): string {
  if (r.status.state !== 'connected' && r.status.summary) return r.status.summary
  if (r.plan) {
    const plan = r.plan.name === 'custom' ? 'Custom limits' : r.plan.title
    const most = r.limits.sandboxes
    const running = r.status.running
    return [plan, running != null && most != null ? `${running} of ${most} running` : most != null ? `up to ${most} at once` : ''].filter(Boolean).join(' · ')
  }
  return [r.host ? 'On this server' : 'Hosted', r.status.running != null ? `${r.status.running} running` : r.status.summary]
    .filter(Boolean).join(' · ')
}

function RuntimeRow({ r, children }: { r: SandboxRuntime; children?: ReactNode }) {
  const limits = r.plan ? '' : limitsText(r.limits)   // a plan's limits are in its name; the form shows them
  return <li className="px-4 py-3">
    <div className="flex flex-wrap items-center gap-2">
      <h3 className="font-semibold">{r.title}</h3>
      {r.default && <span className="text-[12px] text-muted">Default</span>}
      <ConnectionMark state={r.status.state} />
    </div>
    <div className="mt-0.5 break-words text-[13px] text-muted"><DetailText text={runtimeLine(r)} /></div>
    {limits && <p className="mt-0.5 text-[12px] text-muted">{limits}</p>}
    {r.limit_hit && <p role="note" className="mt-1 break-words text-[12.5px]">{r.limit_hit.message.split(';')[0]}.</p>}
    {children}
  </li>
}

/** For a runtime whose sandboxes can't reach this server's private network: the addresses they reach instead. */
function Reaches({ endpoints }: { endpoints: Endpoints }) {
  const on = endpoints.endpoints.filter(e => e.state === 'on').map(e => e.name)
  const link = QUIET_LINK
  return <p className="mt-1 break-words text-[12.5px] text-muted">
    {on.length
      ? <>Reaches <Link to={ENDPOINTS_LINK} className={link}>{on.join(', ')}</Link></>
      : <>Reaches nothing yet — <Link to={ENDPOINTS_LINK} className={link}>Share a server</Link></>}
  </p>
}
