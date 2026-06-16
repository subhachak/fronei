'use client'

import { IconSend } from '@tabler/icons-react'
import { useEffect, useRef, useState } from 'react'
import { Button } from './ui/button'
import { Textarea } from './ui/textarea'
import { ToggleGroup, ToggleGroupItem } from './ui/toggle-group'

export type QualityMode = 'draft' | 'standard' | 'executive'

interface InputBarProps {
  onSend: (message: string, qualityMode: QualityMode) => void
  disabled?: boolean
}

export function InputBar({ onSend, disabled = false }: InputBarProps) {
  const [value, setValue] = useState('')
  const [qualityMode, setQualityMode] = useState<QualityMode>('standard')
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)

  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 144)}px`
  }, [value])

  function send() {
    const text = value.trim()
    if (!text || disabled) return
    onSend(text, qualityMode)
    setValue('')
  }

  return (
    <div className="border-t border-border bg-background px-4 py-3">
      <div className="mx-auto flex max-w-3xl items-end gap-2">
        <ToggleGroup value={qualityMode} onValueChange={value => setQualityMode(value as QualityMode)} className="hidden shrink-0 sm:inline-flex">
          <ToggleGroupItem value="draft" title="Fast, lower cost">Draft</ToggleGroupItem>
          <ToggleGroupItem value="standard" title="Balanced">Standard</ToggleGroupItem>
          <ToggleGroupItem value="executive" title="Best quality, runs judge loop">Executive</ToggleGroupItem>
        </ToggleGroup>
        <Textarea
          ref={textareaRef}
          value={value}
          rows={1}
          placeholder="Message Fronei"
          className="max-h-36 min-h-10 resize-none"
          disabled={disabled}
          onChange={e => setValue(e.target.value)}
          onKeyDown={e => {
            const meta = e.metaKey || e.ctrlKey
            if ((e.key === 'Enter' && !e.shiftKey) || (meta && e.key === 'Enter')) {
              e.preventDefault()
              send()
            }
          }}
        />
        <Button type="button" size="icon" aria-label="Send message" onClick={send} disabled={disabled || value.trim().length === 0}>
          <IconSend className="h-4 w-4" />
        </Button>
      </div>
    </div>
  )
}
