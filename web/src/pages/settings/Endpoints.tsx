import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { useLocation } from 'react-router'
import { api } from '@/api/endpoints'
import { keys, useEndpoints } from '@/api/queries'
import type { AddEndpoint, Endpoint, EndpointCandidate, Endpoints as EndpointsData } from '@/api/types'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { CopyButton } from '@/components/CopyCommand'
import { AppForm, SubmitButton, useAppForm } from '@/components/form'
import { ErrorState } from '@/components/states'
import { StateBadge } from '@/components/StatusBadge'
import { Button, LinkButton } from '@/components/ui/button'
import { Panel, Spinner } from '@/components/ui/primitives'
import { cn } from '@/lib/utils'

export const ENDPOINTS_LINK = '/app/settings/general#endpoints'
const INTERNAL = ':internal'
const KIND_TITLE: Record<string, string> = { postgres: 'Postgres', clickhouse: 'ClickHouse', temporal: 'Temporal' }
const kindTitle = (kind?: string) => (kind ? KIND_TITLE[kind] ?? kind : '')

/** What an endpoint is, in a few words: "dev-pg · Postgres", or "Internal host". */
export function endpointWhat(e: Pick<Endpoint, 'kind' | 'server' | 'server_kind'>): string {
  if (e.kind === 'internal') return 'Internal host'
  return [e.server, kindTitle(e.server_kind) || 'shared server'].filter(Boolean).join(' · ')
}

/** Managed shared servers and internal hosts reachable from sandboxes at a provider, which can't reach this
 * server's private network. Shown while a runtime needs it or something is reachable. */
export function Endpoints() {
  const query = useEndpoints()
  const [adding, setAdding] = useState(false)
  const box = useRef<HTMLDivElement>(null)
  const { hash } = useLocation()
  const d = query.data
  const shown = !!d && (d.needed || d.endpoints.length > 0)
  useEffect(() => {
    if (hash !== '#endpoints' || !shown) return
    box.current?.scrollIntoView({ block: 'start' })
    box.current?.focus({ preventScroll: true })
  }, [hash, shown])
  if (!d) return query.error ? <ErrorState error={query.error} retry={() => query.refetch()} /> : null
  if (!shown) return null
  return <div ref={box} tabIndex={-1} className="scroll-mt-16 rounded-lg outline-none lg:scroll-mt-4">
    <Panel id="endpoints" title="Reachable from provider sandboxes" bodyClassName="p-0"
      description="Sandboxes at a provider can't reach this server's private network. Make a shared server or an internal host reachable to give them an address for it."
      actions={d.domain && !adding ? <Button size="sm" onClick={() => setAdding(true)}>Make reachable</Button> : undefined}>
      {!d.domain && <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
        <p className="min-w-0 flex-1 basis-64 text-[13px] text-muted">A domain is needed first: each reachable server gets an address under it.</p>
        <LinkButton size="sm" to="/app/settings/general#domain">Set the domain</LinkButton>
      </div>}
      {adding && <MakeReachable data={d} onClose={() => setAdding(false)} />}
      {d.endpoints.length > 0
        ? <ul aria-label="Reachable from provider sandboxes" className={cn('divide-y divide-line', (adding || !d.domain) && 'border-t border-line')}>
          {d.endpoints.map(e => <EndpointRow key={e.name} e={e} />)}
        </ul>
        : d.domain && !adding && <p className="px-4 py-5 text-[13px] text-muted">Nothing is reachable yet: provider sandboxes reach no shared server.</p>}
    </Panel>
  </div>
}

function EndpointState({ e }: { e: Endpoint }) {
  if (e.state === 'applying') return <Spinner className="size-3.5" label="Making reachable…" />
  if (e.state === 'failed') return <StateBadge tone="bad" label="Failed" />
  return <StateBadge tone="ok" label="Reachable" />
}

function EndpointRow({ e }: { e: Endpoint }) {
  const qc = useQueryClient()
  const used = e.used_by.length > 0
  const repos = e.used_by.join(', ')
  return <li className="flex flex-wrap items-start gap-x-4 gap-y-2 px-4 py-3">
    <div className="min-w-0 flex-1 basis-64">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="flex min-w-0 items-center gap-0.5">
          <code className="min-w-0 break-all text-[13.5px] font-medium">{e.address}</code>
          <CopyButton text={e.address} size="icon-sm" variant="ghost" label={`Copy ${e.address}`} className="size-6" />
        </span>
        <EndpointState e={e} />
      </div>
      <p className="mt-0.5 text-[13px] text-muted">{endpointWhat(e)}</p>
      <p className="mt-0.5 break-words text-[12px] text-muted">{used ? `Used by ${repos}` : 'No repository uses it yet'}</p>
      {used && <p className="mt-0.5 text-[12px] text-muted">Can't stop exposing it while repositories use it.</p>}
      {e.state === 'failed' && <p role="note" className="mt-1 break-words text-[12.5px] text-bad">{e.message || 'It could not be made reachable; see the server log.'}</p>}
    </div>
    {used
      ? <Button size="sm" variant="ghost" disabled title={`Used by ${repos}`}>Stop exposing</Button>
      : <ConfirmDialog title={`Stop exposing ${e.address}?`} confirm="Stop exposing"
        description="Provider sandboxes can no longer reach it at this address. Sandboxes on this server keep their access."
        note="You can make it reachable again later."
        trigger={<Button size="sm" variant="ghost" disabled={e.state === 'applying'}>Stop exposing</Button>}
        onConfirm={async () => { await api.endpoints.remove(e.name); await qc.invalidateQueries({ queryKey: keys.endpoints }) }} />}
  </li>
}

type Pending = { body: AddEndpoint; address: string; what: string; title: string }

/** Two steps: choose a managed server (or name an internal host), then confirm, seeing the address it gets. */
function MakeReachable({ data, onClose }: { data: EndpointsData; onClose: () => void }) {
  const [choice, setChoice] = useState(data.candidates[0]?.server ?? INTERNAL)
  const [pending, setPending] = useState<Pending | null>(null)
  const address = (name: string) => `${name}.${data.domain}`
  if (pending) return <Confirm pending={pending} onBack={() => setPending(null)} onDone={onClose} />
  const candidate = data.candidates.find(c => c.server === choice)
  return <div className="space-y-3 px-4 py-4">
    <h3 className="font-semibold">Make reachable</h3>
    <Choices candidates={data.candidates} value={choice} onChange={setChoice} />
    {choice === INTERNAL
      ? <InternalHost domain={data.domain} taken={data.endpoints.map(e => e.name)} onCancel={onClose}
        onNext={(name, target) => setPending({ body: { name, target }, address: address(name), what: 'Internal host', title: 'this internal host' })} />
      : <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="primary" disabled={!candidate} onClick={() => candidate && setPending({
          body: { server: candidate.server }, address: address(candidate.server), title: candidate.server, what: endpointWhat({ kind: 'managed-server', server: candidate.server, server_kind: candidate.kind }),
        })}>Continue</Button>
      </div>}
  </div>
}

function Choices({ candidates, value, onChange }: { candidates: EndpointCandidate[]; value: string; onChange: (v: string) => void }) {
  const first = useRef<HTMLInputElement>(null)
  useEffect(() => { first.current?.focus() }, [])
  const options = [
    ...candidates.map(c => ({ value: c.server, title: c.server, note: `${kindTitle(c.kind)} shared server` })),
    { value: INTERNAL, title: 'An internal host', note: 'A service on the network this server can reach' },
  ]
  return <fieldset className="grid gap-2 sm:grid-cols-2">
    <legend className="sr-only">What to make reachable</legend>
    {options.map((o, i) => <label key={o.value} className={cn('flex cursor-pointer items-start gap-3 rounded-lg border bg-panel p-3 transition-[border-color,box-shadow] duration-150 ease-out',
      value === o.value ? 'border-accent ring-1 ring-accent' : 'border-line hover:border-line-strong')}>
      <input ref={i === 0 ? first : undefined} type="radio" name="endpoint-choice" value={o.value} checked={value === o.value} onChange={() => onChange(o.value)}
        className="mt-1 shrink-0 accent-[var(--accent)]" />
      <span className="min-w-0"><span className="block break-all font-medium">{o.title}</span><span className="block text-[12.5px] text-muted">{o.note}</span></span>
    </label>)}
  </fieldset>
}

const DNS_LABEL = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/
const HOST_PORT = /^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?):(\d{1,5})$/

export function endpointNameError(value: string, taken: string[] = []): string | undefined {
  if (!value) return 'Enter a name.'
  if (!DNS_LABEL.test(value)) return 'Use lowercase letters, digits and hyphens (up to 63), not starting or ending with a hyphen.'
  if (taken.includes(value)) return 'Something is already reachable under that name.'
  return undefined
}

export function endpointTargetError(value: string): string | undefined {
  const m = HOST_PORT.exec(value.trim())
  const port = m ? Number(m[3]) : 0
  return m && port >= 1 && port <= 65535 ? undefined : 'Enter the host and its port as host:port.'
}

function InternalHost({ domain, taken, onCancel, onNext }: { domain: string; taken: string[]; onCancel: () => void; onNext: (name: string, target: string) => void }) {
  const form = useAppForm({
    defaultValues: { name: '', target: '' },
    onSubmit: ({ value }) => onNext(value.name, value.target.trim()),
  })
  return <AppForm form={form} className="space-y-3">
    <div className="grid gap-3 sm:grid-cols-2">
      <form.AppField name="name" validators={{ onChange: ({ value }) => endpointNameError(value, taken) }}>
        {field => <field.TextField label="Name" autoComplete="off" spellCheck={false} placeholder="billing-api"
          help={<>Its address: <span className="mono break-all text-ink">{field.state.value || 'name'}.{domain}</span></>} />}
      </form.AppField>
      <form.AppField name="target" validators={{ onChange: ({ value }) => endpointTargetError(value) }}>
        {field => <field.TextField label="Host and port" autoComplete="off" spellCheck={false} placeholder="host:port"
          help="As this server reaches it on its network." />}
      </form.AppField>
    </div>
    <div className="flex justify-end gap-2">
      <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      <SubmitButton form={form}>Continue</SubmitButton>
    </div>
  </AppForm>
}

/** The opt-in: the address it gets, and who can reach it there. */
function Confirm({ pending, onBack, onDone }: { pending: Pending; onBack: () => void; onDone: () => void }) {
  const qc = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const submit = async () => {
    setBusy(true); setError(null)
    try {
      const added = await api.endpoints.add(pending.body)
      qc.setQueryData<EndpointsData>(keys.endpoints, d => d && {
        ...d,
        endpoints: [...d.endpoints.filter(e => e.name !== added.name), added],
        candidates: d.candidates.filter(c => !('server' in pending.body) || c.server !== pending.body.server),
      })
      void qc.invalidateQueries({ queryKey: keys.endpoints })
      onDone()
    } catch (e) { setError(e) } finally { setBusy(false) }
  }
  return <section aria-labelledby="endpoint-confirm" className="space-y-3 px-4 py-4">
    <h3 id="endpoint-confirm" className="font-semibold">Make {pending.title} reachable?</h3>
    <div className="rounded-lg border border-line bg-sunken px-3 py-2.5">
      <p className="text-[12px] text-muted">Its address</p>
      <p className="mono mt-0.5 break-all text-[14px] font-medium text-ink" data-testid="endpoint-address">{pending.address}</p>
      <p className="mt-0.5 text-[12px] text-muted">{pending.what}</p>
    </div>
    <p className="text-[13px] text-muted">It becomes reachable from the internet at this address, but only by this installation's sandboxes: each has its own certificate. The service's own login still applies.</p>
    <ErrorState compact error={error} />
    <div className="flex justify-end gap-2">
      <Button variant="ghost" onClick={onBack} disabled={busy}>Back</Button>
      <Button variant="primary" autoFocus busy={busy} onClick={submit}>Make reachable</Button>
    </div>
  </section>
}
