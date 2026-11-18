import { useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, ExternalLink, Link2 } from 'lucide-react'
import { useState } from 'react'
import { useParams } from 'react-router'
import { api } from '@/api/endpoints'
import { keys } from '@/api/queries'
import type { RuntimeApply, SecretRequest, SecretRequestItem } from '@/api/types'
import { AppForm, SubmitButton, useAppForm } from '@/components/form'
import { ErrorState, Loading } from '@/components/states'
import { Spinner } from '@/components/ui/primitives'
import { ago } from '@/lib/format'
import { AuthFrame } from './SignInPage'

const TITLE = 'Your agent needs these keys'
const field = (n: number) => `k${n}`

/** The keys page an agent sends its owner (/keys/ID): one masked field per key, each saved straight to where it is
 *  used. Signed-in admins only: a 401 sends the browser to the sign-in and back here (api/client). */
export default function KeysPage() {
  const { id = '' } = useParams()
  const request = useQuery({
    queryKey: keys.secretRequest(id), queryFn: () => api.secretRequests.get(id), retry: false, refetchOnWindowFocus: false,
    // Followed while the server applies runtime keys; a poll its restart fails keeps the last answer.
    refetchInterval: q => (IN_PROGRESS.has(q.state.data?.runtime_changes?.state ?? '') ? 3000 : false),
  })
  if (request.isPending) return <AuthFrame title={TITLE} wide><Loading /></AuthFrame>
  if (!request.data) return <AuthFrame title={TITLE} wide><ErrorState error={request.error} compact retry={() => request.refetch()} /></AuthFrame>
  const found = request.data
  if (found.state !== 'pending') return <AuthFrame title={TITLE} wide><Finished request={found} /></AuthFrame>
  return <AuthFrame title={TITLE} wide description={<>
    {found.note && <span className="block text-ink">{found.note}</span>}
    <span className="block text-[12.5px]">Never shown again · expires {ago(found.expires_at)}</span>
  </>}>
    <KeysForm request={found} />
  </AuthFrame>
}

const IN_PROGRESS = new Set(['queued', 'applying', 'restarting'])
const APPLYING: Record<string, string> = {
  queued: 'waiting for the server', applying: 'being applied on the server', restarting: 'applied; the server is restarting to use them',
}

/** Where the server is with the runtime keys (one change for all of them), while it matters. */
function RuntimeProgress({ apply }: { apply: RuntimeApply }) {
  if (IN_PROGRESS.has(apply.state)) return <p className="flex items-center gap-2 text-[12.5px] text-muted">
    <Spinner className="size-3.5" />The runtime keys are {APPLYING[apply.state]}.
  </p>
  if (apply.state === 'failed') return <ErrorState compact error={`The server could not apply a runtime key: ${apply.message || 'see Settings → Sandbox runtimes'}. Ask your agent for a new link to paste it again.`} />
  return null
}

function Finished({ request }: { request: SecretRequest }) {
  if (request.state === 'completed') return <div className="space-y-3">
    {request.runtime_changes.state !== 'failed' && <div className="flex items-start gap-2.5 text-[13.5px]">
      <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-ok" />
      <p>All {request.items.length} keys are saved. {IN_PROGRESS.has(request.runtime_changes.state)
        ? 'Your agent carries on once the server has applied them.' : 'You can close this page; your agent carries on.'}</p>
    </div>}
    <RuntimeProgress apply={request.runtime_changes} />
  </div>
  return <ErrorState compact error={request.state === 'expired'
    ? 'This page has expired. Ask your agent for a new link.'
    : 'Your agent cancelled this request. Ask it for a new link if it still needs keys.'} />
}

function KeysForm({ request }: { request: SecretRequest }) {
  const queryClient = useQueryClient()
  const [reuseErrors, setReuseErrors] = useState<Record<string, string>>({})
  const form = useAppForm({
    defaultValues: Object.fromEntries(request.items.map((_, n) => [field(n), ''])) as Record<string, string>,
    onSubmit: async ({ value, formApi }) => {
      const values: Record<string, string> = {}
      request.items.forEach((item, n) => {
        const pasted = value[field(n)]?.trim()
        if (!item.done && !item.reuse && pasted) values[item.target] = pasted
      })
      if (!Object.keys(values).length && !request.items.some(i => !i.done && i.reuse)) throw new Error('Paste at least one key.')
      const result = await api.secretRequests.submit(request.id, values)
      const outcomes = new Map(result.results.map(r => [r.target, r]))
      const failedReuse: Record<string, string> = {}
      request.items.forEach((item, n) => {
        const outcome = outcomes.get(item.target)
        if (!outcome) return
        if (item.reuse) {
          if (!outcome.ok) failedReuse[item.target] = outcome.error
        } else if (outcome.ok) {
          formApi.setFieldValue(field(n), '')   // a saved key leaves form memory at once
        } else {
          formApi.setFieldMeta(field(n), m => ({ ...m, errorMap: { ...m.errorMap, onSubmit: outcome.error } }))
        }
      })
      setReuseErrors(failedReuse)
      queryClient.setQueryData(keys.secretRequest(request.id), result.request)
    },
  })
  return <AppForm form={form} className="space-y-5">
    {request.items.map((item, n) => <div key={item.target}>
      {item.done ? <Saved item={item} />
        : item.reuse ? <Reused item={item} error={reuseErrors[item.target]} asked={request.items.find(i => i.target === item.reuse)} />
        : <form.AppField name={field(n)}
          validators={{ onSubmit: () => undefined }}
          listeners={{ onChange: ({ fieldApi }) => fieldApi.setMeta(m => ({ ...m, errorMap: { ...m.errorMap, onSubmit: undefined } })) }}>
          {f => <f.TextField label={item.label} type="password" autoComplete="off" spellCheck={false}
            help={item.help_url ? undefined : capital(item.what)} />}
        </form.AppField>}
      {!item.done && !item.reuse && item.help_url && <a href={item.help_url} target="_blank" rel="noreferrer"
        className="mt-1 inline-flex items-center gap-1 text-[12px] text-accent hover:underline">How do I get this?<ExternalLink className="size-3" /></a>}
    </div>)}
    <RuntimeProgress apply={request.runtime_changes} />
    <SubmitButton form={form} className="w-full">Save keys</SubmitButton>
  </AppForm>
}

function Saved({ item }: { item: SecretRequestItem }) {
  return <div className="flex items-center gap-2 text-[13px]">
    <CheckCircle2 className="size-4 shrink-0 text-ok" />
    <span className="font-medium">{item.label}</span>
    <span className={item.applied?.state === 'failed' ? 'text-bad' : 'text-muted'}>
      {!item.applied || item.applied.state === 'done' || item.applied.state === 'unknown' ? 'saved' : item.applied.state === 'failed' ? 'not applied' : 'saved, applying'}
    </span>
  </div>
}

/** A key the server holds, or one pasted on this page (asked), used again: nothing to paste. */
function Reused({ item, error, asked }: { item: SecretRequestItem; error?: string; asked?: SecretRequestItem }) {
  return <div className="text-[13px]">
    <div className="flex items-center gap-2">
      <Link2 className="size-4 shrink-0 text-muted" />
      <span className="font-medium">{item.label}</span>
    </div>
    <p className={error ? 'mt-1 text-[12px] text-bad' : 'mt-1 text-[12px] text-muted'}>
      {error || (asked && !asked.done
        ? `Nothing to paste: the ${asked.label} you paste here is used for ${item.what} too.`
        : `Nothing to paste: saving uses ${item.reuse_from}, which this server already has, for ${item.what}.`)}
    </p>
  </div>
}

const capital = (text: string) => text.charAt(0).toUpperCase() + text.slice(1)
