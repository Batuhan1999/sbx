import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { api } from '@/api/endpoints'
import { isApplying, keys, useRuntimeChange } from '@/api/queries'
import type { AvailableRuntime, RuntimeChange, RuntimeSetting } from '@/api/types'
import { AppForm, SubmitButton, useAppForm, useStore } from '@/components/form'
import { ErrorState } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/primitives'

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

/** The preset whose values the current ones are, else custom. */
function presetOf(r: AvailableRuntime, values: Record<string, string>) {
  const effective = (s: RuntimeSetting) => values[s.key] || s.default
  return r.presets.find(p => Object.entries(p.values).every(([k, v]) => effective(r.settings.find(s => s.key === k)!) === v))?.name ?? CUSTOM
}

/** A runtime's settings: a provider's key (write-only: "set" until a new one is typed), its other
 * settings and, when it has plans, a plan that fills its limits (Custom unlocks them). */
export function RuntimeSettingsForm({ runtime: r, mode, flow, onDone, onCancel }: {
  runtime: AvailableRuntime; mode: 'add' | 'change'; flow: ChangeFlow; onDone?: () => void; onCancel?: () => void
}) {
  const planned = new Set(r.presets.flatMap(p => Object.keys(p.values)))
  const start: Record<string, string> = Object.fromEntries(r.settings.map(s => [s.key, s.secret ? '' : (r.values[s.key] ?? '')]))
  const first = mode === 'add' && r.presets.length ? r.presets[0] : null
  if (first) Object.assign(start, first.values)
  const form = useAppForm({
    defaultValues: { ...start, __preset: first ? first.name : presetOf(r, start) } as Record<string, string>,
    onSubmit: async ({ value }) => {
      const values: Record<string, string> = {}
      for (const s of r.settings) {
        const v = (value[s.key] ?? '').trim()
        if (s.secret ? v : (v || mode === 'change')) values[s.key] = v
      }
      await flow.submit(() => mode === 'add' ? api.runtimeSettings.add(r.kind, values) : api.runtimeSettings.update(r.kind, values))
      onDone?.()
    },
  })
  const preset = useStore(form.store, st => st.values.__preset)
  const choose = (name: string) => {
    form.setFieldValue('__preset', name)
    const p = r.presets.find(x => x.name === name)
    if (p) for (const [k, v] of Object.entries(p.values)) form.setFieldValue(k, v)
  }
  const locked = (s: RuntimeSetting) => preset !== CUSTOM && planned.has(s.key)
  const source = r.presets.find(p => p.name === preset)?.source
  return <AppForm form={form} aria-label={`${r.title} settings`} className="space-y-4">
    <div className="grid gap-3 sm:grid-cols-2">
      {r.settings.filter(s => !s.limit || !planned.has(s.key)).map(s => <form.AppField key={s.key} name={s.key}
        validators={{ onChange: ({ value }: { value: string }) => fieldError(s, value, mode === 'add' || !r.secrets_set.includes(s.key)) }}>
        {field => <field.TextField label={s.label} optional={s.optional} type={s.secret ? 'password' : 'text'} autoComplete="off" spellCheck={false}
          inputMode={s.kind === 'int' ? 'numeric' : undefined}
          placeholder={s.secret && r.secrets_set.includes(s.key) ? 'Set · type to replace' : s.default}
          help={s.secret && r.secrets_set.includes(s.key) ? 'Set. Left empty, it stays as it is.' : s.help || undefined} />}
      </form.AppField>)}
    </div>
    {r.presets.length > 0 && <fieldset className="space-y-3">
      <legend className="mb-1.5 text-[12.5px] font-medium">Plan limits</legend>
      <div role="radiogroup" aria-label="Plan" className="flex flex-wrap gap-2">
        {[...r.presets.map(p => [p.name, p.title] as const), [CUSTOM, 'Custom'] as const].map(([name, title]) =>
          <label key={name} className="inline-flex items-center gap-1.5 rounded-md border border-line bg-panel px-2.5 py-1 text-[13px]">
            <input type="radio" name={`${r.kind}-plan`} value={name} checked={preset === name} onChange={() => choose(name)} className="accent-[var(--accent)]" />{title}
          </label>)}
      </div>
      <p className="text-[12px] text-muted">{preset === CUSTOM ? 'Set each limit to what your plan allows.' : `Your plan's limits${source ? ` (${source})` : ''}. Choose Custom to change them.`}</p>
      <div className="grid gap-3 sm:grid-cols-2">
        {r.settings.filter(s => planned.has(s.key)).map(s => <form.AppField key={s.key} name={s.key}
          validators={{ onChange: ({ value }: { value: string }) => fieldError(s, value, false) }}>
          {field => <field.TextField label={s.label} inputMode="numeric" disabled={locked(s)} placeholder={s.default} help={s.help || undefined} />}
        </form.AppField>)}
      </div>
    </fieldset>}
    <div className="flex items-center justify-end gap-2">
      {onCancel && <Button variant="ghost" onClick={onCancel}>Cancel</Button>}
      <SubmitButton form={form} disabled={flow.busy}>{mode === 'add' ? `Add ${r.title}` : 'Save settings'}</SubmitButton>
    </div>
  </AppForm>
}

function fieldError(s: RuntimeSetting, value: string, needed: boolean) {
  const v = (value ?? '').trim()
  if (s.secret) return needed && !v ? `Enter the ${s.label.toLowerCase()}.` : undefined
  if (!v) return undefined
  if (s.kind === 'int' && !(/^\d+$/.test(v) && Number(v) >= 1)) return 'Enter a whole number, at least 1.'
  return undefined
}
