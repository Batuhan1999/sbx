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
import { Switch } from '@/components/ui/switch'
import { useId } from 'react'
import { ConnectionCard } from '../extensions/ConnectionCard'
import { Endpoints } from './Endpoints'
import { People } from './People'
import { Runtimes } from './Runtimes'
import { Updates } from './Updates'
import { toast } from 'sonner'

const SECTIONS = { general: 'General', people: 'People', updates: 'Updates' } as const

export default function SettingsPage() {
  const { section = 'general' } = useParams()
  const me = useMe()
  if (me.data && !me.data.admin) return <><PageHeader title="Settings" /><EmptyState title="Admins only">Ask an admin of this server to change its settings.</EmptyState></>
  if (!(section in SECTIONS)) return <Navigate to="/app/settings/general" replace />
  return <>
    <PageHeader title="Settings" description="This server: its address, sign-in, sandbox defaults, who has access and its release." />
    <NavTabs label="Settings sections" tabs={Object.entries(SECTIONS).map(([id, label]) => ({ to: `/app/settings/${id}`, label }))} />
    {section === 'general' && <General />}
    {section === 'people' && <PeopleSection />}
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
    onSubmit: async ({ value }) => { await save.mutateAsync({ paused_days: Number(value.days), detected_previews: value.detected }); toast.success('Sandbox defaults saved') },
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
      <div className="flex items-center justify-end gap-3"><SubmitButton form={form}>Save</SubmitButton></div>
    </AppForm>
  </Panel>
}

function PeopleSection() {
  const settings = useSettings()
  return <div className="max-w-4xl space-y-6">
    <People />
    <QueryView query={settings} loading={null}>{s => (s.offered.public_repos || s.access.public_repos) ? <PublicRepos key={s.revision} settings={s} /> : null}</QueryView>
  </div>
}

function PublicRepos({ settings }: { settings: Settings }) {
  const save = useAction((public_repos: boolean) => api.settings.put('access', { ...settings.access, public_repos }, settings.revision), keys.settings)
  const checked = save.isPending ? !!save.variables : settings.access.public_repos
  const id = useId()
  return <section className="space-y-3">
    <h2 className="text-[16px] font-semibold tracking-tight">Repositories</h2>
    <div className="rounded-lg border border-line bg-panel">
      <div className="flex items-center justify-between gap-6 p-4">
      <label htmlFor={id} className="min-w-0 cursor-pointer space-y-0.5">
        <span className="block text-[13.5px] font-medium">Serve any public repository</span>
        <span id={`${id}-hint`} className="block text-[12.5px] text-muted">Anyone signed in can start a sandbox from a public repository. It runs untrusted: cloned anonymously, with no secrets or services.</span>
      </label>
      <Switch id={id} checked={checked} disabled={save.isPending} aria-describedby={`${id}-hint`} onCheckedChange={v => save.mutate(v, { onSuccess: () => toast.success(v ? 'Public repositories are served' : 'Public repositories are no longer served') })} />
    </div>
    {save.error ? <div className="border-t border-line px-4 py-3"><ErrorState error={save.error} compact /></div> : null}
    </div>
  </section>
}
