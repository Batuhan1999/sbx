import { useQueryClient } from '@tanstack/react-query'
import { ArrowUpRight, Settings2, ShieldCheck } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { toast } from 'sonner'
import { api } from '@/api/endpoints'
import { keys, useAction } from '@/api/queries'
import type { Check, Connection, ConnectionField, ConnectionState, Rebuilds } from '@/api/types'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { AppForm, MoreOptions, SECRET_KEPT, SubmitButton, useAppForm, useStore, type FieldControls } from '@/components/form'
import { DocsLink } from '@/components/PackageLogo'
import { DetailText, ErrorState } from '@/components/states'
import { AnchorButton, Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Dialog } from '@/components/ui/dialog'
import type { Tone } from '@/lib/status'
import { cn } from '@/lib/utils'
import { domainError, emailError, numberError, urlError } from '@/lib/validation'

export const CONNECTION_STATE: Record<ConnectionState, { label: string; tone: Tone }> = {
  connected: { label: 'Connected', tone: 'ok' },
  attention: { label: 'Needs attention', tone: 'warn' },
  not_connected: { label: 'Not connected', tone: 'idle' },
  off: { label: 'Off', tone: 'idle' },
}

/** A connection's (or runtime's) state, quiet when all is well: a small muted dot and label; only
 * a state that needs action takes the warning color. */
export function ConnectionMark({ state }: { state: ConnectionState | string }) {
  const s = CONNECTION_STATE[state as ConnectionState] ?? { label: state, tone: 'idle' as Tone }
  return <span className={cn('inline-flex items-center gap-1.5 text-[12px]', s.tone === 'warn' ? 'font-medium text-warn' : 'text-muted')}>
    <span aria-hidden className={cn('size-1.5 shrink-0 rounded-full', s.tone === 'ok' ? 'bg-ok/60' : s.tone === 'warn' ? 'bg-warn' : 'bg-faint')} />
    {s.label}
  </span>
}

function CheckList({ checks }: { checks: Check[] }) {
  return <ul className="space-y-1.5 text-[12.5px]">
    {checks.map(c => <li key={c.name} className="flex gap-2">
      <span className={cn('mt-1.5 size-1.5 shrink-0 rounded-full', c.ok ? 'bg-ok' : 'bg-bad')} aria-hidden />
      <span><span className="font-medium">{c.name}</span> <DetailText className="text-muted" text={c.detail} />{!c.ok && c.fix && <span className="block text-muted">Fix: {c.fix}</span>}{!c.ok && c.help_url && <span className="block"><DocsLink href={c.help_url}>How to set it up</DocsLink></span>}</span>
      <span className="sr-only">{c.ok ? 'passed' : 'failed'}</span>
    </li>)}
  </ul>
}

/** Checks, each with its result; failedFirst: the failed ones (with their fix), the passed ones folded under a count. */
export function Checks({ checks, failedFirst }: { checks: Check[]; failedFirst?: boolean }) {
  if (!failedFirst) return <CheckList checks={checks} />
  const failed = checks.filter(c => !c.ok)
  const passed = checks.filter(c => c.ok)
  return <div className="space-y-2">
    {failed.length > 0 && <CheckList checks={failed} />}
    {passed.length > 0 && <Collapsible>
      <CollapsibleTrigger className="w-auto text-[12.5px]">{failed.length ? `${passed.length} passed` : `All ${passed.length} passed`}</CollapsibleTrigger>
      <CollapsibleContent><div className="mt-2"><CheckList checks={passed} /></div></CollapsibleContent>
    </Collapsible>}
  </div>
}

/** The fields a form shows: not the API-only ones. */
export const formFields = (c: Connection) => c.fields.filter(f => !f.hidden)

export function ConnectionCard({ c, icon, children }: { c: Connection; icon?: ReactNode; children?: ReactNode }) {
  const [open, setOpen] = useState(false)
  const [checks, setChecks] = useState<Check[] | null>(null)
  const test = useAction(async () => setChecks((await api.connections.test(c.kind)).checks), keys.connections)
  // Nothing a person changes (Codex's version is pinned by sbx): no Configure, the card shows its state.
  const fields = formFields(c)
  const canConnect = c.actions.includes('connect') && fields.length > 0
  const installUrl = typeof c.facts.install_url === 'string' ? c.facts.install_url : ''
  return <article className="rounded-lg border border-line bg-panel">
    <div className="flex flex-wrap items-start gap-x-3 gap-y-2 p-4">
      {icon && <span className="grid size-8 shrink-0 place-items-center rounded-md border border-line bg-sunken text-muted">{icon}</span>}
      <div className="min-w-0 grow basis-56">
        <div className="flex flex-wrap items-center gap-2"><h3 className="font-semibold">{c.title}</h3><ConnectionMark state={c.state} /></div>
        <p className="mt-0.5 break-words text-[13px] text-muted">{c.summary}</p>
        {c.display.length > 0 && <p className="mt-1 break-words text-[12px] text-muted">{c.display.map(d => `${d.label}: ${d.value}`).join(' · ')}</p>}
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        {installUrl && c.state === 'connected' && <AnchorButton size="sm" href={installUrl} target="_blank" rel="noreferrer">Install on repositories<ArrowUpRight /></AnchorButton>}
        {c.actions.includes('test') && c.state !== 'not_connected' && c.state !== 'off' && <Button size="sm" busy={test.isPending} onClick={() => test.mutate()}><ShieldCheck />Test</Button>}
        {canConnect && <Button size="sm" variant={c.state === 'not_connected' ? 'primary' : 'secondary'} onClick={() => setOpen(true)}><Settings2 />{c.state === 'not_connected' ? 'Connect' : 'Configure'}</Button>}
      </div>
    </div>
    {(checks || test.error || children) && <div className="space-y-3 border-t border-line px-4 py-3">
      {checks && <Checks checks={checks} />}
      <ErrorState error={test.error} compact />
      {children}
    </div>}
    {canConnect && <Dialog open={open} onOpenChange={setOpen} title={`${c.title}`} wide={fields.filter(f => !f.advanced).length > 2}>
      <ConnectionForm c={c} onDone={() => setOpen(false)} />
    </Dialog>}
  </article>
}

/** What a change to an extension rebuilds, in the server's words; '' when no template changes. */
export function rebuildNote(r?: Rebuilds) {
  return r && Object.keys(r.runtimes).length > 0 ? r.message : ''
}

/** A field's value as the form starts it: a secret empty (write-only), a yes/no as "true"/"false". */
const initialValue = (f: ConnectionField) => f.secret ? '' : f.value ?? (f.kind === 'bool' ? 'false' : '')

/** Whether a field shows, given what the others hold now (its `when`). */
const shownWith = (f: ConnectionField, values: Record<string, string>) =>
  Object.entries(f.when ?? {}).every(([name, allowed]) => allowed.includes(values[name] ?? ''))

/** What is wrong with a field's value, mirroring the server's own check (it checks again). */
function fieldError(f: ConnectionField, value: string) {
  const v = (value ?? '').trim()
  if (!v) return undefined
  if (f.kind === 'email') return emailError(v)
  if (f.kind === 'url') return urlError(v)
  if (f.kind === 'hostname') return domainError(v)
  if (f.kind === 'int') return numberError(v, { min: f.min, max: f.max, integer: true })
  return undefined
}

/** One connection field as its kind reads: a list for choices, a switch for yes or no, a number, a key file's lines,
 * an email or URL, a hidden field for a secret (empty keeps a saved one). */
export function ConnectionFieldControl({ field, f }: { field: FieldControls; f: ConnectionField }) {
  const common = { label: f.label, help: f.help || undefined, optional: f.optional, autoComplete: 'off', spellCheck: false }
  const placeholder = f.secret && f.set ? SECRET_KEPT : f.placeholder || undefined
  if (f.kind === 'choice' && f.options?.length) return <field.SelectField label={f.label} help={f.help || undefined}>
    {f.options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
  </field.SelectField>
  if (f.kind === 'bool') return <field.ToggleField label={f.label} help={f.help || undefined} />
  if (f.kind === 'int') return <field.NumberField {...common} min={f.min ?? undefined} max={f.max ?? undefined} step={f.step ?? 1}
    unit={f.unit || undefined} placeholder={placeholder} />
  if (f.kind === 'multiline') return <field.TextareaField {...common} rows={4} placeholder={placeholder} className="font-mono text-[12px]" />
  return <field.TextField {...common} placeholder={placeholder}
    type={f.secret ? 'password' : f.kind === 'email' ? 'email' : f.kind === 'url' ? 'url' : 'text'}
    className={f.secret || f.kind === 'email' ? undefined : 'font-mono text-[12.5px]'} />
}

export function ConnectionForm({ c, onDone }: { c: Connection; onDone: () => void }) {
  const [note, setNote] = useState('')
  const [rebuilds, setRebuilds] = useState('')
  const qc = useQueryClient()
  const fields = formFields(c)
  const initial = Object.fromEntries(fields.map(f => [f.name, initialValue(f)])) as Record<string, string>
  const save = useAction((values: Record<string, string>) => api.connections.save(c.kind, values), keys.connections, keys.me, keys.setup, keys.settings)
  const form = useAppForm({
    defaultValues: initial,
    onSubmit: async ({ value }) => {
      // Only what changed: a secret typed now, a value edited. What is left alone stays as saved.
      const sent = Object.fromEntries(Object.entries(value).filter(([k, v]) => {
        const f = fields.find(x => x.name === k)
        return f && shownWith(f, value) && (f.secret ? v.trim() !== '' : v !== initial[k])
      }))
      const result = await save.mutateAsync(sent)
      const rebuilt = rebuildNote(result.rebuilds)
      setRebuilds(rebuilt)
      if (result.facts.generated_password) setNote(`Generated password (shown once): ${result.facts.generated_password}`)
      else if (!rebuilt) onDone()
    },
  })
  const values = useStore(form.store, s => s.values)
  const shown = fields.filter(f => shownWith(f, values))
  const main = shown.filter(f => !f.advanced)
  const advanced = shown.filter(f => f.advanced)
  const wide = (f: ConnectionField) => f.kind === 'multiline' || f.kind === 'choice' || !!f.help
  const render = (f: ConnectionField) => <div key={f.name} className={main.length > 1 && wide(f) ? 'sm:col-span-2' : undefined}>
    <form.AppField name={f.name} validators={{ onChange: ({ value }) => fieldError(f, value) }}>
      {field => <ConnectionFieldControl field={field} f={f} />}
    </form.AppField>
  </div>
  return <AppForm form={form} className="space-y-4">
    <div className={main.length > 1 ? 'grid gap-3 sm:grid-cols-2' : 'grid gap-3'}>{main.map(render)}</div>
    {advanced.length > 0 && <MoreOptions open={advanced.some(f => !f.secret && (initial[f.name] ?? '') !== '' && f.kind !== 'int')}>
      <div className="grid gap-3">{advanced.map(render)}</div>
    </MoreOptions>}
    {note && <p role="status" className="rounded-md bg-ok-soft px-3 py-2 font-mono text-[12.5px] text-ok">{note}</p>}
    {rebuilds && <p role="status" className="rounded-md border border-line bg-sunken px-3 py-2 text-[12.5px]">Saved. {rebuilds}</p>}
    <div className="flex flex-wrap items-center justify-between gap-2">
      {c.actions.includes('disconnect') && c.state !== 'not_connected' ? <ConfirmDialog trigger={<Button variant="danger" size="sm">Disconnect</Button>}
        title={`Disconnect ${c.title}?`} description="Its saved credentials are removed from this server." confirm="Disconnect"
        onConfirm={async () => {
          const result = await api.connections.disconnect(c.kind)
          const rebuilt = rebuildNote(result.rebuilds)
          if (rebuilt) toast(`${c.title} disconnected`, { description: rebuilt })
          await qc.invalidateQueries({ queryKey: keys.connections })
          onDone()
        }} /> : <span />}
      <div className="flex gap-2"><Button variant="ghost" onClick={onDone}>{note || rebuilds ? 'Done' : 'Cancel'}</Button><SubmitButton form={form}>Test and save</SubmitButton></div>
    </div>
  </AppForm>
}
