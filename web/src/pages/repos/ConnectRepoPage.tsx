import { useQueryClient } from '@tanstack/react-query'
import { ArrowRight, Globe, Lock } from 'lucide-react'
import { useState } from 'react'
import { Link, useNavigate } from 'react-router'
import { api } from '@/api/endpoints'
import { keys, useConnections, useGitHubRepositories, useMe, useRepos } from '@/api/queries'
import type { RepoInspection } from '@/api/types'
import { AppForm, SubmitButton, useAppForm } from '@/components/form'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState, ErrorState } from '@/components/states'
import { Button, LinkButton } from '@/components/ui/button'
import { Badge, Panel, Spinner } from '@/components/ui/primitives'
import { repoError } from '@/lib/validation'

export default function ConnectRepoPage() {
  const me = useMe()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const connections = useConnections(!!me.data?.admin)
  const github = connections.data?.find(c => c.kind === 'github')
  const appReady = github?.state === 'connected'
  const available = useGitHubRepositories(appReady)
  const served = new Set((useRepos().data ?? []).filter(r => r.trust).map(r => r.repo))
  const [found, setFound] = useState<RepoInspection | null>(null)
  const [connectError, setConnectError] = useState<unknown>(null)
  const [connecting, setConnecting] = useState(false)
  const form = useAppForm({
    defaultValues: { repo: '' },
    onSubmit: async ({ value }) => { setConnectError(null); setFound(await api.repos.inspect(value.repo.trim())) },
  })
  async function connect(repo: string) {
    setConnecting(true); setConnectError(null)
    try {
      const result = await api.repos.connect(repo)
      await Promise.all([qc.invalidateQueries({ queryKey: keys.repos }), qc.invalidateQueries({ queryKey: keys.me })])
      navigate(`/app/repos/${result.repo}`)
    } catch (e) { setConnectError(e) } finally { setConnecting(false) }
  }
  if (me.data && !me.data.admin) return <EmptyState title="Admins connect repositories" action={<LinkButton to="/app/repos">Back to repositories</LinkButton>}>Ask an admin of this server to connect the repository.</EmptyState>
  return <div className="max-w-2xl">
    <PageHeader title="Connect a repository" crumbs={[{ label: 'Repositories', to: '/app/repos' }]}
      description="The server clones it into sandboxes and serves its previews. Nothing runs until its recipe exists; your coding agent writes that next." />
    <div className="space-y-4">
      {connections.data && !appReady && <div className="rounded-lg border border-warn/30 bg-warn-soft px-4 py-3 text-[13px]">
        <p className="font-medium text-warn">GitHub is not connected</p>
        <p className="mt-0.5 text-muted">Public repositories work without it. Private ones need this server's GitHub App: <Link to="/app/extensions#github" className="text-accent hover:underline">connect GitHub</Link>.</p>
      </div>}
      <Panel title="Repository">
        <AppForm form={form} className="flex flex-col gap-3 sm:flex-row sm:items-start">
          <div className="grow"><form.AppField name="repo" validators={{ onChange: ({ value }) => repoError(value) }}>
            {field => <field.TextField label="GitHub repository" placeholder="owner/repo or https://github.com/owner/repo" autoComplete="off" spellCheck={false} />}
          </form.AppField></div>
          <SubmitButton form={form} variant="secondary" className="sm:mt-[22px]">Look up</SubmitButton>
        </AppForm>
        {found && <div className="mt-4 rounded-md border border-line p-3">
          <div className="flex flex-wrap items-center gap-2">
            {found.private ? <Lock className="size-3.5 text-muted" /> : <Globe className="size-3.5 text-muted" />}
            <a href={found.url} target="_blank" rel="noreferrer" className="font-medium hover:underline">{found.repo}</a>
            <Badge>{found.branch}</Badge>
            {found.github_app ? <Badge tone="ok">GitHub App installed</Badge> : found.private ? <Badge tone="warn">GitHub App not installed</Badge> : null}
            {found.registered && <Badge tone="accent">already connected</Badge>}
          </div>
          {found.description && <p className="mt-1 text-[13px] text-muted">{found.description}</p>}
          <div className="mt-3 flex justify-end">
            {found.registered ? <LinkButton variant="primary" to={`/app/repos/${found.repo}`}>Open<ArrowRight /></LinkButton>
              : <Button variant="primary" busy={connecting} onClick={() => connect(found.repo)}>Connect {found.repo}<ArrowRight /></Button>}
          </div>
        </div>}
        <ErrorState error={connectError} compact className="mt-3" />
      </Panel>
      {appReady && <Panel title="From the GitHub App" description="Repositories this server's GitHub App can read." bodyClassName="p-0">
        {available.isPending ? <div className="p-4"><Spinner label="Asking GitHub…" /></div>
          : available.error ? <div className="p-4"><ErrorState error={available.error} compact retry={() => available.refetch()} /></div>
          : !available.data?.length ? <EmptyState title="No repositories" className="py-8">Install the App on a repository in GitHub.</EmptyState>
          : <ul className="max-h-96 divide-y divide-line overflow-y-auto">
            {available.data.map(r => <li key={r.repo} className="flex items-center gap-3 px-4 py-2.5">
              <div className="min-w-0 grow">
                <div className="flex items-center gap-1.5 font-medium">{r.private && <Lock className="size-3 text-muted" />}{r.repo}</div>
                {r.description && <p className="truncate text-[12px] text-muted">{r.description}</p>}
              </div>
              {served.has(r.repo.toLowerCase()) ? <LinkButton size="sm" variant="ghost" to={`/app/repos/${r.repo.toLowerCase()}`}>Open</LinkButton>
                : <Button size="sm" disabled={connecting} onClick={() => connect(r.repo)}>Connect</Button>}
            </li>)}
          </ul>}
      </Panel>}
    </div>
  </div>
}
