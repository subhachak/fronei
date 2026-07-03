'use client'

import { Check, Pencil, Plus, Trash2, X } from 'lucide-react'
import { useState } from 'react'
import type { ReactNode } from 'react'
import type { Fact, FactInput } from '../hooks/useFacts'

export function FactsPanel({
  facts,
  loading,
  error,
  onDelete,
  onAdd,
}: {
  facts: Fact[]
  loading: boolean
  error: string | null
  onDelete: (entityId: string, factKey: string) => void | Promise<void>
  onAdd: (fact: FactInput) => void | Promise<void>
}) {
  const [adding, setAdding] = useState(false)
  const [form, setForm] = useState({
    entity_id: '',
    entity_type: 'workspace',
    fact_key: '',
    fact_value: '',
  })
  const [editingKey, setEditingKey] = useState<string | null>(null)
  const [editValue, setEditValue] = useState('')

  function submit() {
    const next = {
      entity_id: form.entity_id.trim(),
      entity_type: form.entity_type.trim() || 'workspace',
      fact_key: form.fact_key.trim(),
      fact_value: form.fact_value.trim(),
      confidence: 1.0,
    }
    if (!next.entity_id || !next.fact_key || !next.fact_value) return
    void onAdd(next)
    setForm({ entity_id: '', entity_type: 'workspace', fact_key: '', fact_value: '' })
    setAdding(false)
  }

  function beginEdit(fact: Fact) {
    setEditingKey(`${fact.entity_id}:${fact.fact_key}`)
    setEditValue(fact.fact_value)
  }

  function saveEdit(fact: Fact) {
    const nextValue = editValue.trim()
    if (!nextValue) return
    void onAdd({
      entity_id: fact.entity_id,
      entity_type: fact.entity_type,
      fact_key: fact.fact_key,
      fact_value: nextValue,
      confidence: fact.confidence,
    })
    setEditingKey(null)
    setEditValue('')
  }

  return (
    <div className="grid gap-3">
      <div className="flex items-center justify-between gap-3">
        <span className="min-w-0 truncate text-xs font-bold uppercase tracking-wide text-neutral-400">Pinned facts</span>
        <button
          type="button"
          onClick={() => setAdding(value => !value)}
          className="flex h-8 shrink-0 items-center gap-1.5 rounded-md px-2.5 text-xs font-semibold text-neutral-500 hover:bg-neutral-100 dark:hover:bg-neutral-800"
        >
          <Plus size={13} /> Add
        </button>
      </div>

      {adding && (
        <div className="grid gap-2 rounded-lg border border-neutral-200 p-3 dark:border-neutral-800">
          <input
            placeholder="Entity, e.g. project"
            value={form.entity_id}
            onChange={event => setForm(value => ({ ...value, entity_id: event.target.value }))}
            className="rounded-md border border-neutral-200 bg-transparent px-2.5 py-1.5 text-sm outline-none focus:border-neutral-400 dark:border-neutral-700 dark:focus:border-neutral-500"
          />
          <input
            placeholder="Type, e.g. workspace"
            value={form.entity_type}
            onChange={event => setForm(value => ({ ...value, entity_type: event.target.value }))}
            className="rounded-md border border-neutral-200 bg-transparent px-2.5 py-1.5 text-sm outline-none focus:border-neutral-400 dark:border-neutral-700 dark:focus:border-neutral-500"
          />
          <input
            placeholder="Key, e.g. stack"
            value={form.fact_key}
            onChange={event => setForm(value => ({ ...value, fact_key: event.target.value }))}
            className="rounded-md border border-neutral-200 bg-transparent px-2.5 py-1.5 text-sm outline-none focus:border-neutral-400 dark:border-neutral-700 dark:focus:border-neutral-500"
          />
          <input
            placeholder="Value, e.g. Next.js + FastAPI"
            value={form.fact_value}
            onChange={event => setForm(value => ({ ...value, fact_value: event.target.value }))}
            className="rounded-md border border-neutral-200 bg-transparent px-2.5 py-1.5 text-sm outline-none focus:border-neutral-400 dark:border-neutral-700 dark:focus:border-neutral-500"
          />
          <div className="grid grid-cols-2 gap-2">
            <button
              type="button"
              onClick={submit}
              className="h-8 rounded-md bg-neutral-900 text-xs font-semibold text-white dark:bg-white dark:text-neutral-900"
            >
              Save
            </button>
            <button
              type="button"
              onClick={() => setAdding(false)}
              className="h-8 rounded-md border border-neutral-200 text-xs font-semibold text-neutral-600 dark:border-neutral-700 dark:text-neutral-300"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {loading && <p className="text-xs text-neutral-400">Loading...</p>}
      {error && <p className="rounded-md border-l-2 border-red-400 bg-red-50 px-2.5 py-1.5 text-xs font-medium text-red-700 dark:bg-red-500/10 dark:text-red-400">{error}</p>}
      {!loading && facts.length === 0 && !adding && (
        <p className="text-xs leading-relaxed text-neutral-400">
          No facts stored yet. Facts are captured automatically from research turns, or add one manually.
        </p>
      )}

      <div className="grid gap-1.5">
        {facts.map(fact => (
          <FactRow
            key={`${fact.entity_id}:${fact.fact_key}`}
            fact={fact}
            editing={editingKey === `${fact.entity_id}:${fact.fact_key}`}
            editValue={editValue}
            onEditValue={setEditValue}
            onBeginEdit={beginEdit}
            onSaveEdit={saveEdit}
            onCancelEdit={() => {
              setEditingKey(null)
              setEditValue('')
            }}
            onDelete={onDelete}
          />
        ))}
      </div>
    </div>
  )
}

function FactRow({
  fact,
  editing,
  editValue,
  onEditValue,
  onBeginEdit,
  onSaveEdit,
  onCancelEdit,
  onDelete,
}: {
  fact: Fact
  editing: boolean
  editValue: string
  onEditValue: (value: string) => void
  onBeginEdit: (fact: Fact) => void
  onSaveEdit: (fact: Fact) => void
  onCancelEdit: () => void
  onDelete: (entityId: string, factKey: string) => void | Promise<void>
}) {
  return (
    <div className="flex items-start gap-2 rounded-lg border border-neutral-100 bg-white p-2.5 dark:border-neutral-800 dark:bg-neutral-900">
      <div className="min-w-0 flex-1">
        <p className="truncate text-xs font-semibold text-neutral-700 dark:text-neutral-200">
          {fact.entity_id} / {fact.fact_key}
        </p>
        {editing ? (
          <input
            autoFocus
            value={editValue}
            onChange={event => onEditValue(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Enter') onSaveEdit(fact)
              if (event.key === 'Escape') onCancelEdit()
            }}
            className="mt-1 w-full rounded-md border border-neutral-200 bg-transparent px-2 py-1.5 text-xs outline-none focus:border-neutral-400 dark:border-neutral-700 dark:focus:border-neutral-500"
          />
        ) : (
          <p className="mt-0.5 break-words text-xs leading-relaxed text-neutral-500 dark:text-neutral-400">{fact.fact_value}</p>
        )}
      </div>
      {editing ? (
        <div className="flex shrink-0 gap-1">
          <IconButton label="Save fact" onClick={() => onSaveEdit(fact)}>
            <Check size={13} />
          </IconButton>
          <IconButton label="Cancel edit" onClick={onCancelEdit}>
            <X size={13} />
          </IconButton>
        </div>
      ) : (
        <div className="flex shrink-0 gap-1">
          <IconButton label="Edit fact" onClick={() => onBeginEdit(fact)}>
            <Pencil size={13} />
          </IconButton>
          <IconButton label={`Delete ${fact.entity_id} ${fact.fact_key}`} danger onClick={() => void onDelete(fact.entity_id, fact.fact_key)}>
            <Trash2 size={13} />
          </IconButton>
        </div>
      )}
    </div>
  )
}

function IconButton({
  label,
  danger = false,
  onClick,
  children,
}: {
  label: string
  danger?: boolean
  onClick: () => void
  children: ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className={`grid h-7 w-7 place-items-center rounded-md ${
        danger
          ? 'text-neutral-300 hover:bg-red-50 hover:text-red-500 dark:text-neutral-600 dark:hover:bg-red-500/10 dark:hover:text-red-400'
          : 'text-neutral-300 hover:bg-neutral-100 hover:text-neutral-700 dark:text-neutral-600 dark:hover:bg-neutral-800 dark:hover:text-neutral-200'
      }`}
    >
      {children}
    </button>
  )
}
