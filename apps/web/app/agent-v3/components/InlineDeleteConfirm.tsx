export function InlineDeleteConfirm({
  title,
  description,
  onCancel,
  onConfirm,
}: {
  title: string
  description: string
  onCancel: () => void
  onConfirm: () => void
}) {
  return (
    <div role="alertdialog" aria-label={title} className="grid gap-2.5 border-t border-red-100 bg-red-50/60 px-3 py-3 dark:border-red-500/15 dark:bg-red-500/5">
      <div>
        <p className="text-xs font-bold text-red-700 dark:text-red-400">{title}</p>
        <p className="mt-0.5 text-[11px] leading-relaxed text-red-600/80 dark:text-red-400/70">{description}</p>
      </div>
      <div className="flex gap-2">
        <button
          type="button"
          onClick={onCancel}
          className="h-7 rounded-full border border-neutral-200 bg-white px-3 text-[11px] font-bold text-neutral-600 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-300"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={onConfirm}
          className="h-7 rounded-full bg-red-600 px-3 text-[11px] font-bold text-white hover:bg-red-700"
        >
          Delete
        </button>
      </div>
    </div>
  )
}
