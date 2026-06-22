import * as React from 'react'
import { cn } from '../../lib/cn'

export function SelectField({
  label,
  value,
  onChange,
  options,
  className,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  options: { value: string; label: string }[]
  className?: string
}) {
  return (
    <label className={cn('flex min-h-9 items-center justify-between gap-2 rounded-lg border border-neutral-200 bg-neutral-50 px-2.5 text-xs dark:border-neutral-800 dark:bg-neutral-900', className)}>
      <span className="font-semibold text-neutral-500 dark:text-neutral-400">{label}</span>
      <select
        value={value}
        onChange={event => onChange(event.target.value)}
        className="min-w-0 flex-1 bg-transparent text-right text-xs font-semibold text-neutral-900 outline-none dark:text-neutral-100"
      >
        {options.map(option => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>
    </label>
  )
}

export const Textarea = React.forwardRef<HTMLTextAreaElement, React.TextareaHTMLAttributes<HTMLTextAreaElement>>(
  ({ className, ...props }, ref) => (
    <textarea
      ref={ref}
      className={cn(
        'w-full flex-1 resize-none bg-transparent text-[15px] leading-relaxed text-neutral-900 outline-none placeholder:text-neutral-400 dark:text-neutral-100 dark:placeholder:text-neutral-500',
        className,
      )}
      {...props}
    />
  ),
)
Textarea.displayName = 'Textarea'

export function SearchInput({
  value,
  onChange,
  onClear,
  placeholder,
}: {
  value: string
  onChange: (value: string) => void
  onClear: () => void
  placeholder: string
}) {
  return (
    <div className="mb-2 flex items-center gap-2 rounded-lg border border-neutral-200 bg-neutral-50 px-2.5 py-1.5 dark:border-neutral-800 dark:bg-neutral-900">
      <input
        value={value}
        onChange={event => onChange(event.target.value)}
        placeholder={placeholder}
        autoFocus
        className="min-w-0 flex-1 bg-transparent text-xs font-medium text-neutral-900 outline-none placeholder:text-neutral-400 dark:text-neutral-100 dark:placeholder:text-neutral-500"
      />
      {value && (
        <button type="button" onClick={onClear} className="text-[11px] font-semibold text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-200">
          Clear
        </button>
      )}
    </div>
  )
}
