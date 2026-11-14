import * as D from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

export function Dialog({ open, onOpenChange, title, description, children, footer, trigger, wide }: {
  open?: boolean; onOpenChange?: (open: boolean) => void; title: ReactNode; description?: ReactNode
  children?: ReactNode; footer?: ReactNode; trigger?: ReactNode; wide?: boolean
}) {
  return <D.Root open={open} onOpenChange={onOpenChange}>
    {trigger && <D.Trigger asChild>{trigger}</D.Trigger>}
    <D.Portal>
      <D.Overlay className="overlay fixed inset-0 z-40 bg-black/40 backdrop-blur-[1px]" />
      <D.Content
        className={cn('modal fixed left-1/2 top-1/2 z-50 flex max-h-[calc(100dvh-2rem)] w-[calc(100vw-2rem)] -translate-x-1/2 -translate-y-1/2 flex-col rounded-xl border border-line bg-panel shadow-2xl outline-none', wide ? 'max-w-2xl' : 'max-w-md')}>
        <div className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
          <div className="min-w-0">
            <D.Title className="text-[15px] font-semibold">{title}</D.Title>
            {description ? <D.Description className="mt-1 text-[13px] text-muted">{description}</D.Description> : <D.Description className="sr-only">{title}</D.Description>}
          </div>
          <D.Close className="-mr-1.5 -mt-0.5 rounded-md p-1 text-muted hover:bg-hover hover:text-ink" aria-label="Close"><X className="size-4" /></D.Close>
        </div>
        <div className="min-h-0 overflow-y-auto px-5 py-4">{children}</div>
        {footer && <div className="flex flex-wrap justify-end gap-2 border-t border-line px-5 py-3">{footer}</div>}
      </D.Content>
    </D.Portal>
  </D.Root>
}

export function Drawer({ open, onOpenChange, children, label }: { open: boolean; onOpenChange: (open: boolean) => void; children: ReactNode; label: string }) {
  return <D.Root open={open} onOpenChange={onOpenChange}>
    <D.Portal>
      <D.Overlay className="overlay fixed inset-0 z-40 bg-black/40" />
      <D.Content className="drawer fixed inset-y-0 left-0 z-50 flex w-[272px] max-w-[85vw] flex-col border-r border-line bg-panel outline-none">
        <D.Title className="sr-only">{label}</D.Title>
        <D.Description className="sr-only">{label}</D.Description>
        {children}
      </D.Content>
    </D.Portal>
  </D.Root>
}
