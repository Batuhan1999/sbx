// shadcn/ui's RadioGroup (MIT, see LICENSE.md), on this app's tokens, plus the
// choice-card variant (a whole bordered card is the radio).
import * as R from '@radix-ui/react-radio-group'
import type { ComponentProps, ReactNode } from 'react'
import { cn } from '@/lib/utils'

export function RadioGroup({ className, ...props }: ComponentProps<typeof R.Root>) {
  return <R.Root className={cn('grid gap-2', className)} {...props} />
}

export function RadioGroupItem({ className, ...props }: ComponentProps<typeof R.Item>) {
  return <R.Item className={cn(
    'grid size-4 shrink-0 place-items-center rounded-full border border-line-strong bg-panel outline-none',
    'transition-[border-color,box-shadow] duration-150 ease-out data-[state=checked]:border-accent',
    'focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-accent/25 disabled:cursor-not-allowed disabled:opacity-50', className)} {...props}>
    <R.Indicator className="check-indicator size-2 rounded-full bg-accent" />
  </R.Item>
}

/** A bordered card that is itself the radio: title, optional description and extra content. */
export function RadioCard({ className, title, description, children, ...props }: Omit<ComponentProps<typeof R.Item>, 'title'> & {
  title: ReactNode; description?: ReactNode; children?: ReactNode
}) {
  return <R.Item className={cn(
    'group flex w-full items-start gap-3 rounded-lg border border-line bg-panel p-3 text-left outline-none',
    'transition-[border-color,background-color,box-shadow] duration-150 ease-out hover:border-line-strong active:scale-[0.99]',
    'data-[state=checked]:border-accent data-[state=checked]:bg-accent-soft/40',
    'focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-accent/25 disabled:cursor-not-allowed disabled:opacity-50 disabled:active:scale-100', className)} {...props}>
    <span className="mt-0.5 grid size-4 shrink-0 place-items-center rounded-full border border-line-strong transition-colors duration-150 group-data-[state=checked]:border-accent">
      <R.Indicator className="check-indicator size-2 rounded-full bg-accent" />
    </span>
    <span className="min-w-0 grow">
      <span className="block text-[13.5px] font-medium">{title}</span>
      {description && <span className="block text-[12.5px] text-muted">{description}</span>}
      {children}
    </span>
  </R.Item>
}
