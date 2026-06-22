'use client'

import { useClerk, useUser } from '@clerk/nextjs'
import { ChevronsUpDown, LogOut, Shield, UserRound } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

export function AccountMenu({ isAdmin }: { isAdmin: boolean }) {
  const { user, isLoaded } = useUser()
  const { signOut, openUserProfile } = useClerk()
  const [open, setOpen] = useState(false)
  const popoverRef = useRef<HTMLDivElement | null>(null)
  const triggerRef = useRef<HTMLButtonElement | null>(null)

  useEffect(() => {
    if (!open) return
    function onPointerDown(event: PointerEvent) {
      const target = event.target as Node
      if (popoverRef.current?.contains(target) || triggerRef.current?.contains(target)) return
      setOpen(false)
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  if (!isLoaded || !user) return null

  const name = user.fullName || user.username || user.primaryEmailAddress?.emailAddress || 'Account'
  const email = user.primaryEmailAddress?.emailAddress || ''
  const initials = ((user.firstName?.[0] || name[0] || '?') + (user.lastName?.[0] || '')).toUpperCase()

  return (
    <div className="relative flex-shrink-0 border-t border-neutral-100 pt-2 dark:border-neutral-800">
      {open && (
        <div
          ref={popoverRef}
          className="absolute inset-x-0 bottom-full z-30 mb-2 overflow-hidden rounded-xl border border-neutral-200 bg-white shadow-xl dark:border-neutral-700 dark:bg-neutral-900"
        >
          <div className="flex items-center gap-3 border-b border-neutral-100 px-3.5 py-3 dark:border-neutral-800">
            <Avatar imageUrl={user.imageUrl} initials={initials} size={36} />
            <div className="min-w-0">
              <p className="truncate text-sm font-bold text-neutral-900 dark:text-neutral-50">{name}</p>
              {email && <p className="truncate text-xs text-neutral-400">{email}</p>}
            </div>
          </div>
          <div className="p-1.5">
            <MenuItem
              icon={UserRound}
              label="Manage account"
              onClick={() => {
                setOpen(false)
                openUserProfile()
              }}
            />
            {isAdmin && (
              <MenuItem
                icon={Shield}
                label="Admin panel"
                onClick={() => {
                  setOpen(false)
                  window.location.href = '/admin'
                }}
              />
            )}
          </div>
          <div className="border-t border-neutral-100 p-1.5 dark:border-neutral-800">
            <MenuItem
              icon={LogOut}
              label="Sign out"
              tone="danger"
              onClick={() => {
                setOpen(false)
                void signOut(() => {
                  window.location.href = '/'
                })
              }}
            />
          </div>
        </div>
      )}

      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen(value => !value)}
        aria-expanded={open}
        aria-label="Account menu"
        className="flex w-full items-center gap-2.5 rounded-lg px-2 py-2 text-left hover:bg-neutral-100 dark:hover:bg-neutral-800"
      >
        <Avatar imageUrl={user.imageUrl} initials={initials} size={28} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13px] font-bold text-neutral-900 dark:text-neutral-50">{name}</p>
          {isAdmin && <p className="text-[10px] font-bold uppercase tracking-wide text-amber-600 dark:text-amber-400">Admin</p>}
        </div>
        <ChevronsUpDown size={14} className="flex-shrink-0 text-neutral-400" />
      </button>
    </div>
  )
}

function Avatar({ imageUrl, initials, size }: { imageUrl?: string; initials: string; size: number }) {
  if (imageUrl) {
    // eslint-disable-next-line @next/next/no-img-element
    return <img src={imageUrl} alt="" width={size} height={size} className="flex-shrink-0 rounded-full object-cover" style={{ width: size, height: size }} />
  }
  return (
    <span
      className="flex flex-shrink-0 items-center justify-center rounded-full bg-neutral-900 text-xs font-bold text-white dark:bg-white dark:text-neutral-900"
      style={{ width: size, height: size }}
    >
      {initials}
    </span>
  )
}

function MenuItem({
  icon: Icon,
  label,
  onClick,
  tone = 'default',
}: {
  icon: typeof UserRound
  label: string
  onClick: () => void
  tone?: 'default' | 'danger'
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm font-semibold ${
        tone === 'danger'
          ? 'text-red-600 hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-500/10'
          : 'text-neutral-700 hover:bg-neutral-100 dark:text-neutral-200 dark:hover:bg-neutral-800'
      }`}
    >
      <Icon size={15} />
      {label}
    </button>
  )
}
