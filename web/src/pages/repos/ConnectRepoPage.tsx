import { useQueryClient } from '@tanstack/react-query'
import { ArrowRight, ExternalLink, Github, Lock, RefreshCw, Search } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router'
import { api } from '@/api/endpoints'
import { keys, useConnections, useGitHubRepositories, useGitHubSearch, useMe, useRepos } from '@/api/queries'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState, ErrorState } from '@/components/states'
import { Button, LinkButton } from '@/components/ui/button'
import { Skeleton, Spinner } from '@/components/ui/primitives'
import { ago } from '@/lib/format'
import { cn } from '@/lib/utils'
import { repoName } from '@/lib/validation'

/** Connecting a repository, as hosting platforms import one: one field that searches what the GitHub App can see
 * (the last pushed first) and public repositories on GitHub (after a pause in typing), and takes a pasted
 * owner/repo or GitHub URL, which then gets a row of its own. */
/** How many of the App's repositories show before a search. */
const RECENT = 6

export default function ConnectRepoPage() {
  const me = useMe()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const connections = useConnections(!!me.data?.admin)
  const github = connections.data?.find(c => c.kind === 'github')
  const appReady = github?.state === 'connected'
  const available = useGitHubRepositories(appReady)
  const served = new Set((useRepos().data ?? []).filter(r => r.trust).map(r => r.repo))
  const [query, setQuery] = useState('')
  const [everything, setEverything] = useState(false)
  const [connecting, setConnecting] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)
  async function connect(repo: string) {
    setConnecting(repo); setError(null)
    try {
      const result = await api.repos.connect(repo)
      await Promise.all([qc.invalidateQueries({ queryKey: keys.repos }), qc.invalidateQueries({ queryKey: keys.me })])
      navigate(`/app/repos/${result.repo}`)
    } catch (e) { setError(e); setConnecting(null) }
  }
  if (me.data && !me.data.admin) return <EmptyState title="Admins connect repositories" action={<LinkButton to="/app/repos">Back to repositories</LinkButton>} />

  const all = available.data ?? []
  const pasted = repoName(query)
  const q = (pasted ?? query.trim()).toLowerCase()
  // Before a search, the few last pushed (Vercel's import does the same); the search finds the rest.
  const matching = q ? all.filter(r => r.repo.includes(q)) : all
  const shown = q || everything ? matching : matching.slice(0, RECENT)
  // A repository typed in full that the list does not have: its own row (a public one clones without the App).
  const extra = pasted && !all.some(r => r.repo === pasted) ? pasted : null
  // GitHub-wide, once typing pauses, and not for a pasted full name (its row is there already).
  const typed = pasted ? '' : query.trim()
  const [term, setTerm] = useState('')
  useEffect(() => { const t = setTimeout(() => setTerm(typed), 400); return () => clearTimeout(t) }, [typed])
  const search = useGitHubSearch(term === typed ? term : '')
  const mine = new Set(all.map(r => r.repo))
  const publicHits = (search.data ?? []).filter(r => !mine.has(r.repo))
  const installUrl = typeof github?.facts.install_url === 'string' ? github.facts.install_url : ''
  const row = (repo: string, extra?: React.ReactNode) => <li key={repo} className="flex h-12 items-center gap-3 px-4">
    <p className="min-w-0 grow truncate text-[13.5px]"><span className="text-muted">{repo.split('/')[0]}/</span><span className="font-medium">{repo.split('/')[1]}</span></p>
    {extra}
    {served.has(repo) ? <LinkButton size="sm" variant="ghost" to={`/app/repos/${repo}`}>Open<ArrowRight /></LinkButton>
      : <Button size="sm" variant="secondary" busy={connecting === repo} disabled={connecting !== null} onClick={() => connect(repo)}>Connect</Button>}
  </li>

  return <div className="max-w-2xl">
    <PageHeader title="Connect a repository" crumbs={[{ label: 'Repositories', to: '/app/repos' }]} />
    <div className="space-y-3">
      <ErrorState error={error} />
      <section aria-label="Repositories to connect" className="overflow-hidden rounded-xl border border-line bg-panel">
        <div className="flex items-center gap-2 border-b border-line p-2">
          <label className="flex h-8 grow items-center gap-2 px-2">
            <Search aria-hidden className="size-4 shrink-0 text-faint" />
            <input type="search" value={query} onChange={e => setQuery(e.target.value)} aria-label="Search or paste a repository"
              placeholder="Search, or paste a GitHub URL…"
              onKeyDown={e => { if (e.key === 'Enter' && extra && !served.has(extra)) void connect(extra) }}
              autoComplete="off" spellCheck={false} className="h-full min-w-0 grow bg-transparent text-[13px] outline-none placeholder:text-faint" />
          </label>
          {appReady && <Button size="icon-sm" variant="ghost" aria-label="Refresh" title="Refresh" disabled={available.isFetching} onClick={() => available.refetch()}>
            <RefreshCw className={cn(available.isFetching && 'animate-spin')} />
          </Button>}
        </div>
        {extra && <ul aria-label="Pasted repository" className="border-b border-line">{row(extra)}</ul>}
        {connections.isPending ? <div className="p-4"><Skeleton className="h-20 w-full" /></div>
          : !appReady ? !query.trim() && <div className="flex flex-wrap items-center gap-3 p-4">
            <Github className="size-5 shrink-0" aria-hidden />
            <p className="grow text-[13px]">Connect GitHub to import private repositories.</p>
            <LinkButton size="sm" variant="primary" to="/app/extensions#github">Connect GitHub</LinkButton>
          </div>
          : available.isPending ? <div className="p-4"><Spinner label="Asking GitHub…" /></div>
          : available.error ? <div className="p-4"><ErrorState error={available.error} compact retry={() => available.refetch()} /></div>
          : !shown.length ? !extra && !typed && <p className="px-4 py-6 text-center text-[13px] text-muted">The GitHub App can’t see any repository yet.</p>
          : <ul aria-label="GitHub repositories" className="max-h-[26rem] divide-y divide-line overflow-y-auto">
            {shown.map(r => row(r.repo, <>
              {r.private && <Lock className="size-3.5 shrink-0 text-faint" aria-label="private" />}
              {r.pushed_at && <span className="hidden shrink-0 text-[12px] tabular-nums text-faint sm:inline">{ago(r.pushed_at)}</span>}
            </>))}
          </ul>}
        {!q && !everything && all.length > RECENT && <button type="button" onClick={() => setEverything(true)}
          className="w-full border-t border-line px-4 py-2.5 text-left text-[12.5px] text-muted transition-colors hover:text-ink">Show all {all.length}</button>}
        {typed.length >= 2 && <div aria-label="Public on GitHub" role="group" className="border-t border-line">
          <p className="flex items-center gap-2 px-4 pb-1 pt-3 text-[12px] font-medium text-faint">Public on GitHub
            {(search.isFetching || term !== typed) && <Spinner className="size-3" />}</p>
          {search.error ? <p className="px-4 pb-3 text-[12.5px] text-muted">{search.error.message}</p>
            : search.data && !publicHits.length && !shown.length ? <p className="px-4 pb-3 text-[12.5px] text-muted">No match for “{typed}”.</p>
            : publicHits.length > 0 && <ul aria-label="Public repositories" className="divide-y divide-line">{publicHits.map(r => row(r.repo,
              r.pushed_at && <span className="hidden shrink-0 text-[12px] tabular-nums text-faint sm:inline">{ago(r.pushed_at)}</span>))}</ul>}
        </div>}
        {appReady && installUrl && <a href={installUrl} target="_blank" rel="noreferrer"
          className="flex items-center gap-1.5 border-t border-line px-4 py-2.5 text-[12.5px] text-muted transition-colors hover:text-ink">
          {all.length || available.isPending ? 'Adjust GitHub App permissions' : 'Configure GitHub App'}<ExternalLink aria-hidden className="size-3" /><span className="sr-only">(opens in a new tab)</span>
        </a>}
      </section>
    </div>
  </div>
}
