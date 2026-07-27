type DiffOp = { type: 'same' | 'added' | 'removed'; text: string }

/**
 * Paragraph-level diff via LCS. Not a real text-diff algorithm (no
 * word-level granularity), but blog edits are almost always paragraph
 * rewrites/additions/removals, so this is an honest, dependency-free
 * approximation that's good enough to visually confirm what an LLM edit
 * actually did, independent of its own self-reported change summary.
 */
function paragraphDiff(oldText: string, newText: string): DiffOp[] {
  const oldParas = oldText.split(/\n{2,}/).map(p => p.trim()).filter(Boolean)
  const newParas = newText.split(/\n{2,}/).map(p => p.trim()).filter(Boolean)
  const m = oldParas.length
  const n = newParas.length

  const dp: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0))
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      dp[i][j] = oldParas[i] === newParas[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }

  const ops: DiffOp[] = []
  let i = 0
  let j = 0
  while (i < m && j < n) {
    if (oldParas[i] === newParas[j]) {
      ops.push({ type: 'same', text: oldParas[i] })
      i += 1
      j += 1
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      ops.push({ type: 'removed', text: oldParas[i] })
      i += 1
    } else {
      ops.push({ type: 'added', text: newParas[j] })
      j += 1
    }
  }
  while (i < m) {
    ops.push({ type: 'removed', text: oldParas[i] })
    i += 1
  }
  while (j < n) {
    ops.push({ type: 'added', text: newParas[j] })
    j += 1
  }
  return ops
}

function truncate(text: string, max = 160): string {
  return text.length > max ? `${text.slice(0, max)}…` : text
}

export function PostDiff({ before, after }: { before: string; after: string }) {
  const ops = paragraphDiff(before, after)
  return (
    <div className="space-y-1.5 text-sm">
      {ops.map((op, idx) => {
        if (op.type === 'same') {
          return (
            <p key={idx} className="text-neutral-400 dark:text-neutral-500">
              {truncate(op.text)}
            </p>
          )
        }
        if (op.type === 'removed') {
          return (
            <p key={idx} className="rounded bg-red-50 px-2 py-1 text-red-800 line-through dark:bg-red-950/60 dark:text-red-300">
              {op.text}
            </p>
          )
        }
        return (
          <p key={idx} className="rounded bg-emerald-50 px-2 py-1 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300">
            {op.text}
          </p>
        )
      })}
    </div>
  )
}
