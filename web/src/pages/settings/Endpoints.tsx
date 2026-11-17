import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useId, useRef, useState } from 'react'
import { useLocation } from 'react-router'
import { api } from '@/api/endpoints'
import { keys, useEndpoints, useRepos } from '@/api/queries'
import type { AddEndpoint, Endpoint, EndpointCandidate, Endpoints as EndpointsData } from '@/api/types'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { CopyButton } from '@/components/CopyCommand'
import { AppForm, SubmitButton, useAppForm, useStore } from '@/components/form'
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

/** "a", "a and b", "a, b and c". */
export function listText(items: string[]): string {
  return items.length < 2 ? items.join('') : `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`
}

function EndpointRow({ e }: { e: Endpoint }) {
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  const used = e.used_by.length > 0
  const internal = e.kind === 'internal'
  const repos = e.repos ?? []
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
      {internal
        ? <div className="mt-0.5 flex flex-wrap items-baseline gap-x-2 text-[12.5px]">
          <p className="min-w-0 break-words text-muted">Repositories: <span className="text-ink">{repos.length ? repos.join(', ') : 'none'}</span></p>
          {!editing && <button type="button" onClick={() => setEditing(true)} aria-label={`Edit repositories for ${e.address}`} className="rounded-sm text-accent underline-offset-2 hover:underline focus-visible:underline">Edit</button>}
        </div>
        : <>
          <p className="mt-0.5 break-words text-[12.5px] text-muted">Reached by: <span className="text-ink">{repos.length ? repos.join(', ') : 'no repository yet'}</span></p>
          <p className="text-[12px] text-faint">The repositories {e.server} is attached to; attaching it to a repository grants that repository access.</p>
        </>}
      {used && <p className="mt-0.5 break-words text-[12px] text-muted">Can't stop exposing it while {listText(e.used_by)} {e.used_by.length > 1 ? 'use' : 'uses'} it.</p>}
      {e.state === 'failed' && <p role="note" className="mt-1 break-words text-[12.5px] text-bad">{e.message || 'It could not be made reachable; see the server log.'}</p>}
      {editing && <EditRepos e={e} onDone={() => setEditing(false)} />}
    </div>
    {used
      ? <Button size="sm" variant="ghost" disabled title={`Used by ${e.used_by.join(', ')}`}>Stop exposing</Button>
      : <ConfirmDialog title={`Stop exposing ${e.address}?`} confirm="Stop exposing"
        description="Provider sandboxes can no longer reach it at this address. Sandboxes on this server keep their access."
        note="You can make it reachable again later."
        trigger={<Button size="sm" variant="ghost" disabled={e.state === 'applying'}>Stop exposing</Button>}
        onConfirm={async () => { await api.endpoints.remove(e.name); await qc.invalidateQueries({ queryKey: keys.endpoints }) }} />}
  </li>
}

const reposError = (repos: string[]) => (repos.length ? undefined : 'Choose at least one repository.')

/** The installation's repositories as checkboxes. */
function RepoPicker({ value, onChange, onBlur, error }: { value: string[]; onChange: (repos: string[]) => void; onBlur?: () => void; error?: string }) {
  const repos = useRepos()
  const id = useId()
  const toggle = (repo: string, on: boolean) => onChange(on ? [...value, repo] : value.filter(r => r !== repo))
  const names = [...new Set([...(repos.data ?? []).map(r => r.repo), ...value])].sort()
  return <fieldset className="min-w-0" aria-describedby={`${id}-hint`} onBlur={onBlur}>
    <legend className="mb-1 text-[12.5px] font-medium">Repositories</legend>
    {repos.isPending ? <Spinner className="size-3.5" label="Loading repositories…" />
      : repos.error && !names.length ? <ErrorState compact error={repos.error} retry={() => repos.refetch()} />
      : names.length === 0 ? <p className="text-[13px] text-muted">No repository is connected yet.</p>
      : <ul className="max-h-48 divide-y divide-line overflow-y-auto rounded-md border border-line">
        {names.map(repo => <li key={repo}>
          <label className="flex cursor-pointer items-center gap-2.5 px-3 py-2 text-[13px] hover:bg-hover">
            <input type="checkbox" className="size-4 shrink-0 accent-[var(--accent)]" checked={value.includes(repo)} onChange={e => toggle(repo, e.target.checked)} />
            <span className="min-w-0 break-all">{repo}</span>
          </label>
        </li>)}
      </ul>}
    <p id={`${id}-hint`} aria-live="polite" className={cn('mt-1 text-[12px]', error ? 'text-bad' : 'text-muted')}>{error || 'Only their sandboxes reach it.'}</p>
  </fieldset>
}

/** An internal host's repositories, replaced as a whole. */
function EditRepos({ e, onDone }: { e: Endpoint; onDone: () => void }) {
  const qc = useQueryClient()
  const [repos, setRepos] = useState(e.repos ?? [])
  const [tried, setTried] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const invalid = reposError(repos)
  const save = async () => {
    setTried(true)
    if (invalid) return
    setBusy(true); setError(null)
    try {
      await api.endpoints.setRepos(e.name, repos)
      await qc.invalidateQueries({ queryKey: keys.endpoints })
      onDone()
    } catch (x) { setError(x) } finally { setBusy(false) }
  }
  return <div className="mt-2 space-y-2 rounded-lg border border-line p-3">
    <RepoPicker value={repos} onChange={setRepos} error={tried ? invalid : undefined} />
    <ErrorState compact error={error} />
    <div className="flex justify-end gap-2">
      <Button size="sm" variant="ghost" onClick={onDone} disabled={busy}>Cancel</Button>
      <Button size="sm" variant="primary" busy={busy} onClick={save}>Save</Button>
    </div>
  </div>
}

type Pending = { body: AddEndpoint; address: string; what: string; title: string; repos?: string[] }
type Draft = { name: string; target: string; repos: string[] }

/** Two steps: choose a managed server (or name an internal host and its repositories), then confirm, seeing the address it gets. */
function MakeReachable({ data, onClose }: { data: EndpointsData; onClose: () => void }) {
  const [choice, setChoice] = useState(data.candidates[0]?.server ?? INTERNAL)
  const [pending, setPending] = useState<Pending | null>(null)
  const [draft, setDraft] = useState<Draft>({ name: '', target: '', repos: [] })
  const address = (name: string) => `${name}.${data.domain}`
  if (pending) return <Confirm pending={pending} onBack={() => setPending(null)} onDone={onClose} />
  const candidate = data.candidates.find(c => c.server === choice)
  return <div className="space-y-3 px-4 py-4">
    <h3 className="font-semibold">Make reachable</h3>
    <Choices candidates={data.candidates} value={choice} onChange={setChoice} />
    {choice === INTERNAL
      ? <InternalHost domain={data.domain} taken={data.endpoints.map(e => e.name)} draft={draft} onCancel={onClose}
        onNext={d => { setDraft(d); setPending({ body: d, address: address(d.name), what: 'Internal host', title: 'this internal host', repos: d.repos }) }} />
      : <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="primary" disabled={!candidate} onClick={() => candidate && setPending({
          body: { server: candidate.server }, address: candidate.address || address(candidate.server), title: candidate.server,
          what: endpointWhat({ kind: 'managed-server', server: candidate.server, server_kind: candidate.kind }),
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

function InternalHost({ domain, taken, draft, onCancel, onNext }: { domain: string; taken: string[]; draft: Draft; onCancel: () => void; onNext: (d: Draft) => void }) {
  const form = useAppForm({
    defaultValues: draft,
    // Repositories are checked here: a field validator on this array field did not run at submit.
    onSubmit: ({ value }) => { if (!reposError(value.repos)) onNext({ ...value, target: value.target.trim() }) },
  })
  const attempted = useStore(form.store, s => s.submissionAttempts > 0)
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
    <form.Field name="repos">
      {field => <RepoPicker value={field.state.value} onChange={field.handleChange} onBlur={field.handleBlur}
        error={attempted ? reposError(field.state.value) : undefined} />}
    </form.Field>
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
    <p className="break-words text-[13px] font-medium text-ink">{pending.repos
      ? `Only sandboxes of ${listText(pending.repos)} reach it.`
      : `Only sandboxes of repositories ${pending.title} is attached to reach it.`}</p>
    <ErrorState compact error={error} />
    <div className="flex justify-end gap-2">
      <Button variant="ghost" onClick={onBack} disabled={busy}>Back</Button>
      <Button variant="primary" autoFocus busy={busy} onClick={submit}>Make reachable</Button>
    </div>
  </section>
}
