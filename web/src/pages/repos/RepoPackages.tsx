import { useQueryClient } from '@tanstack/react-query'
import { Lock, Package, Plus, Settings2, ShieldCheck, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { api } from '@/api/endpoints'
import { keys, useAction, useCatalog, useSharedConnections } from '@/api/queries'
import type { Attachment, AttachmentState, CatalogPackage, SharedConnection } from '@/api/types'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { EntryForm } from '@/components/AttachForm'
import { PackageLogo, PackagePicker } from '@/components/PackageLogo'
import { EmptyState, ErrorState, Loading, QueryView } from '@/components/states'
import { StateBadge } from '@/components/StatusBadge'
import { Button, LinkButton } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { Panel } from '@/components/ui/primitives'
import { article, inputLabel } from '@/lib/packages'
import type { Tone } from '@/lib/status'
import { useRepo } from './RepoLayout'
import { LinkedText } from '@/components/LinkedText'

const ATTACHMENT_STATE: Record<AttachmentState, { label: string; tone: Tone }> = {
  ready: { label: 'Ready', tone: 'ok' },
  needs_values: { label: 'Needs settings', tone: 'warn' },
  needs_connection: { label: 'Needs a connection', tone: 'warn' },
  check_failed: { label: 'Check failed', tone: 'bad' },
  disabled: { label: 'Off', tone: 'idle' },
  error: { label: 'Error', tone: 'bad' },
}

export default function RepoPackages() {
  const { repo, env, envError, trusted, me } = useRepo()
  const admin = !!me?.admin
  const catalog = useCatalog()
  const shared = useSharedConnections(admin && trusted)
  const [adding, setAdding] = useState(false)
  if (!trusted) return <EmptyState icon={Lock} title="No packages for public repositories" />
  if (envError) return <ErrorState error={envError} />
  if (!env) return <Loading />
  return <div className="space-y-4">
    <Panel title="Packages" bodyClassName="p-0"
      actions={admin && <Button size="sm" variant="primary" onClick={() => setAdding(true)}><Plus />Add package</Button>}>
      {env.attachments.length === 0
        ? <EmptyState icon={Package} title="No packages" className="py-10" />
        : <ul className="divide-y divide-line">{env.attachments.map(a => <AttachmentRow key={a.key} a={a} repo={repo} admin={admin}
          catalog={catalog.data ?? []} shared={shared.data} />)}</ul>}
    </Panel>
    {admin && <Dialog open={adding} onOpenChange={setAdding} title="Add a package" wide>
      <AttachPicker repo={repo} onDone={() => setAdding(false)} />
    </Dialog>}
  </div>
}

/** What a row says under its title: the one thing to know, in words, from the attachment's state. */
function rowNote(a: Attachment, catalog: CatalogPackage[], shared: SharedConnection[] | undefined, admin: boolean) {
  const pkg = catalog.find(p => p.name === a.package)
  const title = (kind: string) => catalog.find(p => p.name === kind)?.title ?? kind
  if (a.state === 'needs_connection' && pkg) {
    const kinds = Object.values(pkg.requires).map(r => r.kind)
    const none = kinds.find(k => shared && !shared.some(s => s.package === k))
    if (none) return { text: `Needs ${article(title(none))}, and none is connected yet.`, connect: none }
    if (kinds[0]) return { text: admin ? `Choose which ${title(kinds[0])} it uses.` : `Needs ${article(title(kinds[0]))}; an admin picks which.` }
  }
  if (a.state === 'needs_values' && a.problem?.startsWith('needs ')) {
    const names = a.problem.slice(6).split(', ')
    return { text: `Needs ${names.map(n => inputLabel(n, pkg?.inputs[n])).join(', ')}.` }
  }
  if (a.state === 'disabled') return { text: 'New sandboxes don’t get it.' }
  if (a.problem) return { text: a.problem, bad: a.state !== 'needs_connection' && a.state !== 'needs_values' }
  const on = Object.values(a.connections).map(key => {
    const found = shared?.find(s => s.key === key)
    return found ? `${found.title} ${key}` : key
  })
  return { text: on.length ? `Made on ${on.join(', ')}` : '' }
}

function AttachmentRow({ a, repo, admin, catalog, shared }: {
  a: Attachment; repo: string; admin: boolean; catalog: CatalogPackage[]; shared: SharedConnection[] | undefined
}) {
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  const check = useAction(() => api.attachments.check(repo, a.key), keys.env(repo))
  const state = ATTACHMENT_STATE[a.state] ?? { label: a.state, tone: 'idle' as Tone }
  const note = rowNote(a, catalog, shared, admin)
  const pkg = catalog.find(p => p.name === a.package)
  // Something to change: an input, or a connection to choose among several (one alone is taken without asking).
  const configurable = !!pkg && (Object.keys(pkg.inputs).length > 0
    || Object.values(pkg.requires).some(r => (shared ?? []).filter(s => s.package === r.kind).length !== 1))
  const fix = admin && (a.state === 'needs_values' || a.state === 'needs_connection' || a.state === 'check_failed')
  return <li className="px-4 py-3">
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
      <PackageLogo logo={a.logo} large />
      <div className="min-w-0 grow basis-60">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="font-medium">{a.title}</span>
          {a.key !== a.title && <span className="text-[12.5px] text-muted">{a.key}</span>}
          {a.origin !== 'here' && <span className="text-[12px] text-faint" title="Declared in .sbx/sbx.yaml">from the recipe</span>}
        </div>
        {note.text && <p className={note.bad ? 'mt-0.5 text-[12.5px] text-bad' : 'mt-0.5 text-[12.5px] text-muted'}><LinkedText text={note.text} /></p>}
      </div>
      <StateBadge tone={state.tone} label={state.label} />
      <div className="flex items-center gap-1.5">
        {fix && note.connect ? <LinkButton size="sm" variant="primary" to="/app/services" state={{ connect: note.connect }}>Connect</LinkButton>
          : fix ? <Button size="sm" variant="primary" onClick={() => setEditing(true)}>{a.state === 'needs_connection' ? 'Choose' : 'Set up'}</Button>
          : admin && configurable && a.state !== 'disabled' ? <Button size="sm" onClick={() => setEditing(true)}><Settings2 />Edit</Button> : null}
        {!a.project && (a.state === 'ready' || a.state === 'check_failed') && <Button size="sm" busy={check.isPending} onClick={() => check.mutate()}><ShieldCheck />Test</Button>}
        {admin && a.origin !== 'sbx.yaml' && <ConfirmDialog trigger={<Button size="icon-sm" variant="ghost" aria-label={`Remove ${a.key}`}><Trash2 /></Button>}
          title={a.origin === 'both' ? `Turn off ${a.title}?` : `Remove ${a.title}?`}
          description={a.origin === 'both' ? 'The recipe still names it, so it stays, turned off.' : 'New sandboxes stop getting it.'}
          confirm={a.origin === 'both' ? 'Turn off' : 'Remove'} onConfirm={async () => { await api.attachments.detach(repo, a.key); await qc.invalidateQueries({ queryKey: keys.env(repo) }) }} />}
      </div>
    </div>
    {check.data && <p role="status" className={check.data.ok ? 'mt-2 text-[12.5px] text-ok' : 'mt-2 text-[12.5px] text-bad'}>{check.data.ok ? 'Test passed.' : <LinkedText text={check.data.detail || 'Test failed.'} />}</p>}
    <ErrorState error={check.error} compact className="mt-2" />
    {admin && <Dialog open={editing} onOpenChange={setEditing} title={<span className="flex items-center gap-3"><PackageLogo logo={a.logo} large />{a.title}</span>} wide>
      <EntryForm repo={repo} name={a.package} attachment={a} onDone={() => setEditing(false)} />
    </Dialog>}
  </li>
}

function AttachPicker({ repo, onDone }: { repo: string; onDone: () => void }) {
  const catalog = useCatalog()
  const [chosen, setChosen] = useState<string | null>(null)
  if (chosen) return <EntryForm repo={repo} name={chosen} onDone={onDone} onBack={() => setChosen(null)} />
  return <QueryView query={catalog}>{packages => {
    const usable = packages.filter(p => p.kind === 'provision')
    return usable.length === 0 ? <EmptyState title="The catalog is empty" /> : <PackagePicker packages={usable} onChoose={setChosen} />
  }}</QueryView>
}
