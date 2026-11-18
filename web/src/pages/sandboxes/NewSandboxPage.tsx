import { useSearchParams } from 'react-router'
import { PageHeader } from '@/components/PageHeader'
import { NewSandboxForm } from './NewSandboxForm'

export default function NewSandboxPage() {
  const [params] = useSearchParams()
  return <div className="max-w-lg">
    <PageHeader title="New sandbox" crumbs={[{ label: 'Sandboxes', to: '/app/sandboxes' }]} />
    <div className="rounded-lg border border-line bg-panel p-5">
      <NewSandboxForm repo={params.get('repo') || undefined} branch={params.get('branch') || ''} />
    </div>
  </div>
}
