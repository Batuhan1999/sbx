import { Slot } from '@radix-ui/react-slot'
import { cva, type VariantProps } from 'class-variance-authority'
import { forwardRef, type ButtonHTMLAttributes, type ComponentProps } from 'react'
import { Link } from 'react-router'
import { cn } from '@/lib/utils'
import { Spinner } from './primitives'

export const buttonVariants = cva(
  'inline-flex shrink-0 select-none items-center justify-center gap-1.5 whitespace-nowrap rounded-md font-medium transition-[background-color,border-color,color,transform] duration-150 ease-out active:scale-[0.97] disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-[15px] [&_svg]:shrink-0',
  {
    variants: {
      variant: {
        primary: 'bg-accent text-accent-ink hover:bg-accent/90',
        secondary: 'border border-line bg-panel text-ink shadow-[0_1px_0_rgb(0_0_0/0.03)] hover:bg-hover hover:border-line-strong',
        ghost: 'text-muted hover:bg-hover hover:text-ink',
        danger: 'border border-line bg-panel text-bad hover:bg-bad-soft hover:border-bad/40',
        'danger-solid': 'bg-bad text-white hover:bg-bad/90',
      },
      size: {
        sm: 'h-7 px-2.5 text-[12.5px]',
        md: 'h-8 px-3 text-[13px]',
        icon: 'size-8',
        'icon-sm': 'size-7',
      },
    },
    defaultVariants: { variant: 'secondary', size: 'md' },
  },
)

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & VariantProps<typeof buttonVariants> & { asChild?: boolean; busy?: boolean }

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant, size, asChild, busy, disabled, children, type = 'button', ...props }, ref,
) {
  const Comp = asChild ? Slot : 'button'
  return <Comp ref={ref} type={asChild ? undefined : type} className={cn(buttonVariants({ variant, size }), className)}
    disabled={asChild ? undefined : disabled || busy} aria-busy={busy || undefined} {...props}>
    {asChild ? children : <>{busy && <Spinner className="size-3.5" />}{children}</>}
  </Comp>
})

export function LinkButton({ className, variant, size, ...props }: ComponentProps<typeof Link> & VariantProps<typeof buttonVariants>) {
  return <Link className={cn(buttonVariants({ variant, size }), className)} {...props} />
}

export function AnchorButton({ className, variant, size, ...props }: ComponentProps<'a'> & VariantProps<typeof buttonVariants>) {
  return <a className={cn(buttonVariants({ variant, size }), className)} {...props} />
}
