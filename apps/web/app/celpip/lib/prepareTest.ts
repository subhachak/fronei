import type { BankItem, GenerationRun } from '../types'

type Api = {
  getJson: <T>(path: string, init?: RequestInit) => Promise<T>
  postJson: <T>(path: string, body?: unknown) => Promise<T>
}

type PreparedTest = { attempt_id: string }
export type FreshRequirement = { taskKey: string; count?: number }

const wait = (milliseconds: number) => new Promise(resolve => window.setTimeout(resolve, milliseconds))

function readShortfalls(error: unknown): Record<string, number> | null {
  const message = error instanceof Error ? error.message : String(error)
  try {
    const detail = JSON.parse(message)?.detail
    return detail?.shortfalls && typeof detail.shortfalls === 'object' ? detail.shortfalls : null
  } catch {
    return null
  }
}

/** Generate a launch-private batch and assemble exclusively from it. */
export async function prepareAndCreateTest(
  api: Api,
  body: Record<string, unknown>,
  requirements: FreshRequirement[],
  onProgress: (message: string) => void,
): Promise<PreparedTest> {
  const runIds = new Set<string>()
  let missing = Object.fromEntries(requirements.map(item => [item.taskKey, item.count ?? 1]))

  for (let round = 1; round <= 4; round += 1) {
    const requested = Object.entries(missing).filter(([, count]) => count > 0)
    const total = requested.reduce((sum, [, count]) => sum + count, 0)
    onProgress(`Creating ${total} new ${total === 1 ? 'question' : 'questions'} for this test…`)

    const newRuns = await Promise.all(requested.map(([taskKey, count]) =>
      api.postJson<{ run_id: string }>('/admin/celpip/bank/generate', {
        task_key: taskKey,
        count: Math.min(10, count),
      }),
    ))
    newRuns.forEach(run => runIds.add(run.run_id))

    const generationDeadline = Date.now() + 20 * 60_000
    while (Date.now() < generationDeadline) {
      const list = await api.getJson<{ runs: GenerationRun[] }>('/admin/celpip/bank/runs/list?limit=100')
      const ours = list.runs.filter(run => runIds.has(run.id))
      const active = ours.some(run => ['queued', 'running'].includes(run.status))
      const accepted = ours.reduce((sum, run) => sum + run.accepted, 0)
      onProgress(`Generating and validating fresh questions · ${accepted} accepted`)
      if (ours.length === runIds.size && !active) break
      await wait(4000)
    }

    const assetDeadline = Date.now() + 10 * 60_000
    while (Date.now() < assetDeadline) {
      const batches = await Promise.all([...runIds].map(runId =>
        api.getJson<{ questions: BankItem[] }>(
          `/admin/celpip/bank?limit=200&generation_run_id=${encodeURIComponent(runId)}`,
        ),
      ))
      const ours = batches.flatMap(batch => batch.questions)
      const building = ours.some(item => ['draft', 'awaiting_assets'].includes(item.status))
      if (!building) break
      onProgress('Finishing audio and task images for your new questions…')
      await wait(4000)
    }

    try {
      onProgress('Opening your fresh test…')
      return await api.postJson<PreparedTest>('/admin/celpip/tests', {
        ...body,
        generation_run_ids: [...runIds],
      })
    } catch (error) {
      const shortfalls = readShortfalls(error)
      if (!shortfalls) throw error
      missing = shortfalls
    }
  }

  throw new Error('The app could not validate enough fresh questions after several attempts. Try again.')
}
