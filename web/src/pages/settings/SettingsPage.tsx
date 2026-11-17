import { Globe, LogIn } from 'lucide-react'
import { useEffect } from 'react'
import { Navigate, useLocation, useParams } from 'react-router'
import { api } from '@/api/endpoints'
import { keys, useAction, useConnections, useMe, useSettings } from '@/api/queries'
import type { Settings } from '@/api/types'
import { AppForm, SubmitButton, useAppForm } from '@/components/form'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState, ErrorState, QueryView, SkeletonRows } from '@/components/states'
import { NavTabs } from '@/components/Tabs'
import { Panel } from '@/components/ui/primitives'
import { emailError } from '@/lib/validation'
import { ConnectionCard } from '../extensions/ConnectionCard'
import { Endpoints } from './Endpoints'
import { Runtimes } from './Runtimes'
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
  const { hash } = useLocation()
  const hasDomain = !!domain
  useEffect(() => {
    if (hash === '#domain' && hasDomain) document.getElementById('domain')?.scrollIntoView({ block: 'start' })
  }, [hash, hasDomain])
  return <div className="max-w-3xl space-y-4">
    {connections.error && <ErrorState error={connections.error} retry={() => connections.refetch()} />}
    {connections.isPending && <div className="rounded-lg border border-line bg-panel"><SkeletonRows rows={2} /></div>}
    {domain && <div id="domain" className="scroll-mt-16 lg:scroll-mt-4"><ConnectionCard c={domain} icon={<Globe className="size-4" />} /></div>}
    {signIn && <ConnectionCard c={signIn} icon={<LogIn className="size-4" />} />}
    <QueryView query={settings} loading={null}>{s => <SandboxDefaults key={s.revision} settings={s} />}</QueryView>
    <Runtimes />
    <Endpoints />
  </div>
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
