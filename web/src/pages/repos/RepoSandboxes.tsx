import { Boxes, Plus } from 'lucide-react'
import { useState } from 'react'
import { useSandboxes } from '@/api/queries'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { NewSandboxForm } from '../sandboxes/NewSandboxForm'
import { CapacityNote } from '../sandboxes/SandboxesPage'
import { SandboxTable } from '../sandboxes/SandboxTable'
import { useRepo } from './RepoLayout'

export default function RepoSandboxes() {
  const { repo, me } = useRepo()
  const scope = me?.admin ? 'all' : 'mine'
  const list = useSandboxes(scope, repo, !!me)
  const [open, setOpen] = useState(false)
  return <div className="space-y-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <p className="text-[13px] text-muted">{scope === 'all' ? 'Every sandbox of this repository.' : 'Your sandboxes of this repository.'} <CapacityNote list={list.data} /></p>
      <Dialog open={open} onOpenChange={setOpen} title="New sandbox" description={repo} trigger={<Button size="sm" variant="primary"><Plus />New sandbox</Button>}>
        <NewSandboxForm repo={repo} onCancel={() => setOpen(false)} />
      </Dialog>
    </div>
    {list.error ? <ErrorState error={list.error} retry={() => list.refetch()} />
      : !list.data ? <div className="rounded-lg border border-line bg-panel"><SkeletonRows rows={3} /></div>
      : list.data.sandboxes.length ? <SandboxTable sandboxes={list.data.sandboxes} showRepo={false} />
      : <div className="rounded-lg border border-dashed border-line"><EmptyState icon={Boxes} title="No sandboxes for this repository" /></div>}
  </div>
}
