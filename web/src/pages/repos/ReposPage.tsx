import { Bot, ChevronRight, FolderGit2, Plus } from 'lucide-react'
import { Link } from 'react-router'
import { useMe, useRepos } from '@/api/queries'
import type { RepoSummary } from '@/api/types'
import { CopyCommand } from '@/components/CopyCommand'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/states'
import { StateBadge } from '@/components/StatusBadge'
import { LinkButton } from '@/components/ui/button'
import { Badge } from '@/components/ui/primitives'
import { ago, plural } from '@/lib/format'

export default function ReposPage() {
  const me = useMe()
  const repos = useRepos()
  const admin = !!me.data?.admin
  const list = repos.data ?? []
  const needAgent = list.filter(r => r.trust && !r.configured)
  return <>
    <PageHeader title="Repositories"
      actions={admin && <LinkButton variant="primary" to="/app/repos/new"><Plus />Connect repository</LinkButton>} />
    {repos.error ? <ErrorState error={repos.error} retry={() => repos.refetch()} />
      : repos.isPending ? <div className="rounded-lg border border-line bg-panel"><SkeletonRows /></div>
      : list.length === 0 ? <div className="rounded-lg border border-dashed border-line">
        <EmptyState icon={FolderGit2} title="No repositories yet" action={admin ? <LinkButton variant="primary" size="sm" to="/app/repos/new"><Plus />Connect repository</LinkButton> : undefined} />
      </div>
      : <div className="space-y-4">
        {needAgent.length > 0 && <AgentCallout repos={needAgent} />}
        <ul aria-label="Repositories" className="divide-y divide-line overflow-hidden rounded-lg border border-line bg-panel">
          {list.map(r => <RepoRow key={r.repo} r={r} />)}
        </ul>
      </div>}
  </>
}

function AgentCallout({ repos }: { repos: RepoSummary[] }) {
  return <section aria-label="Agent setup" className="rounded-lg border border-accent/25 bg-accent-soft/50 p-4">
    <div className="flex items-start gap-3">
      <span className="grid size-8 shrink-0 place-items-center rounded-md bg-accent text-accent-ink"><Bot className="size-4" /></span>
      <div className="min-w-0 grow">
        <p className="font-medium">{repos.length === 1 ? `${repos[0].repo} needs a recipe` : `${repos.length} repositories need a recipe`}</p>
        <p className="mt-0.5 text-[13px] text-muted">In a checkout:</p>
        <CopyCommand command="sbx repo connect --agent" className="mt-3 max-w-md bg-panel" label="agent setup command" />
      </div>
    </div>
  </section>
}

function RepoRow({ r }: { r: RepoSummary }) {
  const state = r.state
  return <li className="relative flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 transition-colors hover:bg-hover/60">
    <div className="min-w-0 grow basis-60">
      <Link to={`/app/repos/${r.repo}`} className="font-medium after:absolute after:inset-0 hover:underline">{r.repo}</Link>
      <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[12px] text-muted">
        {r.trust === 'public' && <Badge>public</Badge>}
        <span>{r.prebuilt ? 'Template' : 'Default template'}{r.built_at ? ` built ${ago(r.built_at)}` : ' not built yet'}</span>
      </div>
    </div>
    <span className="text-[12.5px] text-muted">{plural(r.sandboxes, 'sandbox', 'sandboxes')}</span>
    <span className="flex w-52 items-center justify-end gap-2">
      {state.who === 'agent' && <Bot className="size-3.5 text-muted" aria-label="for your coding agent" />}
      <StateBadge tone={state.level} label={state.label} pulse={state.level === 'progress'} />
    </span>
    <ChevronRight className="hidden size-4 text-faint sm:block" aria-hidden />
  </li>
}
