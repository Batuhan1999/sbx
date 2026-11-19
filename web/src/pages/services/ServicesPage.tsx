import { useQueryClient } from '@tanstack/react-query'
import { Package } from 'lucide-react'
import { Fragment, useEffect, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router'
import { api } from '@/api/endpoints'
import { keys, useAction, useCatalog, useCatalogProblems, useMe, useRepos, useServers, useSharedConnections } from '@/api/queries'
import type { CatalogPackage, ManagedServer, SharedConnection } from '@/api/types'
import { EntryForm } from '@/components/AttachForm'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { LinkedText } from '@/components/LinkedText'
import { PackageHeading, PackageInputControl, PackageLogo, packageInputError } from '@/components/PackageLogo'
import { AppForm, MoreOptions, SECRET_KEPT, SubmitButton, useAppForm, useStore } from '@/components/form'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState, ErrorState, QueryView, SkeletonRows } from '@/components/states'
import { StateBadge } from '@/components/StatusBadge'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { RadioCard, RadioGroup } from '@/components/ui/radio-group'
import { DropdownMenuItem, DropdownMenuSeparator } from '@/components/ui/dropdown-menu'
import { ago, plural } from '@/lib/format'
import { repositories, sandboxes, suggestKey } from '@/lib/packages'
import { keyError, required } from '@/lib/validation'
import { Group, Row, RowMenu, Tile } from '../extensions/parts'

/** Accounts and servers that sandboxes get their own database or key on: the connected ones, then what can be added. */
export default function ServicesPage() {
  const me = useMe()
  const admin = !!me.data?.admin
  return <><PageHeader title="Services" />{me.data && !admin ? <Available /> : <AdminServices />}</>
}

function AdminServices() {
  const shared = useSharedConnections()
  const servers = useServers()
  const catalog = useCatalog()
  const problems = useCatalogProblems(true)
  const navigate = useNavigate()
  const location = useLocation()
  // A repository's Packages tab sends people here to connect what a package needs.
  const asked = (location.state as { connect?: string } | null)?.connect ?? null
  const [adding, setAdding] = useState<string | null>(null)
  useEffect(() => {
    if (!asked) return
    setAdding(asked)
    navigate(location.pathname + location.hash, { replace: true, state: null })
  }, [asked, navigate, location.pathname, location.hash])
  const accounts = (catalog.data ?? []).filter(p => p.kind === 'connection' && Object.keys(p.inputs).length > 0)
  const connectedPackages = new Set((shared.data ?? []).map(s => s.package))
  const newAccounts = accounts.filter(p => !connectedPackages.has(p.name))
  const chosen = adding ? accounts.find(p => p.name === adding) : undefined
  // A server this host runs has its connection once it is ready: until then, and when it failed, it shows here.
  const connected = new Set((shared.data ?? []).map(s => s.key))
  const unconnected = (servers.data ?? []).filter(s => !connected.has(s.name))
  // A server that became ready has a connection now.
  const ready = (servers.data ?? []).filter(s => s.status === 'ready').map(s => s.name).join(' ')
  const refetchShared = shared.refetch
  useEffect(() => { if (ready) void refetchShared() }, [ready, refetchShared])
  return <div className="max-w-4xl space-y-8">
    {shared.error && <ErrorState error={shared.error} retry={() => shared.refetch()} />}
    {!!problems.data?.length && <div role="alert" className="rounded-lg border border-warn/30 bg-warn-soft px-4 py-3 text-[13px]">
      <p className="font-medium">{plural(problems.data.length, 'package')} in this server’s catalog could not be read</p>
      <ul className="mt-1 space-y-0.5 font-mono text-[12px] text-muted">{problems.data.map(p => <li key={p}>{p}</li>)}</ul>
    </div>}
    {(shared.isPending || !!shared.data?.length || unconnected.length > 0) && <Group id="connected" title="Connected">
      <div className="overflow-hidden rounded-lg border border-line bg-panel">
        {shared.isPending ? <SkeletonRows rows={2} />
          : <ul className="divide-y divide-line">
            {unconnected.map(s => <ServerRow key={s.name} s={s} pkg={(catalog.data ?? []).find(p => p.name === s.package)} />)}
            {(shared.data ?? []).map(s => <SharedRow key={s.key} s={s} catalog={catalog.data ?? []} onAnother={() => setAdding(s.package)} />)}
          </ul>}
      </div>
    </Group>}
    <Group id="add" title="Add">
      {catalog.isPending ? <div className="rounded-lg border border-line bg-panel"><SkeletonRows rows={2} /></div>
        : catalog.error ? <ErrorState error={catalog.error} retry={() => catalog.refetch()} />
        : newAccounts.length === 0 ? <p className="text-[13px] text-muted">Everything in the catalog is connected.</p>
        : <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {newAccounts.map(p => <Tile key={p.name} id={p.name} icon={<PackageLogo logo={p.logo} large />} title={p.title} onAdd={() => setAdding(p.name)} />)}
        </ul>}
    </Group>
    <Dialog open={!!chosen} onOpenChange={o => !o && setAdding(null)} title={chosen ? `Connect ${chosen.title}` : ''} wide>
      {chosen && <PackageConnectionForm pkg={chosen} onDone={() => setAdding(null)} />}
    </Dialog>
  </div>
}

/** Where a connection is used: repositories as links, sandboxes as a count. */
function UsedIn({ usedBy }: { usedBy: string[] }) {
  const repos = repositories(usedBy)
  const count = sandboxes(usedBy).length
  if (!repos.length && !count) return <>Not used yet</>
  return <>{repos.length > 0 && <>Used in {repos.map((r, i) => <Fragment key={r}>{i > 0 && ', '}<Link className="hover:text-ink hover:underline" to={`/app/repos/${r}/packages`}>{r}</Link></Fragment>)}</>}
    {count > 0 && `${repos.length ? ' · ' : 'Used by '}${plural(count, 'sandbox', 'sandboxes')}`}</>
}

/** A catalog connection (a Postgres server, an OpenRouter account): Test, Edit, Use in a repository, Add another
 * and Remove in its menu; "Can't connect" only when its check failed. */
function SharedRow({ s, catalog, onAnother }: { s: SharedConnection; catalog: CatalogPackage[]; onAnother: () => void }) {
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [using, setUsing] = useState(false)
  const [removing, setRemoving] = useState(false)
  const check = useAction(() => api.catalog.checkConnection(s.key), keys.sharedConnections)
  const failed = check.data ? !check.data.ok : s.check_status === 'failed'
  const repos = repositories(s.used_by)
  const remove = async () => {
    // A server this host runs goes with its connection: nothing of it stays behind, running unseen.
    await (s.managed ? api.servers.remove(s.key) : api.catalog.removeConnection(s.key))
    await Promise.all([keys.sharedConnections, keys.servers].map(queryKey => qc.invalidateQueries({ queryKey })))
  }
  const uses = catalog.filter(p => p.kind === 'provision' && Object.values(p.requires).some(r => r.kind === s.package))
  return <Row icon={<PackageLogo logo={s.logo} large />} title={s.title}
    aside={<span className="text-[12.5px] text-muted">{s.key}</span>}
    badge={failed ? <StateBadge tone="bad" label="Can’t connect" /> : !s.known ? <StateBadge tone="warn" label="Not in the catalog" /> : null}
    line={<>{s.managed ? 'Runs on this server · ' : ''}<UsedIn usedBy={s.used_by} />{s.added_at ? ` · added ${ago(s.added_at)}` : ''}</>}
    actions={<>
      {check.isPending && <span className="text-[12px] text-muted">Testing…</span>}
      {check.data?.ok && <span role="status" className="text-[12px] text-ok">Test passed</span>}
      <RowMenu label={`Actions for ${s.key}`}>
        <DropdownMenuItem onSelect={() => check.mutate()}>Test</DropdownMenuItem>
        {s.known && !s.managed && <DropdownMenuItem onSelect={() => setEditing(true)}>Edit</DropdownMenuItem>}
        {uses.length > 0 && <DropdownMenuItem onSelect={() => setUsing(true)}>Use in a repository</DropdownMenuItem>}
        {s.known && <DropdownMenuItem onSelect={onAnother}>Add another</DropdownMenuItem>}
        <DropdownMenuSeparator />
        <DropdownMenuItem variant="destructive" onSelect={() => setRemoving(true)}>Remove</DropdownMenuItem>
      </RowMenu>
    </>}>
    {check.data && !check.data.ok && <p className="mt-2 pl-11 text-[12.5px] text-bad"><LinkedText text={check.data.detail} /></p>}
    <ErrorState error={check.error} compact className="mt-2" />
    {s.known && <Dialog open={editing} onOpenChange={setEditing} title={`Edit ${s.key}`} wide>
      {editing && <PackageConnectionForm pkg={catalog.find(p => p.name === s.package)} existing={s} onDone={() => { setEditing(false); check.reset() }} />}
    </Dialog>}
    <Dialog open={using} onOpenChange={setUsing} title="Use in a repository" wide>
      {using && <UseInRepo packages={uses} onDone={() => setUsing(false)} />}
    </Dialog>
    {removing && <ConfirmDialog open onOpenChange={setRemoving} title={`Remove ${s.key}?`}
      description={repos.length ? `${repos.join(', ')} still ${repos.length === 1 ? 'uses' : 'use'} it; remove it from ${repos.length === 1 ? 'that repository' : 'those repositories'} first.`
        : s.managed ? 'This server runs it: removing it stops the server and deletes its data. This cannot be undone.'
        : 'Sandboxes can no longer get resources on it.'}
      confirm={s.managed ? 'Stop and delete' : 'Remove'} onConfirm={remove} />}
  </Row>
}

const SERVER_STATE: Record<string, { tone: 'bad' | 'warn' | 'progress' | 'idle'; label: string; pulse?: boolean }> = {
  preparing: { tone: 'progress', label: 'Starting', pulse: true },
  checking: { tone: 'progress', label: 'Starting', pulse: true },
  failed: { tone: 'bad', label: 'Failed to start' },
  stopped: { tone: 'warn', label: 'Stopped' },
}

/** A server this host runs that has no connection (yet): starting, failed or stopped. Start again, or remove it with its data. */
function ServerRow({ s, pkg }: { s: ManagedServer; pkg?: CatalogPackage }) {
  const [removing, setRemoving] = useState(false)
  const start = useAction(() => api.servers.start({ name: s.name, package: s.package }), keys.servers)
  const remove = useAction(() => api.servers.remove(s.name), keys.servers, keys.sharedConnections)
  const state = SERVER_STATE[s.status] ?? { tone: 'idle' as const, label: s.status }
  const starting = s.status === 'preparing' || s.status === 'checking'
  return <Row icon={<PackageLogo logo={pkg?.logo ?? ''} large />} title={pkg?.title ?? s.package}
    aside={<span className="text-[12.5px] text-muted">{s.name}</span>}
    badge={<StateBadge tone={state.tone} label={state.label} pulse={state.pulse} />}
    line={starting ? 'Runs on this server · pulling its images and starting can take a few minutes' : 'Runs on this server'}
    actions={<RowMenu label={`Actions for ${s.name}`}>
      {!starting && <DropdownMenuItem onSelect={() => start.mutate()}>Start again</DropdownMenuItem>}
      {!starting && <DropdownMenuSeparator />}
      <DropdownMenuItem variant="destructive" onSelect={() => setRemoving(true)}>Remove</DropdownMenuItem>
    </RowMenu>}>
    {s.error && <p className="mt-2 pl-11 text-[12.5px] text-bad"><LinkedText text={s.error} /></p>}
    <ErrorState error={start.error} compact className="mt-2" />
    {removing && <ConfirmDialog open onOpenChange={setRemoving} title={`Remove ${s.name}?`}
      description="Removing it stops the server and deletes its data. This cannot be undone."
      confirm="Stop and delete" onConfirm={async () => { await remove.mutateAsync() }} />}
  </Row>
}

/** A package that runs on a connection, added to a repository from here: which package (when there are several),
 * which repository, then its settings. A repository's Packages tab does the same. */
function UseInRepo({ packages, onDone }: { packages: CatalogPackage[]; onDone: () => void }) {
  const repos = useRepos()
  const [chosen, setChosen] = useState<{ name: string; repo: string } | null>(null)
  const form = useAppForm({
    defaultValues: { name: packages[0]?.name ?? '', repo: '' },
    onSubmit: ({ value }) => setChosen(value),
  })
  const name = useStore(form.store, s => s.values.name)
  const pkg = packages.find(p => p.name === name)
  if (chosen) return <EntryForm repo={chosen.repo} name={chosen.name} bare onDone={onDone} onBack={() => setChosen(null)} />
  return <QueryView query={repos}>{list => {
    const usable = list.filter(r => r.trust === 'trusted')
    if (!usable.length) return <EmptyState title="No trusted repository yet" />
    return <AppForm form={form} className="space-y-4">
      {packages.length > 1 && <form.AppField name="name">{field => <field.SelectField label="Package">
        {packages.map(p => <option key={p.name} value={p.name}>{p.title}</option>)}
      </field.SelectField>}</form.AppField>}
      <form.AppField name="repo" validators={{ onSubmit: ({ value }) => (value ? undefined : 'Choose a repository.') }}>{field => <field.SelectField label="Repository">
        <option value="">Choose…</option>
        {usable.map(r => <option key={r.repo} value={r.repo} disabled={!!pkg?.used_by.includes(r.repo)}>{r.repo}{pkg?.used_by.includes(r.repo) ? ' (has it)' : ''}</option>)}
      </field.SelectField>}</form.AppField>
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onDone}>Cancel</Button>
        <SubmitButton form={form}>Continue</SubmitButton>
      </div>
    </AppForm>
  }}</QueryView>
}

/** Connect an account or server of a catalog connection package, or edit a saved one (its non-secret values filled in,
 * a secret left empty kept, its name fixed). The package's description sits here, not on the page. */
function PackageConnectionForm({ pkg, existing, onDone }: { pkg?: CatalogPackage; existing?: SharedConnection; onDone: () => void }) {
  const shared = useSharedConnections()
  const add = useAction((v: { key: string; inputs: Record<string, string> }) => api.catalog.connect({ package: pkg!.name, key: v.key, inputs: v.inputs }), keys.sharedConnections)
  const servers = useServers()
  // A package whose server this host can run: here (the default) or one of the owner's.
  const [where, setWhere] = useState<'here' | 'own'>('here')
  if (!pkg) return <EmptyState title="This package isn’t in the catalog" />
  if (shared.isPending) return <SkeletonRows rows={2} />
  if (!shared.data) return <ErrorState error={shared.error} compact retry={() => shared.refetch()} />
  const taken = [...shared.data.map(s => s.key), ...(servers.data ?? []).map(s => s.name)]
  const offered = !existing && !!pkg.server
  return <div className="space-y-4">
    {!existing && <PackageHeading logo={pkg.logo} title={pkg.title} description={pkg.description} docs={pkg.docs} />}
    {offered && <RadioGroup aria-label="Where it runs" value={where} onValueChange={v => setWhere(v as 'here' | 'own')} className="sm:grid-cols-2">
      <RadioCard value="here" title="Run it on this server" description="sbx starts it, private to this server’s sandboxes." />
      <RadioCard value="own" title="Connect one you run" description="Give its address and credentials." />
    </RadioGroup>}
    {offered && where === 'here' ? <RunServerForm pkg={pkg} taken={taken} onDone={onDone} /> : <ConnectionFields inputs={Object.entries(pkg.inputs).filter(([, i]) => !i.hidden)} existing={existing} taken={shared.data.map(s => s.key)}
      defaultKey={existing?.key ?? suggestKey(pkg.name, shared.data.map(s => s.key))}
      onSubmit={async v => { await add.mutateAsync(v); onDone() }} onCancel={onDone} />}
  </div>
}

/** Run the package's server on this host: only its name to choose. It shows under Connected while it starts. */
function RunServerForm({ pkg, taken, onDone }: { pkg: CatalogPackage; taken: string[]; onDone: () => void }) {
  const start = useAction((name: string) => api.servers.start({ name, package: pkg.name }), keys.servers)
  const form = useAppForm({
    defaultValues: { name: suggestKey(pkg.name, taken) },
    onSubmit: async ({ value }) => { await start.mutateAsync(value.name); onDone() },
  })
  const submitting = useStore(form.store, s => s.isSubmitting)
  return <AppForm form={form} className="space-y-4">
    <form.AppField name="name" validators={{ onChange: ({ value }) => keyError(value) ?? (taken.includes(value) ? `${value} exists already; choose another name.` : undefined) }}>
      {field => <field.TextField label="Name" help="The server’s name, and its connection’s." autoComplete="off" spellCheck={false} />}
    </form.AppField>
    <ErrorState error={start.error} compact />
    <div className="flex justify-end gap-2">
      <Button variant="ghost" disabled={submitting} onClick={onDone}>Cancel</Button>
      <SubmitButton form={form}>Run</SubmitButton>
    </div>
  </AppForm>
}

function ConnectionFields({ inputs, existing, taken, defaultKey, onSubmit, onCancel }: {
  inputs: [string, CatalogPackage['inputs'][string]][]; existing?: SharedConnection; taken: string[]; defaultKey: string
  onSubmit: (v: { key: string; inputs: Record<string, string> }) => Promise<void>; onCancel: () => void
}) {
  const saved = (n: string) => existing?.inputs[n] === '(set)'
  const form = useAppForm({
    defaultValues: {
      key: defaultKey,
      inputs: Object.fromEntries(inputs.map(([n, i]) => [n, i.secret || saved(n) ? '' : existing?.inputs[n] ?? i.default ?? ''])) as Record<string, string>,
    },
    onSubmit: ({ value }) => onSubmit({ key: value.key, inputs: Object.fromEntries(Object.entries(value.inputs).filter(([n, v]) => !(v === '' && saved(n)))) }),
  })
  const submitting = useStore(form.store, s => s.isSubmitting)
  const render = ([n, i]: [string, CatalogPackage['inputs'][string]]) => <form.AppField key={n} name={`inputs.${n}`}
    validators={{ onChange: ({ value }) => (i.required && !i.default && !saved(n) ? required(value ?? '') : undefined) ?? packageInputError(i, value) }}>
    {field => <PackageInputControl field={field} name={n} input={i} placeholder={saved(n) ? SECRET_KEPT : undefined} />}
  </form.AppField>
  const advanced = inputs.filter(([, i]) => i.advanced)
  return <AppForm form={form} className="space-y-4">
    {inputs.filter(([, i]) => !i.advanced).map(render)}
    {(advanced.length > 0 || !existing) && <MoreOptions open={advanced.some(([n, i]) => !i.secret && !!existing?.inputs[n] && existing.inputs[n] !== i.default)}>
      {advanced.map(render)}
      {!existing && <form.AppField name="key" validators={{ onChange: ({ value }) => keyError(value) ?? (taken.includes(value) ? `A connection named ${value} exists already; edit it instead.` : undefined) }}>
        {field => <field.TextField label="Name" autoComplete="off" spellCheck={false} />}
      </form.AppField>}
    </MoreOptions>}
    <div className="flex justify-end gap-2">
      <Button variant="ghost" disabled={submitting} onClick={onCancel}>Cancel</Button>
      <SubmitButton form={form}>{existing ? 'Save' : 'Connect'}</SubmitButton>
    </div>
  </AppForm>
}

/** For people who are not admins: what the catalog offers, read-only. */
function Available() {
  const catalog = useCatalog()
  return <Group id="available" title="Packages">
    <QueryView query={catalog}>{list => list.length === 0 ? <EmptyState icon={Package} title="The catalog is empty" />
      : <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {list.filter(p => p.kind === 'provision').map(p => <Tile key={p.name} icon={<PackageLogo logo={p.logo} large />} title={p.title} />)}
      </ul>}</QueryView>
  </Group>
}
