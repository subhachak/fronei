import { describe, expect, it } from 'vitest'
import { readSse } from './sse'

function streamingResponse(chunks: string[]) {
  const encoder = new TextEncoder()
  return new Response(new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  }))
}

describe('readSse', () => {
  it('parses frames split across arbitrary network chunks', async () => {
    const response = streamingResponse([
      'id: event_1\nevent: prog',
      'ress\ndata: {"stage":"planning"}\n',
      '\n: keepalive\n\n',
      'event: turn\ndata: {"status":"completed"}\n\n',
    ])

    const messages = []
    for await (const message of readSse(response)) messages.push(message)

    expect(messages).toEqual([
      { id: 'event_1', event: 'progress', data: '{"stage":"planning"}' },
      { event: 'turn', data: '{"status":"completed"}' },
    ])
  })

  it('joins multiline data fields', async () => {
    const response = streamingResponse(['event: note\ndata: first\ndata: second\n\n'])
    const messages = []
    for await (const message of readSse(response)) messages.push(message)
    expect(messages[0].data).toBe('first\nsecond')
  })
})
