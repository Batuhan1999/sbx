import { useQueryClient } from '@tanstack/react-query'
import { Bot, Github, Network, Plus, ShieldCheck, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useLocation } from 'react-router'
import { api } from '@/api/endpoints'
import { keys, useAction, useCatalog, useCatalogEntry, useCatalogProblems, useConnections, useMe, useSharedConnections } from '@/api/queries'
import type { CatalogPackage, SharedConnection } from '@/api/types'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { PackageHeading, PackageLogo, PackagePicker } from '@/components/PackageLogo'
import { AppForm, SubmitButton, useAppForm } from '@/components/form'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState, ErrorState, QueryView, SkeletonRows } from '@/components/states'
import { StateBadge } from '@/components/StatusBadge'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { Badge, Code } from '@/components/ui/primitives'
import { ago, plural } from '@/lib/format'
import { keyError, required } from '@/lib/validation'
import { ConnectionCard } from './ConnectionCard'

const KIND_LABEL = { provision: 'per sandbox', connection: 'shared connection' }

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

function SharedConnections() {
  const shared = useSharedConnections()
  const qc = useQueryClient()
  const [adding, setAdding] = useState(false)
  return <Section id="connections" title="Shared connections" description="Servers and accounts packages provision from: a Postgres host, a Neon project, an AWS role. Each sandbox gets its own database or credentials on them."
    actions={<Button size="sm" variant="primary" onClick={() => setAdding(true)}><Plus />Add connection</Button>}>
    <div className="overflow-hidden rounded-lg border border-line bg-panel">
      {shared.error ? <div className="p-4"><ErrorState error={shared.error} compact retry={() => shared.refetch()} /></div>
        : shared.isPending ? <SkeletonRows rows={2} />
        : shared.data.length === 0 ? <EmptyState icon={Network} title="No shared connections" className="py-8">Packages that provision per-sandbox resources ask for one. Your agent's package brief says which.</EmptyState>
        : <ul className="divide-y divide-line">{shared.data.map(s => <ConnectionRow key={s.key} s={s} onRemoved={() => qc.invalidateQueries({ queryKey: keys.sharedConnections })} />)}</ul>}
    </div>
    <Dialog open={adding} onOpenChange={setAdding} title="Add a shared connection" description="Checked before it is saved. Secret inputs stay on the server." wide>
      <AddConnection onDone={() => setAdding(false)} />
    </Dialog>
  </Section>
}

function ConnectionRow({ s, onRemoved }: { s: SharedConnection; onRemoved: () => void }) {
  const check = useAction(() => api.catalog.checkConnection(s.key), keys.sharedConnections)
  const status = check.data ? (check.data.ok ? 'passed' : 'failed') : s.check_status
  return <li className="px-4 py-3">
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
      <PackageLogo logo={s.logo} large />
      <div className="min-w-0 grow basis-56">
        <div className="flex flex-wrap items-center gap-2"><span className="font-medium">{s.key}</span><span className="text-[12.5px] text-muted">{s.title}</span>{!s.known && <Badge tone="warn">unknown package</Badge>}</div>
        <p className="mt-0.5 truncate text-[12px] text-muted">
          {Object.entries(s.inputs).map(([k, v]) => `${k}=${v}`).join('  ') || 'no inputs'}
        </p>
        <p className="text-[12px] text-muted">{s.used_by.length ? `Used by ${s.used_by.join(', ')}` : 'Not used yet'}{s.added_at ? ` · added ${ago(s.added_at)}` : ''}</p>
      </div>
      {status && <StateBadge tone={status === 'passed' || status === 'ok' ? 'ok' : status === 'failed' ? 'bad' : 'idle'} label={status === 'passed' || status === 'ok' ? 'Check passed' : status === 'failed' ? 'Check failed' : status} />}
      <Button size="sm" busy={check.isPending} onClick={() => check.mutate()}><ShieldCheck />Check</Button>
      <ConfirmDialog trigger={<Button size="icon-sm" variant="ghost" aria-label={`Remove ${s.key}`}><Trash2 /></Button>}
        title={`Remove ${s.key}?`} description={s.used_by.length ? `Still used by ${s.used_by.join(', ')}; the server refuses while it is.` : 'Packages can no longer provision on it.'}
        confirm="Remove" onConfirm={async () => { await api.catalog.removeConnection(s.key); onRemoved() }} />
    </div>
    {check.data && !check.data.ok && <p className="mt-2 text-[12.5px] text-bad">{check.data.detail}</p>}
    <ErrorState error={check.error} compact className="mt-2" />
  </li>
}

function AddConnection({ onDone }: { onDone: () => void }) {
  const catalog = useCatalog()
  const [name, setName] = useState<string | null>(null)
  if (name) return <ConnectionForm key={name} name={name} onDone={onDone} onBack={() => setName(null)} />
  return <QueryView query={catalog}>{packages => {
    const kinds = packages.filter(p => p.kind === 'connection')
    return kinds.length === 0 ? <EmptyState title="No connection packages in the catalog" /> : <PackagePicker packages={kinds} onChoose={setName} />
  }}</QueryView>
}

function ConnectionForm({ name, onDone, onBack }: { name: string; onDone: () => void; onBack: () => void }) {
  const entry = useCatalogEntry(name)
  const add = useAction((v: { key: string; inputs: Record<string, string> }) => api.catalog.connect({ package: name, key: v.key, inputs: v.inputs }), keys.sharedConnections)
  return <QueryView query={entry}>{e => {
    const inputs = Object.entries(e.inputs)
    return <div className="space-y-3"><PackageHeading logo={e.logo} title={e.title} />
      <ConnectionFields inputs={inputs} defaultKey={name.split('/').pop()!} description={e.description}
        onSubmit={async v => { await add.mutateAsync(v); onDone() }} onCancel={onDone} onBack={onBack} />
    </div>
  }}</QueryView>
}

function ConnectionFields({ inputs, defaultKey, description, onSubmit, onCancel, onBack }: {
  inputs: [string, CatalogPackage['inputs'][string]][]; defaultKey: string; description: string
  onSubmit: (v: { key: string; inputs: Record<string, string> }) => Promise<void>; onCancel: () => void; onBack: () => void
}) {
  const form = useAppForm({
    defaultValues: { key: defaultKey, inputs: Object.fromEntries(inputs.map(([n, i]) => [n, i.default ?? ''])) as Record<string, string> },
    onSubmit: ({ value }) => onSubmit(value),
  })
  return <AppForm form={form} className="space-y-3">
    <p className="text-[13px] text-muted">{description}</p>
    <form.AppField name="key" validators={{ onChange: ({ value }) => keyError(value) }}>{field => <field.TextField label="Key" className="font-mono" autoComplete="off" help="Packages refer to it by this key." />}</form.AppField>
    {inputs.map(([n, i]) => <form.AppField key={n} name={`inputs.${n}`} validators={{ onChange: ({ value }) => i.required && !i.default ? required(value ?? '') : undefined }}>
      {field => <field.TextField label={<span className="font-mono text-[12px]">{n}</span>} optional={!i.required} type={i.secret ? 'password' : 'text'} help={i.help} autoComplete="off" spellCheck={false} />}
    </form.AppField>)}
    <div className="flex justify-between gap-2">
      <Button variant="ghost" onClick={onBack}>Back</Button>
      <div className="flex gap-2"><Button variant="ghost" onClick={onCancel}>Cancel</Button><SubmitButton form={form}>Check and save</SubmitButton></div>
    </div>
  </AppForm>
}

function Catalog({ admin }: { admin: boolean }) {
  const catalog = useCatalog()
  const problems = useCatalogProblems(admin)
  const [open, setOpen] = useState<string | null>(null)
  const chosen = catalog.data?.find(p => p.name === open)
  return <Section id="packages" title="Package catalog" description="Declarative add-ons a recipe uses in its services section. Your agent picks them from evidence in the repository; you attach and configure them per repository.">
    {admin && !!problems.data?.length && <div role="alert" className="mb-3 rounded-lg border border-warn/30 bg-warn-soft px-4 py-3 text-[13px]">
      <p className="font-medium">{plural(problems.data.length, 'package')} in this server’s catalog could not be read</p>
      <ul className="mt-1 space-y-0.5 font-mono text-[12px] text-muted">{problems.data.map(p => <li key={p}>{p}</li>)}</ul>
    </div>}
    {catalog.error ? <ErrorState error={catalog.error} retry={() => catalog.refetch()} />
      : catalog.isPending ? <div className="rounded-lg border border-line bg-panel"><SkeletonRows rows={3} /></div>
      : <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
        {catalog.data.map(p => <button key={p.name} type="button" onClick={() => setOpen(p.name)} className="flex flex-col items-start rounded-lg border border-line bg-panel p-3 text-left transition-colors hover:border-line-strong hover:bg-hover/40">
          <span className="flex items-center gap-2.5"><PackageLogo logo={p.logo} /><span className="font-medium">{p.title}</span></span>
          <span className="mt-1 line-clamp-2 text-[12.5px] text-muted">{p.description}</span>
          <span className="mt-auto flex flex-wrap items-center gap-1.5 pt-2"><Badge>{KIND_LABEL[p.kind]}</Badge>{p.used_by.length > 0 && <span className="text-[11.5px] text-muted">{plural(p.used_by.length, 'repository', 'repositories')}</span>}</span>
        </button>)}
      </div>}
    <Dialog open={!!open} onOpenChange={o => !o && setOpen(null)} wide
      title={<span className="flex items-center gap-2.5"><PackageLogo logo={chosen?.logo} />{chosen?.title ?? open}</span>}>
      {open && <PackageDetail name={open} admin={admin} />}
    </Dialog>
  </Section>
}

function PackageDetail({ name, admin }: { name: string; admin: boolean }) {
  const entry = useCatalogEntry(name)
  return <QueryView query={entry}>{e => <div className="space-y-4 text-[13px]">
    <p className="text-muted">{e.description}</p>
    <div className="flex flex-wrap gap-1.5"><Badge>{KIND_LABEL[e.kind]}</Badge>{e.checks && <Badge tone="ok">has a check</Badge>}</div>
    {Object.keys(e.inputs).length > 0 && <div><h3 className="mb-1.5 font-medium">Inputs</h3><ul className="space-y-1">
      {Object.entries(e.inputs).map(([n, i]) => <li key={n}><Code>{n}</Code>{i.required && <span className="text-warn"> required</span>}{i.secret && <span className="text-muted"> secret</span>} <span className="text-muted">{i.help}</span></li>)}
    </ul></div>}
    {Object.keys(e.env).length > 0 && <div><h3 className="mb-1.5 font-medium">Gives each sandbox</h3><p className="font-mono text-[12px] text-muted">{Object.keys(e.env).join('  ')}</p></div>}
    {Object.keys(e.requires).length > 0 && <div><h3 className="mb-1.5 font-medium">Needs</h3><p className="text-muted">{Object.entries(e.requires).map(([slot, r]) => `${slot}: a ${r.kind} connection`).join('; ')}</p></div>}
    {e.used_by.length > 0 && <div><h3 className="mb-1.5 font-medium">Used by</h3><p className="text-muted">{e.used_by.join(', ')}</p></div>}
    {e.readme && <details><summary className="cursor-pointer font-medium">README</summary><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap rounded-md bg-sunken p-3 text-[12px]">{e.readme}</pre></details>}
    {admin && e.kind === 'connection' && <p className="text-[12.5px] text-muted">Add one under Shared connections.</p>}
  </div>}</QueryView>
}

