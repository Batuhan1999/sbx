import { ArrowUpRight } from 'lucide-react'
import { Outlet, useOutletContext, useParams } from 'react-router'
import { useEnv, useMe, useReadiness } from '@/api/queries'
import type { EnvView, Me, RepoReadiness } from '@/api/types'
import { PageHeader } from '@/components/PageHeader'
import { ErrorState, Loading } from '@/components/states'
import { NavTabs } from '@/components/Tabs'
import { Badge } from '@/components/ui/primitives'

export type RepoContext = { repo: string; readiness: RepoReadiness; env: EnvView | undefined; envError: unknown; me: Me | undefined; trusted: boolean }
export const useRepo = () => useOutletContext<RepoContext>()

export default function RepoLayout() {
  const { owner = '', name = '' } = useParams()
  const repo = `${owner}/${name}`.toLowerCase()
  const me = useMe()
  const readiness = useReadiness(repo)
  const trusted = readiness.data?.info.trust === 'trusted'
  const env = useEnv(repo, trusted)
  if (readiness.error) return <><PageHeader title={repo} crumbs={[{ label: 'Repositories', to: '/app/repos' }]} /><ErrorState error={readiness.error} retry={() => readiness.refetch()} /></>
  if (!readiness.data) return <Loading label="Loading repository…" />
  const info = readiness.data.info
  const missing = env.data?.missing.length ?? info.secrets_missing.length
  const problems = readiness.data.steps.find(s => s.step === 'services')?.missing.length ?? 0
  const base = `/app/repos/${repo}`
  const count = (n: number, tone: 'warn' | 'neutral' = 'warn') => n > 0 ? <Badge tone={tone} className="h-4 px-1 text-[10.5px]">{n}</Badge> : null
  return <>
    <PageHeader title={repo} crumbs={[{ label: 'Repositories', to: '/app/repos' }]}
      meta={<>
        <Badge tone={info.trust === 'trusted' ? 'accent' : 'neutral'}>{info.trust ?? 'not served'}</Badge>
        <a href={`https://github.com/${repo}`} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 hover:text-ink">GitHub<ArrowUpRight className="size-3" /></a>
        {info.previews.length > 0 && <span>Previews: {info.previews.join(', ')}</span>}
      </>} />
    <NavTabs label="Repository sections" tabs={[
      { to: base, label: 'Setup', end: true },
      { to: `${base}/sandboxes`, label: 'Sandboxes' },
      { to: `${base}/env`, label: 'Environment', badge: count(missing) },
      { to: `${base}/packages`, label: 'Packages', badge: count(problems) },
      { to: `${base}/recipe`, label: 'Recipe & template' },
    ]} />
    <Outlet context={{ repo, readiness: readiness.data, env: env.data, envError: env.error, me: me.data, trusted } satisfies RepoContext} />
  </>
}
