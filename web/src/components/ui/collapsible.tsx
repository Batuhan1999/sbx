// shadcn/ui's Collapsible (MIT, see LICENSE.md) with this app's trigger: a row with a turning chevron.
import * as C from '@radix-ui/react-collapsible'
import { ChevronRight } from 'lucide-react'
import type { ComponentProps, ReactNode } from 'react'
import { cn } from '@/lib/utils'

export const Collapsible = C.Root
export const CollapsibleContent = C.Content

export function CollapsibleTrigger({ className, children, ...props }: ComponentProps<typeof C.Trigger> & { children: ReactNode }) {
  return <C.Trigger className={cn(
    'group flex w-full items-center gap-1.5 rounded-md text-left text-[13px] text-muted outline-none transition-colors duration-150 hover:text-ink',
    'focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-accent/25', className)} {...props}>
    <ChevronRight aria-hidden className="size-4 shrink-0 transition-transform duration-200 ease-[var(--ease-out-strong)] group-data-[state=open]:rotate-90" />
    {children}
  </C.Trigger>
}
