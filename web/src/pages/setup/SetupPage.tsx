import { useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ArrowRight, Check, Globe, Server } from 'lucide-react'
import { useEffect, useState } from 'react'
import { setSetupToken, setupToken } from '@/api/client'
import { api } from '@/api/endpoints'
import { keys, useAvailableRuntimes, useRuntimes, useSetup, useSetupChecks } from '@/api/queries'
import type { SandboxRuntime, SetupState } from '@/api/types'
import { CopyCommand } from '@/components/CopyCommand'
import { AppForm, SubmitButton, useAppForm, useStore } from '@/components/form'
import { ErrorState, Loading } from '@/components/states'
import { AnchorButton, Button, LinkButton } from '@/components/ui/button'
import { Facts, Spinner } from '@/components/ui/primitives'
import { RadioCard, RadioGroup } from '@/components/ui/radio-group'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { portsHint } from '@/lib/format'
import { cn } from '@/lib/utils'
import { domainError, emailError, passwordError, required } from '@/lib/validation'
import { Checks } from '../extensions/ConnectionCard'
import { ChangeStatus, RuntimeSettingsForm, useRuntimeChangeFlow } from '../settings/RuntimeForms'
import { blockedText, RuntimeChoices, RuntimeList } from '../settings/Runtimes'

type Step = 'address' | 'runtime' | 'admin'

function goToLink(s: SetupState) {
  const target = new URL(s.link)
  if (target.origin === location.origin) return false
  const token = setupToken()
  target.pathname = '/app/setup'
  target.hash = token ? `t=${token}` : ''
  location.assign(target.href)
  return true
}

export default function SetupPage() {
  const setup = useSetup()
  const qc = useQueryClient()
  const [step, setStep] = useState<Step | null>(null)
  const s = setup.data
  useEffect(() => {
    if (s?.complete) { setSetupToken(null); qc.invalidateQueries({ queryKey: keys.me }) }
  }, [s?.complete, qc])
  const addressDone = !!s?.steps.find(x => x.step === 'address')?.done
  const runtimeDone = !!s?.steps.find(x => x.step === 'runtime')?.done
  const current: Step = step ?? (!addressDone ? 'address' : !runtimeDone ? 'runtime' : 'admin')
  useEffect(() => {   // a runtime was added: the lists name it
    if (runtimeDone) void qc.invalidateQueries({ queryKey: keys.runtimes })
  }, [runtimeDone, qc])
  return <div className="flex min-h-dvh flex-col">
    <header className="flex h-14 items-center justify-between border-b border-line bg-panel px-4 sm:px-8">
      <div className="flex items-center gap-2"><span aria-hidden className="grid size-6 place-items-center rounded-md bg-ink text-[11px] font-bold text-bg">sb</span><span className="font-semibold">sbx</span></div>
      <span className="text-[12.5px] text-muted">Server setup{s?.version ? ` · ${s.version}` : ''}</span>
    </header>
    <main className="mx-auto w-full max-w-[640px] px-4 pb-16 pt-8 sm:px-6 sm:pt-12">
      {setup.isPending ? <Loading label="Checking this installation…" />
        : setup.error ? <NoToken error={setup.error} retry={() => setup.refetch()} />
        : s!.complete ? <Done s={s!} />
        : <>
          <ol aria-label="Setup progress" className="mb-8 flex items-center gap-3 text-[13px]">
            <StepPill n={1} label="Server address" active={current === 'address'} done={addressDone} onClick={() => setStep('address')} />
            <span aria-hidden className="h-px w-8 bg-line" />
            <StepPill n={2} label="Sandbox runtime" active={current === 'runtime'} done={runtimeDone} onClick={addressDone ? () => setStep('runtime') : undefined} />
            <span aria-hidden className="h-px w-8 bg-line" />
            <StepPill n={3} label="Admin access" active={current === 'admin'} done={s!.signed_in} onClick={addressDone && runtimeDone ? () => setStep('admin') : undefined} />
          </ol>
          {current === 'address' ? <AddressStep s={s!} onNext={() => setStep(runtimeDone ? 'admin' : 'runtime')} />
            : current === 'runtime' ? <RuntimeStep done={runtimeDone} onBack={() => setStep('address')} onNext={() => setStep('admin')} />
            : <AdminStep s={s!} runtimeDone={runtimeDone} onBack={() => setStep('runtime')} />}
        </>}
    </main>
  </div>
}

function StepPill({ n, label, active, done, onClick }: { n: number; label: string; active: boolean; done: boolean; onClick?: () => void }) {
  return <li><button type="button" disabled={!onClick} onClick={onClick} aria-current={active ? 'step' : undefined}
    className={cn('inline-flex items-center gap-2 disabled:cursor-default', active ? 'font-medium text-ink' : 'text-muted')}>
    <span className={cn('grid size-5 place-items-center rounded-full text-[11px]', done ? 'bg-ok text-white dark:text-black' : active ? 'bg-ink text-bg' : 'border border-line-strong')}>{done ? <Check className="size-3" /> : n}</span>{label}
  </button></li>
}

function NoToken({ error, retry }: { error: unknown; retry: () => void }) {
  return <div className="space-y-5">
    <h1 className="text-[22px] font-semibold tracking-tight">Open your setup link</h1>
    <p className="text-muted">The installer printed a one-time link. To get a new one, run this on the server:</p>
    <CopyCommand command="sudo sbx-admin setup-link" />
    <ErrorState error={error} compact />
    <div className="flex flex-wrap gap-2"><Button onClick={retry}>Try again</Button><AnchorButton variant="ghost" href="/auth/login?next=%2Fapp%2Fsetup">Sign in as an admin</AnchorButton></div>
  </div>
}

function HostFacts({ s }: { s: SetupState }) {
  return <div className="flex items-start gap-3 rounded-lg border border-line bg-panel p-3">
    <Server className="mt-0.5 size-4 shrink-0 text-muted" />
    <div className="min-w-0 text-[13px]">
      <div className="text-muted">This server's public IP</div>
      <div className="break-all font-mono">{s.host.address || 'unknown'}</div>
    </div>
  </div>
}

function AddressStep({ s, onNext }: { s: SetupState; onNext: () => void }) {
  const qc = useQueryClient()
  const [waiting, setWaiting] = useState(false)
  const form = useAppForm({
    defaultValues: { mode: s.access.domain ? 'domain' : 'ip', domain: s.access.domain, email: s.access.certificate_email, acme: s.access.domain ? s.access.certificate || 'delegated' : 'delegated', cloudflare_api_token: '', aws_access_key_id: '', aws_secret_access_key: '' },
    onSubmit: async ({ value }) => {
      if (value.mode === 'ip') {
        const result = await api.setup.address(s.host.address)
        qc.setQueryData(keys.setup, result)
        if (result.steps.find(x => x.step === 'address')?.done) onNext()
        else setWaiting(true)
      } else {
        const credentials: Record<string, string> = value.acme === 'cloudflare' ? { cloudflare_api_token: value.cloudflare_api_token }
          : value.acme === 'route53' ? { aws_access_key_id: value.aws_access_key_id, aws_secret_access_key: value.aws_secret_access_key } : {}
        await api.connections.save('domain', { name: value.domain.trim(), address: s.host.address, acme: value.acme, acme_email: value.email.trim(), ...credentials })
        await qc.invalidateQueries({ queryKey: keys.setup })
        setWaiting(true)
      }
    },
  })
  const { mode, domain, acme } = useStore(form.store, st => st.values)
  useEffect(() => {
    if (waiting && s.steps.find(x => x.step === 'address')?.done && !goToLink(s)) onNext()
  }, [waiting, s, onNext])
  const name = domain || 'sbx.example.com'
  const records = [['A', name, s.host.address], ['A', `*.${name}`, s.host.address], ...(acme === 'delegated' ? [['NS', `_acme-challenge.${name}`, `${name}.`]] : [])]
  return <AppForm form={form} className="space-y-6">
    <div>
      <h1 className="text-[24px] font-semibold tracking-tight">Where will this server answer?</h1>
      <p className="mt-1.5 text-muted">Sandboxes get preview URLs under this address, with a trusted HTTPS certificate that renews itself.</p>
    </div>
    <HostFacts s={s} />
    <RadioGroup aria-label="Address" value={mode} onValueChange={v => { form.setFieldValue('mode', v as typeof mode); setWaiting(false) }} className="sm:grid-cols-2">
      {([['ip', 'This server’s IP', 'No domain needed. Previews get a port each.', Server], ['domain', 'A domain', 'Named preview URLs under your own domain.', Globe]] as const).map(([id, title, text, Icon]) =>
        <RadioCard key={id} value={id} disabled={!!s.access.domain && id === 'ip'} description={text}
          title={<span className="flex items-center gap-1.5"><Icon className="size-3.5 text-muted" />{title}</span>} />)}
    </RadioGroup>
    {mode === 'domain' && <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <form.AppField name="domain" validators={{ onChange: ({ value }) => domainError(value) }}>{field => <field.TextField label="Domain" placeholder="sbx.example.com" autoComplete="off" spellCheck={false} />}</form.AppField>
        <form.AppField name="email" validators={{ onChange: ({ value }) => emailError(value) }}>{field => <field.TextField label="Certificate email" type="email" autoComplete="email" help="For certificate expiry notices." />}</form.AppField>
      </div>
      <form.AppField name="acme">{field => <field.SelectField label="Certificate verification">
        <option value="delegated">DNS delegation (no API credentials)</option><option value="cloudflare">Cloudflare API</option><option value="route53">Amazon Route 53</option>
      </field.SelectField>}</form.AppField>
      {acme === 'cloudflare' && <form.AppField name="cloudflare_api_token" validators={{ onChange: ({ value }) => required(value) }}>{field => <field.TextField label="Cloudflare API token" type="password" autoComplete="off" />}</form.AppField>}
      {acme === 'route53' && <div className="grid gap-3 sm:grid-cols-2">
        <form.AppField name="aws_access_key_id">{field => <field.TextField label="AWS access key ID" optional autoComplete="off" help="Empty uses the server's instance role." />}</form.AppField>
        <form.AppField name="aws_secret_access_key">{field => <field.TextField label="AWS secret access key" optional type="password" autoComplete="off" />}</form.AppField>
      </div>}
      <div>
        <p className="mb-1.5 text-[12.5px] font-medium">DNS records to add</p>
        <Table className="text-[12px]">
          <TableHeader><TableRow className="hover:bg-transparent"><TableHead className="h-8 px-3">Type</TableHead><TableHead className="h-8 px-3">Name</TableHead><TableHead className="h-8 px-3">Value</TableHead></TableRow></TableHeader>
          <TableBody>{records.map(([t, n, v]) => <TableRow key={t + n} className="hover:bg-transparent"><TableCell className="px-3 py-2">{t}</TableCell><TableCell className="break-all px-3 py-2 font-mono">{n}</TableCell><TableCell className="break-all px-3 py-2 font-mono">{v}</TableCell></TableRow>)}</TableBody>
        </Table>
      </div>
    </div>}
    {waiting && !s.access.certificate_error && <div role="status" className="rounded-lg border border-line bg-panel px-4 py-3 text-[13px]"><Spinner label="Getting the certificate…" /><p className="mt-1 text-muted">This page continues on its own when HTTPS works{mode === 'domain' ? ', on the new address' : ''}.{portsHint(s.access.needs_ports) && ` It needs ${portsHint(s.access.needs_ports)} open.`}</p></div>}
    {s.access.certificate_error && <ErrorState error={`HTTPS is not ready: ${s.access.certificate_error}.${portsHint(s.access.needs_ports) ? ` Make sure this server accepts ${portsHint(s.access.needs_ports)}, in any cloud firewall too, then retry.` : ' Retry once it is fixed.'}`} />}
    <div className="flex justify-end border-t border-line pt-5">
      <SubmitButton form={form} disabled={mode === 'ip' && !s.host.address}>{waiting || s.access.certificate_error ? 'Retry' : 'Continue'}<ArrowRight /></SubmitButton>
    </div>
  </AppForm>
}

/** Where sandboxes run: with none enabled, the runtimes to choose from and the chosen one's settings;
 * adding it is applied by the host (sbx-control restarts) and this step follows it. With one or more, read-only. */
function RuntimeStep({ done, onBack, onNext }: { done: boolean; onBack: () => void; onNext: () => void }) {
  const runtimes = useRuntimes()
  const available = useAvailableRuntimes(!done)
  const flow = useRuntimeChangeFlow()
  const choices = (available.data ?? []).filter(a => !a.enabled)
  const [kind, setKind] = useState('')
  const selectable = choices.filter(c => !c.host && c.can_run)
  const chosen = choices.find(c => c.kind === (kind || selectable[0]?.kind))
  return <div className="space-y-6">
    <div>
      <h1 className="text-[24px] font-semibold tracking-tight">Where will sandboxes run?</h1>
      <p className="mt-1.5 text-muted">{done ? 'You can change this later in Settings.' : 'Choose a runtime for your sandboxes.'}</p>
    </div>
    {done ? <RuntimeList runtimes={runtimes.data?.runtimes ?? []} className="rounded-lg border border-line bg-panel" />
      : available.isPending ? <Spinner label="Loading…" />
      : available.error ? <ErrorState error={available.error} compact retry={() => available.refetch()} />
      : <>
        <RuntimeChoices choices={choices} value={chosen?.kind ?? ''} onChange={setKind} />
        <ChangeStatus flow={flow} />
        {chosen && (chosen.edit_blocker ? !flow.busy && <div className="space-y-2">
          <ErrorState compact error={blockedText(chosen.edit_blocker, chosen.kind)} />
          {chosen.edit_blocker.code === 'needs_domain' && <Button size="sm" onClick={onBack}><Globe />Choose a domain</Button>}
        </div>
          // Hidden, not unmounted, while the host applies it: a change that fails keeps what was typed.
          : <div hidden={flow.busy}><RuntimeSettingsForm key={chosen.kind} runtime={chosen} mode="add" flow={flow} /></div>)}
        {!chosen && <p className="text-[13px] text-muted">None can run here. Rerun install.sh on a server with /dev/kvm.</p>}
      </>}
    <div className="flex justify-between gap-3 border-t border-line pt-5">
      <Button variant="ghost" onClick={onBack}><ArrowLeft />Back</Button>
      <Button variant="primary" disabled={!done} onClick={onNext}>Continue<ArrowRight /></Button>
    </div>
  </div>
}

function AdminStep({ s, runtimeDone, onBack }: { s: SetupState; runtimeDone: boolean; onBack: () => void }) {
  const qc = useQueryClient()
  const checks = useSetupChecks()
  const runtimes = useRuntimes()
  const existing = !!s.steps.find(x => x.step === 'sign_in')?.done  // someone can sign in already
  const ready = s.signed_in || s.can_complete
  const finish = async (admin?: { email: string; password: string }) => {
    if (admin) await api.setup.admin(admin.email.trim(), admin.password)
    const result = await api.setup.complete()
    setSetupToken(null)
    qc.setQueryData(keys.setup, result)
  }
  const form = useAppForm({
    defaultValues: { email: '', password: '', confirm: '' },
    onSubmit: async ({ value }) => { await finish(value); form.reset() },
  })
  const submitting = useStore(form.store, st => st.isSubmitting)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const blocked = checks.isPending || !!checks.error || checks.data?.ok === false || !runtimeDone
  return <div className="space-y-6">
    <div>
      <h1 className="text-[24px] font-semibold tracking-tight">Who runs this server?</h1>
      <p className="mt-1.5 text-muted">{s.signed_in ? `Signed in as ${s.email}. Finish to start connecting repositories.`
        : ready ? 'Access is already configured for this private network. Finish to start connecting repositories.'
        : existing ? 'Admin access is configured. Sign in to finish.'
        : 'The first admin signs in with an email and password. You can switch to single sign-on later in Settings.'}</p>
    </div>
    {!ready && !existing && <AppForm form={form} id="setup-admin" className="space-y-4">
      <form.AppField name="email" validators={{ onChange: ({ value }) => emailError(value) }}>{field => <field.TextField label="Admin email" type="email" autoComplete="username" />}</form.AppField>
      <div className="grid gap-3 sm:grid-cols-2">
        <form.AppField name="password" validators={{ onChange: ({ value }) => passwordError(value) }}>{field => <field.TextField label="Password" type="password" autoComplete="new-password" help="At least 8 characters." />}</form.AppField>
        <form.AppField name="confirm" validators={{ onChangeListenTo: ['password'], onChange: ({ value, fieldApi }) => !value ? 'Repeat the password.' : value !== fieldApi.form.getFieldValue('password') ? 'The passwords differ.' : undefined }}>
          {field => <field.TextField label="Repeat password" type="password" autoComplete="new-password" />}
        </form.AppField>
      </div>
    </AppForm>}
    {!ready && existing && <AnchorButton variant="primary" href="/auth/login?next=%2Fapp%2Fsetup">Sign in to continue<ArrowRight /></AnchorButton>}
    <ServerChecks checks={checks} runtimes={runtimes.data?.runtimes ?? []} />
    {ready && <ErrorState error={error} compact />}
    <div className="flex justify-between gap-3 border-t border-line pt-5">
      <Button variant="ghost" onClick={onBack}><ArrowLeft />Back</Button>
      {!ready && !existing && <Button variant="primary" type="submit" form="setup-admin" busy={submitting} disabled={blocked}>Finish setup<ArrowRight /></Button>}
      {ready && <Button variant="primary" busy={busy} disabled={blocked} onClick={async () => { setBusy(true); setError(null); try { await finish() } catch (e) { setError(e) } finally { setBusy(false) } }}>Finish setup<ArrowRight /></Button>}
    </div>
    <p className="text-[12px] text-muted">Finishing disables the one-time setup link.</p>
  </div>
}

/** The server's checks; each runtime's own checks under that runtime when the runtimes could be read, else one list as before. */
function ServerChecks({ checks, runtimes }: { checks: ReturnType<typeof useSetupChecks>; runtimes: SandboxRuntime[] }) {
  const all = checks.data?.checks ?? []
  const kinds = new Set(runtimes.map(r => r.kind))
  const own = all.filter(c => !c.runtime || !kinds.has(c.runtime))
  return <>
    {(!checks.data || own.length > 0) && <div className="space-y-2">
      <p className="text-[12.5px] font-medium">Server checks</p>
      {checks.isPending ? <Spinner label="Checking…" /> : checks.error ? <ErrorState error={checks.error} compact retry={() => checks.refetch()} /> : <Checks checks={own} />}
    </div>}
    {runtimes.length > 0 && <div className="space-y-2">
      <p className="text-[12.5px] font-medium">Sandbox runtimes</p>
      <RuntimeList runtimes={runtimes} className="rounded-lg border border-line bg-panel">{r => {
        const mine = all.filter(c => c.runtime === r.kind)
        return mine.length > 0 && <div className="mt-2"><Checks checks={mine} /></div>
      }}</RuntimeList>
    </div>}
    {checks.data && !checks.data.ok && <Button size="sm" busy={checks.isFetching} onClick={() => checks.refetch()}>Check again</Button>}
  </>
}

function Done({ s }: { s: SetupState }) {
  return <div className="space-y-6">
    <span className="grid size-10 place-items-center rounded-full bg-ok-soft text-ok"><Check className="size-5" /></span>
    <div>
      <h1 className="text-[24px] font-semibold tracking-tight">The server is ready</h1>
      <p className="mt-1.5 text-muted">Next: connect GitHub and a repository, then hand its setup to your coding agent.</p>
    </div>
    <Facts className="rounded-lg border border-line bg-panel p-4" items={[['Address', <span className="break-all font-mono text-[12.5px]">{s.access.url}</span>], ['Admin', s.email ?? s.admins.join(', ')], ['Setup link', 'Disabled']]} />
    <LinkButton variant="primary" to="/app/repos/new">Connect a repository<ArrowRight /></LinkButton>
  </div>
}
