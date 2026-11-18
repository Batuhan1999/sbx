import { useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, ExternalLink, Link2 } from 'lucide-react'
import { useState } from 'react'
import { useParams } from 'react-router'
import { api } from '@/api/endpoints'
import { keys } from '@/api/queries'
import type { SecretRequest, SecretRequestItem } from '@/api/types'
import { AppForm, SubmitButton, useAppForm } from '@/components/form'
import { ErrorState, Loading } from '@/components/states'
import { ago } from '@/lib/format'
import { AuthFrame } from './SignInPage'

const TITLE = 'Your agent needs these keys'
const field = (n: number) => `k${n}`

/** The keys page an agent sends its owner (/keys/ID): one masked field per key, each saved straight to where it is
 *  used. Signed-in admins only: a 401 sends the browser to the sign-in and back here (api/client). */
export default function KeysPage() {
  const { id = '' } = useParams()
  const request = useQuery({ queryKey: keys.secretRequest(id), queryFn: () => api.secretRequests.get(id), retry: false, refetchOnWindowFocus: false })
  if (request.isPending) return <AuthFrame title={TITLE} wide><Loading /></AuthFrame>
  if (request.error) return <AuthFrame title={TITLE} wide><ErrorState error={request.error} compact retry={() => request.refetch()} /></AuthFrame>
  const found = request.data
  if (found.state !== 'pending') return <AuthFrame title={TITLE} wide><Finished request={found} /></AuthFrame>
  return <AuthFrame title={TITLE} wide description={<>
    Paste each key once. It goes straight to where it is used and is never shown again.
    {found.note && <span className="mt-2 block text-ink">{found.note}</span>}
    <span className="mt-1 block text-[12.5px]">Asked by {found.created_by === 'setup' ? 'the setup' : found.created_by} · this page expires {ago(found.expires_at)}</span>
  </>}>
    <KeysForm request={found} />
  </AuthFrame>
}

function Finished({ request }: { request: SecretRequest }) {
  if (request.state === 'completed') return <div className="flex items-start gap-2.5 text-[13.5px]">
    <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-ok" />
    <p>All {request.items.length} keys are saved. You can close this page; your agent carries on.</p>
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
            help={item.hint ? `${capital(item.what)}. ${item.hint}` : capital(item.what)} />}
        </form.AppField>}
      {!item.done && !item.reuse && item.help_url && <a href={item.help_url} target="_blank" rel="noreferrer"
        className="mt-1 inline-flex items-center gap-1 text-[12px] text-accent hover:underline">How do I get this?<ExternalLink className="size-3" /></a>}
    </div>)}
    <SubmitButton form={form} className="w-full">Save keys</SubmitButton>
  </AppForm>
}

function Saved({ item }: { item: SecretRequestItem }) {
  return <div className="flex items-center gap-2 text-[13px]">
    <CheckCircle2 className="size-4 shrink-0 text-ok" />
    <span className="font-medium">{item.label}</span>
    <span className="text-muted">saved</span>
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
