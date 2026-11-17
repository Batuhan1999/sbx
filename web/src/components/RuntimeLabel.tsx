import { useRuntimeChoice } from '@/api/queries'
import { Badge } from './ui/primitives'

/** The runtime a sandbox runs on, as a small label; nothing when the installation has only one. */
export function RuntimeLabel({ kind, className }: { kind?: string; className?: string }) {
  const choice = useRuntimeChoice()
  if (!choice || !kind) return null
  return <Badge className={className} title="Runtime">{choice.runtimes.find(r => r.kind === kind)?.title ?? kind}</Badge>
}
