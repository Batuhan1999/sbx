// shadcn/ui's Select (MIT, see LICENSE.md), on this app's tokens.
import * as S from '@radix-ui/react-select'
import { Check, ChevronDown } from 'lucide-react'
import type { ComponentProps, ReactNode } from 'react'
import { cn } from '@/lib/utils'

export const Select = S.Root
export const SelectValue = S.Value

export function SelectTrigger({ className, children, size = 'md', ...props }: ComponentProps<typeof S.Trigger> & { size?: 'sm' | 'md' }) {
  return <S.Trigger className={cn(
    'flex w-full items-center justify-between gap-2 rounded-md border border-line bg-panel text-left text-ink shadow-[0_1px_0_rgb(0_0_0/0.03)] outline-none',
    'transition-[border-color,box-shadow] duration-150 ease-out hover:border-line-strong',
    'focus-visible:border-line-strong focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-accent/15',
    'disabled:cursor-not-allowed disabled:opacity-50 data-[placeholder]:text-faint [&>span]:truncate',
    size === 'sm' ? 'h-8 px-2.5 text-[13px]' : 'h-9 px-3 text-[13.5px]', className)} {...props}>
    {children}
    <S.Icon asChild><ChevronDown className="size-4 shrink-0 text-muted" /></S.Icon>
  </S.Trigger>
}

export function SelectContent({ className, children, ...props }: ComponentProps<typeof S.Content>) {
  return <S.Portal>
    <S.Content position="popper" sideOffset={4} className={cn(
      'popover z-50 max-h-(--radix-select-content-available-height) min-w-(--radix-select-trigger-width) overflow-hidden rounded-lg border border-line bg-panel p-1 text-ink shadow-lg',
      'origin-(--radix-select-content-transform-origin)', className)} {...props}>
      <S.Viewport>{children}</S.Viewport>
    </S.Content>
  </S.Portal>
}

/** An option; with description, a second line under its title (the trigger shows the title alone). */
export function SelectItem({ className, children, description, ...props }: ComponentProps<typeof S.Item> & { description?: ReactNode }) {
  return <S.Item className={cn(
    'relative flex w-full cursor-default select-none flex-col rounded-md py-1.5 pl-2 pr-8 text-[13px] outline-none',
    'data-[highlighted]:bg-hover data-[disabled]:pointer-events-none data-[disabled]:opacity-50', className)} {...props}>
    <S.ItemText>{children}</S.ItemText>
    {description && <span className="text-[12px] text-muted">{description}</span>}
    <span className="absolute right-2 top-2 grid size-4 place-items-center">
      <S.ItemIndicator><Check className="size-4" /></S.ItemIndicator>
    </span>
  </S.Item>
}
