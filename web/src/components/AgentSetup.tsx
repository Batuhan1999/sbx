import type { Agent } from '@/api/types'
import { CopyCommand } from './CopyCommand'

/** `sbx repo connect --agent COMMAND`, one per agent the server offers; with
 * none, the bare flag (the CLI's default agent, or one the reader names). */
export function AgentSetupCommands({ agents, className }: { agents: Agent[]; className?: string }) {
  if (!agents.length) return <CopyCommand command="sbx repo connect --agent" className={className} label="agent setup command" />
  return <div className="space-y-2">{agents.map(a =>
    <CopyCommand key={a.command} command={`sbx repo connect --agent ${a.command}`} className={className} label={`${a.title} setup command`} />)}
  </div>
}

export const agentWords = (agents: Agent[]) =>
  agents.length ? agents.map(a => a.title).join(' or ') : 'your agent CLI (claude unless --agent names another)'
