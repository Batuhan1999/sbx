import { useState, type ReactNode } from 'react'
import { Button } from './ui/button'
import { Dialog } from './ui/dialog'
import { ErrorState } from './states'

export function ConfirmDialog({ trigger, title, description, confirm, onConfirm, tone = 'danger', note = 'This cannot be undone.' }: {
  trigger: ReactNode; title: ReactNode; description: ReactNode; confirm: string; onConfirm: () => Promise<unknown>; tone?: 'danger' | 'primary'; note?: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  return <Dialog open={open} onOpenChange={o => { setOpen(o); if (!o) setError(null) }} trigger={trigger} title={title} description={description}
    footer={<>
      <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
      <Button variant={tone === 'danger' ? 'danger-solid' : 'primary'} busy={busy} onClick={async () => {
        setBusy(true); setError(null)
        try { await onConfirm(); setOpen(false) } catch (e) { setError(e) } finally { setBusy(false) }
      }}>{confirm}</Button>
    </>}>
    {error ? <ErrorState error={error} compact /> : <p className="text-[13px] text-muted">{note}</p>}
  </Dialog>
}
