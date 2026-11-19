import { useQueryClient } from '@tanstack/react-query'
import { Bot, FileUp, KeyRound, Lock, Pencil, Plus, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { api } from '@/api/endpoints'
import { keys, useAction } from '@/api/queries'
import type { Audience, EnvRow, EnvView } from '@/api/types'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { AppForm, MoreOptions, SECRET_KEPT, SubmitButton, useAppForm, useStore } from '@/components/form'
import { EmptyState, ErrorState, Loading } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { Badge, Panel } from '@/components/ui/primitives'
import { ago, plural } from '@/lib/format'
import { cn } from '@/lib/utils'
import { envNameError } from '@/lib/validation'
import { useRepo } from './RepoLayout'
import { MissingValues } from './RepoSetup'

const KIND_LABEL: Record<string, string> = { service: 'package', 'sbx.yaml': 'sbx.yaml', installation: 'server', repo: 'repo', mine: 'yours' }
const AUDIENCE: Record<Audience, string> = { all: 'All sandboxes', previews: 'PR previews only', sessions: 'Agent sessions only' }

export default function RepoEnv() {
  const { repo, env, envError, trusted } = useRepo()
  if (!trusted) return <EmptyState icon={Lock} title="No secrets for public repositories" />
  if (envError) return <ErrorState error={envError} />
  if (!env) return <Loading />
  return <EnvPage repo={repo} env={env} />
}

function EnvPage({ repo, env }: { repo: string; env: EnvView }) {
  // What the dialog is for stays while it fades out, so its title never reads "Edit null" as it closes.
  const [editing, setEditing] = useState<EnvRow | 'new'>('new')
  const [open, setOpen] = useState(false)
  const edit = (what: EnvRow | 'new') => { setEditing(what); setOpen(true) }
  const [importing, setImporting] = useState(false)
  const shown = env.variables
  return <div className="space-y-4">
    {env.store_error && <p role="alert" className="rounded-lg border border-warn/30 bg-warn-soft px-4 py-3 text-[13px] text-warn">
      Saved values could not be read just now, so some may show as missing below. Try again in a moment. ({env.store_error})
    </p>}
    {env.missing.length > 0 && <Panel title={`${plural(env.missing.length, 'value')} missing`}>
      <MissingValues repo={repo} names={env.missing} admin={env.admin} bare />
    </Panel>}
    <Panel title="Variables" bodyClassName="p-0"
      actions={<>
        <Button size="sm" onClick={() => setImporting(true)}><FileUp />Import .env</Button>
        <Button size="sm" variant="primary" onClick={() => edit('new')}><Plus />Add variable</Button>
      </>}>
      {shown.length === 0 ? <EmptyState icon={KeyRound} title="No variables yet" className="py-10" />
        : <ul className="divide-y divide-line">
          {shown.map((v, i) => <VariableRow key={`${v.name}-${v.kind}-${i}`} v={v} repo={repo} admin={env.admin} onEdit={() => edit(v)} />)}
        </ul>}
    </Panel>
    <Dialog open={open} onOpenChange={setOpen} title={editing === 'new' ? 'Add variable' : `Edit ${editing.name}`}
      description="Running sandboxes restart their app with it.">
      <VariableForm repo={repo} admin={env.admin} row={editing === 'new' ? null : editing} onDone={() => setOpen(false)} />
    </Dialog>
    <Dialog open={importing} onOpenChange={setImporting} title="Import a .env file" description="Each line is saved as a secret.">
      <ImportForm repo={repo} admin={env.admin} onDone={() => setImporting(false)} />
    </Dialog>
  </div>
}

function VariableRow({ v, repo, admin, onEdit }: { v: EnvRow; repo: string; admin: boolean; onEdit: () => void }) {
  const qc = useQueryClient()
  const editable = (v.kind === 'repo' && admin) || v.kind === 'mine'
  return <li className="flex flex-wrap items-center gap-x-4 gap-y-1.5 px-4 py-2.5">
    <div className="min-w-0 grow basis-64">
      <div className="flex flex-wrap items-center gap-2">
        <span className={cn('break-all font-mono text-[12.5px] font-medium', v.overridden_by && 'text-muted line-through decoration-faint')}>{v.name}</span>
        <Badge tone={v.kind === 'mine' ? 'accent' : 'neutral'}>{KIND_LABEL[v.kind] ?? v.kind}</Badge>
        {v.secret && <Lock className="size-3 text-muted" aria-label="secret" />}
        {v.agent && <span className="inline-flex items-center gap-0.5 text-[11.5px] text-muted" title="The coding agent can read this value"><Bot className="size-3" />agent</span>}
        {v.audience && v.audience !== 'all' && <Badge tone="info">{AUDIENCE[v.audience as Audience] ?? v.audience}</Badge>}
      </div>
      <div className="mt-0.5 truncate text-[12px] text-muted">
        {v.template != null ? (v.template === '' ? <span className="italic">empty</span> : <span className="font-mono">{v.template}</span>) : v.secret ? '••••••' : v.value != null ? <span className="font-mono">{v.value}</span> : 'set'}
        {v.overridden_by && <> · overridden by {v.overridden_by}</>}
        {v.owned_by && <> · from package {v.owned_by}{v.attachment && v.attachment !== v.owned_by ? ` (${v.attachment})` : ''}</>}
        {v.note && <> · {v.note}</>}
        {v.updated_at && <> · {v.updated_by ? `${v.updated_by.split('@')[0]}, ` : ''}{ago(v.updated_at)}</>}
      </div>
    </div>
    {editable && !v.owned_by && <div className="flex items-center gap-1">
      <Button size="icon-sm" variant="ghost" onClick={onEdit} aria-label={`Edit ${v.name}`}><Pencil /></Button>
      <ConfirmDialog trigger={<Button size="icon-sm" variant="ghost" aria-label={`Delete ${v.name}`}><Trash2 /></Button>}
        title={`Delete ${v.name}?`} description="Running sandboxes restart their app without it." confirm="Delete variable"
        onConfirm={async () => { await api.env.remove(repo, v.name, v.kind === 'mine'); await qc.invalidateQueries({ queryKey: keys.env(repo) }) }} />
    </div>}
  </li>
}

function VariableForm({ repo, admin, row, onDone }: { repo: string; admin: boolean; row: EnvRow | null; onDone: () => void }) {
  const save = useAction((value: { name: string; value: string; secret: boolean; audience: Audience; agent: boolean; mine: boolean }) =>
    api.env.set(repo, value.name, { value: row && !value.value ? null : value.value, secret: value.secret, audience: value.audience, agent: value.agent, mine: value.mine, restart: true }),
  keys.env(repo), keys.readiness(repo))
  const form = useAppForm({
    defaultValues: { name: row?.name ?? '', value: '', secret: row?.secret ?? true, audience: (row?.audience as Audience) ?? 'all', agent: row?.agent ?? false, mine: row ? row.kind === 'mine' : !admin },
    onSubmit: async ({ value }) => { await save.mutateAsync(value); onDone() },
  })
  const secret = useStore(form.store, s => s.values.secret)
  // Who gets it is rarely changed: folded, open when this variable is not the default (all sandboxes, agent kept out).
  const unusual = !!row && ((row.audience ?? 'all') !== 'all' || !!row.agent)
  return <AppForm form={form} className="space-y-4">
    {!row && <form.AppField name="name" validators={{ onChange: ({ value }) => envNameError(value) }}>
      {field => <field.TextField label="Name" autoComplete="off" spellCheck={false} placeholder="STRIPE_SECRET_KEY" className="font-mono" />}
    </form.AppField>}
    <form.AppField name="value" validators={{ onChange: ({ value }) => (!row && !value ? 'Enter a value.' : undefined) }}>
      {field => <field.TextField label="Value" type={secret ? 'password' : 'text'} autoComplete="off" spellCheck={false}
        placeholder={row ? (row.secret ? SECRET_KEPT : 'leave empty to keep') : ''} className={secret ? undefined : 'font-mono text-[12.5px]'} />}
    </form.AppField>
    <form.AppField name="secret">{field => <field.SwitchField label="Secret" />}</form.AppField>
    <MoreOptions open={unusual}>
      <form.AppField name="audience">{field => <field.SelectField label="Which sandboxes get it">
        {Object.entries(AUDIENCE).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
      </field.SelectField>}</form.AppField>
      <form.AppField name="agent">{field => <field.SwitchField label="The coding agent may read it" />}</form.AppField>
      {admin && !row && <form.AppField name="mine">{field => <field.SwitchField label="Only my sandboxes" />}</form.AppField>}
    </MoreOptions>
    <div className="flex justify-end gap-2"><Button variant="ghost" onClick={onDone}>Cancel</Button><SubmitButton form={form}>{row ? 'Save' : 'Add variable'}</SubmitButton></div>
  </AppForm>
}

function ImportForm({ repo, admin, onDone }: { repo: string; admin: boolean; onDone: () => void }) {
  const run = useAction((v: { text: string; audience: Audience; mine: boolean }) => api.env.import(repo, { ...v, agent: false, restart: true }), keys.env(repo), keys.readiness(repo))
  const form = useAppForm({
    defaultValues: { text: '', audience: 'all' as Audience, mine: !admin },
    onSubmit: async ({ value }) => { await run.mutateAsync(value); form.reset(); onDone() },
  })
  return <AppForm form={form} className="space-y-4">
    <form.AppField name="text" validators={{ onChange: ({ value }) => /^\s*(export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*\S/m.test(value) ? undefined : 'Paste KEY=value lines.' }}>
      {field => <field.TextareaField label=".env contents" rows={8} placeholder={'DATABASE_URL=postgres://…\nSTRIPE_SECRET_KEY=sk_test_…'} className="font-mono text-[12px]" />}
    </form.AppField>
    <MoreOptions>
      <form.AppField name="audience">{field => <field.SelectField label="Which sandboxes get them">
        {Object.entries(AUDIENCE).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
      </field.SelectField>}</form.AppField>
      {admin && <form.AppField name="mine">{field => <field.SwitchField label="Only my sandboxes" />}</form.AppField>}
    </MoreOptions>
    <div className="flex justify-end gap-2"><Button variant="ghost" onClick={onDone}>Cancel</Button><SubmitButton form={form}>Import</SubmitButton></div>
  </AppForm>
}
