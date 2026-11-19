import { AlertCircle, Check } from 'lucide-react'
import { useTemplateProgress } from '@/api/queries'
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
    <span className="min-w-0 break-words"><span className="font-medium">Template {stuck.key} failed to build.</span>{stuck.error && <DetailText className="block text-muted" text={stuck.error} />}</span>
  </div>
  if (ready === all.length) return quiet ? null : <div role="status" className={box}><Check className="mt-0.5 size-4 shrink-0 text-ok" /><span>Templates ready.</span></div>
  const building = all.find(t => t.phase === 'building')
  const step = building?.steps ? ` · ${building.key}, step ${building.step ?? 0} of ${building.steps}` : building ? ` · ${building.key}` : ''
  const left = building?.eta_s != null ? `about ${Math.max(1, Math.round(building.eta_s / 60))} min left` : ready === 0 ? 'about 10 minutes' : ''
  return <div role="status" className={box}>
    <Spinner className="mt-0.5 shrink-0" />
    <span className="min-w-0">Building templates: {ready} of {all.length} ready{step}{left ? `, ${left}` : ''}. You can keep going.</span>
  </div>
}
