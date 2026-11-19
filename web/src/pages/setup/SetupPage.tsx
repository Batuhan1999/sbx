import { useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ArrowRight, Check, Copy, Github, Globe, Server } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router'
import { ApiError } from '@/api/client'
import { api } from '@/api/endpoints'
import { keys, useAvailableRuntimes, useConnections, useSetup, useSetupOpen } from '@/api/queries'
import type { AvailableRuntime, Check as CheckT, SetupState } from '@/api/types'
import { useCopy } from '@/components/CopyCommand'
import { AppForm, SubmitButton, useAppForm, useStore } from '@/components/form'
import { DetailText, ErrorState, Loading } from '@/components/states'
import { TemplateProgressLine } from '@/components/TemplateProgressLine'
import { AnchorButton, Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/primitives'
import { browser } from '@/lib/browser'
import { cn } from '@/lib/utils'
import { domainError, emailError, passwordError } from '@/lib/validation'
import { Checks } from '../extensions/ConnectionCard'
import { ChangeStatus, KeyHelp, useRuntimeChangeFlow, type ChangeFlow } from '../settings/RuntimeForms'
import { blockedText, RuntimeChoices } from '../settings/Runtimes'

/** The steps in the order the owner meets them. The account comes first: until it exists, whoever opens the
 * page may create it, and every step after it is a signed-in admin's. GitHub can be skipped; after it the control
 * plane opens on a summary (pages/repos/ReadyDialog), and leaving that finishes the onboarding. Coding agents are
 * not a step: every template has their CLIs, and running an agent's cloud sessions here is set up later, under
 * Extensions. */
type Step = 'account' | 'address' | 'runtime' | 'github'
const STEPS: { step: Step; label: string }[] = [
  { step: 'account', label: 'Account' }, { step: 'address', label: 'Domain' },
  { step: 'runtime', label: 'Sandboxes' }, { step: 'github', label: 'GitHub' },
]

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

/** The server's own address once its domain's certificate is ready (the control plane's host under the
 * domain, which the state's link names). Null while there is nowhere to move, or the page is there. */
function ownAddress(s: SetupState) {
  if (s.complete || s.access.mode !== 'domain' || !s.access.certificate_ready || !s.link) return null
  const target = new URL(s.link)
  return target.origin === location.origin ? null : target
}

/** Go on at the server's own address, signed in there: the session cookie of the IP address does not reach
 * the domain, so a one-time code carries the sign-in over. Without one the owner signs in there. */
async function moveTo(target: URL) {
  let code = ''
  try { code = (await api.setup.handoff()).code } catch { /* signs in at the new address instead */ }
  const to = new URL(target.href)
  to.hash = code ? `h=${code}` : ''
  browser.assign(to.href)
}

/** How long the page tries to move by itself before it also offers the address as a button. */
const MOVE_PATIENCE_MS = 10_000

/** The sign-in a move carried here (#h=CODE), taken out of the address: null when there is none. */
function arrivingCode() {
  const code = new URLSearchParams(location.hash.slice(1)).get('h')
  if (code === null) return null
  history.replaceState(null, '', location.pathname + location.search)
  return /^[A-Za-z0-9_-]+$/.test(code) ? code : ''
}

export default function SetupPage() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  // A move from the IP address signs in here first; the page asks who it is only after.
  const [entering, setEntering] = useState(() => new URLSearchParams(location.hash.slice(1)).has('h'))
  useEffect(() => {
    const code = arrivingCode()
    if (code === null) return
    const done = () => { void qc.invalidateQueries({ queryKey: keys.setup }); setEntering(false) }
    if (!code) return done()
    api.setup.enter(code).catch(() => { /* used or too old: the page asks to sign in */ }).finally(done)
  }, [qc])
  const open = useSetupOpen(!entering)
  const signedIn = !!open.data?.signed_in
  const setup = useSetup(signedIn && !open.data?.complete)
  const s = signedIn ? setup.data : undefined
  // A session that ended (or a password changed elsewhere): ask who this is again.
  const refused = setup.error instanceof ApiError && setup.error.status === 401
  useEffect(() => { if (refused) void qc.invalidateQueries({ queryKey: keys.setupOpen }) }, [refused, qc])
  const complete = !!open.data?.complete || !!s?.complete
  useEffect(() => { if (complete) navigate('/app/repos', { replace: true }) }, [complete, navigate])
  const connections = useConnections(!!s)
  const github = connections.data?.find(c => c.kind === 'github')?.state === 'connected'

  const [step, setStep] = useState<Step | null>(null)
  const stepDone = (x: string) => !!s?.steps.find(st => st.step === x)?.done
  const target = s ? ownAddress(s) : null
  // The domain step is done only with a domain whose certificate is there, and with the page at it: the
  // installer may have set IP access for the first visit's certificate.
  const done: Record<Step, boolean> = {
    account: signedIn, address: !!s?.access.domain && !!s.access.certificate_ready && !target, runtime: stepDone('runtime'),
    github,
  }
  const reached: Record<Step, boolean> = {
    account: !signedIn, address: signedIn, runtime: done.address, github: done.address && done.runtime,
  }
  const first: Step = !done.account ? 'account' : !done.address ? 'address' : !done.runtime ? 'runtime' : 'github'
  const current: Step = step && reached[step] ? step : first
  // The step shown stays until the owner leaves it: a check that passes shows its result, not the next step.
  useEffect(() => { if (s && !step) setStep(first) }, [s, step, first])
  // The move: once the certificate is ready and the new address answers this browser.
  const [answers, setAnswers] = useState(0)
  useEffect(() => onAnswer(() => setAnswers(n => n + 1)), [])
  const leaving = useRef(false)   // one move: the answer and the button may both ask for it
  const move = () => { if (target && !leaving.current) { leaving.current = true; void moveTo(target) } }
  useEffect(() => {
    if (!target) return
    if (answering.has(target.origin)) move()
    else askOrigin(target.origin)
  }, [target?.href, answers]) // eslint-disable-line react-hooks/exhaustive-deps
  // A runtime added anywhere (this page, the CLI, an agent): the lists and the builds follow.
  const runtimeDone = done.runtime
  useEffect(() => {
    if (!runtimeDone) return
    for (const key of [keys.templates, keys.runtimesAvailable, keys.runtimes]) void qc.invalidateQueries({ queryKey: key })
  }, [runtimeDone, qc])

  const version = s?.version ?? open.data?.version
  return <div className="flex min-h-dvh flex-col">
    <header className="flex h-14 shrink-0 items-center justify-between gap-3 border-b border-line bg-panel px-4 sm:px-8">
      <div className="flex items-center gap-2"><span aria-hidden className="grid size-6 place-items-center rounded-md bg-ink text-[11px] font-bold text-bg">sb</span><span className="font-semibold">sbx</span></div>
      <span className="truncate text-[12.5px] text-muted">Server setup{version ? ` · ${version}` : ''}</span>
    </header>
    <main className="mx-auto w-full max-w-[640px] px-4 pb-16 pt-8 sm:px-6 sm:pt-12">
      {entering || open.isPending || complete ? <Loading label="Checking this installation…" />
        : !open.data ? <div className="space-y-5"><ErrorState error={open.error} /><Button onClick={() => open.refetch()}>Try again</Button></div>
        : !signedIn ? <>
          <Steps current="account" done={{ account: false, address: false, runtime: false, github: false }} />
          {open.data.account ? <SignInStep link={open.data.link} /> : <AccountStep />}
        </>
        : !s ? (setup.error && !refused ? <div className="space-y-5"><ErrorState error={setup.error} /><Button onClick={() => setup.refetch()}>Try again</Button></div>
          : <Loading label="Checking this installation…" />)
        : <>
          <Steps current={current} done={done} onGo={x => x !== 'account' && reached[x] ? () => setStep(x) : undefined} />
          {current === 'address' ? <AddressStep s={s} target={target} onMove={move} onNext={() => setStep('runtime')} />
            : current === 'runtime' ? <RuntimeStep onBack={() => setStep('address')} onNext={() => setStep('github')} />
            : <GitHubStep onBack={() => setStep('runtime')} onNext={() => navigate('/app/repos')} />}
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

/** Someone can sign in already and this browser is not: sign in here, or go on where the page is now. */
function SignInStep({ link }: { link: string }) {
  const elsewhere = link && new URL(link).origin !== location.origin ? new URL(link) : null
  return <div className="space-y-6">
    <Heading title="Sign in to go on" text={elsewhere ? `This server already has an admin, and its setup goes on at ${elsewhere.host}.` : 'This server already has an admin.'} />
    <Nav>
      {elsewhere ? <AnchorButton variant="primary" href={elsewhere.href}>Open {elsewhere.host}<ArrowRight /></AnchorButton>
        : <AnchorButton variant="primary" href="/login?next=%2Fonboarding">Sign in<ArrowRight /></AnchorButton>}
    </Nav>
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

/** The domain and its three records; nothing to choose. Saving checks the records and port 53; the step then
 * waits for the certificate and, opened at the server's IP address, moves the page to the domain (target). */
function AddressStep({ s, target, onMove, onNext }: { s: SetupState; target: URL | null; onMove: () => void; onNext: () => void }) {
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
  // The saved domain, untouched: its records passed.
  const saved = !!s.access.domain && typed === s.access.domain.toLowerCase() && !failed
  const certificateError = saved ? s.access.certificate_error : ''
  const waitUntil = certificateLimit(certificateError)?.at
  const now = useNow()
  // The page moves by itself once its new address answers this browser. One that doesn't (its DNS not there
  // yet, say) is offered as a button, so the owner is never left watching a spinner.
  const moving = saved && !certificateError && !!target
  const [stalled, setStalled] = useState(false)
  useEffect(() => {
    if (!moving) return
    const timer = setTimeout(() => setStalled(true), MOVE_PATIENCE_MS)
    return () => { clearTimeout(timer); setStalled(false) }
  }, [moving])
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
    {saved && (certificateError ? <CertificateError error={certificateError} domain={s.access.domain} />
      : moving ? <Notice tone="busy">Certificate ready. Moving to {target.host}…</Notice>
      : s.access.certificate_ready ? <Notice tone="ok">Certificate ready for {s.access.domain}.</Notice>
      : <Notice tone="busy">Records found. Getting the certificate for {s.access.domain}, usually under a minute.</Notice>)}
    <Nav>
      {!saved ? <SubmitButton form={form}>{checks ? 'Check again' : 'Check records'}</SubmitButton>
        : certificateError ? (waitUntil && now < waitUntil.getTime()
          ? <Button variant="secondary" disabled>Try again at {clock(waitUntil)}</Button>
          : <SubmitButton form={form} variant="secondary">Try again</SubmitButton>)
        : moving ? stalled && <Button variant="primary" onClick={onMove}>Open {target.host}<ArrowRight /></Button>
        : s.access.certificate_ready && <Button variant="primary" onClick={onNext}>Continue<ArrowRight /></Button>}
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
  // A key being checked and its runtime added: one step, its form busy until the server runs it (it restarts).
  const pending = flow.busy ? all.find(a => a.kind === flow.change?.kind) : undefined
  const chosen = pending ?? hosted.find(c => c.kind === kind) ?? hosted[0]
  const offering = (!enabled.length || adding || !!pending) && !!chosen
  const count = flow.busy ? -1 : enabled.length
  useEffect(() => setAdding(false), [count])   // the provider was added: back to what is chosen
  return <div className="space-y-6">
    <Heading title="Where will sandboxes run?"
      text={!available.data ? undefined : own ? 'This server can run sandboxes itself, so it will.'
        : enabled.length ? 'Sandboxes run at your provider. It bills you per second while one runs.'
        : host && !host.can_run ? `This server can’t run sandboxes itself${host.why_not ? ` (${host.title} ${host.why_not})` : ''}, so they run at a provider, which bills you per second while one runs.`
        : 'Sandboxes run at a provider, which bills you per second while one runs.'} />
    {available.isPending ? <Spinner label="Looking at this server…" />
      : available.error && !available.data ? <ErrorState error={available.error} retry={() => available.refetch()} />
      : <>
        {!pending && enabled.length > 0 && <ul aria-label="Chosen" className="space-y-2">{enabled.map(r => <EnabledRuntime key={r.kind} r={r} />)}</ul>}
        {!enabled.length && !chosen && !flow.busy && <ErrorState error="No runtime can run here. Run the installer on a server with /dev/kvm, or on one that can reach a provider." />}
        {!flow.busy && flow.change?.state === 'failed' && <ChangeStatus flow={flow} />}
        {offering && <div className={cn('space-y-4', adding && 'rounded-lg border border-line bg-panel p-4')}>
          {adding && <p className="font-medium">Add a provider</p>}
          {hosted.length > 1 && !pending && <RuntimeChoices choices={hosted} value={chosen.kind} onChange={setKind} />}
          {chosen.edit_blocker ? <>
            <ErrorState compact error={blockedText(chosen.edit_blocker, chosen.kind)} />
            {adding ? <div className="flex justify-end"><Button variant="ghost" onClick={() => setAdding(false)}>Cancel</Button></div> : <div className="pt-2"><Nav onBack={onBack} /></div>}
          </> : <RuntimeKeyForm key={chosen.kind} r={chosen} flow={flow} busy={!!pending} onBack={adding ? undefined : onBack} onCancel={adding ? () => setAdding(false) : undefined} />}
        </div>}
        {own && !adding && !flow.busy && hosted.length > 0 && <button type="button" onClick={() => setAdding(true)} className="rounded text-[13px] text-accent hover:underline">Also add a provider for more room</button>}
        {!pending && enabled.length > 0 && <TemplateProgressLine />}
      </>}
    {!(offering && !adding) && <Nav onBack={onBack}><Button variant="primary" disabled={!enabled.length || flow.busy} onClick={onNext}>Continue<ArrowRight /></Button></Nav>}
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
function RuntimeKeyForm({ r, flow, busy, onBack, onCancel }: { r: AvailableRuntime; flow: ChangeFlow; busy?: boolean; onBack?: () => void; onCancel?: () => void }) {
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
  const submit = <SubmitButton form={form} busy={busy} variant={onBack ? 'primary' : 'secondary'}>{onBack ? 'Check key' : `Add ${r.title}`}</SubmitButton>
  return <AppForm form={form} aria-label={`${r.title} key`} className="space-y-4">
    {secret && <form.AppField name="key" validators={{ onChange: ({ value }) => !value.trim() ? `Paste your ${secret.label}.` : undefined }}>
      {field => <field.TextField label={secret.label} type="password" autoComplete="off" spellCheck={false} disabled={busy} help={<KeyHelp setting={secret} />} />}
    </form.AppField>}
    <ErrorState error={error} compact />
    {onBack ? <div className="pt-2"><Nav onBack={onBack}>{submit}</Nav></div>
      : <div className="flex flex-wrap justify-end gap-2">{onCancel && <Button variant="ghost" onClick={onCancel}>Cancel</Button>}{submit}</div>}
  </AppForm>
}

/** The first admin's account, the first step: until it exists whoever opens the page may make it, so nothing
 * comes before it. Every later step is this admin's. */
function AccountStep() {
  const qc = useQueryClient()
  const [error, setError] = useState<unknown>(null)
  const form = useAppForm({
    defaultValues: { email: '', password: '', confirm: '' },
    onSubmit: async ({ value }) => {
      setError(null)
      try {
        qc.setQueryData(keys.setup, await api.setup.admin(value.email.trim(), value.password))
        form.reset()
        await qc.invalidateQueries({ queryKey: keys.setupOpen })
      } catch (e) {
        setError(e)
        void qc.invalidateQueries({ queryKey: keys.setupOpen })   // someone else made it first: the page says so
      }
    },
  })
  return <div className="space-y-6">
    <Heading title="Create your account" text="You’re the first admin. You can add people and single sign-on later, under Settings." />
    <AppForm form={form} id="setup-account" className="space-y-4">
      <form.AppField name="email" validators={{ onChange: ({ value }) => emailError(value) }}>{field => <field.TextField label="Email" type="email" autoComplete="username" />}</form.AppField>
      <div className="grid gap-4 sm:grid-cols-2 sm:gap-3">
        <form.AppField name="password" validators={{ onChange: ({ value }) => passwordError(value) }}>{field => <field.TextField label="Password" type="password" autoComplete="new-password" help="At least 8 characters." />}</form.AppField>
        <form.AppField name="confirm" validators={{ onChangeListenTo: ['password'], onChange: ({ value, fieldApi }) => !value ? 'Repeat the password.' : value !== fieldApi.form.getFieldValue('password') ? 'The passwords differ.' : undefined }}>
          {field => <field.TextField label="Repeat password" type="password" autoComplete="new-password" />}
        </form.AppField>
      </div>
    </AppForm>
    <ErrorState error={error} />
    <Nav><SubmitButton form={form} formId="setup-account">Create account<ArrowRight /></SubmitButton></Nav>
  </div>
}

/** GitHub, for cloning private repositories and opening the recipe pull request: the App is created on GitHub,
 * which brings the browser to the control plane and its summary. It can be skipped (Extensions has it later);
 * either way the control plane's summary comes next. */
function GitHubStep({ onBack, onNext }: { onBack: () => void; onNext: () => void }) {
  const connections = useConnections()
  const [error, setError] = useState<unknown>(null)
  const [forOrg, setForOrg] = useState(false)
  const github = connections.data?.find(c => c.kind === 'github')
  const connected = github?.state === 'connected'
  const form = useAppForm({
    defaultValues: { owner: '' },
    onSubmit: async ({ value }) => {
      setError(null)
      try {
        const { action_url, manifest } = await api.connections.githubManifest(forOrg ? value.owner.trim() : '', '/app/repos')
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
  return <div className="space-y-6">
    <Heading title="Connect GitHub" text="To clone your repositories and open pull requests." />
    <AppForm form={form} id="setup-github" className="rounded-lg border border-line bg-panel">
      <div className="flex items-center gap-3 p-3">
        <span className="grid size-8 shrink-0 place-items-center rounded-md border border-line bg-sunken"><Github className="size-4" /></span>
        <div className="min-w-0 grow text-[13px]">
          <p className="font-medium">GitHub</p>
          <p className="text-muted">{connected ? github.summary || 'Connected' : 'Not connected'}</p>
        </div>
        {connected ? <Check className="size-4 shrink-0 text-ok" aria-label="connected" />
          : <SubmitButton form={form} formId="setup-github" variant="secondary">Connect GitHub</SubmitButton>}
      </div>
      {!connected && (forOrg
        ? <div className="border-t border-line p-3">
          <form.AppField name="owner" validators={{ onChange: ({ value }) => !/^[A-Za-z0-9-]+$/.test(value.trim()) ? 'Enter the organization’s name as in its GitHub address.' : undefined }}>
            {field => <field.TextField label="Organization" placeholder="acme" autoFocus autoCapitalize="none" autoCorrect="off" autoComplete="off" spellCheck={false} />}
          </form.AppField>
        </div>
        : <button type="button" onClick={() => setForOrg(true)} className="block w-full border-t border-line px-3 py-2 text-left text-[12.5px] text-muted hover:text-ink">For an organization?</button>)}
    </AppForm>
    <ErrorState error={error} />
    <TemplateProgressLine />
    <Nav onBack={onBack}>
      {connected ? <Button variant="primary" onClick={onNext}>Continue<ArrowRight /></Button>
        : <Button variant="ghost" onClick={onNext}>Skip for now</Button>}
    </Nav>
  </div>
}

/** Let's Encrypt's refusal for too many certificates for the same names (5 a week): how many, and when it allows
 * the next. Null for any other error. */
export function certificateLimit(error: string): { count: string; at: Date } | null {
  const found = /too many certificates \((\d+)\).*?retry after (\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d) UTC/s.exec(error)
  return found ? { count: found[1], at: new Date(`${found[2]}T${found[3]}Z`) } : null
}

const clock = (at: Date) => at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

/** Now, again every 30 seconds: what a wait until a given time needs. */
function useNow() {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 30_000); return () => clearInterval(t) }, [])
  return now
}

export function CertificateError({ error, domain }: { error: string; domain?: string }) {
  const port = /^port 53:\s*(.*?)(?:;\s*fix:\s*(.*))?$/s.exec(error.trim())
  const limit = certificateLimit(error)
  return <div role="alert" className="rounded-lg border border-bad/25 bg-bad-soft px-4 py-3 text-[13px]">
    {limit ? <>
      <p className="font-medium text-bad">Let’s Encrypt’s weekly limit is reached</p>
      <p className="mt-1">It issued {limit.count} certificates for {domain ?? 'this domain'} this week. Try again after {clock(limit.at)}, or use another subdomain.</p>
    </> : port ? <>
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
