'use client'

import * as React from 'react'
import { cn } from '@/v2/lib/utils'

interface ToggleGroupProps {
  value: string
  onValueChange: (value: string) => void
  children: React.ReactNode
  className?: string
}

export function ToggleGroup({ value, onValueChange, children, className }: ToggleGroupProps) {
  return (
    <div className={cn('inline-flex rounded-md border border-border bg-muted p-1', className)} role="group">
      {React.Children.map(children, child => {
        if (!React.isValidElement<ToggleGroupItemProps>(child)) return child
        return React.cloneElement(child, {
          selected: child.props.value === value,
          onValueSelect: onValueChange,
        })
      })}
    </div>
  )
}

interface ToggleGroupItemProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  value: string
  selected?: boolean
  onValueSelect?: (value: string) => void
}

export function ToggleGroupItem({ value, selected, onValueSelect, className, children, ...props }: ToggleGroupItemProps) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={() => onValueSelect?.(value)}
      className={cn(
        'h-7 rounded px-2 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground',
        selected && 'bg-background text-foreground shadow-sm',
        className,
      )}
      {...props}
    >
      {children}
    </button>
  )
}
