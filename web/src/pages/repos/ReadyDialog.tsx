import { ArrowRight, Check } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { Link } from 'react-router'
import { useAgents, useConnections, useMe, useRuntimes } from '@/api/queries'
import { TemplateProgressLine } from '@/components/TemplateProgressLine'
import { Button, LinkButton } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/primitives'
import { clearReady, isMarkedReady } from '@/lib/ready'

/** Once, right after the setup: what was set up, and the next step. */
export function ReadyDialog() {
  const [open, setOpen] = useState(isMarkedReady)
  useEffect(clearReady, [])   // shown now: never again, whatever closes it
  const me = useMe()
  const runtimes = useRuntimes()
  const connections = useConnections(open)
  const agents = useAgents()
  if (!open) return null
  const close = () => setOpen(false)
  const github = connections.data?.find(c => c.kind === 'github')
  const address = (me.data?.base_url || location.origin).replace(/^https?:\/\//, '')
  const loading = <Skeleton className="my-1 h-3 w-28" />
  return <Dialog open onOpenChange={o => { if (!o) close() }}
    title={<span className="flex items-center gap-2.5 leading-tight">
      <span aria-hidden className="grid size-6 shrink-0 place-items-center rounded-full bg-ok-soft text-ok"><Check className="size-3.5" strokeWidth={3} /></span>Your server is ready
    </span>}
    description="Next, connect a repository. Your coding agent writes its recipe and starts the first sandbox."
    footer={<>
      <Button variant="ghost" onClick={close}>Look around</Button>
      <LinkButton variant="primary" to="/app/repos/new">Connect a repository<ArrowRight /></LinkButton>
    </>}>
    <div className="space-y-3">
      <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-5 gap-y-2 rounded-lg border border-line bg-bg/40 px-3.5 py-3 text-[13px]">
        {([
        ['Address', <span className="break-all font-mono text-[12.5px]">{address}</span>],
        ['Sandboxes', runtimes.isPending ? loading : (runtimes.data?.runtimes ?? []).map(r => r.title).join(', ') || 'No runtime yet'],
        ['Coding agents', me.isPending ? loading : agents.length ? agents.map(a => a.title).join(', ') : 'None yet'],
        ['GitHub', connections.isPending ? loading : github?.state === 'connected' ? github.summary || 'Connected'
          : <>Not connected · <Link to="/app/extensions" className="text-accent hover:underline">Connect</Link></>],
      ] as [string, ReactNode][]).map(([k, v]) => <div key={k} className="contents"><dt className="text-muted">{k}</dt><dd className="min-w-0 break-words">{v}</dd></div>)}
      </dl>
      <TemplateProgressLine />
    </div>
  </Dialog>
}
