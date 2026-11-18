import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useId, useRef, useState } from 'react'
import { useLocation } from 'react-router'
import { api } from '@/api/endpoints'
import { keys, useEndpoints, useRepos, useRuntimes } from '@/api/queries'
import type { AddEndpoint, Endpoint, EndpointCandidate, Endpoints as EndpointsData } from '@/api/types'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { CopyButton } from '@/components/CopyCommand'
import { AppForm, SubmitButton, useAppForm, useStore } from '@/components/form'
import { ErrorState } from '@/components/states'
import { ToneDot } from '@/components/StatusBadge'
import { Button, LinkButton } from '@/components/ui/button'
import { Panel, Spinner } from '@/components/ui/primitives'
import { cn } from '@/lib/utils'

export const ENDPOINTS_LINK = '/app/settings/general#endpoints'
/** A link in running text: neutral, underlined; accent is kept for the primary action. */
export const QUIET_LINK = 'rounded-sm text-ink underline decoration-line-strong underline-offset-2 hover:decoration-current focus-visible:decoration-current'
const INTERNAL = ':internal'
const KIND_TITLE: Record<string, string> = { postgres: 'Postgres', clickhouse: 'ClickHouse', temporal: 'Temporal' }
const kindTitle = (kind?: string) => (kind ? KIND_TITLE[kind] ?? kind : '')

/** What an endpoint is, in a word or two: "Postgres", "Internal host". */
export function endpointWhat(e: Pick<Endpoint, 'kind' | 'server_kind'>): string {
  return e.kind === 'internal' ? 'Internal host' : kindTitle(e.server_kind) || 'Server'
}

/** Managed shared servers and internal hosts reachable from sandboxes at a provider, which can't reach this
 * server's private network. Shown while a runtime needs it or something is reachable. */
export function Endpoints() {
  const query = useEndpoints()
  const providers = (useRuntimes().data?.runtimes ?? []).filter(r => !r.capabilities.private_networks).map(r => r.title)
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
    <Panel id="endpoints" title="Shared servers" bodyClassName="p-0"
      description={providers.length ? `For sandboxes at ${listText(providers)}.` : undefined}
      actions={d.domain && !adding ? <Button size="sm" onClick={() => setAdding(true)}>Share a server</Button> : undefined}>
      {!d.domain && <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
        <p className="min-w-0 flex-1 basis-48 text-[13px] text-muted">Set a domain first.</p>
        <LinkButton size="sm" to="/app/settings/general#domain">Set domain</LinkButton>
      </div>}
      {adding && <MakeReachable data={d} onClose={() => setAdding(false)} />}
      {d.endpoints.length > 0
        ? <ul aria-label="Shared servers" className={cn('divide-y divide-line', (adding || !d.domain) && 'border-t border-line')}>
          {d.endpoints.map(e => <EndpointRow key={e.name} e={e} />)}
        </ul>
        : d.domain && !adding && <p className="px-4 py-5 text-[13px] text-muted">Nothing shared yet.</p>}
    </Panel>
  </div>
}

function EndpointState({ e }: { e: Endpoint }) {
  if (e.state === 'applying') return <Spinner className="size-3.5" label="Sharing…" />
  // Healthy is the quiet default (a muted dot); a failure says so once, in the row's message.
  if (e.state === 'failed') return null
  return <span title="Shared" className="inline-flex items-center"><ToneDot tone="idle" /><span className="sr-only">Shared</span></span>
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
      <p className="mt-0.5 flex flex-wrap items-baseline gap-x-1.5 break-words text-[13px] text-muted">
        <span>{endpointWhat(e)}</span><span aria-hidden>·</span>
        <span className={cn('min-w-0 break-words', repos.length > 0 && 'text-ink')}>{repos.length ? repos.join(', ') : internal ? 'No repositories' : `No repositories — attach ${e.server} to one`}</span>
        {internal && !editing && <button type="button" onClick={() => setEditing(true)} aria-label={`Edit repositories for ${e.name}`} className={cn('text-[12.5px]', QUIET_LINK)}>Edit</button>}
      </p>
      {e.state === 'failed' && <p role="note" className="mt-1 break-words text-[12.5px] text-bad">{e.message ? `Sharing failed: ${e.message}` : 'Sharing failed. Stop sharing and share it again.'}</p>}
      {editing && <EditRepos e={e} onDone={() => setEditing(false)} />}
    </div>
    {used
      ? <span className="flex items-center gap-1.5"><span className="text-[12px] text-muted">In use</span><Button size="sm" variant="ghost" disabled>Stop sharing</Button></span>
      : <ConfirmDialog title={`Stop sharing ${e.name}?`} confirm="Stop sharing" description={undefined}
        note="Sandboxes at the provider lose access to it."
        trigger={<Button size="sm" variant="ghost" disabled={e.state === 'applying'}>Stop sharing</Button>}
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
      : names.length === 0 ? <p className="text-[13px] text-muted">Connect a repository first.</p>
      : <ul className="max-h-48 divide-y divide-line overflow-y-auto rounded-md border border-line">
        {names.map(repo => <li key={repo}>
          <label className="flex cursor-pointer items-center gap-2.5 px-3 py-2 text-[13px] hover:bg-hover">
            <input type="checkbox" className="size-4 shrink-0 accent-[var(--ink)]" checked={value.includes(repo)} onChange={e => toggle(repo, e.target.checked)} />
            <span className="min-w-0 break-all">{repo}</span>
          </label>
        </li>)}
      </ul>}
    <p id={`${id}-hint`} aria-live="polite" className="mt-1 text-[12px] text-bad empty:hidden">{error}</p>
  </fieldset>
}

/** An internal host's repositories, replaced as a whole; none takes the last one's access back. */
function EditRepos({ e, onDone }: { e: Endpoint; onDone: () => void }) {
  const qc = useQueryClient()
  const [repos, setRepos] = useState(e.repos ?? [])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const save = async () => {
    setBusy(true); setError(null)
    try {
      await api.endpoints.setRepos(e.name, repos)
      await qc.invalidateQueries({ queryKey: keys.endpoints })
      onDone()
    } catch (x) { setError(x) } finally { setBusy(false) }
  }
  return <div className="mt-2 space-y-2 rounded-lg border border-line p-3">
    <RepoPicker value={repos} onChange={setRepos} />
    <ErrorState compact error={error} />
    <div className="flex justify-end gap-2">
      <Button size="sm" variant="ghost" onClick={onDone} disabled={busy}>Cancel</Button>
      <Button size="sm" variant="primary" busy={busy} onClick={save}>Save</Button>
    </div>
  </div>
}

type Pending = { body: AddEndpoint; address: string; name: string; repos?: string[] }
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
    <h3 className="font-semibold">Share a server</h3>
    <Choices candidates={data.candidates} value={choice} onChange={setChoice} />
    {choice === INTERNAL
      ? <InternalHost domain={data.domain} taken={data.endpoints.map(e => e.name)} draft={draft} onCancel={onClose}
        onNext={d => { setDraft(d); setPending({ body: d, address: address(d.name), name: d.name, repos: d.repos }) }} />
      : <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="primary" disabled={!candidate} onClick={() => candidate && setPending({
          body: { server: candidate.server }, address: candidate.address || address(candidate.server), name: candidate.server,
        })}>Continue</Button>
      </div>}
  </div>
}

function Choices({ candidates, value, onChange }: { candidates: EndpointCandidate[]; value: string; onChange: (v: string) => void }) {
  const first = useRef<HTMLInputElement>(null)
  useEffect(() => { first.current?.focus() }, [])
  const options = [
    ...candidates.map(c => ({ value: c.server, title: c.server, note: kindTitle(c.kind) })),
    { value: INTERNAL, title: 'Internal host', note: 'A service on your network' },
  ]
  return <fieldset className="grid gap-2 sm:grid-cols-2">
    <legend className="sr-only">Server to share</legend>
    {options.map((o, i) => <label key={o.value} className={cn('flex cursor-pointer items-start gap-3 rounded-lg border bg-panel p-3 transition-[border-color,box-shadow] duration-150 ease-out',
      value === o.value ? 'border-accent ring-1 ring-accent' : 'border-line hover:border-line-strong')}>
      <input ref={i === 0 ? first : undefined} type="radio" name="endpoint-choice" value={o.value} checked={value === o.value} onChange={() => onChange(o.value)}
        className="mt-1 shrink-0 accent-[var(--accent)]" />
      <span className="min-w-0"><span className="block break-all font-medium">{o.title}</span><span className="block text-[12.5px] text-muted">{o.note}</span></span>
    </label>)}
  </fieldset>
}

const DNS_LABEL = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/
/** An IP address and a port, as the server takes them: 10.0.0.5:7233 or [fd00::5]:7233 (no host names). */
const HOST_PORT = /^(\[[0-9A-Fa-f:.]+\]|(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)):(\d{1,5})$/

export function endpointNameError(value: string, taken: string[] = []): string | undefined {
  if (!value) return 'Enter a name.'
  if (!DNS_LABEL.test(value)) return 'Use lowercase letters, digits and hyphens.'
  if (taken.includes(value)) return 'That name is taken.'
  return undefined
}

export function endpointTargetError(value: string): string | undefined {
  const m = HOST_PORT.exec(value.trim())
  const port = m ? Number(m[2]) : 0
  return m && port >= 1 && port <= 65535 ? undefined : 'Enter an IP address and port, such as 10.0.0.5:7233.'
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
          help={<span className="mono break-all">{field.state.value || 'name'}.{domain}</span>} />}
      </form.AppField>
      <form.AppField name="target" validators={{ onChange: ({ value }) => endpointTargetError(value) }}>
        {field => <field.TextField label="IP address and port" autoComplete="off" spellCheck={false} placeholder="10.0.0.5:7233" />}
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
    <h3 id="endpoint-confirm" className="font-semibold">Share {pending.name}?</h3>
    <p className="mono break-all rounded-lg border border-line bg-sunken px-3 py-2.5 text-[14px] font-medium text-ink" data-testid="endpoint-address">{pending.address}</p>
    <div className="space-y-1 text-[13px]">
      <p className="break-words text-ink">Reachable over the internet, only by sandboxes of {pending.repos ? listText(pending.repos) : `repositories ${pending.name} is attached to`}.</p>
    </div>
    <ErrorState compact error={error} />
    <div className="flex justify-end gap-2">
      <Button variant="ghost" onClick={onBack} disabled={busy}>Back</Button>
      <Button variant="primary" autoFocus busy={busy} onClick={submit}>Share</Button>
    </div>
  </section>
}
