import { api } from '@/api/endpoints'
import { useAgents, useMe } from '@/api/queries'
import { AppForm, SubmitButton, useAppForm } from '@/components/form'
import { passwordError } from '@/lib/validation'
import { AgentSetupCommands, agentWords } from '@/components/AgentSetup'
import { CopyCommand } from '@/components/CopyCommand'
import { PageHeader } from '@/components/PageHeader'
import { Panel } from '@/components/ui/primitives'
import { toast } from 'sonner'

const daily = (agents: string[]): [string, string][] => [
  ['sbx up my-feature', 'Start a sandbox for the branch you are on; prints its preview links when the app is healthy'],
  ['sbx ls', 'Your sandboxes'],
  ['sbx open my-feature code', `Open it in VS Code (also ${['cursor', 'zed', 'ssh', ...agents].join(', ')})`],
  ['sbx logs my-feature', 'Follow the app’s logs'],
  ['sbx env ls', 'The repository’s env on the server, names and sources'],
  ['sbx down my-feature', 'Delete it and the resources it owns'],
]

export default function CliPage() {
  const me = useMe()
  const agents = useAgents()
  const base = me.data?.base_url || location.origin
  return <div className="max-w-3xl">
    <PageHeader title="CLI & access" description="The sbx CLI is how you and your coding agent work with this server from a repository checkout." />
    <div className="space-y-4">
      <Panel title="1. Install and sign in" description="On macOS. Needs websocat for SSH; sbx doctor checks.">
        <div className="space-y-2">
          <CopyCommand command={`curl -fsSL ${base}/install | sh`} label="install command" />
          <CopyCommand command="sbx login" label="sign in command" />
        </div>
      </Panel>
      <Panel title="2. Set a repository up with your agent" description={`In a checkout: starts ${agentWords(agents)} with this server's setup brief. It writes .sbx/, tries it in a sandbox and opens a pull request.`}>
        <AgentSetupCommands agents={agents} />
      </Panel>
      <Panel title="3. Every day" bodyClassName="p-0">
        <ul className="divide-y divide-line">{daily(agents.map(a => a.command)).map(([cmd, what]) => <li key={cmd} className="grid gap-1.5 px-4 py-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] sm:items-center sm:gap-4">
          <CopyCommand command={cmd} label={what} /><span className="text-[13px] text-muted">{what}</span>
        </li>)}</ul>
      </Panel>
      {me.data?.login === 'password' && <ChangePassword />}
    </div>
  </div>
}

function ChangePassword() {
  const form = useAppForm({
    defaultValues: { current: '', password: '', confirm: '' },
    onSubmit: async ({ value }) => { await api.changePassword(value.current, value.password); form.reset(); toast.success('Password changed', { description: 'You were signed out everywhere else.' }) },
  })
  return <Panel title="Your password" description="Changing it signs you out everywhere else.">
    <AppForm form={form} className="space-y-4">
      <input type="email" autoComplete="username" value={useMe().data?.email ?? ''} readOnly hidden />
      <form.AppField name="current" validators={{ onChange: ({ value }) => (value ? undefined : 'Enter your current password.') }}>
        {field => <field.TextField label="Current password" type="password" autoComplete="current-password" />}
      </form.AppField>
      <div className="grid gap-3 sm:grid-cols-2">
        <form.AppField name="password" validators={{ onChange: ({ value }) => passwordError(value) }}>
          {field => <field.TextField label="New password" type="password" autoComplete="new-password" help="At least 8 characters." />}
        </form.AppField>
        <form.AppField name="confirm" validators={{ onChangeListenTo: ['password'], onChange: ({ value, fieldApi }) => !value ? 'Repeat the password.' : value !== fieldApi.form.getFieldValue('password') ? 'The passwords differ.' : undefined }}>
          {field => <field.TextField label="Repeat new password" type="password" autoComplete="new-password" />}
        </form.AppField>
      </div>
      <div className="flex items-center justify-end gap-3"><SubmitButton form={form}>Change password</SubmitButton></div>
    </AppForm>
  </Panel>
}
