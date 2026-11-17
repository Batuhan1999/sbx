// shadcn/ui's Table (MIT, see LICENSE.md), on this app's tokens.
import { ArrowDown, ArrowUp, ChevronsUpDown } from 'lucide-react'
import type { ComponentProps, ReactNode } from 'react'
import { cn } from '@/lib/utils'

export function Table({ className, ...props }: ComponentProps<'table'>) {
  return <div className="relative w-full overflow-x-auto rounded-lg border border-line bg-panel">
    <table className={cn('w-full caption-bottom text-[13px]', className)} {...props} />
  </div>
}

export function TableHeader({ className, ...props }: ComponentProps<'thead'>) {
  return <thead className={cn('[&_tr]:border-b [&_tr]:border-line', className)} {...props} />
}

export function TableBody({ className, ...props }: ComponentProps<'tbody'>) {
  return <tbody className={cn('[&_tr:last-child]:border-0', className)} {...props} />
}

export function TableRow({ className, ...props }: ComponentProps<'tr'>) {
  return <tr className={cn('border-b border-line transition-colors duration-150 ease-out hover:bg-hover/50', className)} {...props} />
}

export function TableHead({ className, ...props }: ComponentProps<'th'>) {
  return <th className={cn('h-10 whitespace-nowrap px-4 text-left align-middle text-[12px] font-medium text-muted', className)} {...props} />
}

export function TableCell({ className, ...props }: ComponentProps<'td'>) {
  return <td className={cn('px-4 py-3 align-middle', className)} {...props} />
}

/** A header that sorts its TanStack Table column, with the direction shown and announced. */
export function SortHeader({ column, children, className }: {
  column: { getIsSorted: () => false | 'asc' | 'desc'; getNextSortingOrder: () => false | 'asc' | 'desc'; toggleSorting: () => void }
  children: ReactNode; className?: string
}) {
  const sorted = column.getIsSorted()
  const next = column.getNextSortingOrder()   // the column's own cycle: newest first for times, then back to unsorted
  const Icon = sorted === 'asc' ? ArrowUp : sorted === 'desc' ? ArrowDown : ChevronsUpDown
  return <button type="button" onClick={() => column.toggleSorting()}
    aria-label={`${typeof children === 'string' ? children : 'Column'}: ${next === 'asc' ? 'sort ascending' : next === 'desc' ? 'sort descending' : 'clear sorting'}`}
    className={cn('-ml-2 inline-flex h-7 items-center gap-1 rounded-md px-2 font-medium outline-none transition-colors duration-150 hover:bg-hover hover:text-ink focus-visible:ring-[3px] focus-visible:ring-accent/25', sorted && 'text-ink', className)}>
    {children}<Icon aria-hidden className={cn('size-3.5', !sorted && 'opacity-50')} />
  </button>
}
