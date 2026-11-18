import { useQueryClient } from '@tanstack/react-query'
import { Check, CircleAlert, ExternalLink, RefreshCw, TriangleAlert, X } from 'lucide-react'
import { type ReactNode, useId, useState } from 'react'
import { api } from '@/api/endpoints'
import { keys } from '@/api/queries'
import type { Release, Updates as UpdatesView, Upgrade, UpgradeStep } from '@/api/types'
import { ProgressBar, useUpgradeFollow } from '@/app/UpdateNotice'
import { CopyCommand } from '@/components/CopyCommand'
import { LinkedText } from '@/components/LinkedText'
import { ErrorState, QueryView } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Dialog } from '@/components/ui/dialog'
import { Badge, Code, Facts, Panel, Spinner } from '@/components/ui/primitives'
import { ago, dateTime, plural } from '@/lib/format'
import { parseNotes, type NoteItem, type NoteSection } from '@/lib/releaseNotes'
import { cn } from '@/lib/utils'

/** Settings → Updates: this server's release, what is newer and what changed,
 * and an upgrade's progress through sbx-control's restart. */
export function Updates() {
  const follow = useUpgradeFollow()
  return <div className="max-w-3xl space-y-4">
    <QueryView query={follow.updates}>{data => <>
      <Version data={data} />
      {data.upgrade && (data.upgrade.state !== 'done' || follow.finished) && <Progress upgrade={data.upgrade} reconnecting={follow.reconnecting} />}
      {/* Keyed by what is offered: an acknowledgment holds for the notes it was given on. */}
      {data.available && data.upgrade?.state !== 'running' && <WhatsNew key={`${data.available.releases.map(r => r.tag).join()}:${data.available.breaking}`}
        available={data.available} installed={data.installed} />}
    </>}</QueryView>
  </div>
}

function Version({ data }: { data: UpdatesView }) {
  const qc = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  // Up to date only once GitHub has answered that nothing is newer.
  const status = data.upgrade?.state === 'running' ? <Badge tone="accent">Updating to {data.upgrade.tag}</Badge>
    : data.available ? <Badge tone="accent">{data.available.latest} available</Badge>
    : !data.check ? <Badge>Not checked</Badge>
    : data.check_error ? <Badge tone="warn">Check failed</Badge>
    : data.checked_at ? <Badge tone="ok">Up to date</Badge> : <Badge>Not checked yet</Badge>
  return <Panel title="Version"
    actions={data.check && <Button size="sm" busy={busy} onClick={async () => {
      setBusy(true); setError(null)
      try { qc.setQueryData(keys.updates, await api.updates.check()) } catch (e) { setError(e) } finally { setBusy(false) }
    }}><RefreshCw />Check now</Button>}>
    <Facts items={[
      ['Installed', <Code key="v">{data.installed}</Code>],
      ['Status', status],
      ['Last checked', data.check
        ? (data.checked_at ? <span title={dateTime(data.checked_at)}>{ago(data.checked_at)}</span> : 'Not yet: a minute after sbx-control starts, then every 6 hours')
        : <>Off: turn it on with <Code>sbx-admin config set updates {'\'{"check": true}\''}</Code></>],
    ]} />
    {data.check_error && <p className="mt-3 text-[12.5px] text-warn">{data.check_error}</p>}
    {!!error && <ErrorState error={error} compact className="mt-3" />}
  </Panel>
}

function Progress({ upgrade, reconnecting }: { upgrade: Upgrade; reconnecting: boolean }) {
  const steps = upgrade.steps ?? []
  const running = upgrade.state === 'running'
  const percent = Math.round((upgrade.progress ?? 0) * 100)
  const title = running ? `Updating to ${upgrade.tag}` : upgrade.state === 'done' ? `Updated to ${upgrade.tag}` : `Update to ${upgrade.tag} failed`
  const current = [...steps].reverse().find(s => s.state === 'running')
  return <Panel title={title} description={<>From {upgrade.from}{upgrade.started_at && <>, started {ago(upgrade.started_at)}</>}. Log on the host: <Code>{upgrade.log}</Code></>}>
    <div className="space-y-4">
      <div className="space-y-1.5" role={running ? 'status' : undefined}>
        <div className="flex justify-between text-[12.5px]">
          <span className="truncate text-muted">{reconnecting ? 'sbx-control restarts on the new release; reconnecting…'
            : running ? (current?.title ?? 'Waiting for the host…') : upgrade.state === 'done' ? 'Done' : 'Stopped'}</span>
          <span className="tabular-nums">{upgrade.state === 'done' ? 100 : percent}%</span>
        </div>
        <ProgressBar value={upgrade.state === 'done' ? 1 : upgrade.progress ?? 0} label={title}
          className={cn('h-2', upgrade.state === 'failed' && '[&>div]:bg-bad')} />
      </div>
      <ol className="space-y-1.5" aria-label="Steps">
        {steps.map((s, i) => <Step key={i} step={s} />)}
        {reconnecting && <Step step={{ title: 'Reconnecting to sbx-control', state: 'running', detail: 'it restarts on the new release' }} />}
      </ol>
      {upgrade.state === 'failed' && <div role="alert" className="space-y-2 rounded-md border border-bad/30 bg-bad-soft px-3 py-2.5 text-[13px]">
        <div className="flex gap-2 font-medium text-bad"><CircleAlert className="mt-0.5 size-4 shrink-0" />{upgrade.error || 'The upgrade failed.'}</div>
        {upgrade.rollback && <div className="space-y-1 text-muted">
          <div>{upgrade.tag} is running. To go back to {upgrade.from}, on the host:</div>
          <CopyCommand command={upgrade.rollback} label="rollback command" />
        </div>}
      </div>}
      {upgrade.state === 'done' && <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-ok/30 bg-ok-soft px-3 py-2.5 text-[13px]">
        <span className="flex items-center gap-2 font-medium text-ok"><Check className="size-4" />sbx-control runs {upgrade.tag} now.</span>
        <Button size="sm" variant="primary" onClick={() => window.location.reload()}>Reload the page</Button>
      </div>}
      {!!upgrade.log_tail?.length && <Collapsible className="rounded-md border border-line">
        <CollapsibleTrigger className="px-3 py-2 text-[12.5px]">Log, last {upgrade.log_tail.length} lines</CollapsibleTrigger>
        <CollapsibleContent><pre className="max-h-72 overflow-auto border-t border-line bg-sunken px-3 py-2 text-[11.5px] leading-relaxed">{upgrade.log_tail.join('\n')}</pre></CollapsibleContent>
      </Collapsible>}
    </div>
  </Panel>
}

function Step({ step }: { step: UpgradeStep }) {
  return <li className="flex items-center gap-2.5 text-[13px]">
    <span className={cn('grid size-5 shrink-0 place-items-center rounded-full',
      step.state === 'done' ? 'bg-ok-soft text-ok' : step.state === 'failed' ? 'bg-bad-soft text-bad' : 'text-accent')}>
      {step.state === 'done' ? <Check className="size-3" /> : step.state === 'failed' ? <X className="size-3" /> : <Spinner className="size-3.5" />}
    </span>
    <span className={cn('min-w-0 grow truncate', step.state === 'running' && 'font-medium')}>{step.title}</span>
    {step.detail && <span className="shrink-0 tabular-nums text-[12px] text-muted">{step.detail}</span>}
  </li>
}

function WhatsNew({ available, installed }: { available: NonNullable<UpdatesView['available']>; installed: string }) {
  const [read, setRead] = useState(false)
  const id = useId()
  const { latest, breaking, releases } = available
  return <Panel title="What's new" description={<>{installed} → {latest}: {plural(releases.length, 'release')} with their changes, newest first.</>}
    actions={<Install latest={latest} disabled={breaking && !read} />}>
    <div className="space-y-6">
      {breaking && <div className="space-y-2 rounded-md border border-warn/30 bg-warn-soft px-3 py-2.5 text-[13px]">
        <div className="flex gap-2 text-warn"><TriangleAlert className="mt-0.5 size-4 shrink-0" />
          <span>{releases.length > 1 ? 'These releases have' : 'This release has'} breaking changes. Read them and do the upgrade steps before you update.</span></div>
        <div className="flex items-start gap-2.5 pl-6">
          <Checkbox id={id} className="mt-0.5" checked={read} onCheckedChange={v => setRead(v === true)} />
          <label htmlFor={id} className="cursor-pointer">I have read the breaking changes and done the upgrade steps</label>
        </div>
      </div>}
      {releases.map(r => <ReleaseCard key={r.tag} release={r} />)}
    </div>
  </Panel>
}

function ReleaseCard({ release }: { release: Release }) {
  const notes = parseNotes(release.notes)
  return <section aria-label={`Release ${release.tag}`} className="space-y-3">
    <header className="flex flex-wrap items-center gap-2 border-b border-line pb-2">
      <h3 className="text-[14px] font-semibold">{release.tag}</h3>
      {release.published_at && <span className="text-[12.5px] text-muted">{dateTime(release.published_at)}</span>}
      {release.prerelease && <Badge tone="info">Pre-release</Badge>}
      {release.breaking && <Badge tone="warn">Breaking changes</Badge>}
      <a href={release.url} target="_blank" rel="noreferrer" className="ml-auto inline-flex items-center gap-1 text-[12.5px] text-accent hover:underline">
        On GitHub<ExternalLink className="size-3" /></a>
    </header>
    {!!notes.steps.length && <div className="space-y-1.5 text-[13px]">
      {notes.steps.map((s, i) => <Item key={i} item={s} />)}
    </div>}
    {notes.sections.map((s, i) => <div key={i} className="space-y-1.5">
      <SectionTitle section={s} />
      <SectionItems section={s} />
    </div>)}
    {!notes.steps.length && !notes.sections.length && <p className="text-[13px] text-muted">No notes.</p>}
    {notes.compare && <a href={notes.compare} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-[12.5px] text-muted hover:text-accent hover:underline">
      Every commit<ExternalLink className="size-3" /></a>}
  </section>
}

/** GitHub's categories as small labels; the release's own headings as headings
 * below the card's h3, by depth. */
function SectionTitle({ section: s }: { section: NoteSection }) {
  if (!s.title) return null
  if (s.generated)
    return <h4 className={cn('text-[12px] font-medium uppercase tracking-wide', s.breaking ? 'text-warn' : 'text-muted')}>{s.title}</h4>
  const tone = s.breaking ? 'text-warn' : undefined
  const title = <NoteText text={s.title} />
  if (s.depth <= 1) return <h4 className={cn('border-t border-line pt-3 text-[14px] font-semibold', tone)}>{title}</h4>
  if (s.depth === 2) return <h5 className={cn('pt-1 text-[13px] font-semibold', tone)}>{title}</h5>
  return <h6 className={cn('text-[13px] font-medium', tone)}>{title}</h6>
}

/** Paragraphs and code as they are; each run of bullets as one list, in order. */
function SectionItems({ section: s }: { section: NoteSection }) {
  const blocks: (NoteItem | NoteItem[])[] = []
  for (const item of s.items) {
    const last = blocks.at(-1)
    if (item.code || item.para) blocks.push(item)
    else if (Array.isArray(last)) last.push(item)
    else blocks.push([item])
  }
  return <>{blocks.map((block, i) => !Array.isArray(block)
    ? <div key={i} className="text-[13px]"><Item item={block} /></div>
    : <ul key={i} className="space-y-1 text-[13px]">
      {block.map((item, j) => <li key={j} className="flex gap-2">
        <span aria-hidden className={cn('mt-[7px] size-1.5 shrink-0 rounded-full', s.breaking ? 'bg-warn' : 'bg-faint')} />
        <span className="min-w-0"><NoteText text={item.text} />{item.pr && <> <a href={item.pr.url} target="_blank" rel="noreferrer" className="text-muted hover:text-accent hover:underline">#{item.pr.number}</a></>}</span>
      </li>)}
    </ul>)}</>
}

function Item({ item }: { item: NoteItem }) {
  return item.code
    ? <pre className="my-1 overflow-x-auto rounded border border-line bg-panel px-2.5 py-1.5 text-[12px]"><code>{item.text}</code></pre>
    : <p className={item.para ? 'text-muted' : undefined}><NoteText text={item.text} /></p>
}

// Inline Markdown in release notes: **bold**, `code` and [text](https://…), whose
// address may hold balanced parentheses; bare addresses through LinkedText. Built as
// elements, never as HTML. In a link's text (plain) nothing more becomes a link.
const INLINE = /\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?:\/\/(?:[^\s()]|\([^\s()]*\))+)\)/g

function NoteText({ text, plain = false }: { text: string; plain?: boolean }) {
  const parts: ReactNode[] = []
  let last = 0
  const prose = (at: number, end: number) => {
    const chunk = text.slice(at, end)
    parts.push(plain ? chunk : <LinkedText key={`t${at}`} text={chunk} />)
  }
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0
    if (m[3] !== undefined && plain) continue   // no link inside a link
    if (at > last) prose(last, at)
    if (m[1] !== undefined) parts.push(<strong key={at} className="font-semibold"><NoteText text={m[1]} plain={plain} /></strong>)
    else if (m[2] !== undefined) parts.push(<code key={at} className="rounded bg-sunken px-1 py-px font-mono text-[12px]">{m[2]}</code>)
    else parts.push(<a key={at} href={m[4]} target="_blank" rel="noreferrer" className="text-accent hover:underline"><NoteText text={m[3]} plain /></a>)
    last = at + m[0].length
  }
  if (last < text.length) prose(last, text.length)
  return <>{parts}</>
}

function Install({ latest, disabled }: { latest: string; disabled: boolean }) {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  return <Dialog open={open} onOpenChange={o => { setOpen(o); if (!o) setError(null) }}
    trigger={<Button size="sm" variant="primary" disabled={disabled}>Update to {latest}</Button>}
    title={`Update to ${latest}?`}
    description="Takes a few minutes. Settings and running sandboxes are kept."
    footer={<>
      <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
      <Button variant="primary" busy={busy} disabled={disabled} onClick={async () => {
        setBusy(true); setError(null)
        try { qc.setQueryData(keys.updates, await api.updates.install(latest)); setOpen(false) } catch (e) { setError(e) } finally { setBusy(false) }
      }}>Update now</Button>
    </>}>
    {error ? <ErrorState error={error} compact /> : null}
  </Dialog>
}
