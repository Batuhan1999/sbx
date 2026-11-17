// shadcn/ui's DropdownMenu (MIT, see LICENSE.md), on this app's tokens.
import * as M from '@radix-ui/react-dropdown-menu'
import type { ComponentProps } from 'react'
import { cn } from '@/lib/utils'

export const DropdownMenu = M.Root
export const DropdownMenuTrigger = M.Trigger

export function DropdownMenuContent({ className, sideOffset = 4, ...props }: ComponentProps<typeof M.Content>) {
  return <M.Portal>
    <M.Content sideOffset={sideOffset} className={cn(
      'popover z-50 min-w-44 overflow-hidden rounded-lg border border-line bg-panel p-1 text-ink shadow-lg',
      'origin-(--radix-dropdown-menu-content-transform-origin)', className)} {...props} />
  </M.Portal>
}

export function DropdownMenuItem({ className, variant, ...props }: ComponentProps<typeof M.Item> & { variant?: 'destructive' }) {
  return <M.Item className={cn(
    'relative flex cursor-default select-none items-center gap-2 rounded-md px-2 py-1.5 text-[13px] outline-none',
    'data-[highlighted]:bg-hover data-[disabled]:pointer-events-none data-[disabled]:opacity-50 [&_svg]:size-4 [&_svg]:shrink-0 [&_svg]:text-muted',
    variant === 'destructive' && 'text-bad data-[highlighted]:bg-bad-soft [&_svg]:text-bad', className)} {...props} />
}

export function DropdownMenuSeparator({ className, ...props }: ComponentProps<typeof M.Separator>) {
  return <M.Separator className={cn('-mx-1 my-1 h-px bg-line', className)} {...props} />
}

export function DropdownMenuLabel({ className, ...props }: ComponentProps<typeof M.Label>) {
  return <M.Label className={cn('truncate px-2 py-1.5 text-[12px] text-muted', className)} {...props} />
}
