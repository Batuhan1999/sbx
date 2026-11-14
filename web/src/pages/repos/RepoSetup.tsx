import { ArrowRight, ArrowUpRight, Bot, Check, Clock, Hammer, Lock, Plus, ShieldCheck, User } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router'
import { api } from '@/api/endpoints'
import { keys, useAction, useSetupPr } from '@/api/queries'
import type { Actor, ReadinessStage, ReadinessStep, ReadinessStepId } from '@/api/types'
import { AppForm, SubmitButton, useAppForm } from '@/components/form'
import { ErrorState } from '@/components/states'
import { AnchorButton, Button, LinkButton } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { Badge, Facts, Panel, Spinner } from '@/components/ui/primitives'
import { cn } from '@/lib/utils'
import { NewSandboxForm } from '../sandboxes/NewSandboxForm'
import { AgentHandoff } from './AgentHandoff'
import { useRepo } from './RepoLayout'

const ACTOR: Record<Actor, { label: string; icon: typeof Bot }> = {
  agent: { label: 'Your agent', icon: Bot },
  person: { label: 'You', icon: User },
  admin: { label: 'An admin', icon: ShieldCheck },
  platform: { label: 'In progress', icon: Clock },
}

const STEP_TITLE: Record<ReadinessStepId, string> = {
  served: 'Repository connected',
  github: 'GitHub access',
  configured: 'Recipe (.sbx/)',
  services: 'Packages',
  env: 'Environment',
  template: 'Template',
  sandbox: 'First sandbox',
}

const STAGE_TITLE: Record<ReadinessStage, string> = {
  agent: 'Waiting on your coding agent',
  person: 'Waiting on you',
  working: 'The server is working on it',
  ready: 'Ready for sandboxes',
  live: 'Up and running',
}

export default function RepoSetup() {
  const { repo, readiness, envError, me, trusted } = useRepo()
  const admin = !!me?.admin
  const showHandoff = readiness.stage === 'agent' || !readiness.info.configured
  return <div className="grid gap-4 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
    <div className="min-w-0 space-y-4">
      {showHandoff && <AgentHandoff repo={repo} configured={readiness.info.configured} />}
      <Panel id="checklist" title={STAGE_TITLE[readiness.stage]} description={`${readiness.done} of ${readiness.total} done`}
        actions={readiness.stage === 'ready' || readiness.stage === 'live' ? <NewSandboxButton repo={repo} primary /> : undefined} bodyClassName="p-0">
        <div className="h-1 bg-sunken" aria-hidden><div className="h-full bg-ok transition-[width] duration-500 ease-out" style={{ width: `${(readiness.done / readiness.total) * 100}%` }} /></div>
        <ol className="divide-y divide-line">
          {readiness.steps.map(step => <StepRow key={step.step} step={step} isNext={readiness.next === step.step}>
            <StepAction step={step} repo={repo} admin={admin} />
          </StepRow>)}
        </ol>
      </Panel>
      {envError ? <ErrorState error={envError} compact /> : null}
      {!showHandoff && <details className="rounded-lg border border-line bg-panel px-4 py-3">
        <summary className="cursor-pointer text-[13px] font-medium">Change or repair the recipe with your agent</summary>
        <div className="mt-3"><AgentHandoff repo={repo} configured /></div>
      </details>}
    </div>
    <div className="min-w-0 space-y-4">
      <Panel title="Repository">
        <Facts items={[
          ['Trust', trusted ? 'Trusted: secrets and services' : readiness.info.trust === 'public' ? 'Public: no secrets or services' : 'Not served'],
          ['GitHub', readiness.info.github === 'app' ? 'GitHub App' : readiness.info.github === 'key' ? 'Deploy key' : 'Anonymous'],
          ['Recipe', readiness.info.configured ? <Link to={`/app/repos/${repo}/recipe`} className="hover:underline">.sbx/ on the default branch</Link> : 'none yet'],
          ['Previews', readiness.info.previews.join(', ') || '—'],
        ]} />
      </Panel>
      {admin && trusted && <SetupPrPanel repo={repo} />}
    </div>
  </div>
}

function StepRow({ step, isNext, children }: { step: ReadinessStep; isNext: boolean; children: React.ReactNode }) {
  if (step.status === 'done') return <li className="flex items-center gap-3 px-4 py-2">
    <span className="grid size-5 shrink-0 place-items-center rounded-full bg-ok text-white dark:text-black"><Check className="size-3" /></span>
    <span className="shrink-0 font-medium">{STEP_TITLE[step.step]}</span>
    <span className="min-w-0 truncate text-[12.5px] text-muted" title={step.detail}>{step.detail}</span>
    <span className="sr-only">done</span>
  </li>
  const actor = step.who ? ACTOR[step.who] : null
  return <li className={cn('flex gap-3 px-4 py-3', isNext && 'bg-accent-soft/30')} aria-current={isNext ? 'step' : undefined}>
    <span className={cn('mt-0.5 grid size-5 shrink-0 place-items-center rounded-full border',
      step.status === 'todo' && (isNext ? 'border-accent bg-panel' : 'border-line-strong bg-panel'),
      step.status === 'working' && 'border-info bg-info-soft text-info',
      step.status === 'blocked' && 'border-dashed border-line-strong bg-sunken text-faint')}>
      {step.status === 'working' ? <Spinner className="size-3" /> : step.status === 'blocked' ? <Lock className="size-2.5" /> : null}
    </span>
    <div className="min-w-0 grow">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className={cn('font-medium', step.status === 'blocked' && 'text-muted')}>{STEP_TITLE[step.step]}</span>
        {actor && step.status !== 'blocked' && <span className="inline-flex items-center gap-1 text-[11.5px] text-muted"><actor.icon className="size-3" />{actor.label}</span>}
        <span className="sr-only">{step.status}</span>
      </div>
      <p className="mt-0.5 break-words text-[12.5px] text-muted">{step.detail}</p>
      {children}
    </div>
  </li>
}

function StepAction({ step, repo, admin }: { step: ReadinessStep; repo: string; admin: boolean }) {
  const build = useAction(() => api.repos.buildTemplate(repo), keys.template(repo), keys.readiness(repo), keys.repos)
  const read = useAction(() => api.repos.readRecipe(repo), keys.readiness(repo), keys.files(repo), keys.env(repo))
  if (step.status === 'done' || step.status === 'blocked') return null
  const base = `/app/repos/${repo}`
  const row = (children: React.ReactNode) => <div className="mt-2 flex flex-wrap items-center gap-2">{children}</div>
  switch (step.step) {
    case 'github':
      return step.action && admin ? row(<AnchorButton size="sm" href={step.action} target="_blank" rel="noreferrer">Install the GitHub App<ArrowUpRight /></AnchorButton>) : null
    case 'configured':
      return admin ? <>
        {row(<><Button size="sm" busy={read.isPending} onClick={() => read.mutate()}>Read .sbx/ from the default branch</Button>
          {read.data && !read.data.configured && <span className="text-[12px] text-muted">No .sbx/ on the default branch yet.</span>}</>)}
        <ErrorState error={read.error} compact className="mt-2" />
      </> : null
    case 'services':
      return row(<LinkButton size="sm" to={`${base}/packages`}>Open packages<ArrowRight /></LinkButton>)
    case 'env':
      return <MissingValues repo={repo} names={step.missing} admin={admin} />
    case 'template':
      if (step.status === 'working') return null
      return <>
        {row(<>{admin && <Button size="sm" busy={build.isPending} onClick={() => build.mutate()}><Hammer />Build template</Button>}
          {step.who === 'agent' && <LinkButton size="sm" variant="ghost" to={`${base}/recipe`}>Build log</LinkButton>}</>)}
        <ErrorState error={build.error} compact className="mt-2" />
      </>
    case 'sandbox':
      return row(<NewSandboxButton repo={repo} />)
    default:
      return null
  }
}

function NewSandboxButton({ repo, primary }: { repo: string; primary?: boolean }) {
  const [open, setOpen] = useState(false)
  return <Dialog open={open} onOpenChange={setOpen} title="New sandbox" description={repo}
    trigger={<Button size="sm" variant={primary ? 'primary' : 'secondary'}><Plus />New sandbox</Button>}>
    <NewSandboxForm repo={repo} onCancel={() => setOpen(false)} />
  </Dialog>
}

/** The repo's values are its admins' to set (a person's own values don't count as the repo's). */
export function MissingValues({ repo, names, admin, bare }: { repo: string; names: string[]; admin: boolean; bare?: boolean }) {
  const save = useAction(async (values: Record<string, string>) => {
    for (const [name, value] of Object.entries(values)) if (value) await api.env.set(repo, name, { value, secret: true, audience: 'all', agent: false })
  }, keys.env(repo), keys.readiness(repo))
  const form = useAppForm({
    defaultValues: Object.fromEntries(names.map(n => [n, ''])) as Record<string, string>,
    validators: { onSubmit: ({ value }) => Object.values(value).some(v => v.trim()) ? undefined : 'Enter at least one value.' },
    onSubmit: async ({ value }) => { await save.mutateAsync(value); form.reset() },
  })
  if (!names.length) return null
  if (!admin) return <p className={cn('text-[12.5px] text-muted', !bare && 'mt-2')}>An admin of this server sets these: {names.map((n, i) => <span key={n}>{i ? ', ' : ''}<code className="font-mono text-[12px]">{n}</code></span>)}.</p>
  return <AppForm form={form} className={cn('space-y-2.5', !bare && 'mt-3 rounded-md border border-line bg-panel p-3')}>
    {names.map(n => <form.AppField key={n} name={n}>
      {field => <field.TextField label={<span className="font-mono text-[12px]">{n}</span>} type="password" autoComplete="off" placeholder="value" />}
    </form.AppField>)}
    <form.Subscribe selector={s => s.errorMap.onSubmit}>{error => error ? <p role="alert" className="text-[12px] text-bad">{String(error)}</p> : null}</form.Subscribe>
    <div className="flex flex-wrap items-center justify-between gap-2">
      <span className="text-[12px] text-muted">Stored as secrets; running sandboxes get them within seconds. Hidden from the coding agent.</span>
      <SubmitButton form={form} size="sm">Save values</SubmitButton>
    </div>
  </AppForm>
}

function SetupPrPanel({ repo }: { repo: string }) {
  const pr = useSetupPr(repo, true)
  if (pr.isPending || pr.error || !pr.data?.pull_request) return null
  const p = pr.data.pull_request
  return <Panel title="Setup pull request">
    <div className="flex flex-wrap items-center gap-2 text-[13px]">
      <a href={p.url} target="_blank" rel="noreferrer" className="font-medium hover:underline">#{p.number}</a>
      <Badge tone={p.state === 'merged' ? 'ok' : p.state === 'open' ? 'info' : 'neutral'}>{p.draft && p.state === 'open' ? 'draft' : p.state}</Badge>
      <span className="text-muted">{p.state === 'merged' ? 'Merged; templates build from the default branch.' : 'Review the recipe, then merge it.'}</span>
    </div>
    <Link to={`/app/repos/${repo}/recipe`} className="mt-2 inline-block text-[12.5px] text-accent hover:underline">View the recipe</Link>
  </Panel>
}
