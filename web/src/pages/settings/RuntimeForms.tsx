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
import { gigabytes, planSummary, runLength } from '@/lib/format'
import { cn } from '@/lib/utils'

const CUSTOM = 'custom'
const VERBS: Record<string, string> = { add: 'Adding', change: 'Changing', remove: 'Removing', default: 'Making the default:' }

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

/** The change in flight or the last one's outcome: applying, failed (why), or done (only right after this page asked for it). */
export function ChangeStatus({ flow, title }: { flow: ChangeFlow; title?: (kind: string) => string }) {
  const c = flow.change
  if (!c || c.state === 'idle') return null
  const what = `${VERBS[c.operation] ?? 'Changing'} ${title?.(c.kind) || c.kind}`
  if (flow.busy) return <div role="status" className="rounded-lg border border-line bg-sunken px-3 py-2 text-[13px]">
    <Spinner label={`${what}…`} />
    <p className="mt-1 text-[12.5px] text-muted">The server applies it and restarts; this page follows on its own.</p>
  </div>
  if (c.state === 'failed') return <ErrorState compact error={`${what} failed: ${c.message || 'see the server log'}`} />
  if (c.state === 'done' && flow.mine) return <p role="status" className="text-[12.5px] text-ok">{c.message || 'Done.'}</p>
  return null
}

// Limits, in people's units: a run in minutes and hours, memory and disk in GB, counts as they are.

/** A limit's value in words: "1 h 30 min", "8 GB", "8 vCPU", "10 at once". */
export function limitText(s: RuntimeSetting, value: number): string {
  if (s.unit === 'seconds') return runLength(value)
  if (s.unit === 'MiB') return gigabytes(value)
  if (s.limit === 'cpu') return `${value} vCPU`
  if (s.limit === 'sandboxes') return `${value} at once`
  return String(value)
}

/** The precise entry beside a slider: minutes for a run, GB for memory and disk. */
function entryUnit(s: RuntimeSetting): { label: string; factor: number } {
  if (s.unit === 'seconds') return { label: 'min', factor: 60 }
  if (s.unit === 'MiB') return { label: 'GB', factor: 1024 }
  return { label: '', factor: 1 }
}

const clamp = (s: RuntimeSetting, v: number) => {
  const step = s.step ?? 1
  const stepped = Math.round(v / step) * step
  return Math.min(s.max ?? stepped, Math.max(s.min ?? 1, stepped))
}

/** What a set of limit values (by setting key) says, as planSummary reads it; a limit in
 * adjustable reads as "at least" ("8+ vCPU"): the plan allows more without a published number. */
export function presetSummary(r: { settings: RuntimeSetting[] }, values: Record<string, string>, adjustable: string[] = []): string {
  return r.settings.filter(s => s.limit && values[s.key]).map(s => {
    const text = planSummary({ [s.limit]: Number(values[s.key]) })
    return adjustable.includes(s.key) ? text.replace(/^([\d.]+)/, '$1+') : text
  }).join(' · ')
}

/** The preset whose fixed values the current ones are, else custom. */
function presetOf(r: AvailableRuntime) {
  const effective = (key: string) => r.values[key] || r.settings.find(s => s.key === key)?.default || ''
  return r.presets.find(p => Object.entries(p.values).every(([k, v]) => p.adjustable?.includes(k) || effective(k) === v))?.name ?? CUSTOM
}

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
  const [plan, setPlan] = useState(mode === 'add' ? (r.presets[0]?.name ?? CUSTOM) : presetOf(r))
  const [custom, setCustom] = useState<Record<string, number>>(() => Object.fromEntries(
    limits.map(s => [s.key, clamp(s, Number(r.values[s.key] || s.default))])))
  const initial: Record<string, string> = Object.fromEntries([
    ...(secret ? [[secret.key, '']] : []), ...advanced.map(s => [s.key, r.values[s.key] ?? ''])])
  const form = useAppForm({
    defaultValues: initial,
    onSubmit: async ({ value }) => {
      const values: Record<string, string> = {}
      if (secret && value[secret.key]?.trim()) values[secret.key] = value[secret.key].trim()
      for (const s of advanced) {
        const v = (value[s.key] ?? '').trim()
        if (v || mode === 'change') values[s.key] = v
      }
      const preset = r.presets.find(p => p.name === plan)
      for (const s of limits) {
        values[s.key] = preset && !preset.adjustable?.includes(s.key) ? preset.values[s.key] ?? s.default : String(clamp(s, custom[s.key]))
      }
      await flow.submit(() => mode === 'add' ? api.runtimeSettings.add(r.kind, values) : api.runtimeSettings.update(r.kind, values))
      onDone?.()
    },
  })
  /** The key is needed when adding, and when a setting that says where it goes (the domain) changes. */
  const keyNeeded = (get: (key: string) => string) => mode === 'add' || !keySet
    || advanced.some(s => s.rekey && (get(s.key) ?? '').trim() !== (initial[s.key] ?? ''))
  const source = r.presets.find(p => p.source)?.source
  const chosen = r.presets.find(p => p.name === plan)
  const adjustable = limits.filter(s => chosen?.adjustable?.includes(s.key))
  /** A plan with limits it does not fix starts them at its own values (Pro: 8 vCPUs, 8 GB), unless set already. */
  const choose = (name: string) => {
    const p = r.presets.find(x => x.name === name)
    for (const key of p?.adjustable ?? []) {
      const s = limits.find(x => x.key === key)
      if (s && !r.values[key]) setCustom(c => ({ ...c, [key]: clamp(s, Number(p!.values[key])) }))
    }
    setPlan(name)
  }
  return <AppForm form={form} aria-label={`${r.title} settings`} className="space-y-5">
    {secret && <form.AppField name={secret.key} validators={{
      onChangeListenTo: advanced.filter(s => s.rekey).map(s => s.key),
      onChange: ({ value, fieldApi }) => !value?.trim() && keyNeeded(k => fieldApi.form.getFieldValue(k))
        ? (mode === 'add' || !keySet ? `Enter the ${secret.label}.` : `Type the ${secret.label} again: the one set is never sent to a new domain.`)
        : undefined,
    }}>
      {field => <field.TextField label={secret.label} type="password" autoComplete="off" spellCheck={false}
        placeholder={keySet ? 'Set · type to replace' : undefined}
        help={keySet ? 'Set. Leave it empty to keep it.' : secret.help || undefined} />}
    </form.AppField>}

    {r.presets.length > 0 && <fieldset className="min-w-0">
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <legend className="text-[12.5px] font-medium">Plan</legend>
        {source && <a href={source} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-[12px] text-muted underline-offset-2 hover:text-ink hover:underline">
          Check your plan<ExternalLink aria-hidden className="size-3" /><span className="sr-only">(opens in a new tab)</span></a>}
      </div>
      <div className="grid gap-2 sm:grid-cols-3">
        {r.presets.map(p => <PlanCard key={p.name} group={r.kind} name={p.name} title={p.title} summary={presetSummary(r, presetValues(p, limits), p.adjustable)} checked={plan === p.name} onChoose={choose} />)}
        <PlanCard group={r.kind} name={CUSTOM} title="Custom" summary="Set each limit yourself" checked={plan === CUSTOM} onChoose={choose} />
      </div>
      {(plan === CUSTOM || adjustable.length > 0) && <div className="mt-3 space-y-4 rounded-lg border border-line bg-sunken/60 p-3 sm:p-4">
        {chosen?.note && <p className="text-[12.5px] text-muted">{chosen.note}</p>}
        {(plan === CUSTOM ? limits : adjustable).map(s => <LimitSlider key={s.key} setting={s} value={custom[s.key]} onChange={v => setCustom(c => ({ ...c, [s.key]: v }))} />)}
      </div>}
    </fieldset>}

    {advanced.length > 0 && <details className="group rounded-lg border border-line" open={advanced.some(s => !!r.values[s.key]) || undefined}>
      <summary className="flex cursor-pointer list-none items-center gap-1.5 px-3 py-2 text-[12.5px] font-medium text-muted hover:text-ink [&::-webkit-details-marker]:hidden">
        <ChevronRight aria-hidden className="size-3.5 transition-transform duration-150 ease-out group-open:rotate-90 motion-reduce:transition-none" />Advanced
      </summary>
      <div className="space-y-3 border-t border-line px-3 py-3">
        {advanced.map(s => <form.AppField key={s.key} name={s.key} validators={{ onChange: ({ value }) => fieldError(s, value) }}>
          {field => <field.TextField label={s.label} optional={s.optional} autoComplete="off" spellCheck={false} placeholder={s.default}
            help={<>{s.help}{s.rekey && secret && keySet && <span className="block">Changing it needs the {secret.label} again.</span>}</>} />}
        </form.AppField>)}
      </div>
    </details>}

    <div className="flex flex-wrap items-center justify-end gap-2">
      {onCancel && <Button variant="ghost" onClick={onCancel}>Cancel</Button>}
      <SubmitButton form={form} disabled={flow.busy}>{mode === 'add' ? `Add ${r.title}` : 'Save settings'}</SubmitButton>
    </div>
  </AppForm>
}

function presetValues(p: RuntimePreset, limits: RuntimeSetting[]): Record<string, string> {
  return Object.fromEntries(limits.map(s => [s.key, p.values[s.key] ?? s.default]))
}

/** A plan as a card: a native radio (arrow keys move between plans), the card shows it chosen. */
function PlanCard({ group, name, title, summary, checked, onChoose }: {
  group: string; name: string; title: string; summary: string; checked: boolean; onChoose: (name: string) => void
}) {
  return <label className="relative block cursor-pointer">
    <input type="radio" name={`${group}-plan`} value={name} checked={checked} onChange={() => onChoose(name)} className="peer sr-only" />
    <span className={cn('flex h-full flex-col rounded-lg border bg-panel px-3 py-2.5 transition-[border-color,box-shadow,transform] duration-150 ease-out',
      'active:scale-[0.98] motion-reduce:transform-none peer-focus-visible:outline peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-[var(--accent)]',
      checked ? 'border-accent ring-1 ring-accent' : 'border-line [@media(hover:hover)]:hover:border-line-strong')}>
      <span className="flex items-center justify-between gap-2 font-medium">{title}
        <Check aria-hidden className={cn('size-3.5 text-accent transition-opacity duration-150', checked ? 'opacity-100' : 'opacity-0')} /></span>
      <span className="mt-0.5 text-[12px] leading-snug text-muted">{summary}</span>
    </span>
  </label>
}

/** One limit: a slider over its range with the value in words, and a precise entry in minutes or GB. */
function LimitSlider({ setting: s, value, onChange }: { setting: RuntimeSetting; value: number; onChange: (v: number) => void }) {
  const id = useId()
  const unit = entryUnit(s)
  const [entry, setEntry] = useState<string | null>(null)   // what is typed, until it is left
  const shown = entry ?? String(+(value / unit.factor).toFixed(2))
  const commit = () => {
    const typed = Number(entry)
    if (entry !== null && entry.trim() !== '' && Number.isFinite(typed)) onChange(clamp(s, typed * unit.factor))
    setEntry(null)
  }
  return <div>
    <div className="flex items-baseline justify-between gap-3">
      <label htmlFor={id} className="text-[12.5px] font-medium">{s.label}</label>
      <output htmlFor={id} className="text-[13px] tabular-nums">{limitText(s, value)}</output>
    </div>
    <div className="mt-1 flex items-center gap-3">
      <input id={id} type="range" min={s.min ?? 1} max={s.max ?? 100} step={s.step ?? 1} value={value}
        aria-valuetext={limitText(s, value)} onChange={e => onChange(Number(e.target.value))}
        className="h-6 min-w-0 flex-1 cursor-pointer accent-[var(--accent)]" />
      <span className="flex shrink-0 items-center gap-1.5">
        <input type="number" inputMode="decimal" aria-label={`${s.label}${unit.label ? ` in ${unit.label}` : ': exact value'}`}
          min={(s.min ?? 1) / unit.factor} max={(s.max ?? 100) / unit.factor} step={(s.step ?? 1) / unit.factor}
          value={shown} onChange={e => setEntry(e.target.value)} onBlur={commit} onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); commit() } }}
          className="w-20 text-right tabular-nums" />
        <span aria-hidden className="w-6 text-[12px] text-muted">{unit.label}</span>{/* kept when empty: the sliders line up */}
      </span>
    </div>
  </div>
}

function fieldError(s: RuntimeSetting, value: string) {
  const v = (value ?? '').trim()
  if (!v) return undefined
  if (s.kind === 'hostname' && !/^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\.?$/i.test(v)) return 'Enter a host name such as e2b.app.'
  return undefined
}
