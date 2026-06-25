export type SseMessage = {
  event: string
  data: string
  id?: string
}

function parseFrame(frame: string): SseMessage | null {
  let event = 'message'
  let id: string | undefined
  const data: string[] = []
  for (const line of frame.split(/\r?\n/)) {
    if (!line || line.startsWith(':')) continue
    const separator = line.indexOf(':')
    const field = separator >= 0 ? line.slice(0, separator) : line
    const value = separator >= 0 ? line.slice(separator + 1).replace(/^ /, '') : ''
    if (field === 'event') event = value
    if (field === 'id') id = value
    if (field === 'data') data.push(value)
  }
  if (!data.length) return null
  return { event, id, data: data.join('\n') }
}

export async function* readSse(response: Response): AsyncGenerator<SseMessage> {
  if (!response.body) throw new Error('Streaming response body is unavailable.')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
      const frames = buffer.split(/\r?\n\r?\n/)
      buffer = frames.pop() || ''
      for (const frame of frames) {
        const message = parseFrame(frame)
        if (message) yield message
      }
      if (done) {
        const message = parseFrame(buffer)
        if (message) yield message
        return
      }
    }
  } finally {
    reader.releaseLock()
  }
}
