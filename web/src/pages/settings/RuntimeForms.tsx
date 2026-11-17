import { useQueryClient } from '@tanstack/react-query'
import { Check, ChevronRight, ExternalLink } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { api } from '@/api/endpoints'
import { isApplying, keys, useRuntimeChange } from '@/api/queries'
import type { AvailableRuntime, RuntimeChange, RuntimePreset, RuntimeSetting } from '@/api/types'
import { AppForm, SubmitButton, useAppForm } from '@/components/form'
import { ErrorState } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/primitives'
import { cn } from '@/lib/utils'

const CUSTOM = 'custom'
const OUTCOMES: Record<string, { done: string; failed: string }> = {
  add: { done: 'Added', failed: "Couldn't add" }, change: { done: 'Saved', failed: "Couldn't save" },
  remove: { done: 'Removed', failed: "Couldn't remove" }, default: { done: 'Default changed', failed: "Couldn't change the default" },
}

export type ChangeFlow = ReturnType<typeof useRuntimeChangeFlow>

/** A runtime change: queued by the server, applied by the host, which restarts sbx-control.
 * Polls until it failed, or is done and the restarted sbx-control answers, then refreshes what it changed. */
export function useRuntimeChangeFlow(enabled = true) {
  const qc = useQueryClient()
  const change = useRuntimeChange(enabled)
  const [mine, setMine] = useState(false)
  const state = change.data?.state
  const at = change.data?.at
  const busy = isApplying(change.data)
  useEffect(() => {
    if (busy || (state !== 'done' && state !== 'failed')) return
    void qc.invalidateQueries({ queryKey: keys.runtimes })
    void qc.invalidateQueries({ queryKey: keys.setup })
  }, [state, at, busy, qc])
  const submit = async (run: () => Promise<RuntimeChange>) => {
    const queued = await run()
    qc.setQueryData(keys.runtimeChange, queued)
    setMine(true)
  }
  return { change: change.data, busy, mine, submit }
}

/** How long a change that failed is shown to anyone who did not make it here. */
const FAILURE_SHOWN_MS = 10 * 60_000

/** Whether the last change's outcome is still news on this page: one this page made, or a failure of the last
 * ten minutes nobody dismissed; a change in flight always. */
export function changeShown(flow: ChangeFlow, dismissed: string | null): boolean {
  const c = flow.change
  if (!c || c.state === 'idle') return false
  if (flow.busy || flow.mine) return true
  const at = Date.parse(c.at)
  return c.state === 'failed' && dismissed !== c.at && Number.isFinite(at) && Date.now() - at < FAILURE_SHOWN_MS
}

/** The change in flight or the last one's outcome: applying, failed (why, with a dismiss), or done (only right
 * after this page asked for it). */
export function ChangeStatus({ flow, className }: { flow: ChangeFlow; className?: string }) {
  const [dismissed, setDismissed] = useState<string | null>(null)
  const c = flow.change
  if (!c || !changeShown(flow, dismissed)) return null
  const words = OUTCOMES[c.operation] ?? OUTCOMES.change
  // The server's reason, without what the UI says already (nothing changed).
  const reason = (c.message || 'try again').replace(/; nothing was changed$/, '')
  return <div className={className}>
    {flow.busy ? <div role="status" className="rounded-lg border border-line bg-sunken px-3 py-2 text-[13px]"><Spinner label="Applying…" /></div>
      : c.state === 'failed' ? <div className="flex items-start gap-2">
        <ErrorState compact className="min-w-0 flex-1" error={`${words.failed}: ${reason}`} />
        <Button size="sm" variant="ghost" onClick={() => setDismissed(c.at)}>Dismiss</Button>
      </div>
      : <p role="status" className="text-[12.5px] text-muted">{c.unchanged ? 'Already the default' : words.done}</p>}
  </div>
}

// Limits in one unit each, as people read them: a run in hours, memory and disk in GB, counts as
// they are. The API keeps its own (seconds, MiB); `factor` converts.

type Unit = { unit: string; factor: number; step: number; min: number; max: number }

/** A limit's unit, step and range as the form shows them. */
export function unitOf(s: RuntimeSetting): Unit {
  if (s.unit === 'seconds') return { unit: 'h', factor: 3600, step: 0.5, min: 0.5, max: (s.max ?? 86400) / 3600 }
  if (s.unit === 'MiB') return { unit: 'GB', factor: 1024, step: (s.step ?? 1024) / 1024, min: (s.min ?? 1024) / 1024, max: (s.max ?? 8192) / 1024 }
  return { unit: '', factor: 1, step: s.step ?? 1, min: s.min ?? 1, max: s.max ?? 100 }
}

/** A value in the form's unit, snapped to its step and range. */
const snap = (u: Unit, v: number) => Math.min(u.max, Math.max(u.min, Math.round(v / u.step) * u.step))
/** An API value (seconds, MiB) held to what the form offers. */
const clamp = (s: RuntimeSetting, api: number) => { const u = unitOf(s); return Math.round(snap(u, api / u.factor) * u.factor) }
const shown = (u: Unit, api: number) => String(+(api / u.factor).toFixed(2))

/** A plan's limits in two short lines: its run and how many at once, then a sandbox's size ("8+" for what it
 * leaves adjustable). */
export function planLines(r: { settings: RuntimeSetting[] }, values: Record<string, string>, adjustable: string[] = []): [string, string] {
  const of = (limit: string) => r.settings.find(s => s.limit === limit)
  const part = (limit: string, suffix: string) => {
    const s = of(limit)
    if (!s || !values[s.key]) return ''
    return `${shown(unitOf(s), Number(values[s.key]))}${adjustable.includes(s.key) ? '+' : ''} ${suffix}`.trim()
  }
  return [[part('timeout_seconds', 'h'), part('sandboxes', 'at once')].filter(Boolean).join(' · '),
    [part('cpu', 'vCPU'), part('memory_mb', 'GB'), part('disk_mb', 'GB disk')].filter(Boolean).join(' · ')]
}

/** The order the limits sit in, two to a row on wider screens: run and how many, then a sandbox's size. */
const ORDER = ['timeout_seconds', 'sandboxes', 'cpu', 'memory_mb', 'disk_mb']
const ordered = (limits: RuntimeSetting[]) => [...limits].sort((a, b) => ORDER.indexOf(a.limit) - ORDER.indexOf(b.limit))


/** A runtime's settings: its key first (write-only: "set" until a new one is typed), its plan as
 * cards (a plan's limits are known, so not editable; Custom shows a slider per limit), and the
 * rest (a provider's domain) under Advanced. */
export function RuntimeSettingsForm({ runtime: r, mode, flow, onDone, onCancel }: {
  runtime: AvailableRuntime; mode: 'add' | 'change'; flow: ChangeFlow; onDone?: () => void; onCancel?: () => void
}) {
  const secret = r.settings.find(s => s.secret)
  const limits = r.settings.filter(s => s.limit)
  const advanced = r.settings.filter(s => !s.secret && !s.limit)
  const keySet = !!secret && r.secrets_set.includes(secret.key)
  // The plan the server says the runtime is on (adding: the first plan, as an unchosen one reads).
  const startPlan = mode === 'add' ? (r.presets[0]?.name ?? CUSTOM) : (r.plan?.name ?? CUSTOM)
  const [plan, setPlan] = useState(startPlan)
  // What the sliders show (held to their range); only those the admin moves are sent, so a value
  // set higher in runtime.env by hand is never lowered by a save that did not touch it.
  const [custom, setCustom] = useState<Record<string, number>>(() => Object.fromEntries(
    limits.map(s => [s.key, clamp(s, Number(r.values[s.key] || s.default))])))
  const [touched, setTouched] = useState<Set<string>>(() => new Set())
  const move = (key: string, v: number) => { setCustom(c => ({ ...c, [key]: v })); setTouched(t => new Set(t).add(key)) }
  const initial: Record<string, string> = Object.fromEntries([
    ...(secret ? [[secret.key, '']] : []), ...advanced.map(s => [s.key, r.values[s.key] ?? ''])])
  const chosen = r.presets.find(p => p.name === plan)
  const adjustable = limits.filter(s => chosen?.adjustable?.includes(s.key))
  /** The sliders the plan shows: every limit for Custom, the ones a plan leaves adjustable. */
  const shownLimits = plan === CUSTOM ? limits : adjustable
  const form = useAppForm({
    defaultValues: initial,
    onSubmit: async ({ value }) => {
      const values: Record<string, string> = {}
      if (secret && value[secret.key]?.trim()) values[secret.key] = value[secret.key].trim()
      for (const s of advanced) {
        const v = (value[s.key] ?? '').trim()
        if (mode === 'add' ? !!v : v !== (initial[s.key] ?? '')) values[s.key] = v
      }
      // The plan, when chosen now (the server resolves its limits), and the sliders it shows: each one
      // moved, and every one when adding (what they show is what the runtime starts with).
      for (const s of shownLimits) if (mode === 'add' || touched.has(s.key)) values[s.key] = String(clamp(s, custom[s.key]))
      const body = mode === 'add' || plan !== startPlan ? { plan, values } : { values }
      await flow.submit(() => mode === 'add' ? api.runtimeSettings.add(r.kind, body) : api.runtimeSettings.update(r.kind, body))
      onDone?.()
    },
  })
  /** The key is needed when adding, and when a setting that says where it goes (the domain) changes. */
  const keyNeeded = (get: (key: string) => string) => mode === 'add' || !keySet
    || advanced.some(s => s.rekey && (get(s.key) ?? '').trim() !== (initial[s.key] ?? ''))
  const source = r.presets.find(p => p.source)?.source
  /** A plan with limits it does not fix starts them at its own values (Pro: 8 vCPUs, 8 GB), unless set already. */
  const choose = (name: string) => {
    const p = r.presets.find(x => x.name === name)
    for (const key of p?.adjustable ?? []) {
      const s = limits.find(x => x.key === key)
      if (s && !r.values[key] && !touched.has(key)) setCustom(c => ({ ...c, [key]: clamp(s, Number(p!.values[key])) }))
    }
    setPlan(name)
  }
  return <AppForm form={form} aria-label={`${r.title} settings`} className="space-y-5">
    {secret && <form.AppField name={secret.key} validators={{
      onChangeListenTo: advanced.filter(s => s.rekey).map(s => s.key),
      onChange: ({ value, fieldApi }) => !value?.trim() && keyNeeded(k => fieldApi.form.getFieldValue(k))
        ? (mode === 'add' || !keySet ? `Enter the ${secret.label}.` : `Enter the ${secret.label} again to change the domain.`)
        : undefined,
    }}>
      {field => <field.TextField label={secret.label} type="password" autoComplete="off" spellCheck={false}
        placeholder={keySet ? 'Saved · type to replace' : undefined}
        help={keySet ? undefined : secret.help || undefined} />}
    </form.AppField>}

    {r.presets.length > 0 && <fieldset className="min-w-0">
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <legend className="text-[12.5px] font-medium">Plan</legend>
        {source && <a href={source} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-[12px] text-muted underline-offset-2 hover:text-ink hover:underline">
          Check your plan<ExternalLink aria-hidden className="size-3" /><span className="sr-only">(opens in a new tab)</span></a>}
      </div>
      <div className="grid gap-2 sm:grid-cols-3">
        {r.presets.map(p => <PlanCard key={p.name} group={r.kind} name={p.name} title={p.title} lines={planLines(r, presetValues(p, limits), p.adjustable)} checked={plan === p.name} onChoose={choose} />)}
        <PlanCard group={r.kind} name={CUSTOM} title="Custom" lines={['Your own limits', '']} checked={plan === CUSTOM} onChoose={choose} />
      </div>
      {(plan === CUSTOM || adjustable.length > 0) && <div className="mt-4 space-y-3">
        {chosen?.note && <p className="text-[12.5px] text-muted">{chosen.note}</p>}
        <div className="grid gap-x-8 gap-y-4 sm:grid-cols-2">
          {ordered(shownLimits).map(s => <LimitRow key={s.key} setting={s} value={custom[s.key]} onChange={v => move(s.key, v)} />)}
        </div>
      </div>}
    </fieldset>}

    {advanced.length > 0 && <details className="group rounded-lg border border-line" open={advanced.some(s => !!r.values[s.key]) || undefined}>
      <summary className="flex cursor-pointer list-none items-center gap-1.5 px-3 py-2 text-[12.5px] font-medium text-muted hover:text-ink [&::-webkit-details-marker]:hidden">
        <ChevronRight aria-hidden className="size-3.5 transition-transform duration-150 ease-out group-open:rotate-90 motion-reduce:transition-none" />Advanced
      </summary>
      <div className="space-y-3 border-t border-line px-3 py-3">
        {advanced.map(s => <form.AppField key={s.key} name={s.key} validators={{ onChange: ({ value }) => fieldError(s, value) }}>
          {field => <field.TextField label={s.label} optional={s.optional} autoComplete="off" spellCheck={false} placeholder={s.default}
            help={s.rekey && secret && keySet ? `${s.help} Changing it needs the ${secret.label} again.` : s.help || undefined} />}
        </form.AppField>)}
      </div>
    </details>}

    <div className="flex flex-wrap items-center justify-end gap-2">
      {onCancel && <Button variant="ghost" onClick={onCancel}>Cancel</Button>}
      <SubmitButton form={form} disabled={flow.busy}>{mode === 'add' ? `Add ${r.title}` : 'Save'}</SubmitButton>
    </div>
  </AppForm>
}

function presetValues(p: RuntimePreset, limits: RuntimeSetting[]): Record<string, string> {
  return Object.fromEntries(limits.map(s => [s.key, p.values[s.key] ?? s.default]))
}

/** A plan as a card: a native radio (arrow keys move between plans), the card shows it chosen. */
function PlanCard({ group, name, title, lines, checked, onChoose }: {
  group: string; name: string; title: string; lines: [string, string]; checked: boolean; onChoose: (name: string) => void
}) {
  return <label className="relative block cursor-pointer">
    <input type="radio" name={`${group}-plan`} value={name} checked={checked} onChange={() => onChoose(name)} className="peer sr-only" />
    <span className={cn('flex h-full flex-col rounded-lg border bg-panel px-3 py-2.5 transition-[border-color,box-shadow,transform] duration-150 ease-out',
      'active:scale-[0.98] motion-reduce:transform-none peer-focus-visible:outline peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-[var(--accent)]',
      checked ? 'border-accent ring-1 ring-accent' : 'border-line [@media(hover:hover)]:hover:border-line-strong')}>
      <span className="flex items-center justify-between gap-2 font-medium">{title}
        <Check aria-hidden className={cn('size-3.5 text-accent transition-opacity duration-150', checked ? 'opacity-100' : 'opacity-0')} /></span>
      {lines.map((line, i) => <span key={i} className="block min-h-[1.25rem] whitespace-nowrap text-[12px] leading-5 text-muted tabular-nums">{line}</span>)}
    </span>
  </label>
}

/** One limit: its label left, its value right (the one place it shows, and where to type it exactly: Enter or
 * leaving the field snaps it to the step and range), the slider below (arrows step, Shift+arrow ten steps). */
function LimitRow({ setting: s, value, onChange }: { setting: RuntimeSetting; value: number; onChange: (v: number) => void }) {
  const id = useId()
  const u = unitOf(s)
  const [entry, setEntry] = useState<string | null>(null)   // what is typed, until it is left
  const now = shown(u, value)
  const commit = () => {
    const typed = Number(entry)
    if (entry !== null && entry.trim() !== '' && Number.isFinite(typed)) onChange(Math.round(snap(u, typed) * u.factor))
    setEntry(null)
  }
  const ten = (e: React.KeyboardEvent<HTMLInputElement>) => {
    const by = { ArrowRight: 1, ArrowUp: 1, ArrowLeft: -1, ArrowDown: -1 }[e.key]
    if (!e.shiftKey || !by) return
    e.preventDefault()
    onChange(Math.round(snap(u, value / u.factor + by * 10 * u.step) * u.factor))
  }
  return <div>
    <div className="flex h-7 items-center justify-between gap-3">
      <label htmlFor={id} className="text-[12.5px] font-medium">{s.label}</label>
      <span className="flex items-baseline text-[13px] tabular-nums">
        <input type="text" inputMode="decimal" aria-label={`${s.label} (exact)`} value={entry ?? now} size={Math.max(2, (entry ?? now).length)}
          onChange={e => setEntry(e.target.value)} onBlur={commit} onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); commit() } }}
          className="!min-h-0 !w-auto rounded !border-transparent !bg-transparent !px-1 py-0 text-right tabular-nums hover:!border-line focus-visible:!border-accent" />
        {u.unit && <span className="pl-0.5 text-muted">{u.unit}</span>}
      </span>
    </div>
    <input id={id} type="range" min={u.min} max={u.max} step={u.step} value={value / u.factor}
      aria-valuetext={`${now}${u.unit ? ` ${u.unit}` : ''}`}
      onChange={e => onChange(Math.round(Number(e.target.value) * u.factor))} onKeyDown={ten}
      className="block h-5 w-full cursor-pointer accent-[var(--accent)]" />
  </div>
}

function fieldError(s: RuntimeSetting, value: string) {
  const v = (value ?? '').trim()
  if (!v) return undefined
  // The server's own pattern (runtime.base.HOSTNAME); it checks again.
  if (s.kind === 'hostname' && s.pattern && !new RegExp(`^(?:${s.pattern})\\.?$`, 'i').test(v)) return 'Enter a host name such as e2b.app.'
  return undefined
}
