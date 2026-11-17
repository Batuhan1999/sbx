import type { ReactNode } from 'react'
import { useRuntimes } from '@/api/queries'
import type { ConnectionState, SandboxRuntime } from '@/api/types'
import { QueryView, SkeletonRows } from '@/components/states'
import { StateBadge } from '@/components/StatusBadge'
import { Badge, Panel } from '@/components/ui/primitives'
import { limitsText } from '@/lib/format'
import type { Tone } from '@/lib/status'
import { cn } from '@/lib/utils'
import { CONNECTION_STATE } from '../extensions/ConnectionCard'

/** Read-only: the runtimes are enabled in /etc/sbx/runtime.env on the server. */
export function Runtimes() {
  const runtimes = useRuntimes()
  return <Panel id="runtimes" title="Sandbox runtimes" bodyClassName="p-0"
    description="Where sandboxes run. Each sandbox runs on one, chosen when it is created; repositories without a choice of their own use the default. Enabled in /etc/sbx/runtime.env on the server.">
    <QueryView query={runtimes} inline loading={<SkeletonRows rows={1} />}>{d => <RuntimeList runtimes={d.runtimes} />}</QueryView>
  </Panel>
}

/** One row per enabled runtime: title, default, where it runs, its status and limits. */
export function RuntimeList({ runtimes, className, children }: { runtimes: SandboxRuntime[]; className?: string; children?: (r: SandboxRuntime) => ReactNode }) {
  return <ul aria-label="Sandbox runtimes" className={cn('divide-y divide-line', className)}>{runtimes.map(r => <RuntimeRow key={r.kind} r={r}>{children?.(r)}</RuntimeRow>)}</ul>
}

function RuntimeRow({ r, children }: { r: SandboxRuntime; children?: ReactNode }) {
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
    {children}
  </li>
}
