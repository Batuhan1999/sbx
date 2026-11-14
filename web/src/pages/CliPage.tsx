import { useAgents, useMe } from '@/api/queries'
import { AgentSetupCommands, agentWords } from '@/components/AgentSetup'
import { CopyCommand } from '@/components/CopyCommand'
import { PageHeader } from '@/components/PageHeader'
import { Panel } from '@/components/ui/primitives'

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
    </div>
  </div>
}
