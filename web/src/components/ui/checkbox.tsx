// shadcn/ui's Checkbox (MIT, see LICENSE.md), on this app's tokens.
import * as C from '@radix-ui/react-checkbox'
import { Check } from 'lucide-react'
import type { ComponentProps } from 'react'
import { cn } from '@/lib/utils'

export function Checkbox({ className, ...props }: ComponentProps<typeof C.Root>) {
  return <C.Root className={cn(
    'peer grid size-4 shrink-0 place-items-center rounded-[4px] border border-line-strong bg-panel shadow-[0_1px_0_rgb(0_0_0/0.03)] outline-none',
    'transition-[background-color,border-color,box-shadow] duration-150 ease-out',
    'data-[state=checked]:border-accent data-[state=checked]:bg-accent data-[state=checked]:text-accent-ink',
    'focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-accent/25 disabled:cursor-not-allowed disabled:opacity-50', className)} {...props}>
    <C.Indicator className="check-indicator grid place-items-center"><Check className="size-3.5" strokeWidth={3} /></C.Indicator>
  </C.Root>
}
