import { useQueryClient } from '@tanstack/react-query'
import { ArrowUpRight, Settings2, ShieldCheck } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { api } from '@/api/endpoints'
import { keys, useAction } from '@/api/queries'
import type { Check, Connection, ConnectionState } from '@/api/types'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { AppForm, SubmitButton, useAppForm } from '@/components/form'
import { ErrorState } from '@/components/states'
import { StateBadge } from '@/components/StatusBadge'
import { AnchorButton, Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import type { Tone } from '@/lib/status'
import { cn } from '@/lib/utils'

export const CONNECTION_STATE: Record<ConnectionState, { label: string; tone: Tone }> = {
  connected: { label: 'Connected', tone: 'ok' },
  attention: { label: 'Needs attention', tone: 'warn' },
  not_connected: { label: 'Not connected', tone: 'idle' },
  off: { label: 'Off', tone: 'idle' },
}

export function Checks({ checks }: { checks: Check[] }) {
  return <ul className="space-y-1.5 text-[12.5px]">
    {checks.map(c => <li key={c.name} className="flex gap-2">
      <span className={cn('mt-1.5 size-1.5 shrink-0 rounded-full', c.ok ? 'bg-ok' : 'bg-bad')} aria-hidden />
      <span><span className="font-medium">{c.name}</span> <span className="text-muted">{c.detail}</span>{!c.ok && c.fix && <span className="block text-muted">Fix: {c.fix}</span>}</span>
      <span className="sr-only">{c.ok ? 'passed' : 'failed'}</span>
    </li>)}
  </ul>
}

export function ConnectionCard({ c, icon, children }: { c: Connection; icon?: ReactNode; children?: ReactNode }) {
  const [open, setOpen] = useState(false)
  const [checks, setChecks] = useState<Check[] | null>(null)
  const test = useAction(async () => setChecks((await api.connections.test(c.kind)).checks), keys.connections)
  const state = CONNECTION_STATE[c.state] ?? { label: c.state, tone: 'idle' as Tone }
  const canConnect = c.actions.includes('connect') && c.fields.length > 0
  const installUrl = typeof c.facts.install_url === 'string' ? c.facts.install_url : ''
  return <article className="rounded-lg border border-line bg-panel">
    <div className="flex flex-wrap items-start gap-x-3 gap-y-2 p-4">
      {icon && <span className="grid size-8 shrink-0 place-items-center rounded-md border border-line bg-sunken text-muted">{icon}</span>}
      <div className="min-w-0 grow basis-56">
        <div className="flex flex-wrap items-center gap-2"><h3 className="font-semibold">{c.title}</h3><StateBadge tone={state.tone} label={state.label} /></div>
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
    <Dialog open={open} onOpenChange={setOpen} title={`${c.title}`} description="Tested before anything is saved; nothing is written when a check fails." wide>
      <ConnectionForm c={c} onDone={() => setOpen(false)} />
    </Dialog>
  </article>
}

export function ConnectionForm({ c, onDone }: { c: Connection; onDone: () => void }) {
  const [note, setNote] = useState('')
  const qc = useQueryClient()
  const save = useAction((values: Record<string, string>) => api.connections.save(c.kind, values), keys.connections, keys.me, keys.setup, keys.settings)
  const form = useAppForm({
    defaultValues: Object.fromEntries(c.fields.map(f => [f.name, f.secret ? '' : f.value ?? ''])) as Record<string, string>,
    onSubmit: async ({ value }) => {
      const sent = Object.fromEntries(Object.entries(value).filter(([k, v]) => !(c.fields.find(f => f.name === k)?.secret && v === '')))
      const result = await save.mutateAsync(sent)
      if (result.facts.generated_password) setNote(`Generated password (shown once): ${result.facts.generated_password}`)
      else onDone()
    },
  })
  return <AppForm form={form} className="space-y-4">
    <div className="grid gap-3 sm:grid-cols-2">
      {c.fields.map(f => <form.AppField key={f.name} name={f.name}>
        {field => f.value === 'true' || f.value === 'false'
          ? <field.SelectField label={f.label} help={f.help}><option value="true">Yes</option><option value="false">No</option></field.SelectField>
          : <field.TextField label={f.label} help={f.help} type={f.secret ? 'password' : 'text'} placeholder={f.secret && f.set ? 'set · empty keeps it' : ''}
            autoComplete="off" spellCheck={false} className={f.secret ? undefined : 'font-mono text-[12.5px]'} />}
      </form.AppField>)}
    </div>
    {note && <p role="status" className="rounded-md bg-ok-soft px-3 py-2 font-mono text-[12.5px] text-ok">{note}</p>}
    <div className="flex flex-wrap items-center justify-between gap-2">
      {c.actions.includes('disconnect') && c.state !== 'not_connected' ? <ConfirmDialog trigger={<Button variant="danger" size="sm">Disconnect</Button>}
        title={`Disconnect ${c.title}?`} description="Its saved credentials are removed from this server." confirm="Disconnect"
        onConfirm={async () => { await api.connections.disconnect(c.kind); await qc.invalidateQueries({ queryKey: keys.connections }); onDone() }} /> : <span />}
      <div className="flex gap-2"><Button variant="ghost" onClick={onDone}>{note ? 'Done' : 'Cancel'}</Button><SubmitButton form={form}>Test and save</SubmitButton></div>
    </div>
  </AppForm>
}
