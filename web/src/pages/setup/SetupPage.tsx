import { useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ArrowRight, Check, Copy, Github, Globe, Server } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router'
import { ApiError, setSetupToken, setupToken } from '@/api/client'
import { api } from '@/api/endpoints'
import { keys, useAvailableRuntimes, useConnections, useSetup } from '@/api/queries'
import type { AvailableRuntime, Check as CheckT, SetupState } from '@/api/types'
import { CopyCommand, useCopy } from '@/components/CopyCommand'
import { AppForm, SubmitButton, useAppForm, useStore } from '@/components/form'
import { DetailText, ErrorState, Loading } from '@/components/states'
import { TemplateProgressLine } from '@/components/TemplateProgressLine'
import { AnchorButton, Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/primitives'
import { markReady } from '@/lib/ready'
import { cn } from '@/lib/utils'
import { domainError, emailError, passwordError } from '@/lib/validation'
import { Checks } from '../extensions/ConnectionCard'
import { ChangeStatus, useRuntimeChangeFlow, type ChangeFlow } from '../settings/RuntimeForms'
import { blockedText, RuntimeChoices } from '../settings/Runtimes'

/** The steps in the order the owner meets them. The account comes third and finishes the setup; GitHub follows
 * and can be skipped. Coding agents are not a step: every template has their CLIs, and running an agent's cloud
 * sessions here is set up later, under Extensions. */
type Step = 'address' | 'runtime' | 'account' | 'github'
const STEPS: { step: Step; label: string }[] = [
  { step: 'address', label: 'Domain' }, { step: 'runtime', label: 'Sandboxes' },
  { step: 'account', label: 'Account' }, { step: 'github', label: 'GitHub' },
]
const isStep = (x: string | null): x is Step => STEPS.some(s => s.step === x)

/** Whether a key, token or password is typed and not saved yet: a reload now would lose it. */
const typing = () => [...document.querySelectorAll<HTMLInputElement>('main input')].some(i => i.type !== 'checkbox' && i.type !== 'radio' && i.name !== 'domain' && !!i.value)

/** Origins that answered over HTTPS from this browser: the certificate is on the file system a moment before
 * nginx serves it, so the page moves only once its new address really answers (no certificate error). An
 * answer tells the page (onAnswer) to try the move again; no answer is asked again a few seconds later. */
const answering = new Set<string>()
const asking = new Set<string>()
const listeners = new Set<() => void>()
function onAnswer(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
function askOrigin(origin: string, tries = 40) {
  if (asking.has(origin) || answering.has(origin)) return
  asking.add(origin)
  fetch(`${origin}/dist/version`, { mode: 'no-cors', cache: 'no-store' })
    .then(() => { answering.add(origin); asking.delete(origin); listeners.forEach(l => l()) },
      () => { asking.delete(origin); if (tries > 1) setTimeout(() => askOrigin(origin, tries - 1), 3_000) })
}

/** Once the certificate is ready, carry on at the server's own address (with the setup token and the step):
 * one link, start to end. */
function moveToOwnAddress(s: SetupState, step: Step) {
  if (s.complete || !s.access.certificate_ready || !s.link) return false
  const target = new URL(s.link)
  if (target.origin === location.origin) return false
  if (!answering.has(target.origin)) { askOrigin(target.origin); return false }
  const token = setupToken()
  target.pathname = '/app/setup'
  target.search = step === 'address' ? '' : `?step=${step}`
  target.hash = token ? `t=${token}` : ''
  location.assign(target.href)
  return true
}

export default function SetupPage() {
  const setup = useSetup()
  const qc = useQueryClient()
  const [step, setStep] = useState<Step | null>(() => { const asked = new URLSearchParams(location.search).get('step'); return isStep(asked) ? asked : null })
  const s = setup.data
  const stepDone = (x: string) => !!s?.steps.find(st => st.step === x)?.done
  // The domain step is done only with a domain: the installer may have set IP access for the first visit's certificate.
  const addressSaved = !!s?.access.domain
  const runtimeDone = stepDone('runtime')
  const done: Record<Step, boolean> = {
    address: !!addressSaved, runtime: runtimeDone, account: !!s?.complete, github: false,
  }
  const reached: Record<Step, boolean> = {
    address: true, runtime: done.address, account: done.address && done.runtime, github: false,
  }
  const first: Step = !done.address ? 'address' : !done.runtime ? 'runtime' : 'account'
  const current: Step = s?.complete ? 'github' : step && step !== 'github' && reached[step] ? step : first
  // The step shown stays until the owner leaves it: a check that passes shows its result, not the next step.
  useEffect(() => { if (s && !step) setStep(first) }, [s, step, first])
  // The move waits while something is typed; it happens at the next step, or the next poll with nothing typed.
  const [answers, setAnswers] = useState(0)
  useEffect(() => onAnswer(() => setAnswers(n => n + 1)), [])
  useEffect(() => { if (s && (current === 'address' || !typing())) moveToOwnAddress(s, current) }, [s, current, answers])
  // A runtime added anywhere (this page, the CLI, an agent): the lists and the builds follow.
  useEffect(() => {
    if (!runtimeDone) return
    for (const key of [keys.templates, keys.runtimesAvailable, keys.runtimes]) void qc.invalidateQueries({ queryKey: key })
  }, [runtimeDone, qc])
  const go = (to: Step) => { if (!s || !moveToOwnAddress(s, to)) setStep(to) }
  return <div className="flex min-h-dvh flex-col">
    <header className="flex h-14 shrink-0 items-center justify-between gap-3 border-b border-line bg-panel px-4 sm:px-8">
      <div className="flex items-center gap-2"><span aria-hidden className="grid size-6 place-items-center rounded-md bg-ink text-[11px] font-bold text-bg">sb</span><span className="font-semibold">sbx</span></div>
      <span className="truncate text-[12.5px] text-muted">Server setup{s?.version ? ` · ${s.version}` : ''}</span>
    </header>
    <main className="mx-auto w-full max-w-[640px] px-4 pb-16 pt-8 sm:px-6 sm:pt-12">
      {setup.isPending ? <Loading label="Checking this installation…" />
        : setup.error || !s ? <NoToken error={setup.error} retry={() => setup.refetch()} />
        : <>
          <Steps current={current} done={s.complete ? { address: true, runtime: true, account: true, github: false } : done}
            onGo={s.complete ? undefined : x => reached[x] ? () => go(x) : undefined} />
          {current === 'address' ? <AddressStep s={s} onNext={() => go('runtime')} />
            : current === 'runtime' ? <RuntimeStep onBack={() => go('address')} onNext={() => go('account')} />
            : current === 'account' ? <AccountStep s={s} onBack={() => go('runtime')} />
            : <GitHubStep />}
        </>}
    </main>
  </div>
}

/** Where the owner is, the same on every step: the numbered steps in a row; on a phone "Step 2 of 4" over a bar. */
function Steps({ current, done, onGo }: { current: Step; done: Record<Step, boolean>; onGo?: (step: Step) => (() => void) | undefined }) {
  const at = STEPS.findIndex(x => x.step === current)
  return <nav aria-label="Setup progress" className="mb-8">
    <div className="sm:hidden">
      <p className="text-[12.5px] text-muted">Step {at + 1} of {STEPS.length} · <span className="font-medium text-ink">{STEPS[at].label}</span></p>
      <div aria-hidden className="mt-2 flex gap-1">
        {STEPS.map((x, i) => <span key={x.step} className={cn('h-1 grow rounded-full', i < at ? 'bg-ok' : i === at ? 'bg-ink' : 'bg-line')} />)}
      </div>
    </div>
    <ol aria-label="Steps" className="hidden items-center gap-2.5 whitespace-nowrap text-[13px] sm:flex">
      {STEPS.map((x, i) => {
        const active = x.step === current
        const onClick = active ? undefined : onGo?.(x.step)
        return <li key={x.step} className="flex items-center gap-2.5">
          {i > 0 && <span aria-hidden className="h-px w-5 bg-line" />}
          <button type="button" disabled={!onClick} onClick={onClick} aria-current={active ? 'step' : undefined}
            className={cn('inline-flex items-center gap-2 rounded-md disabled:cursor-default', active ? 'font-medium text-ink' : 'text-muted', onClick && 'hover:text-ink')}>
            <span className={cn('grid size-5 place-items-center rounded-full text-[11px] tabular-nums',
              done[x.step] ? 'bg-ok text-white dark:text-black' : active ? 'bg-ink text-bg' : 'border border-line-strong')}>
              {done[x.step] ? <Check className="size-3" strokeWidth={3} aria-label="done" /> : i + 1}
            </span>{x.label}
          </button>
        </li>
      })}
    </ol>
  </nav>
}

function Heading({ title, text }: { title: string; text?: React.ReactNode }) {
  return <div>
    <h1 className="text-balance text-[22px] font-semibold leading-tight tracking-tight sm:text-[24px]">{title}</h1>
    {text && <p className="mt-2 text-pretty text-[13.5px] text-muted">{text}</p>}
  </div>
}

function Nav({ onBack, children }: { onBack?: () => void; children?: React.ReactNode }) {
  return <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-5">
    {onBack ? <Button variant="ghost" onClick={onBack} className="-ml-2"><ArrowLeft />Back</Button> : <span />}
    <div className="flex flex-wrap items-center justify-end gap-2">{children}</div>
  </div>
}

/** A line on something that goes on by itself. */
function Notice({ tone, children }: { tone: 'busy' | 'ok'; children: React.ReactNode }) {
  return <div role="status" className="flex items-start gap-2.5 rounded-lg border border-line bg-panel px-3 py-2 text-[13px]">
    {tone === 'busy' ? <Spinner className="mt-0.5 shrink-0" /> : <Check className="mt-0.5 size-4 shrink-0 text-ok" />}
    <span className="min-w-0 break-words">{children}</span>
  </div>
}

function NoToken({ error, retry }: { error: unknown; retry: () => void }) {
  return <div className="space-y-5">
    <Heading title="Open your onboarding link" text="The installer printed it at the end. To see it again, run this on the server:" />
    <CopyCommand command="sudo sbx-admin setup-link" />
    <ErrorState error={error} compact />
    <div className="flex flex-wrap gap-2"><Button onClick={retry}>Try again</Button><AnchorButton variant="ghost" href="/login?next=%2Fapp%2Fsetup">Sign in as an admin</AnchorButton></div>
  </div>
}

/** A record's name or value: a click copies it. */
function CopyValue({ text, label, className }: { text: string; label: string; className?: string }) {
  const { copied, copy } = useCopy()
  return <button type="button" onClick={() => copy(text)} aria-label={`Copy ${label} ${text}`}
    className={cn('group -mx-1 flex min-w-0 items-start gap-1.5 rounded px-1 text-left hover:bg-hover', className)}>
    <span className="min-w-0 break-all">{text}</span>
    {copied ? <Check className="mt-0.5 size-3 shrink-0 text-ok" /> : <Copy className="mt-0.5 size-3 shrink-0 text-faint group-hover:text-muted" />}
    <span className="sr-only" aria-live="polite">{copied ? 'Copied to clipboard' : ''}</span>
  </button>
}

/** The checks a refused save carries, passed ones too; none when it failed for another reason. */
const checksOf = (e: unknown): CheckT[] => {
  const found = e instanceof ApiError ? (e.detail as { checks?: CheckT[] } | null)?.checks : null
  return Array.isArray(found) ? found : []
}

/** The domain and its three records; nothing to choose. Saving checks the records and port 53, then the
 * certificate comes in the background while the owner goes on. */
function AddressStep({ s, onNext }: { s: SetupState; onNext: () => void }) {
  const qc = useQueryClient()
  const [checks, setChecks] = useState<CheckT[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const form = useAppForm({
    defaultValues: { domain: s.access.domain },
    onSubmit: async ({ value }) => {
      setChecks(null); setError(null)
      try {
        await api.connections.save('domain', { name: value.domain.trim().toLowerCase(), address: s.host.address, acme: 'delegated' })
        await qc.invalidateQueries({ queryKey: keys.setup })
      } catch (e) {
        const found = checksOf(e)
        if (found.some(c => !c.ok)) setChecks(found)
        else setError(e)
      }
    },
  })
  const typed = useStore(form.store, st => st.values.domain).trim().toLowerCase()
  const valid = !!typed && !domainError(typed)
  const name = valid ? typed : 'sbx.example.com'
  const ip = s.host.address || 'this server’s IP address'
  const records: [string, string, string][] = [['A', name, ip], ['A', `*.${name}`, ip], ['NS', `_acme-challenge.${name}`, `${name}.`]]
  const failed = checks?.filter(c => !c.ok).length ?? 0
  // The saved domain, untouched: its records passed, so the owner can go on whatever the certificate is doing.
  const saved = !!s.access.domain && typed === s.access.domain.toLowerCase() && !failed
  const certificateError = saved ? s.access.certificate_error : ''
  return <AppForm form={form} className="space-y-6">
    <Heading title="Where will this server answer?" text="The control plane and every sandbox preview get an address under this domain." />
    <form.AppField name="domain" validators={{ onChange: ({ value }) => domainError(value) }}>
      {field => <field.TextField label="Domain" placeholder="sbx.example.com" inputMode="url" autoCapitalize="none" autoCorrect="off" autoComplete="off" spellCheck={false} help="A subdomain of a domain you own, such as sbx.example.com." />}
    </form.AppField>
    <section aria-label="DNS records">
      <p className="mb-1.5 text-[12.5px] font-medium">Add these records at your DNS provider</p>
      <div className={cn('rounded-lg border border-line bg-panel font-mono text-[12px]', !valid && 'text-muted')}>
        <div aria-hidden className="hidden grid-cols-[3rem_minmax(0,1.3fr)_minmax(0,1fr)] gap-x-3 border-b border-line px-3 py-1.5 font-sans text-[11.5px] text-muted sm:grid"><span>Type</span><span>Name</span><span>Value</span></div>
        {records.map(([type, n, v]) => <div key={type + n} className="grid grid-cols-[2.5rem_minmax(0,1fr)] gap-x-3 gap-y-0.5 border-b border-line px-3 py-2 last:border-0 sm:grid-cols-[3rem_minmax(0,1.3fr)_minmax(0,1fr)]">
          <span className="text-muted">{type}</span>
          {valid ? <CopyValue text={n} label="name" /> : <span className="break-all">{n}</span>}
          {valid && s.host.address ? <CopyValue text={v} label="value" className="col-start-2 text-muted sm:col-start-auto sm:text-ink" /> : <span className="col-start-2 break-all sm:col-start-auto">{v}</span>}
        </div>)}
      </div>
      <p className="mt-1.5 text-[12px] text-muted">The NS record lets this server answer the certificate’s DNS check itself, on port 53, so it needs no API token from your DNS provider.</p>
    </section>
    {checks && failed > 0 && <section aria-label="Record checks" role="alert" className="space-y-2.5 rounded-lg border border-bad/25 bg-bad-soft px-4 py-3">
      <p className="text-[13px] font-medium">{failed === 1 ? '1 check failed' : `${failed} checks failed`}</p>
      <Checks checks={checks} failedFirst />
      <p className="text-[12.5px] text-muted">New records can take a few minutes to show up. Fix what’s listed, then check again.</p>
    </section>}
    <ErrorState error={error} />
    {saved && (certificateError ? <CertificateError error={certificateError} />
      : s.access.certificate_ready ? <Notice tone="ok">Certificate ready for {s.access.domain}.</Notice>
      : <Notice tone="busy">Records found. The certificate is on its way in the background; you can continue.</Notice>)}
    <Nav>
      {saved ? <>
        {certificateError && <SubmitButton form={form} variant="secondary">Try again</SubmitButton>}
        <Button variant="primary" onClick={onNext}>Continue<ArrowRight /></Button>
      </> : <SubmitButton form={form}>{checks ? 'Check again' : 'Check records'}</SubmitButton>}
    </Nav>
  </AppForm>
}

/** Where sandboxes run. A server that can run them itself (/dev/kvm) has E2B Embed already, enabled by install.sh:
 * it is shown as chosen. Otherwise the hosted runtimes, each with only its key (the plan comes from the key). */
function RuntimeStep({ onBack, onNext }: { onBack: () => void; onNext: () => void }) {
  const available = useAvailableRuntimes()
  const flow = useRuntimeChangeFlow()
  const [adding, setAdding] = useState(false)
  const [kind, setKind] = useState('')
  const all = available.data ?? []
  const enabled = all.filter(a => a.enabled)
  const hosted = all.filter(a => !a.enabled && !a.host && a.can_run)
  const host = all.find(a => a.host)
  const own = enabled.some(a => a.host)
  const chosen = hosted.find(c => c.kind === kind) ?? hosted[0]
  const offering = (!enabled.length || adding) && !!chosen
  const count = enabled.length
  useEffect(() => setAdding(false), [count])   // the provider was added: back to what is chosen
  return <div className="space-y-6">
    <Heading title="Where will sandboxes run?"
      text={!available.data ? undefined : own ? 'This server can run sandboxes itself, so it will.'
        : enabled.length ? 'Sandboxes run at your provider. It bills you per second while one runs.'
        : host && !host.can_run ? 'This server can’t run sandboxes itself (it has no /dev/kvm), so they run at a provider, which bills you per second while one runs.'
        : 'Sandboxes run at a provider, which bills you per second while one runs.'} />
    {available.isPending ? <Spinner label="Looking at this server…" />
      : available.error ? <ErrorState error={available.error} retry={() => available.refetch()} />
      : <>
        {enabled.length > 0 && <ul aria-label="Chosen" className="space-y-2">{enabled.map(r => <EnabledRuntime key={r.kind} r={r} />)}</ul>}
        {!enabled.length && !chosen && !flow.busy && <ErrorState error="No runtime can run here. Run the installer on a server with /dev/kvm, or on one that can reach a provider." />}
        {flow.busy ? <Notice tone="busy">Checking the key and adding the runtime. This takes a moment.</Notice>
          : flow.change?.state === 'failed' && <ChangeStatus flow={flow} />}
        {/* Hidden, not gone, while a change applies: a key that fails is still there to correct. */}
        {offering && <div hidden={flow.busy} className={cn('space-y-4', adding && 'rounded-lg border border-line bg-panel p-4')}>
          {adding && <p className="font-medium">Add a provider</p>}
          {hosted.length > 1 && <RuntimeChoices choices={hosted} value={chosen.kind} onChange={setKind} />}
          {chosen.edit_blocker ? <>
            <ErrorState compact error={blockedText(chosen.edit_blocker, chosen.kind)} />
            {adding ? <div className="flex justify-end"><Button variant="ghost" onClick={() => setAdding(false)}>Cancel</Button></div> : <div className="pt-2"><Nav onBack={onBack} /></div>}
          </> : <RuntimeKeyForm key={chosen.kind} r={chosen} flow={flow} onBack={adding ? undefined : onBack} onCancel={adding ? () => setAdding(false) : undefined} />}
        </div>}
        {own && !adding && !flow.busy && hosted.length > 0 && <button type="button" onClick={() => setAdding(true)} className="rounded text-[13px] text-accent hover:underline">Also add a provider for more room</button>}
        {enabled.length > 0 && <TemplateProgressLine />}
      </>}
    {!(offering && !adding && !flow.busy) && <Nav onBack={onBack}><Button variant="primary" disabled={!enabled.length || flow.busy} onClick={onNext}>Continue<ArrowRight /></Button></Nav>}
  </div>
}

function EnabledRuntime({ r }: { r: AvailableRuntime }) {
  const memory = r.facts.memory_gb ?? 0
  // What the resource guide says: 12 GiB stay with the host, a default sandbox takes 4 GiB.
  const fits = Math.max(1, Math.floor((memory - 12) / 4))
  return <li className="flex items-start gap-3 rounded-lg border border-line-strong bg-panel p-3">
    <span className="grid size-8 shrink-0 place-items-center rounded-md border border-line bg-sunken text-muted">{r.host ? <Server className="size-4" /> : <Globe className="size-4" />}</span>
    <div className="min-w-0 grow text-[13px]">
      <p className="font-medium">{r.host ? 'On this server' : r.title}<span className="font-normal text-muted">{r.host ? ` · ${r.title}` : r.plan ? ` · ${r.plan.title}` : ''}</span></p>
      <p className="text-muted">{r.host
        ? `${memory ? `${memory} GiB of memory: room for about ${fits} ${fits === 1 ? 'sandbox' : 'sandboxes'} at 4 GiB each. ` : ''}No key, no extra cost.`
        : r.plan ? 'Key checked. Its limits come from your plan.' : 'Key checked.'}</p>
    </div>
    <Check className="mt-0.5 size-4 shrink-0 text-ok" aria-label="chosen" />
  </li>
}

/** A hosted runtime's key, and nothing else: the server reads the plan from the key. With onBack it is the step's
 * own action; with onCancel, an extra beside a runtime already chosen. */
function RuntimeKeyForm({ r, flow, onBack, onCancel }: { r: AvailableRuntime; flow: ChangeFlow; onBack?: () => void; onCancel?: () => void }) {
  const secret = r.settings.find(x => x.secret)
  const [error, setError] = useState<unknown>(null)
  const form = useAppForm({
    defaultValues: { key: '' },
    onSubmit: async ({ value }) => {
      setError(null)
      // The key stays typed until the runtime is there (this form then goes): a refused one is corrected in place.
      try { await flow.submit(() => api.runtimeSettings.add(r.kind, { values: secret ? { [secret.key]: value.key.trim() } : {} })) } catch (e) { setError(e) }
    },
  })
  const submit = <SubmitButton form={form} variant={onBack ? 'primary' : 'secondary'}>{onBack ? 'Check key' : `Add ${r.title}`}</SubmitButton>
  return <AppForm form={form} aria-label={`${r.title} key`} className="space-y-4">
    {secret && <form.AppField name="key" validators={{ onChange: ({ value }) => !value.trim() ? `Paste your ${secret.label}.` : undefined }}>
      {field => <field.TextField label={secret.label} type="password" autoComplete="off" spellCheck={false} help={secret.help || undefined} />}
    </form.AppField>}
    <ErrorState error={error} compact />
    {onBack ? <div className="pt-2"><Nav onBack={onBack}>{submit}</Nav></div>
      : <div className="flex flex-wrap justify-end gap-2">{onCancel && <Button variant="ghost" onClick={onCancel}>Cancel</Button>}{submit}</div>}
  </AppForm>
}

/** The first admin's account, which also finishes the setup. On a private network nobody signs in: just finish. */
function AccountStep({ s, onBack }: { s: SetupState; onBack: () => void }) {
  const qc = useQueryClient()
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const existing = !!s.steps.find(x => x.step === 'sign_in')?.done
  const signedIn = s.signed_in || (existing && s.open_access)
  const complete = async () => {
    const result = await api.setup.complete()
    markReady()
    setSetupToken(null)
    qc.setQueryData(keys.setup, result)
    void qc.invalidateQueries({ queryKey: keys.me })
  }
  const form = useAppForm({
    defaultValues: { email: '', password: '', confirm: '' },
    onSubmit: async ({ value }) => {
      setError(null)
      try {
        qc.setQueryData(keys.setup, await api.setup.admin(value.email.trim(), value.password))
        form.reset()
        await complete()
      } catch (e) { setError(e) }
    },
  })
  const finish = async () => { setBusy(true); setError(null); try { await complete() } catch (e) { setError(e) } finally { setBusy(false) } }
  const create = !signedIn && !existing
  // The account is created at the server's own address, so the owner signs in where they stay (the session
  // cookie is the domain's): the form waits until the page is there, nothing typed is lost on the way.
  const domain = s.access.mode === 'domain' ? s.access.domain : ''
  const waiting = create && !!domain && (!s.access.certificate_ready || location.hostname !== domain)
  const certificateError = waiting ? s.access.certificate_error : ''
  const [retrying, setRetrying] = useState(false)
  const retry = async () => {
    setRetrying(true); setError(null)
    try {
      await api.connections.save('domain', { name: domain, address: s.host.address, acme: 'delegated' })
      await qc.invalidateQueries({ queryKey: keys.setup })
    } catch (e) { setError(e) } finally { setRetrying(false) }
  }
  return <div className="space-y-6">
    <Heading title={create ? 'Create your account' : signedIn ? 'Finish the setup' : 'Sign in to finish'}
      text={s.signed_in ? `You’re signed in as ${s.email}.` : signedIn ? 'Nobody signs in on this private network.'
        : existing ? 'This server already has an admin.' : 'You’re the first admin. You can add people and single sign-on later, under Settings.'} />
    {create && <AppForm form={form} id="setup-account"><fieldset disabled={waiting} className="min-w-0 space-y-4 disabled:opacity-60">
      <form.AppField name="email" validators={{ onChange: ({ value }) => emailError(value) }}>{field => <field.TextField label="Email" type="email" autoComplete="username" />}</form.AppField>
      <div className="grid gap-4 sm:grid-cols-2 sm:gap-3">
        <form.AppField name="password" validators={{ onChange: ({ value }) => passwordError(value) }}>{field => <field.TextField label="Password" type="password" autoComplete="new-password" help="At least 8 characters." />}</form.AppField>
        <form.AppField name="confirm" validators={{ onChangeListenTo: ['password'], onChange: ({ value, fieldApi }) => !value ? 'Repeat the password.' : value !== fieldApi.form.getFieldValue('password') ? 'The passwords differ.' : undefined }}>
          {field => <field.TextField label="Repeat password" type="password" autoComplete="new-password" />}
        </form.AppField>
      </div>
    </fieldset></AppForm>}
    {certificateError && <CertificateError error={certificateError} domain={domain} />}
    <TemplateProgressLine />
    <ErrorState error={error} />
    <Nav onBack={onBack}>
      {certificateError ? <Button busy={retrying} onClick={retry}>Try again</Button>
        : waiting ? <Spinner label={s.access.certificate_ready ? `Moving to ${domain}…` : `Waiting for the certificate for ${domain}, usually under a minute`} />
        : create ? <SubmitButton form={form} formId="setup-account">Create account and finish<ArrowRight /></SubmitButton>
        : signedIn ? <Button variant="primary" busy={busy} onClick={finish}>Finish setup<ArrowRight /></Button>
        : <AnchorButton variant="primary" href="/login?next=%2Fapp%2Fsetup">Sign in<ArrowRight /></AnchorButton>}
    </Nav>
  </div>
}

/** After the setup: GitHub, for cloning private repositories and opening the recipe pull request. Skippable;
 * either way the owner lands in the control plane, which says the server is ready. */
function GitHubStep() {
  const navigate = useNavigate()
  const connections = useConnections()
  const [error, setError] = useState<unknown>(null)
  const connected = connections.data?.find(c => c.kind === 'github')?.state === 'connected'
  const enter = () => { markReady(); navigate('/app/repos', { replace: true }) }
  useEffect(() => { if (connected) enter() }, [connected]) // eslint-disable-line react-hooks/exhaustive-deps
  const form = useAppForm({
    defaultValues: { owner: '' },
    onSubmit: async ({ value }) => {
      setError(null)
      try {
        const { action_url, manifest } = await api.connections.githubManifest(value.owner.trim(), '/app/repos')
        markReady()
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
      } catch (e) { setError(e) }
    },
  })
  if (connected) return <Loading label="Opening your control plane…" />
  return <div className="space-y-6">
    <Heading title="Connect GitHub" text="Your account is ready. sbx clones your repositories and opens pull requests through a GitHub App of its own, which you create now. GitHub then asks which repositories it may use." />
    <AppForm form={form} id="setup-github" className="space-y-4">
      <form.AppField name="owner" validators={{ onChange: ({ value }) => value.trim() && !/^[A-Za-z0-9-]+$/.test(value.trim()) ? 'Enter the organization’s name as in its GitHub address.' : undefined }}>
        {field => <field.TextField label="GitHub organization" optional autoCapitalize="none" autoCorrect="off" autoComplete="off" spellCheck={false} help="Leave it empty for repositories under your own account." />}
      </form.AppField>
    </AppForm>
    <ErrorState error={error} />
    <Nav>
      <Button variant="ghost" onClick={enter}>Skip for now</Button>
      <SubmitButton form={form} formId="setup-github"><Github />Create the app on GitHub</SubmitButton>
    </Nav>
  </div>
}

/** Why the certificate failed, with the fix in plain sight: a closed port 53 says where to open it (the server's
 * own probe: "port 53: ...; fix: ..."); anything else is Let's Encrypt's error, whose text is the detail. */
export function CertificateError({ error, domain }: { error: string; domain?: string }) {
  const port = /^port 53:\s*(.*?)(?:;\s*fix:\s*(.*))?$/s.exec(error.trim())
  return <div role="alert" className="rounded-lg border border-bad/25 bg-bad-soft px-4 py-3 text-[13px]">
    {port ? <>
      <p className="font-medium text-bad">Port 53 is blocked</p>
      <p className="mt-1">Nothing from outside reached this server on port 53, so it can’t answer the certificate’s DNS check{domain ? ` for ${domain}` : ''}.</p>
      <p className="mt-1.5">Allow inbound <span className="font-medium">TCP and UDP port 53</span> in your provider’s firewall, then try again.</p>
      {port[1] && <details className="mt-1.5 text-[12px] [&>summary]:cursor-pointer"><summary className="w-fit text-muted hover:text-ink">Details</summary>
        <span className="mt-1 block break-words font-mono text-[11.5px] text-muted">{port[1]}</span></details>}
    </> : <>
      <p className="font-medium text-bad">Couldn’t get the certificate{domain ? ` for ${domain}` : ''}</p>
      <p className="mt-1 break-words text-muted"><DetailText text={error} /></p>
    </>}
  </div>
}
