import { useQueryClient } from '@tanstack/react-query'
import { ArrowRight, Check } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router'
import { api } from '@/api/endpoints'
import { keys, useConnections, useMe, useRuntimes, useSetup } from '@/api/queries'
import { ErrorState } from '@/components/states'
import { TemplateProgressLine } from '@/components/TemplateProgressLine'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/primitives'

/** The agents whose cloud sessions this server can run, once their connector is set up under Extensions. */
const SESSIONS = [['Amp', 'Run Amp threads in sbx sandboxes'], ['Claude Code', 'Run Claude Code cloud sessions here']]

/** The onboarding's last step, over the control plane: what was set up, and the way on. It shows while the
 * onboarding is open and has nothing left to do (the server says so); any way out of it finishes the onboarding.
 * A server that refuses to finish says why, and the summary stays. */
export function ReadyDialog() {
  const me = useMe()
  const open = !!me.data?.admin && me.data.setup_complete === false
  const setup = useSetup(open)
  if (!open || !setup.data?.can_complete || setup.data.complete) return null
  return <Summary />
}

function Summary() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const me = useMe()
  const runtimes = useRuntimes()
  const connections = useConnections()
  const [error, setError] = useState<unknown>(null)
  const [going, setGoing] = useState<string | null>(null)
  // Its main way on, ready before it is chosen: the page's code and the repositories GitHub lists (a second).
  useEffect(() => {
    void import('@/pages/repos/ConnectRepoPage')
    void qc.prefetchQuery({ queryKey: keys.github, queryFn: () => api.repos.github().then(d => d.repositories), staleTime: 60_000 })
  }, [qc])
  const finish = async (to?: string) => {
    if (going !== null) return
    setGoing(to ?? '')
    setError(null)
    try {
      qc.setQueryData(keys.setup, await api.setup.complete())
      if (to) navigate(to)
      await qc.invalidateQueries({ queryKey: keys.me })
    } catch (e) { setError(e); setGoing(null) }
  }
  const github = connections.data?.find(c => c.kind === 'github')
  const address = (me.data?.base_url || location.origin).replace(/^https?:\/\//, '')
  const loading = <Skeleton className="my-1 h-3 w-28" />
  return <Dialog open onOpenChange={o => { if (!o) void finish() }}
    title={<span className="flex items-center gap-2.5 leading-tight">
      <span aria-hidden className="grid size-6 shrink-0 place-items-center rounded-full bg-ok-soft text-ok"><Check className="size-3.5" strokeWidth={3} /></span>Your server is ready
    </span>}
    description="Next, connect a repository. Your coding agent writes its recipe and starts the first sandbox."
    footer={<>
      <Button variant="ghost" busy={going === ''} disabled={going !== null} onClick={() => finish()}>Look around</Button>
      <Button variant="primary" busy={going === '/app/repos/new'} disabled={going !== null} onClick={() => finish('/app/repos/new')}>Connect a repository<ArrowRight /></Button>
    </>}>
    <div className="space-y-3">
      <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-5 gap-y-2 rounded-lg border border-line bg-bg/40 px-3.5 py-3 text-[13px]">
        {([
        ['Address', <span className="break-all font-mono text-[12.5px]">{address}</span>],
        ['Sandboxes', runtimes.isPending ? loading : (runtimes.data?.runtimes ?? []).map(r => r.title).join(', ') || 'No runtime yet'],
        ['GitHub', connections.isPending ? loading : github?.state === 'connected' ? github.summary || 'Connected'
          : <>Not connected · <button type="button" onClick={() => finish('/app/extensions')} className="text-accent hover:underline">Connect</button></>],
      ] as [string, ReactNode][]).map(([k, v]) => <div key={k} className="contents"><dt className="text-muted">{k}</dt><dd className="min-w-0 break-words">{v}</dd></div>)}
      </dl>
      <TemplateProgressLine />
      <section aria-label="Run agent sessions here" className="text-[13px]">
        <p className="font-medium">Run agent sessions here <span className="font-normal text-faint">optional</span></p>
        <ul className="mt-1.5 divide-y divide-line rounded-lg border border-line">
          {SESSIONS.map(([agent, text]) => <li key={agent}>
            <button type="button" onClick={() => finish('/app/extensions')} className="flex w-full items-center gap-3 px-3.5 py-2 text-left hover:bg-hover/60">
              <span className="min-w-0 grow">{text}</span><span className="shrink-0 text-accent">Set up {agent}</span>
            </button>
          </li>)}
        </ul>
        <p className="mt-1.5 text-[12.5px] text-muted">Codex is included: each developer uses their own login.</p>
      </section>
      <ErrorState error={error} />
    </div>
  </Dialog>
}
