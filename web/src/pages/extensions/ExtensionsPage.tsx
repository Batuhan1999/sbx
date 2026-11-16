import { useQueryClient } from '@tanstack/react-query'
import { ArrowUpRight, Bot, Check, CircleCheck, FolderGit2, Github, Network, Plus, Settings2, ShieldCheck, Trash2 } from 'lucide-react'
import { Fragment, useEffect, useState, type ReactNode } from 'react'
import { Link, useLocation, useNavigate } from 'react-router'
import { api } from '@/api/endpoints'
import { keys, useAction, useCatalog, useCatalogProblems, useConnections, useMe, useRepos, useSharedConnections } from '@/api/queries'
import type { CatalogPackage, SharedConnection } from '@/api/types'
import { EntryForm } from '@/components/AttachForm'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { DocsLink, PackageHeading, PackageInputControl, PackageLogo, PackagePicker } from '@/components/PackageLogo'
import { AppForm, SubmitButton, useAppForm, useStore } from '@/components/form'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState, ErrorState, QueryView, SkeletonRows } from '@/components/states'
import { StateBadge } from '@/components/StatusBadge'
import { Button, LinkButton } from '@/components/ui/button'
import { Dialog, SidePanel } from '@/components/ui/dialog'
import { Badge } from '@/components/ui/primitives'
import { ago, plural } from '@/lib/format'
import { cn } from '@/lib/utils'
import { providers, repositories, sandboxes, sentences, suggestKey, type Provider } from '@/lib/packages'
import { keyError, required } from '@/lib/validation'
import { ConnectionCard } from './ConnectionCard'
import { LinkedText } from '@/components/LinkedText'

function Section({ id, title, description, actions, children }: { id: string; title: string; description: string; actions?: React.ReactNode; children: React.ReactNode }) {
  return <section id={id} aria-labelledby={`${id}-h`} className="scroll-mt-16">
    <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
      <div><h2 id={`${id}-h`} className="text-[15px] font-semibold">{title}</h2><p className="text-[13px] text-muted">{description}</p></div>
      {actions}
    </div>
    {children}
  </section>
}

export default function ExtensionsPage() {
  const me = useMe()
  const admin = !!me.data?.admin
  const connections = useConnections(admin)
  const { hash } = useLocation()
  useEffect(() => {
    if (hash && connections.data) document.getElementById(hash.slice(1))?.scrollIntoView({ block: 'start' })
  }, [hash, connections.data])
  if (me.data && !admin) return <><PageHeader title="Extensions" /><Catalog admin={false} /></>
  const list = connections.data ?? []
  const agents = list.filter(c => c.group === 'agent')
  const github = list.find(c => c.kind === 'github')
  return <>
    <PageHeader title="Extensions" description="What plugs into your sandboxes: coding agents that start them, GitHub, and catalog packages for databases and credentials." />
    <div className="space-y-8">
      {connections.error && <ErrorState error={connections.error} retry={() => connections.refetch()} />}
      <Section id="agents" title="Coding agents" description="Agent connectors run sessions in sandboxes with your repository's recipe.">
        {connections.isPending ? <div className="rounded-lg border border-line bg-panel"><SkeletonRows rows={2} /></div>
          : agents.length === 0 ? <EmptyState title="No agent connectors installed" />
          : <div className="grid gap-3 xl:grid-cols-2">{agents.map(c => <ConnectionCard key={c.kind} c={c} icon={<Bot className="size-4" />} />)}</div>}
      </Section>
      <Section id="github" title="GitHub" description="Clones and pushes without keys, pull-request previews, and the gh CLI inside sandboxes.">
        {github ? <ConnectionCard c={github} icon={<Github className="size-4" />}>
          {github.state !== 'connected' && <GitHubAppCreate />}
        </ConnectionCard> : connections.isPending ? <div className="rounded-lg border border-line bg-panel"><SkeletonRows rows={1} /></div> : null}
      </Section>
      <SharedConnections />
      <Catalog admin />
    </div>
  </>
}

/** GitHub's App manifest flow: the browser posts a form to GitHub, which shows the App for confirmation. */
function GitHubAppCreate() {
  const start = useAction((owner: string) => api.connections.githubManifest(owner, '/app/extensions'))
  const form = useAppForm({
    defaultValues: { owner: '' },
    onSubmit: async ({ value }) => {
      const { action_url, manifest } = await start.mutateAsync(value.owner.trim())
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
  return <AppForm form={form} className="flex flex-col gap-2 sm:flex-row sm:items-end">
    <div className="grow"><form.AppField name="owner" validators={{ onChange: ({ value }) => value && !/^[A-Za-z0-9-]+$/.test(value) ? 'Use a GitHub organization name.' : undefined }}>
      {field => <field.TextField label="Create a GitHub App for this server" optional placeholder="organization (empty: your account)" autoComplete="off" spellCheck={false} />}
    </form.AppField></div>
    <SubmitButton form={form}><Github />Create on GitHub</SubmitButton>
  </AppForm>
}

/** A connection's check, in words: "passed" and "ok" read as connected. */
function connectionState(status: string | null | undefined) {
  if (status === 'passed' || status === 'ok') return { tone: 'ok' as const, label: 'Connected' }
  if (status === 'failed') return { tone: 'bad' as const, label: 'Can’t connect' }
  return status ? { tone: 'idle' as const, label: status } : null
}

/** Where a connection is used: repositories as links, sandboxes as a count. */
function UsedIn({ usedBy }: { usedBy: string[] }) {
  const repos = repositories(usedBy)
  const count = sandboxes(usedBy).length
  if (!repos.length && !count) return <>Not used yet</>
  return <>{repos.length > 0 && <>Used in {repos.map((r, i) => <Fragment key={r}>{i > 0 && ', '}<Link className="hover:text-ink hover:underline" to={`/app/repos/${r}/packages`}>{r}</Link></Fragment>)}</>}
    {count > 0 && `${repos.length ? ' · ' : 'Used by '}${plural(count, 'sandbox', 'sandboxes')}`}</>
}

function SharedConnections() {
  const shared = useSharedConnections()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const location = useLocation()
  // A repository's Packages tab sends people here to connect what a package needs.
  const asked = (location.state as { connect?: string } | null)?.connect ?? null
  const [adding, setAdding] = useState<string | boolean>(asked ?? false)
  useEffect(() => {
    if (asked) navigate(location.pathname + location.hash, { replace: true, state: null })
  }, [asked, navigate, location.pathname, location.hash])
  return <Section id="connections" title="Connections" description="The accounts and servers sandboxes get their own resources on: an OpenRouter account, a Neon project, a Postgres server."
    actions={<Button size="sm" variant="primary" onClick={() => setAdding(true)}><Plus />Add connection</Button>}>
    <div className="overflow-hidden rounded-lg border border-line bg-panel">
      {shared.error ? <div className="p-4"><ErrorState error={shared.error} compact retry={() => shared.refetch()} /></div>
        : shared.isPending ? <SkeletonRows rows={2} />
        : shared.data.length === 0 ? <EmptyState icon={Network} title="No connections yet" className="py-8">Connect an account or server from the catalog below, and each sandbox gets its own database or key on it.</EmptyState>
        : <ul className="divide-y divide-line">{shared.data.map(s => <ConnectionRow key={s.key} s={s} onRemoved={() => qc.invalidateQueries({ queryKey: keys.sharedConnections })} />)}</ul>}
    </div>
    <Dialog open={adding !== false} onOpenChange={o => !o && setAdding(false)} title="Add a connection" description="Checked before it’s saved. Keys and passwords stay on this server." wide>
      <AddConnection initial={typeof adding === 'string' ? adding : null} onDone={() => setAdding(false)} />
    </Dialog>
  </Section>
}

function ConnectionRow({ s, onRemoved }: { s: SharedConnection; onRemoved: () => void }) {
  const [editing, setEditing] = useState(false)
  const check = useAction(() => api.catalog.checkConnection(s.key), keys.sharedConnections)
  const state = connectionState(check.data ? (check.data.ok ? 'passed' : 'failed') : s.check_status)
  const repos = repositories(s.used_by)
  return <li className="px-4 py-3">
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
      <PackageLogo logo={s.logo} large />
      <div className="min-w-0 grow basis-56">
        <div className="flex flex-wrap items-baseline gap-x-2"><span className="font-medium">{s.title}</span><span className="text-[12.5px] text-muted">{s.key}</span>{!s.known && <Badge tone="warn">not in the catalog</Badge>}</div>
        <p className="mt-0.5 text-[12px] text-muted"><UsedIn usedBy={s.used_by} />{s.added_at ? ` · added ${ago(s.added_at)}` : ''}</p>
      </div>
      {state && <StateBadge tone={state.tone} label={state.label} />}
      <Button size="sm" busy={check.isPending} onClick={() => check.mutate()}><ShieldCheck />Test</Button>
      {s.known && <Button size="sm" onClick={() => setEditing(true)}><Settings2 />Edit</Button>}
      <ConfirmDialog trigger={<Button size="icon-sm" variant="ghost" aria-label={`Remove ${s.key}`}><Trash2 /></Button>}
        title={`Remove ${s.key}?`} description={repos.length ? `${repos.join(', ')} still ${repos.length === 1 ? 'uses' : 'use'} it; remove it from ${repos.length === 1 ? 'that repository' : 'those repositories'} first.` : 'Sandboxes can no longer get resources on it.'}
        confirm="Remove" onConfirm={async () => { await api.catalog.removeConnection(s.key); onRemoved() }} />
    </div>
    {check.data && !check.data.ok && <p className="mt-2 text-[12.5px] text-bad"><LinkedText text={check.data.detail} /></p>}
    <ErrorState error={check.error} compact className="mt-2" />
    {s.known && <Dialog open={editing} onOpenChange={setEditing} title={`Edit ${s.key}`} description="Checked before it’s saved. Leave a secret empty to keep it." wide>
      {editing && <ConnectionForm name={s.package} existing={s} onDone={() => setEditing(false)} />}
    </Dialog>}
  </li>
}

function AddConnection({ initial, onDone }: { initial: string | null; onDone: () => void }) {
  const catalog = useCatalog()
  const [name, setName] = useState<string | null>(initial)
  if (name) return <ConnectionForm key={name} name={name} onDone={onDone} onBack={() => setName(null)} />
  return <QueryView query={catalog}>{packages => {
    const kinds = packages.filter(p => p.kind === 'connection' && Object.keys(p.inputs).length > 0)
    return kinds.length === 0 ? <EmptyState title="The catalog has nothing to connect" /> : <PackagePicker packages={kinds} onChoose={setName} />
  }}</QueryView>
}

/** Connect an account or server of one connection package: in a dialog under the package's heading, or
 * inline (just the fields and Connect) inside its provider sheet. */
function ConnectionForm({ name, existing, onDone, onBack, onCancel, inline }: {
  name: string; existing?: SharedConnection; onDone: () => void; onBack?: () => void; onCancel?: () => void; inline?: boolean
}) {
  // The catalog is loaded already: the form shows at once instead of after its own request.
  const catalog = useCatalog()
  const shared = useSharedConnections()
  const add = useAction((v: { key: string; inputs: Record<string, string> }) => api.catalog.connect({ package: name, key: v.key, inputs: v.inputs }), keys.sharedConnections)
  if (shared.isPending) return <SkeletonRows rows={2} />
  return <QueryView query={catalog}>{list => {
    const e = list.find(p => p.name === name)
    if (!e) return <EmptyState title="This package isn’t in the catalog" />
    return <div className="space-y-4">
      {!inline && <PackageHeading logo={e.logo} title={e.title} description={e.description} docs={e.docs} />}
      <ConnectionFields inputs={Object.entries(e.inputs)} existing={existing} defaultKey={existing?.key ?? suggestKey(name, (shared.data ?? []).map(s => s.key))}
        onSubmit={async v => { await add.mutateAsync(v); onDone() }} onCancel={inline ? onCancel : onCancel ?? onDone} onBack={onBack} />
    </div>
  }}</QueryView>
}

/** existing: edit that connection; its saved values fill the form, a secret left empty keeps its saved value,
 * and its name stays (the server merges what is sent over what it has). */
function ConnectionFields({ inputs, existing, defaultKey, onSubmit, onCancel, onBack }: {
  inputs: [string, CatalogPackage['inputs'][string]][]; existing?: SharedConnection; defaultKey: string
  onSubmit: (v: { key: string; inputs: Record<string, string> }) => Promise<void>; onCancel?: () => void; onBack?: () => void
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
  return <AppForm form={form} className="space-y-4">
    {inputs.map(([n, i]) => <form.AppField key={n} name={`inputs.${n}`} validators={{ onChange: ({ value }) => i.required && !i.default && !saved(n) ? required(value ?? '') : undefined }}>
      {field => <PackageInputControl field={field} name={n} input={i} placeholder={saved(n) ? 'Saved; leave empty to keep it' : undefined} />}
    </form.AppField>)}
    {!existing && <form.AppField name="key" validators={{ onChange: ({ value }) => keyError(value) }}>
      {field => <field.TextField label="Name" autoComplete="off" spellCheck={false} help="Repositories pick the connection by this name." />}
    </form.AppField>}
    <div className="flex items-center justify-between gap-2">
      {onBack ? <Button variant="ghost" disabled={submitting} onClick={onBack}>Back</Button> : <span />}
      <div className="flex gap-2">{onCancel && <Button variant="ghost" onClick={onCancel}>Cancel</Button>}<SubmitButton form={form}>{existing ? 'Save' : 'Connect'}</SubmitButton></div>
    </div>
  </AppForm>
}

function Catalog({ admin }: { admin: boolean }) {
  const catalog = useCatalog()
  const problems = useCatalogProblems(admin)
  const shared = useSharedConnections(admin)
  const [open, setOpen] = useState<string | null>(null)
  const list = catalog.data ? providers(catalog.data) : []
  const chosen = list.find(p => p.id === open)
  const connectionsOf = (p: Provider) => (shared.data ?? []).filter(s => s.package === p.account?.name)
  return <Section id="packages" title="Package catalog" description="What sandboxes can get: databases, API keys and services. Connect an account once, then add its package to a repository, and each sandbox gets its own.">
    {admin && !!problems.data?.length && <div role="alert" className="mb-3 rounded-lg border border-warn/30 bg-warn-soft px-4 py-3 text-[13px]">
      <p className="font-medium">{plural(problems.data.length, 'package')} in this server’s catalog could not be read</p>
      <ul className="mt-1 space-y-0.5 font-mono text-[12px] text-muted">{problems.data.map(p => <li key={p}>{p}</li>)}</ul>
    </div>}
    {catalog.error ? <ErrorState error={catalog.error} retry={() => catalog.refetch()} />
      : catalog.isPending ? <div className="rounded-lg border border-line bg-panel"><SkeletonRows rows={3} /></div>
      : <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {list.map(p => <ProviderCard key={p.id} p={p} connected={connectionsOf(p)} admin={admin} onOpen={() => setOpen(p.id)} />)}
      </div>}
    {chosen && <ProviderPanel key={chosen.id} p={chosen} admin={admin} connections={connectionsOf(chosen)} onClose={() => setOpen(null)} />}
  </Section>
}

function ProviderCard({ p, connected, admin, onOpen }: { p: Provider; connected: SharedConnection[]; admin: boolean; onOpen: () => void }) {
  const repos = repositories(p.resources.flatMap(r => r.used_by))
  const connectable = !!p.account && Object.keys(p.account.inputs).length > 0
  return <button type="button" onClick={onOpen} className="flex flex-col items-start rounded-lg border border-line bg-panel p-4 text-left transition-colors hover:border-line-strong hover:bg-hover/40">
    <span className="flex items-center gap-3"><PackageLogo logo={p.logo} large /><span className="font-medium">{p.title}</span></span>
    <span className="mt-2 line-clamp-2 text-[12.5px] text-muted">{p.tagline}</span>
    <span className="mt-auto flex flex-wrap items-center gap-2 pt-3 text-[12px] text-muted">
      {connected.length > 0 ? <StateBadge tone="ok" label={connected.length > 1 ? `${connected.length} connected` : 'Connected'} />
        : admin && connectable ? <span>Not connected</span> : null}
      {repos.length > 0 && <span>{`In ${plural(repos.length, 'repository', 'repositories')}`}</span>}
    </span>
  </button>
}

type StepId = 'connect' | 'repo' | 'settings' | 'done'

/** A provider in a side panel, as steps: connect its account, choose a repository, set the package up there. */
function ProviderPanel({ p, admin, connections, onClose }: { p: Provider; admin: boolean; connections: SharedConnection[]; onClose: () => void }) {
  const account = p.account
  const connectable = admin && !!account && Object.keys(account.inputs).length > 0
  const steps: StepId[] = admin ? [...(connectable ? ['connect' as const] : []), ...(p.resources.length ? ['repo' as const, 'settings' as const] : [])] : []
  const [step, setStep] = useState<StepId>(steps[0] ?? 'done')
  const [another, setAnother] = useState(false)
  const [editing, setEditing] = useState<SharedConnection | null>(null)
  const reopened = another || !!editing
  const [pkg, setPkg] = useState<CatalogPackage | undefined>(p.resources[0])
  const [repo, setRepo] = useState<string | null>(null)
  // The connections may arrive after the panel opened: an account that is connected already skips its step.
  useEffect(() => { if (step === 'connect' && connections.length > 0 && !reopened) setStep('repo') }, [step, connections.length, reopened])
  const at = steps.indexOf(step === 'done' ? 'settings' : step)
  const stateOf = (id: StepId) => step === 'done' || steps.indexOf(id) < at ? 'done' : id === step ? 'current' : 'later'
  const [unchosen, setUnchosen] = useState(false)
  const footer = step === 'repo' ? <div className="flex items-center gap-3">
    {unchosen && !repo && <p role="alert" className="text-[12.5px] text-bad">Choose a repository first.</p>}
    <Button variant="primary" onClick={() => repo && pkg ? setStep('settings') : setUnchosen(true)}>Continue</Button>
  </div> : undefined
  const status = connections.length > 0 ? <StateBadge tone="ok" label={connections.length > 1 ? `${connections.length} connected` : 'Connected'} />
    : connectable ? <StateBadge tone="idle" label="Not connected" /> : null
  return <SidePanel open onOpenChange={o => !o && onClose()} description={p.tagline} footer={footer}
    title={<span className="flex items-center gap-3"><PackageLogo logo={p.logo} large />{p.title}</span>}>
    <div className="space-y-6 text-[13px]">
      {status}
      <About p={p} />
      {steps.length > 0 && <ol aria-label="Steps" className="space-y-0 border-t border-line pt-5">
        {steps.includes('connect') && account && <Step n={steps.indexOf('connect') + 1} title={`Connect ${account.title}`} state={reopened ? 'current' : stateOf('connect')}
          summary={<>{connections.map(c => <Fragment key={c.key}>{c.key}{' '}<button type="button" className="text-accent hover:underline" onClick={() => { setEditing(c); setStep('connect') }}>Edit</button>{' · '}</Fragment>)}
            <button type="button" className="text-accent hover:underline" onClick={() => { setAnother(true); setStep('connect') }}>Add another</button></>}>
          <div className="rounded-lg border border-line p-4">
            <ConnectionForm key={editing?.key ?? 'new'} name={account.name} existing={editing ?? undefined} inline
              onDone={() => { setAnother(false); setEditing(null); setStep(p.resources.length ? 'repo' : 'done') }}
              onCancel={reopened ? () => { setAnother(false); setEditing(null); setStep('repo') } : undefined} />
          </div>
        </Step>}
        {steps.includes('repo') && <Step n={steps.indexOf('repo') + 1} title="Choose a repository" state={reopened ? 'later' : stateOf('repo')}
          summary={repo && <>{repo}{pkg && p.resources.length > 1 ? ` · ${pkg.title}` : ''}</>} last={false}>
          <div className="space-y-4">
            {p.resources.length > 1 && <Choices label="Package" items={p.resources.map(r => ({ value: r.name, label: r.title, note: sentences(r.description)[0] }))}
              value={pkg?.name ?? null} onChoose={name => { setPkg(p.resources.find(r => r.name === name)); setRepo(null) }} />}
            {pkg && <RepoChoices pkg={pkg} value={repo} onChoose={setRepo} />}
          </div>
        </Step>}
        {steps.includes('settings') && pkg && <Step n={steps.indexOf('settings') + 1} title="Settings" state={reopened ? 'later' : stateOf('settings')} last>
          {repo && <EntryForm repo={repo} name={pkg.name} bare onDone={() => setStep('done')} onBack={() => setStep('repo')} onCancel={onClose} />}
        </Step>}
      </ol>}
      {step === 'done' && repo && pkg && <div role="status" className="rounded-lg border border-ok/25 bg-ok-soft p-4">
        <p className="flex items-center gap-2 font-medium text-ok"><CircleCheck className="size-4" />Added to {repo}</p>
        <p className="mt-1 text-muted">Each new sandbox of {repo} gets its own {pkg.title}.</p>
        <div className="mt-3 flex flex-wrap gap-2">
          <LinkButton size="sm" variant="primary" to={`/app/repos/${repo}/packages`}>Open {repo}</LinkButton>
          <Button size="sm" onClick={() => { setRepo(null); setStep('repo') }}>Add to another repository</Button>
        </div>
      </div>}
    </div>
  </SidePanel>
}

/** One step: a number or a tick, its title, and what it settled once done; only the current one is open. */
function Step({ n, title, state, summary, last, children }: { n: number; title: string; state: 'done' | 'current' | 'later'; summary?: ReactNode; last?: boolean; children?: ReactNode }) {
  return <li className="relative flex gap-3 pb-5 last:pb-0" aria-current={state === 'current' ? 'step' : undefined}>
    {!last && <span aria-hidden className="absolute bottom-0 left-3 top-7 w-px -translate-x-1/2 bg-line" />}
    <span className={cn('relative grid size-6 shrink-0 place-items-center rounded-full border text-[12px] font-medium',
      state === 'done' ? 'border-transparent bg-ok-soft text-ok' : state === 'current' ? 'border-accent bg-accent text-accent-ink' : 'border-line bg-panel text-faint')}>
      {state === 'done' ? <Check className="size-3.5" aria-label="Done" /> : n}
    </span>
    <div className="min-w-0 grow pt-0.5">
      <p className={cn('font-medium', state === 'later' && 'text-faint')}>{title}</p>
      {state === 'done' && summary && <p className="mt-0.5 text-[12.5px] text-muted">{summary}</p>}
      {state === 'current' && <div className="mt-3">{children}</div>}
    </div>
  </li>
}

/** A list to pick one item from, drawn as radio rows. */
function Choices({ label, items, value, onChoose }: { label: string; items: { value: string; label: string; note?: string; href?: string; aside?: string }[]; value: string | null; onChoose: (value: string) => void }) {
  const row = 'flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors hover:bg-hover'
  return <div role="radiogroup" aria-label={label} className="divide-y divide-line overflow-hidden rounded-lg border border-line">
    {items.map(item => item.href
      ? <Link key={item.value} to={item.href} className={row}>
        <FolderGit2 className="size-4 shrink-0 text-faint" /><span className="grow font-medium text-muted">{item.label}</span>
        <span className="text-[12px] text-muted">{item.aside}</span><ArrowUpRight className="size-3.5 text-muted" />
      </Link>
      : <button key={item.value} type="button" role="radio" aria-checked={value === item.value} onClick={() => onChoose(item.value)}
        className={cn(row, value === item.value && 'bg-accent-soft hover:bg-accent-soft')}>
        <span aria-hidden className={cn('grid size-4 shrink-0 place-items-center rounded-full border', value === item.value ? 'border-accent' : 'border-line-strong')}>
          {value === item.value && <span className="size-2 rounded-full bg-accent" />}
        </span>
        <span className="min-w-0 grow"><span className="block font-medium">{item.label}</span>{item.note && <span className="block text-[12px] text-muted">{item.note}</span>}</span>
      </button>)}
  </div>
}

/** The repositories this server trusts, to add a package to; one that has it already links to its Packages tab. */
function RepoChoices({ pkg, value, onChoose }: { pkg: CatalogPackage; value: string | null; onChoose: (repo: string) => void }) {
  const repos = useRepos()
  return <QueryView query={repos}>{list => {
    const usable = list.filter(r => r.trust === 'trusted')
    return usable.length === 0 ? <EmptyState title="No repository can have packages yet">Packages are for repositories this server trusts.</EmptyState>
      : <Choices label="Repository" value={value} onChoose={onChoose} items={usable.map(r => pkg.used_by.includes(r.repo)
        ? { value: r.repo, label: r.repo, href: `/app/repos/${r.repo}/packages`, aside: 'Has it already' }
        : { value: r.repo, label: r.repo })} />
  }}</QueryView>
}

/** What a provider gives, as one block: what each sandbox gets is the lead; how the account connects and the
 * package's details follow as points; then where it is used and the guides, in one row. */
function About({ p }: { p: Provider }) {
  const [first, ...more] = p.resources
  const lead = sentences(first?.description ?? p.account?.description ?? '')
  const points = [
    ...(first ? sentences(p.account?.description ?? '') : []),
    ...lead.slice(1),
    ...more.flatMap(r => sentences(r.description)),
  ]
  const guides = [
    ...(p.account?.docs ? [{ href: p.account.docs, label: 'Setup guide' }] : []),
    ...p.resources.filter(r => r.docs).map(r => ({ href: r.docs, label: `${r.title} docs` })),
  ]
  const usedBy = p.resources.flatMap(r => r.used_by)
  return <section aria-label={`About ${p.title}`} className="space-y-3">
    {lead[0] && <p className="text-[14px] leading-relaxed"><LinkedText text={lead[0]} /></p>}
    {points.length > 0 && <ul className="space-y-1.5 text-[12.5px] text-muted">
      {points.map(point => <li key={point} className="flex gap-2"><span aria-hidden className="mt-[0.55em] size-1 shrink-0 rounded-full bg-faint" /><span><LinkedText text={point} /></span></li>)}
    </ul>}
    {(usedBy.length > 0 || guides.length > 0) && <div className="flex flex-wrap items-center gap-x-4 gap-y-1 pt-0.5 text-[12.5px]">
      {usedBy.length > 0 && <span className="text-muted"><UsedIn usedBy={usedBy} /></span>}
      {guides.map(g => <DocsLink key={g.href} href={g.href}>{g.label}</DocsLink>)}
    </div>}
  </section>
}

