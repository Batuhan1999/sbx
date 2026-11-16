import { Globe, LogIn } from 'lucide-react'
import { Navigate, useParams } from 'react-router'
import { api } from '@/api/endpoints'
import { keys, useAction, useConnections, useMe, useRuntimes, useSettings } from '@/api/queries'
import type { ConnectionState, SandboxRuntime, Settings } from '@/api/types'
import { AppForm, SubmitButton, useAppForm } from '@/components/form'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState, ErrorState, QueryView, SkeletonRows } from '@/components/states'
import { NavTabs } from '@/components/Tabs'
import { StateBadge } from '@/components/StatusBadge'
import { Badge, Panel } from '@/components/ui/primitives'
import { limitsText } from '@/lib/format'
import type { Tone } from '@/lib/status'
import { emailError } from '@/lib/validation'
import { CONNECTION_STATE, ConnectionCard } from '../extensions/ConnectionCard'
import { Updates } from './Updates'

const SECTIONS = { general: 'General', access: 'Access', updates: 'Updates' } as const

export default function SettingsPage() {
  const { section = 'general' } = useParams()
  const me = useMe()
  if (me.data && !me.data.admin) return <><PageHeader title="Settings" /><EmptyState title="Admins only">Ask an admin of this server to change its settings.</EmptyState></>
  if (!(section in SECTIONS)) return <Navigate to="/app/settings/general" replace />
  return <>
    <PageHeader title="Settings" description="This server: its address, sign-in, sandbox defaults, who has access and its release." />
    <NavTabs label="Settings sections" tabs={Object.entries(SECTIONS).map(([id, label]) => ({ to: `/app/settings/${id}`, label }))} />
    {section === 'general' && <General />}
    {section === 'access' && <Access />}
    {section === 'updates' && <Updates />}
  </>
}

function General() {
  const connections = useConnections()
  const settings = useSettings()
  const domain = connections.data?.find(c => c.kind === 'domain')
  const signIn = connections.data?.find(c => c.kind === 'sign_in')
  return <div className="max-w-3xl space-y-4">
    {connections.error && <ErrorState error={connections.error} retry={() => connections.refetch()} />}
    {connections.isPending && <div className="rounded-lg border border-line bg-panel"><SkeletonRows rows={2} /></div>}
    {domain && <ConnectionCard c={domain} icon={<Globe className="size-4" />} />}
    {signIn && <ConnectionCard c={signIn} icon={<LogIn className="size-4" />} />}
    <QueryView query={settings} loading={null}>{s => <SandboxDefaults key={s.revision} settings={s} />}</QueryView>
    <Runtimes />
  </div>
}

/** Read-only: the runtimes are enabled in /etc/sbx/runtime.env on the server. */
export function Runtimes() {
  const runtimes = useRuntimes()
  return <Panel id="runtimes" title="Sandbox runtimes" bodyClassName="p-0"
    description="Where sandboxes run. Each sandbox runs on one, chosen when it is created; repositories without a choice of their own use the default. Enabled in /etc/sbx/runtime.env on the server.">
    <QueryView query={runtimes} inline loading={<SkeletonRows rows={1} />}>{d => <ul className="divide-y divide-line">
      {d.runtimes.map(r => <RuntimeRow key={r.kind} r={r} />)}
    </ul>}</QueryView>
  </Panel>
}

function RuntimeRow({ r }: { r: SandboxRuntime }) {
  const state = CONNECTION_STATE[r.status.state as ConnectionState] ?? { label: r.status.state, tone: 'idle' as Tone }
  const limits = limitsText(r.limits)
  return <li className="px-4 py-3">
    <div className="flex flex-wrap items-center gap-2">
      <h3 className="font-semibold">{r.title}</h3>
      {r.default && <Badge tone="accent">Default</Badge>}
      <StateBadge tone={state.tone} label={state.label} />
    </div>
    <p className="mt-0.5 break-words text-[13px] text-muted">{r.host ? 'Runs on this server' : 'Runs at the provider'}{r.status.summary ? `. ${r.status.summary}` : ''}</p>
    {limits && <p className="mt-0.5 text-[12px] text-muted">{limits}</p>}
  </li>
}

function SandboxDefaults({ settings }: { settings: Settings }) {
  const current = settings.sandboxes
  const save = useAction((v: { paused_days: number; detected_previews: string }) => api.settings.put('sandboxes', { ...current, ...v }, settings.revision), keys.settings)
  const form = useAppForm({
    defaultValues: { days: String(current.paused_days ?? 3), detected: current.detected_previews ?? 'owner' },
    onSubmit: async ({ value }) => { await save.mutateAsync({ paused_days: Number(value.days), detected_previews: value.detected }) },
  })
  return <Panel title="Sandboxes" description="Defaults for every repository.">
    <AppForm form={form} className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <form.AppField name="days" validators={{ onChange: ({ value }) => /^\d+(\.\d+)?$/.test(value) ? undefined : 'Enter a number of days; 0 keeps paused sandboxes.' }}>
          {field => <field.TextField label="Delete after paused (days)" inputMode="decimal" help="With their service resources. 0 keeps them." />}
        </form.AppField>
        <form.AppField name="detected">{field => <field.SelectField label="Detected previews" help="Links for servers the recipe does not declare.">
          <option value="owner">Owner only</option><option value="team">Everyone signed in</option><option value="off">Off</option>
        </field.SelectField>}</form.AppField>
      </div>
      <div className="flex items-center justify-end gap-3">{save.isSuccess && <span role="status" className="text-[12.5px] text-ok">Saved</span>}<SubmitButton form={form}>Save</SubmitButton></div>
    </AppForm>
  </Panel>
}

function Access() {
  const settings = useSettings()
  return <div className="max-w-3xl"><QueryView query={settings}>{s => <AccessForm key={s.revision} settings={s} />}</QueryView></div>
}

function AccessForm({ settings }: { settings: Settings }) {
  const save = useAction((v: { admins: string[]; public_repos: boolean }) => api.settings.put('access', { ...settings.access, ...v }, settings.revision), keys.settings, keys.me)
  const form = useAppForm({
    defaultValues: { admins: settings.access.admins.join('\n'), public_repos: settings.access.public_repos },
    onSubmit: async ({ value }) => { await save.mutateAsync({ admins: value.admins.split(/[\s,]+/).filter(Boolean).map(e => e.toLowerCase()), public_repos: value.public_repos }) },
  })
  return <Panel title="Access" description={<>Who may sign in is set under Sign-in ({settings.sign_in.method}{settings.sign_in.allowed_emails.length ? `: ${settings.sign_in.allowed_emails.join(', ')}` : ''}). Admins change settings, connect repositories and read every sandbox.</>}>
    <AppForm form={form} className="space-y-4">
      <form.AppField name="admins" validators={{ onChange: ({ value }) => {
        const emails = value.split(/[\s,]+/).filter(Boolean)
        if (!emails.length) return 'Keep at least one admin.'
        const bad = emails.find(e => emailError(e))
        return bad ? `${bad} is not an email address.` : undefined
      } }}>{field => <field.TextareaField label="Admins" rows={4} help="One email per line." />}</form.AppField>
      {(settings.offered.public_repos || settings.access.public_repos) && <form.AppField name="public_repos">{field => <field.CheckboxField label="Serve any public repository" help="Untrusted: cloned anonymously, with no secrets or services." />}</form.AppField>}
      <div className="flex items-center justify-end gap-3">{save.isSuccess && <span role="status" className="text-[12.5px] text-ok">Saved</span>}<SubmitButton form={form}>Save</SubmitButton></div>
    </AppForm>
  </Panel>
}
