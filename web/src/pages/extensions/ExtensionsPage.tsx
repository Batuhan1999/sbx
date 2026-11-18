import { useQueryClient } from '@tanstack/react-query'
import { ArrowUpRight, Bot, Github, MoreHorizontal, Package, Plus } from 'lucide-react'
import { Fragment, useEffect, useState, type ReactNode } from 'react'
import { Link, useLocation, useNavigate } from 'react-router'
import { toast } from 'sonner'
import { api } from '@/api/endpoints'
import { keys, useAction, useCatalog, useCatalogProblems, useConnections, useMe, useRepos, useSharedConnections } from '@/api/queries'
import type { CatalogPackage, Check, Connection, SharedConnection } from '@/api/types'
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
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { ago, plural } from '@/lib/format'
import { repositories, sandboxes, suggestKey } from '@/lib/packages'
import { keyError, required } from '@/lib/validation'
import { Checks, ConnectionForm, formFields } from './ConnectionCard'

/** What the page lists: the connected things first (agents, GitHub, catalog connections), then what can be added. */
export default function ExtensionsPage() {
  const me = useMe()
  const admin = !!me.data?.admin
  if (me.data && !admin) return <><PageHeader title="Extensions" /><Available /></>
  return <><PageHeader title="Extensions" /><AdminExtensions /></>
}

function Group({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return <section aria-labelledby={`${id}-h`} className="space-y-3">
    <h2 id={`${id}-h`} className="text-[15px] font-semibold">{title}</h2>
    {children}
  </section>
}

/** A connection or connector that people added: connected, needing attention, or chosen (in every template) and
 * waiting on its key. */
const isAdded = (c: Connection) => c.state === 'connected' || c.state === 'attention' || (c.state === 'not_connected' && c.facts.chosen === true)

function AdminExtensions() {
  const connections = useConnections(true)
  const shared = useSharedConnections()
  const catalog = useCatalog()
  const problems = useCatalogProblems(true)
  const navigate = useNavigate()
  const location = useLocation()
  // A repository's Packages tab sends people here to connect what a package needs.
  const asked = (location.state as { connect?: string } | null)?.connect ?? null
  const [adding, setAdding] = useState<string | null>(null)
  useEffect(() => {
    if (!asked) return
    setAdding(`package:${asked}`)
    navigate(location.pathname + location.hash, { replace: true, state: null })
  }, [asked, navigate, location.pathname, location.hash])
  const { hash } = location
  useEffect(() => {
    if (hash && connections.data) document.getElementById(hash.slice(1))?.scrollIntoView({ block: 'start' })
  }, [hash, connections.data])

  const list = (connections.data ?? []).filter(c => c.group === 'agent' || c.kind === 'github')
  const added = list.filter(isAdded)
  const addable = list.filter(c => !isAdded(c) && (formFields(c).length > 0 || c.kind === 'github') && c.actions.includes('connect'))
  const accounts = (catalog.data ?? []).filter(p => p.kind === 'connection' && Object.keys(p.inputs).length > 0)
  const connectedPackages = new Set((shared.data ?? []).map(s => s.package))
  const newAccounts = accounts.filter(p => !connectedPackages.has(p.name))
  const loading = connections.isPending || shared.isPending
  const chosen = adding?.startsWith('package:') ? accounts.find(p => p.name === adding.slice(8)) : undefined
  const chosenConnection = adding && !adding.startsWith('package:') ? list.find(c => c.kind === adding) : undefined
  return <div className="max-w-4xl space-y-8">
    {connections.error && <ErrorState error={connections.error} retry={() => connections.refetch()} />}
    {shared.error && <ErrorState error={shared.error} retry={() => shared.refetch()} />}
    {!!problems.data?.length && <div role="alert" className="rounded-lg border border-warn/30 bg-warn-soft px-4 py-3 text-[13px]">
      <p className="font-medium">{plural(problems.data.length, 'package')} in this server’s catalog could not be read</p>
      <ul className="mt-1 space-y-0.5 font-mono text-[12px] text-muted">{problems.data.map(p => <li key={p}>{p}</li>)}</ul>
    </div>}
    <Group id="connected" title="Connected">
      <div className="overflow-hidden rounded-lg border border-line bg-panel">
        {loading ? <SkeletonRows rows={2} />
          : added.length === 0 && !shared.data?.length ? <EmptyState title="Nothing connected yet" className="py-8" />
          : <ul className="divide-y divide-line">
            {added.map(c => <ConnectorRow key={c.kind} c={c} />)}
            {(shared.data ?? []).map(s => <SharedRow key={s.key} s={s} catalog={catalog.data ?? []}
              onAnother={() => setAdding(`package:${s.package}`)} />)}
          </ul>}
      </div>
    </Group>
    <Group id="add" title="Add">
      {loading || catalog.isPending ? <div className="rounded-lg border border-line bg-panel"><SkeletonRows rows={2} /></div>
        : catalog.error ? <ErrorState error={catalog.error} retry={() => catalog.refetch()} />
        : addable.length + newAccounts.length === 0 ? <p className="text-[13px] text-muted">Everything available is connected.</p>
        : <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {addable.map(c => <Tile key={c.kind} id={c.kind} icon={<ConnectorIcon kind={c.kind} />} title={c.title} line={PURPOSE[c.kind] ?? (c.group === 'agent' ? 'Coding agent' : undefined)}
            onAdd={() => setAdding(c.kind)} />)}
          {newAccounts.map(p => <Tile key={p.name} icon={<PackageLogo logo={p.logo} large />} title={p.title}
            onAdd={() => setAdding(`package:${p.name}`)} />)}
        </ul>}
    </Group>
    <Dialog open={!!chosenConnection} onOpenChange={o => !o && setAdding(null)} title={chosenConnection?.title ?? ''}>
      {chosenConnection && (chosenConnection.kind === 'github'
        ? <GitHubAdd onDone={() => setAdding(null)} />
        : <ConnectionForm c={chosenConnection} onDone={() => setAdding(null)} />)}
    </Dialog>
    <Dialog open={!!chosen} onOpenChange={o => !o && setAdding(null)} title={chosen ? `Connect ${chosen.title}` : ''} wide>
      {chosen && <PackageConnectionForm pkg={chosen} onDone={() => setAdding(null)} />}
    </Dialog>
  </div>
}

/** What a connector is for, on its Add tile (its summary says its state, which a tile does not need). */
const PURPOSE: Record<string, string> = { github: 'Private repositories and PR previews' }

function ConnectorIcon({ kind }: { kind: string }) {
  return <span className="grid size-8 shrink-0 place-items-center rounded-md border border-line bg-sunken text-muted">
    {kind === 'github' ? <Github className="size-4" /> : <Bot className="size-4" />}
  </span>
}

/** Something that can be added: its icon, name, one line on what it is, and Add. */
function Tile({ id, icon, title, line, onAdd }: { id?: string; icon: ReactNode; title: string; line?: string; onAdd?: () => void }) {
  return <li id={id} className="flex min-w-0 scroll-mt-16 items-center gap-3 rounded-lg border border-line bg-panel p-3">
    {icon}
    <div className="min-w-0 grow">
      <p className="font-medium leading-snug">{title}</p>
      {line && <p className="line-clamp-2 text-[12.5px] text-muted">{line}</p>}
    </div>
    {onAdd && <Button size="sm" onClick={onAdd} aria-label={`Add ${title}`}><Plus />Add</Button>}
  </li>
}

/** A row's overflow menu. */
function RowMenu({ label, children }: { label: string; children: ReactNode }) {
  // Not modal: an item opens a dialog, and two modal layers can leave the page unclickable.
  return <DropdownMenu modal={false}>
    <DropdownMenuTrigger asChild>
      <Button size="icon-sm" variant="ghost" aria-label={label} className="data-[state=open]:bg-hover"><MoreHorizontal /></Button>
    </DropdownMenuTrigger>
    <DropdownMenuContent align="end">{children}</DropdownMenuContent>
  </DropdownMenu>
}

function Row({ id, icon, title, aside, badge, line, actions, children }: {
  id?: string; icon: ReactNode; title: ReactNode; aside?: ReactNode; badge?: ReactNode; line?: ReactNode; actions?: ReactNode; children?: ReactNode
}) {
  return <li id={id} className="scroll-mt-16 px-4 py-3">
    <div className="flex items-center gap-3">
      {icon}
      <div className="min-w-0 grow">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1"><span className="font-medium">{title}</span>{aside}{badge}</div>
        {line && <p className="mt-0.5 break-words text-[12.5px] text-muted">{line}</p>}
      </div>
      <div className="flex shrink-0 items-center gap-1.5">{actions}</div>
    </div>
    {children}
  </li>
}

/** An agent connector or GitHub: a badge only when it needs something, Test, Edit and Disconnect in its menu. */
function ConnectorRow({ c }: { c: Connection }) {
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [disconnecting, setDisconnecting] = useState(false)
  const [checks, setChecks] = useState<Check[] | null>(null)
  const test = useAction(async () => setChecks((await api.connections.test(c.kind)).checks), keys.connections)
  const waiting = c.state === 'not_connected'
  const editable = formFields(c).length > 0 && c.actions.includes('connect')
  const installUrl = typeof c.facts.install_url === 'string' ? c.facts.install_url : ''
  const badge = waiting ? <StateBadge tone="warn" label="Needs its key" /> : c.state === 'attention' ? <StateBadge tone="warn" label="Needs attention" />
    : checks?.some(x => !x.ok) ? <StateBadge tone="bad" label="Test failed" /> : null
  const hasMenu = (c.actions.includes('test') && !waiting) || (editable && !waiting) || !!installUrl || c.actions.includes('disconnect')
  return <Row id={c.kind} icon={<ConnectorIcon kind={c.kind} />} title={c.title} badge={badge}
    line={c.summary && !waiting ? c.summary : undefined}
    actions={<>
      {waiting && editable && <Button size="sm" variant="primary" onClick={() => setEditing(true)}>Add key</Button>}
      {test.isPending && <span className="text-[12px] text-muted">Testing…</span>}
      {hasMenu && <RowMenu label={`Actions for ${c.title}`}>
        {c.actions.includes('test') && !waiting && <DropdownMenuItem onSelect={() => test.mutate()}>Test</DropdownMenuItem>}
        {editable && !waiting && <DropdownMenuItem onSelect={() => setEditing(true)}>Edit</DropdownMenuItem>}
        {installUrl && <DropdownMenuItem onSelect={() => window.open(installUrl, '_blank', 'noreferrer')}>Manage on GitHub<ArrowUpRight /></DropdownMenuItem>}
        {c.actions.includes('disconnect') && <><DropdownMenuSeparator />
          <DropdownMenuItem variant="destructive" onSelect={() => setDisconnecting(true)}>Disconnect</DropdownMenuItem></>}
      </RowMenu>}
    </>}>
    {(checks || test.error) && <div className="mt-2 space-y-2 pl-11">
      {checks && <Checks checks={checks} failedFirst />}
      <ErrorState error={test.error} compact />
    </div>}
    {editable && <Dialog open={editing} onOpenChange={setEditing} title={c.title} wide={formFields(c).filter(f => !f.advanced).length > 2}>
      <ConnectionForm c={c} onDone={() => setEditing(false)} />
    </Dialog>}
    {disconnecting && <ConfirmDialog open onOpenChange={setDisconnecting} title={`Disconnect ${c.title}?`}
      description="Its saved credentials are removed from this server." confirm="Disconnect"
      onConfirm={async () => {
        const result = await api.connections.disconnect(c.kind)
        if (result.rebuilds && Object.keys(result.rebuilds.runtimes).length) toast(`${c.title} disconnected`, { description: result.rebuilds.message })
        await qc.invalidateQueries({ queryKey: keys.connections })
      }} />}
  </Row>
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
  const uses = catalog.filter(p => p.kind === 'provision' && Object.values(p.requires).some(r => r.kind === s.package))
  return <Row icon={<PackageLogo logo={s.logo} large />} title={s.title}
    aside={<span className="text-[12.5px] text-muted">{s.key}</span>}
    badge={failed ? <StateBadge tone="bad" label="Can’t connect" /> : !s.known ? <StateBadge tone="warn" label="Not in the catalog" /> : null}
    line={<><UsedIn usedBy={s.used_by} />{s.added_at ? ` · added ${ago(s.added_at)}` : ''}</>}
    actions={<>
      {check.isPending && <span className="text-[12px] text-muted">Testing…</span>}
      {check.data?.ok && <span role="status" className="text-[12px] text-ok">Test passed</span>}
      <RowMenu label={`Actions for ${s.key}`}>
        <DropdownMenuItem onSelect={() => check.mutate()}>Test</DropdownMenuItem>
        {s.known && <DropdownMenuItem onSelect={() => setEditing(true)}>Edit</DropdownMenuItem>}
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
      description={repos.length ? `${repos.join(', ')} still ${repos.length === 1 ? 'uses' : 'use'} it; remove it from ${repos.length === 1 ? 'that repository' : 'those repositories'} first.` : 'Sandboxes can no longer get resources on it.'}
      confirm="Remove" onConfirm={async () => { await api.catalog.removeConnection(s.key); await qc.invalidateQueries({ queryKey: keys.sharedConnections }) }} />}
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

/** GitHub's Add: create the App on GitHub (its manifest fills every value; the organization optional). */
function GitHubAdd({ onDone }: { onDone: () => void }) {
  const start = useAction((owner: string) => api.connections.githubManifest(owner, '/app/extensions'))
  const form = useAppForm({
    defaultValues: { owner: '' },
    onSubmit: async ({ value }) => {
      const { action_url, manifest } = await start.mutateAsync(value.owner.trim())
      // GitHub's App manifest flow: the browser posts a form to GitHub, which shows the App for confirmation.
      const f = document.createElement('form')
      f.method = 'post'
      f.action = action_url
      const input = document.createElement('input')
      input.type = 'hidden'
      input.name = 'manifest'
      input.value = manifest
      f.append(input)
      document.body.append(f)
      f.submit()
    },
  })
  return <AppForm form={form} className="space-y-4">
    <form.AppField name="owner" validators={{ onChange: ({ value }) => value && !/^[A-Za-z0-9-]+$/.test(value) ? 'Use a GitHub organization name.' : undefined }}>
      {field => <field.TextField label="GitHub organization" optional placeholder="Your account" autoComplete="off" spellCheck={false} />}
    </form.AppField>
    <div className="flex justify-end gap-2">
      <Button variant="ghost" onClick={onDone}>Cancel</Button>
      <SubmitButton form={form}><Github />Create on GitHub</SubmitButton>
    </div>
  </AppForm>
}

/** Connect an account or server of a catalog connection package, or edit a saved one (its non-secret values filled in,
 * a secret left empty kept, its name fixed). The package's description sits here, not on the page. */
function PackageConnectionForm({ pkg, existing, onDone }: { pkg?: CatalogPackage; existing?: SharedConnection; onDone: () => void }) {
  const shared = useSharedConnections()
  const add = useAction((v: { key: string; inputs: Record<string, string> }) => api.catalog.connect({ package: pkg!.name, key: v.key, inputs: v.inputs }), keys.sharedConnections)
  if (!pkg) return <EmptyState title="This package isn’t in the catalog" />
  if (shared.isPending) return <SkeletonRows rows={2} />
  if (!shared.data) return <ErrorState error={shared.error} compact retry={() => shared.refetch()} />
  return <div className="space-y-4">
    {!existing && <PackageHeading logo={pkg.logo} title={pkg.title} description={pkg.description} docs={pkg.docs} />}
    <ConnectionFields inputs={Object.entries(pkg.inputs).filter(([, i]) => !i.hidden)} existing={existing} taken={shared.data.map(s => s.key)}
      defaultKey={existing?.key ?? suggestKey(pkg.name, shared.data.map(s => s.key))}
      onSubmit={async v => { await add.mutateAsync(v); onDone() }} onCancel={onDone} />
  </div>
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
