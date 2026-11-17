// shadcn/ui's Switch (MIT, see LICENSE.md), on this app's tokens.
import * as S from '@radix-ui/react-switch'
import type { ComponentProps } from 'react'
import { cn } from '@/lib/utils'

export function Switch({ className, ...props }: ComponentProps<typeof S.Root>) {
  return <S.Root className={cn(
    'peer inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border border-transparent outline-none',
    'transition-colors duration-150 ease-out data-[state=checked]:bg-accent data-[state=unchecked]:bg-line-strong',
    'focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-accent/25 disabled:cursor-not-allowed disabled:opacity-50', className)} {...props}>
    <S.Thumb className="pointer-events-none block size-4 rounded-full bg-white shadow-sm transition-transform duration-200 ease-[var(--ease-out-strong)] data-[state=checked]:translate-x-4 data-[state=unchecked]:translate-x-0.5" />
  </S.Root>
}
