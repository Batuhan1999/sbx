import { FileCode2, Hammer } from 'lucide-react'
import { useState } from 'react'
import { api } from '@/api/endpoints'
import { keys, useAction, useRecipeFiles, useRuntimeTitle, useTemplate } from '@/api/queries'
import type { TemplateOnRuntime, TemplateState } from '@/api/types'
import { EmptyState, ErrorState, QueryView } from '@/components/states'
import { StateBadge } from '@/components/StatusBadge'
import { Button } from '@/components/ui/button'
import { Panel } from '@/components/ui/primitives'
import { ago, dateTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import { useRepo } from './RepoLayout'
import { RepoRuntime } from './RepoRuntime'

export default function RepoRecipe() {
  const { repo, readiness, me } = useRepo()
  const files = useRecipeFiles(repo)
  const template = useTemplate(repo)
  const admin = !!me?.admin
  const build = useAction(() => api.repos.buildTemplate(repo), keys.template(repo), keys.readiness(repo), keys.repos)
  const t = template.data
  const title = useRuntimeTitle()
  return <div className="grid gap-4 lg:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
    <Panel title="Recipe" description="The .sbx/ folder on the default branch." bodyClassName="p-0">
      <QueryView query={files} inline>{d => d.files.length === 0
        ? <EmptyState icon={FileCode2} title="No recipe yet" className="py-10">Your coding agent writes .sbx/ and opens a pull request; once merged, it shows here.</EmptyState>
        : <RecipeFiles files={d.files} />}</QueryView>
    </Panel>
    <div className="min-w-0 space-y-4">
      {admin && <RepoRuntime repo={repo} />}
      <Panel title="Template" description="A prebuilt image of the repository with dependencies installed, so sandboxes start in seconds. Rebuilt when the recipe or an extension changes."
        actions={t && <>{title && t.runtime && <span className="text-[12.5px] text-muted">{title(t.runtime)}</span>}<StateBadge tone={t.level} label={t.label} pulse={t.level === 'progress'} /></>}>
        {readiness.info.prebuilt ? <div className="space-y-3">
          <BuildWhen t={t} />
          {t?.status === 'failed' && t.error && <p className="rounded-md bg-bad-soft px-3 py-2 text-[12.5px] text-bad">{t.error}</p>}
          {admin && <Button busy={build.isPending} disabled={t?.status === 'building'} onClick={() => build.mutate()}><Hammer />{t?.status === 'built' ? 'Rebuild now' : 'Build now'}</Button>}
          <ErrorState error={build.error || template.error} compact />
        </div> : <div className="space-y-3">
          <p className="text-[13px] text-muted">The repository has no template of its own: sandboxes start from the platform's default template and run setup on first boot.</p>
          <BuildWhen t={t} />
          <ErrorState error={template.error} compact />
        </div>}
        {title && t?.runtimes?.length ? <OtherRuntimes states={t.runtimes} title={title} /> : null}
      </Panel>
      {t?.status === 'failed' && t.log.length > 0 && <Panel title="Build log" description="Why the last build failed. Your coding agent can read it with the setup brief and fix the recipe." bodyClassName="p-0">
        <pre className="max-h-[420px] overflow-auto bg-sunken p-3 text-[11.5px] leading-relaxed">{t.log.join('\n')}</pre>
      </Panel>}
    </div>
  </div>
}

/** The template's state on the runtimes other than the repo's, one line each. */
function OtherRuntimes({ states, title }: { states: TemplateOnRuntime[]; title: (kind?: string) => string }) {
  return <ul aria-label="On other runtimes" className="mt-3 space-y-1.5 border-t border-line pt-3 text-[12.5px]">
    {states.map(o => <li key={o.runtime} className="flex items-center justify-between gap-2">
      <span className="min-w-0 truncate text-muted">{title(o.runtime)}</span><StateBadge tone={o.level} label={o.label} pulse={o.level === 'progress'} />
    </li>)}
  </ul>
}

function BuildWhen({ t }: { t: TemplateState | undefined }) {
  const when = t?.status === 'building' ? t.started_at : t?.finished_at
  if (!when) return null
  const verb = t?.status === 'building' ? 'Started' : t?.status === 'failed' ? 'Failed' : 'Built'
  return <p className="text-[13px] text-muted"><span title={dateTime(when)}>{verb} {ago(when)}</span></p>
}

function RecipeFiles({ files }: { files: { path: string; text: string | null }[] }) {
  const [current, setCurrent] = useState(files.find(f => f.path === 'sbx.yaml')?.path ?? files[0].path)
  const file = files.find(f => f.path === current) ?? files[0]
  return <div className="grid md:grid-cols-[180px_minmax(0,1fr)]">
    <ul aria-label="Recipe files" className="flex gap-1 overflow-x-auto border-b border-line p-2 md:flex-col md:border-b-0 md:border-r">
      {files.map(f => <li key={f.path}><button type="button" onClick={() => setCurrent(f.path)} aria-current={f.path === current || undefined}
        className={cn('w-full truncate whitespace-nowrap rounded px-2 py-1 text-left font-mono text-[12px]', f.path === current ? 'bg-hover text-ink' : 'text-muted hover:text-ink')}>{f.path}</button></li>)}
    </ul>
    <pre className="max-h-[560px] min-w-0 overflow-auto p-4 text-[12px] leading-relaxed">{file.text ?? '(binary or too large to show)'}</pre>
  </div>
}
