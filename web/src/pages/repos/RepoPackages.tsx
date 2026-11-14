import { useQueryClient } from '@tanstack/react-query'
import { Lock, Package, Plus, ShieldCheck, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router'
import { api } from '@/api/endpoints'
import { keys, useAction, useCatalog, useCatalogEntry, useSharedConnections } from '@/api/queries'
import type { Attachment, AttachmentState, CatalogEntry } from '@/api/types'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { AppForm, SubmitButton, useAppForm, useStore } from '@/components/form'
import { EmptyState, ErrorState, Loading, QueryView } from '@/components/states'
import { StateBadge } from '@/components/StatusBadge'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { Badge, Code, Panel } from '@/components/ui/primitives'
import type { Tone } from '@/lib/status'
import { keyError, required } from '@/lib/validation'
import { useRepo } from './RepoLayout'

export const ATTACHMENT_STATE: Record<AttachmentState, { label: string; tone: Tone }> = {
  ready: { label: 'Ready', tone: 'ok' },
  needs_values: { label: 'Needs values', tone: 'warn' },
  needs_connection: { label: 'Needs a connection', tone: 'warn' },
  check_failed: { label: 'Check failed', tone: 'bad' },
  disabled: { label: 'Disabled', tone: 'idle' },
  error: { label: 'Error', tone: 'bad' },
}
const KIND_LABEL: Record<string, string> = { provision: 'per sandbox', connection: 'shared' }

export default function RepoPackages() {
  const { repo, env, envError, trusted, me } = useRepo()
  const [adding, setAdding] = useState(false)
  if (!trusted) return <EmptyState icon={Lock} title="No packages for public repositories">Packages provision databases and credentials; public repositories get none.</EmptyState>
  if (envError) return <ErrorState error={envError} />
  if (!env) return <Loading />
  const admin = !!me?.admin
  return <div className="space-y-4">
    <Panel title="Attached packages" bodyClassName="p-0"
      description={<>Databases, object stores and credentials each sandbox gets. Your agent declares them in sbx.yaml under <Code>services:</Code>; {admin ? 'you set their values and shared connections here.' : 'an admin sets their values and shared connections.'}</>}
      actions={admin && <Button size="sm" variant="primary" onClick={() => setAdding(true)}><Plus />Attach package</Button>}>
      {env.attachments.length === 0
        ? <EmptyState icon={Package} title="No packages" className="py-10">This repository's sandboxes need no databases or third-party credentials, or the recipe does not declare them yet.</EmptyState>
        : <ul className="divide-y divide-line">{env.attachments.map(a => <AttachmentRow key={a.key} a={a} repo={repo} admin={admin} />)}</ul>}
    </Panel>
    {admin && <Dialog open={adding} onOpenChange={setAdding} title="Attach a package" description="The package's inputs are stored on the server; sandboxes get what it exports." wide>
      <AttachPicker repo={repo} onDone={() => setAdding(false)} />
    </Dialog>}
  </div>
}

function AttachmentRow({ a, repo, admin }: { a: Attachment; repo: string; admin: boolean }) {
  const qc = useQueryClient()
  const [fixing, setFixing] = useState(false)
  const check = useAction(() => api.attachments.check(repo, a.key), keys.env(repo))
  const state = ATTACHMENT_STATE[a.state] ?? { label: a.state, tone: 'idle' as Tone }
  const fixable = admin && (a.state === 'needs_values' || a.state === 'needs_connection' || a.state === 'check_failed')
  return <li className="px-4 py-3">
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
      <div className="min-w-0 grow basis-60">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{a.key}</span>
          <span className="text-[12.5px] text-muted">{a.title !== a.key ? a.title : a.package}</span>
          <Badge>{KIND_LABEL[a.kind] ?? a.kind}</Badge>
          {a.origin !== 'here' && <Badge tone="info" title="Declared in .sbx/sbx.yaml">sbx.yaml</Badge>}
        </div>
        {a.problem && <p className="mt-0.5 text-[12.5px] text-warn">{a.problem}</p>}
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[12px] text-muted">
          {a.exports.length > 0 && <span>Exports <span className="font-mono">{a.exports.join(', ')}</span></span>}
          {Object.entries(a.connections).map(([slot, key]) => <span key={slot}>{slot} → <span className="font-mono">{key}</span></span>)}
          {Object.entries(a.secret_inputs).map(([k, set]) => <span key={k} className="inline-flex items-center gap-0.5"><Lock className="size-3" />{k}{set ? '' : ' (not set)'}</span>)}
        </div>
      </div>
      <StateBadge tone={state.tone} label={state.label} />
      <div className="flex items-center gap-1.5">
        {fixable && <Button size="sm" variant="primary" onClick={() => setFixing(true)}>{a.state === 'needs_connection' ? 'Choose connection' : 'Provide values'}</Button>}
        {!a.project && <Button size="sm" busy={check.isPending} onClick={() => check.mutate()}><ShieldCheck />Check</Button>}
        {admin && a.origin !== 'sbx.yaml' && <ConfirmDialog trigger={<Button size="icon-sm" variant="ghost" aria-label={`Detach ${a.key}`}><Trash2 /></Button>}
          title={`Detach ${a.key}?`} description={a.origin === 'both' ? 'Removes the values set here; sbx.yaml still declares it.' : 'New sandboxes stop getting it. Existing sandboxes keep what was provisioned until they are deleted.'}
          confirm="Detach" onConfirm={async () => { await api.attachments.detach(repo, a.key); await qc.invalidateQueries({ queryKey: keys.env(repo) }) }} />}
      </div>
    </div>
    {check.data && <p role="status" className={check.data.ok ? 'mt-2 text-[12.5px] text-ok' : 'mt-2 text-[12.5px] text-bad'}>{check.data.ok ? 'Check passed.' : check.data.detail || 'Check failed.'}</p>}
    <ErrorState error={check.error} compact className="mt-2" />
    {fixable && <Dialog open={fixing} onOpenChange={setFixing} title={`Configure ${a.key}`} description={a.title} wide>
      <EntryForm repo={repo} name={a.package} attachment={a} onDone={() => setFixing(false)} />
    </Dialog>}
  </li>
}

function AttachPicker({ repo, onDone }: { repo: string; onDone: () => void }) {
  const catalog = useCatalog()
  const [chosen, setChosen] = useState<string | null>(null)
  if (chosen) return <EntryForm repo={repo} name={chosen} onDone={onDone} onBack={() => setChosen(null)} />
  return <QueryView query={catalog}>{packages => {
    const usable = packages.filter(p => p.kind === 'provision')
    return usable.length === 0 ? <EmptyState title="The catalog is empty" /> : <ul className="-mx-2 space-y-0.5">
      {usable.map(p => <li key={p.name}><button type="button" onClick={() => setChosen(p.name)} className="w-full rounded-md px-2 py-2 text-left hover:bg-hover">
        <span className="flex items-center gap-2"><span className="font-medium">{p.title}</span><Badge>{KIND_LABEL[p.kind]}</Badge><span className="font-mono text-[11.5px] text-faint">{p.name}</span></span>
        <span className="mt-0.5 line-clamp-2 text-[12.5px] text-muted">{p.description}</span>
      </button></li>)}
    </ul>
  }}</QueryView>
}

function EntryForm(props: { repo: string; name: string; attachment?: Attachment; onDone: () => void; onBack?: () => void }) {
  const entry = useCatalogEntry(props.name)
  return <QueryView query={entry}>{e => <AttachForm {...props} entry={e} />}</QueryView>
}

/** Secret inputs are write-only. */
export function AttachForm({ repo, entry, attachment, onDone, onBack }: {
  repo: string; entry: CatalogEntry; attachment?: Attachment; onDone: () => void; onBack?: () => void
}) {
  const shared = useSharedConnections(Object.keys(entry.requires).length > 0)
  const attach = useAction((v: { key: string; inputs: Record<string, string>; connections: Record<string, string> }) => api.attachments.attach(repo, {
    package: entry.name, key: v.key, inputs: Object.fromEntries(Object.entries(v.inputs).filter(([, x]) => x !== '')),
    connections: Object.fromEntries(Object.entries(v.connections).filter(([, x]) => x)), check: true,
  }), keys.env(repo), keys.readiness(repo), keys.repos)
  const inputs = Object.entries(entry.inputs)
  const form = useAppForm({
    defaultValues: {
      key: attachment?.key ?? entry.name.split('/').pop()!.replace(/[^a-z0-9_-]/g, '-'),
      inputs: Object.fromEntries(inputs.map(([n, i]) => [n, i.secret ? '' : attachment?.inputs[n] ?? i.default ?? ''])) as Record<string, string>,
      connections: Object.fromEntries(Object.keys(entry.requires).map(slot => [slot, attachment?.connections[slot] ?? ''])) as Record<string, string>,
    },
    onSubmit: async ({ value }) => { await attach.mutateAsync(value); onDone() },
  })
  const result = useStore(form.store, s => s.isSubmitSuccessful)
  return <AppForm form={form} className="space-y-4">
    {entry.description && <p className="text-[13px] text-muted">{entry.description}</p>}
    {!attachment && <form.AppField name="key" validators={{ onChange: ({ value }) => keyError(value) }}>
      {field => <field.TextField label="Key" help="How sbx.yaml and the env refer to it; one package can be attached under several keys." className="font-mono" autoComplete="off" />}
    </form.AppField>}
    {inputs.length > 0 && <fieldset className="space-y-3"><legend className="mb-2 text-[12px] font-medium uppercase tracking-wide text-muted">Inputs</legend>
      {inputs.map(([n, i]) => {
        const set = attachment?.secret_inputs[n]
        return <form.AppField key={n} name={`inputs.${n}`} validators={{ onChange: ({ value }) => i.required && !set && !i.default && !attachment?.inputs[n] ? required(value ?? '') : undefined }}>
          {field => <field.TextField label={<span className="font-mono text-[12px]">{n}</span>} optional={!i.required} type={i.secret ? 'password' : 'text'} autoComplete="off" spellCheck={false}
            placeholder={i.secret && set ? 'set · empty keeps it' : i.default ?? ''} help={<>{i.help}{!i.agent && i.secret ? ' Hidden from the coding agent.' : ''}</>} />}
        </form.AppField>
      })}
    </fieldset>}
    {Object.keys(entry.requires).length > 0 && <fieldset className="space-y-3"><legend className="mb-2 text-[12px] font-medium uppercase tracking-wide text-muted">Shared connections</legend>
      {Object.entries(entry.requires).map(([slot, need]) => {
        const options = (shared.data ?? []).filter(s => s.package === need.kind)
        return <form.AppField key={slot} name={`connections.${slot}`}>
          {field => <field.SelectField label={<>{slot} <span className="font-normal text-muted">({need.kind})</span></>} help={options.length ? 'Each sandbox gets its own resource on this server.' : <>No {need.kind} connection yet. <Link className="text-accent hover:underline" to="/app/extensions#connections">Add one in Extensions</Link>.</>}>
            <option value="">{options.length === 1 ? `Only one: ${options[0].key}` : 'Choose…'}</option>
            {options.map(s => <option key={s.key} value={s.key}>{s.key} · {s.title}</option>)}
          </field.SelectField>}
        </form.AppField>
      })}
    </fieldset>}
    {entry.checks && <p className="text-[12px] text-muted">Saving runs the package's check first; nothing is stored when it fails.</p>}
    {result && <p role="status" className="text-[12.5px] text-ok">Saved.</p>}
    <div className="flex justify-between gap-2">
      {onBack ? <Button variant="ghost" onClick={onBack}>Back</Button> : <span />}
      <div className="flex gap-2"><Button variant="ghost" onClick={onDone}>Cancel</Button><SubmitButton form={form}>{attachment ? 'Save' : 'Attach'}</SubmitButton></div>
    </div>
  </AppForm>
}
