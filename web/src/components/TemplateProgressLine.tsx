import { AlertCircle, Check } from 'lucide-react'
import { useTemplateProgress } from '@/api/queries'
import type { TemplateBuild } from '@/api/types'
import { DetailText } from '@/components/states'
import { Spinner } from '@/components/ui/primitives'
import { cn } from '@/lib/utils'

/** One line on where the templates' builds stand: what a new sandbox waits for. Nothing while there is nothing
 * to build or builds are held back; quiet: nothing once every template is ready either. */
export function TemplateProgressLine({ className, quiet }: { className?: string; quiet?: boolean }) {
  const progress = useTemplateProgress()
  const listed = Object.values(progress.data?.runtimes ?? {}).flat()
  const all = listed.filter(t => t.phase !== 'unplanned')
  const unplanned = listed.find(t => t.phase === 'unplanned')
  if (!listed.length || progress.data?.blocked) return null
  const ready = all.filter(t => t.phase === 'current').length
  const stuck = all.find(t => t.phase === 'stuck')
  const box = cn('flex items-start gap-2.5 rounded-lg border border-line bg-panel px-3 py-2 text-[13px]', className)
  if (unplanned && !stuck) return <div role="status" className={cn(box, 'border-bad/25 bg-bad-soft')}>
    <AlertCircle className="mt-0.5 size-4 shrink-0 text-bad" />
    <span className="min-w-0 break-words"><span className="font-medium">Template {unplanned.key} can’t be built:</span> its recipe can’t be read.</span>
  </div>
  if (stuck) return <div role="status" className={cn(box, 'border-bad/25 bg-bad-soft')}>
    <AlertCircle className="mt-0.5 size-4 shrink-0 text-bad" />
    <span className="min-w-0 break-words"><span className="font-medium">Template {stuck.key} failed to build.</span><Why t={stuck} /></span>
  </div>
  const retrying = all.find(t => t.phase === 'retrying')
  if (retrying && !all.some(t => t.phase === 'building')) return <div role="status" className={cn(box, 'border-warn/30 bg-warn-soft')}>
    <AlertCircle className="mt-0.5 size-4 shrink-0 text-warn" />
    <span className="min-w-0 break-words"><span className="font-medium">Template {retrying.key} failed to build; sbx tries again
      {retrying.retry_at ? ` at ${new Date(retrying.retry_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` : ' shortly'}.</span><Why t={retrying} /></span>
  </div>
  if (ready === all.length) return quiet ? null : <div role="status" className={box}><Check className="mt-0.5 size-4 shrink-0 text-ok" /><span>Templates ready.</span></div>
  const building = all.find(t => t.phase === 'building')
  const step = building?.steps ? ` · ${building.key}, step ${building.step ?? 0} of ${building.steps}` : building ? ` · ${building.key}` : ''
  // The time left by the last build there; a first build has none to go by, so it says the usual length only
  // until its steps show (then "step 22 of 22, about 10 minutes" would be wrong).
  const left = building?.eta_s != null ? `about ${Math.max(1, Math.round(building.eta_s / 60))} min left`
    : ready === 0 && !building?.steps ? 'about 10 minutes' : ''
  return <div role="status" className={box}>
    <Spinner className="mt-0.5 shrink-0" />
    <span className="min-w-0">Building templates: {ready} of {all.length} ready{step}{left ? `, ${left}` : ''}. You can keep going.</span>
  </div>
}

/** A failed build's why: sbx's words when it knows the error, the build log's tail under Details. */
function Why({ t }: { t: TemplateBuild }) {
  if (!t.error) return null
  if (!t.reason) return <DetailText className="block text-muted" text={t.error} />
  return <span className="block text-muted">{t.reason}
    <details className="mt-1 text-[12px] [&>summary]:cursor-pointer"><summary className="w-fit hover:text-ink">Details</summary>
      <span className="mt-1 block break-all font-mono text-[11.5px]">{t.error}</span></details>
  </span>
}
