# New feature: workspace pinned facts (persistent knowledge base) — Implementation Guide

**What exists today, confirmed by reading the code, and why it doesn't cover this:** Fronei already has two auto-derived persistence layers — `User.profile_json["preferences"]` (durable, user-wide, "how this person likes responses") and `Workspace.priorities_json` (workspace-scoped, "what's actively being worked on"). Both are written nightly by `profile_consolidator.py`, an LLM pass over recent turns; both also happen to have a manual-override PATCH endpoint, but that endpoint edits the *same* auto-derived list — the next nightly run can still rewrite over a manual edit. Neither one is a durable, user-owned fact store immune to being silently regenerated. `key_facts` (the third context field) is purely turn-derived and lives only in the rolling `context_json` window this session's earlier fix already had to patch around. There's a genuine gap: nowhere can a user say "always remember X" and have it stay put.

**Design:** a new `Workspace.pinned_facts_json` column — same shape as `priorities_json` (a bare JSON string list) but never touched by `profile_consolidator.py`, only by the user directly. Workspace-scoped (not conversation-scoped, not global-user) because that's the existing scope boundary this codebase already uses for "durable-ish, topic-specific" information, and it reuses the exact rendering/editing plumbing `priorities_json` already has. Rendered into every turn's context unconditionally, same as priorities — these are meant to always be present, not something a user has to ask for.

## 1. Database: new column + migration

`apps/api/app/db/models.py`, `Workspace` (~line 207-228), add alongside `priorities_json`:

```python
# User-curated, durable facts this workspace should always remember --
# distinct from priorities_json (which profile_consolidator.py overwrites
# nightly) and context_json's key_facts (which are turn-derived and evicted
# as the rolling window trims). Nothing auto-writes to this column; only the
# PATCH /profile/workspaces/{id}/facts endpoint does.
pinned_facts_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
```

New migration, `apps/api/alembic/versions/<new_revision>_add_workspace_pinned_facts.py`, mirroring `f4d5e6f7a8b9_add_workspace_priorities.py` exactly (current head is `e8a9b0c1d2f3` — confirm with `alembic heads` before setting `down_revision`, in case something else has landed since):

```python
"""add user-curated pinned facts to workspaces

Revision ID: <generate>
Revises: e8a9b0c1d2f3
Create Date: <today>
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import column_exists


revision: str = "<generate>"
down_revision: Union[str, Sequence[str], None] = "e8a9b0c1d2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not column_exists("workspaces", "pinned_facts_json"):
        op.add_column(
            "workspaces",
            sa.Column("pinned_facts_json", sa.Text(), nullable=False, server_default="[]"),
        )


def downgrade() -> None:
    if column_exists("workspaces", "pinned_facts_json"):
        op.drop_column("workspaces", "pinned_facts_json")
```

## 2. Backend: read helper + context rendering

`apps/api/app/services/agent/persistence.py`, add next to `_workspace_priorities` (~line 125-131). Cap chosen to match `key_facts`' existing per-item length (200 chars) and stay well inside the context budget even if fully used (12 × 200 ≈ 2400 chars, comparable to what `key_facts`/`running_summary` already routinely consume):

```python
def _workspace_pinned_facts(workspace: Workspace | None) -> list[str]:
    if workspace is None:
        return []
    facts = _loads(workspace.pinned_facts_json, [])
    if not isinstance(facts, list):
        return []
    return [str(item).strip()[:200] for item in facts if str(item).strip()][:12]
```

Wire it into the workspace-scoped context exactly where `workspace_priorities` is set — `conversation_context_text` (~line 488, right next to `workspace_ctx["workspace_priorities"] = _workspace_priorities(workspace)`):

```python
workspace_ctx["workspace_priorities"] = _workspace_priorities(workspace)
workspace_ctx["pinned_facts"] = _workspace_pinned_facts(workspace)
```

`_render_context` (~line 194-236), add a block right after the existing priorities block (~line 217), unconditional like priorities — not gated by `include_workspace_history`, since these should always be visible, not just when a user explicitly asks about workspace history:

```python
if ctx.get("workspace_priorities"):
    lines.append("- Active priorities in this workspace:")
    lines.extend(f"  - {item}" for item in ctx["workspace_priorities"])
if ctx.get("pinned_facts"):
    lines.append("- Pinned facts for this workspace (always remember these):")
    lines.extend(f"  - {item}" for item in ctx["pinned_facts"])
```

## 3. Backend: PATCH/GET endpoints

`apps/api/app/routers/profile.py`, add a request model next to `WorkspacePrioritiesUpdate` (~line 81):

```python
class WorkspaceFactsUpdate(BaseModel):
    facts: list[str] = Field(default_factory=list)
```

Add the endpoint right after `update_workspace_priorities` (~line 226-242), same shape:

```python
@router.patch("/workspaces/{workspace_id}/facts")
def update_workspace_facts(
    workspace_id: str,
    body: WorkspaceFactsUpdate,
    user_id: str = CurrentActiveUser,
) -> dict:
    db = SessionLocal()
    try:
        workspace = db.query(Workspace).filter(Workspace.id == workspace_id, Workspace.user_id == user_id).first()
        if workspace is None:
            raise HTTPException(status_code=404, detail="Workspace not found")
        cleaned = [str(item).strip()[:200] for item in body.facts if str(item).strip()][:12]
        workspace.pinned_facts_json = json.dumps(cleaned)
        db.commit()
        return {"workspace_id": workspace_id, "facts": cleaned}
    finally:
        db.close()
```

`GET /profile/workspaces` (~line 206-221) already returns each workspace as a dict — add the new field alongside `priorities`:

```python
"priorities": _loads(w.priorities_json, []) if isinstance(_loads(w.priorities_json, []), list) else [],
"pinned_facts": _loads(w.pinned_facts_json, []) if isinstance(_loads(w.pinned_facts_json, []), list) else [],
```

## 4. Frontend: types + hook

`apps/web/app/types.ts`, `ProfileWorkspace` (~line 188-198):

```typescript
export type ProfileWorkspace = {
  id: string
  name: string
  priorities: string[]
  priorities_updated_at?: string | null
  pinned_facts: string[]
  conversation_count: number
  turn_count: number
  total_cost_usd: number
  last_active_at?: string | null
  created_at: string
}
```

`apps/web/app/hooks/useProfile.ts`, add next to `updateWorkspacePriorities`/`removeWorkspacePriority` (~line 202-217), identical pattern:

```typescript
const updateWorkspaceFacts = useCallback(async (workspaceId: string, facts: string[]) => {
  const response = await authorizedFetch(`/profile/workspaces/${encodeURIComponent(workspaceId)}/facts`, {
    method: 'PATCH',
    body: JSON.stringify({ facts }),
  })
  if (!response.ok) throw new Error(await readErrorBody(response, 'Could not update pinned facts'))
  const payload = await response.json() as { workspace_id: string; facts: string[] }
  setWorkspaces(prev => prev ? prev.map(w => (w.id === workspaceId ? { ...w, pinned_facts: payload.facts } : w)) : prev)
  return payload.facts
}, [authorizedFetch])

const removeWorkspaceFact = useCallback(async (workspaceId: string, item: string) => {
  const workspace = workspaces?.find(w => w.id === workspaceId)
  const next = (workspace?.pinned_facts || []).filter(f => f !== item)
  return updateWorkspaceFacts(workspaceId, next)
}, [workspaces, updateWorkspaceFacts])
```

Export both from the hook's return object next to the priorities pair.

## 5. Frontend: UI

`apps/web/app/components/ProfileView.tsx`. `WorkspaceCard` (~line 716-762) currently renders one editable list (priorities). Add a second, visually distinct section in the same card so the two lists — "what's active right now" vs. "what should always be remembered" — read as clearly different things, not duplicates:

```tsx
function WorkspaceCard({
  workspace,
  draft,
  onDraftChange,
  onAddPriority,
  onRemovePriority,
  factDraft,
  onFactDraftChange,
  onAddFact,
  onRemoveFact,
}: {
  workspace: ProfileWorkspace
  draft: string
  onDraftChange: (value: string) => void
  onAddPriority: () => void
  onRemovePriority: (item: string) => void
  factDraft: string
  onFactDraftChange: (value: string) => void
  onAddFact: () => void
  onRemoveFact: (item: string) => void
}) {
  return (
    <div className="rounded-xl border border-neutral-200 p-3.5 dark:border-neutral-800">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-bold text-neutral-900 dark:text-neutral-50">{workspace.name}</p>
        <div className="flex items-center gap-2">
          <Badge>{workspace.turn_count} turn{workspace.turn_count === 1 ? '' : 's'}</Badge>
          <Badge>${workspace.total_cost_usd.toFixed(2)}</Badge>
        </div>
      </div>

      <p className="mt-3 text-[11px] font-bold uppercase tracking-wide text-neutral-400">Active right now</p>
      <div className="mt-1.5 flex flex-wrap gap-2">
        {workspace.priorities.length === 0 && (
          <p className="text-xs text-neutral-400">Nothing active here yet.</p>
        )}
        {workspace.priorities.map(item => (
          <RemovableChip key={item} label={item} onRemove={() => onRemovePriority(item)} />
        ))}
      </div>
      <div className="mt-2.5 flex gap-2">
        <input
          value={draft}
          onChange={event => onDraftChange(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter' && draft.trim()) {
              event.preventDefault()
              onAddPriority()
            }
          }}
          placeholder="Add what's active in this workspace…"
          className="min-w-0 flex-1 rounded-lg border border-neutral-200 bg-neutral-50 px-3 py-1.5 text-xs text-neutral-900 outline-none dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-100"
        />
      </div>

      <p className="mt-4 text-[11px] font-bold uppercase tracking-wide text-neutral-400">Always remember</p>
      <div className="mt-1.5 flex flex-wrap gap-2">
        {workspace.pinned_facts.length === 0 && (
          <p className="text-xs text-neutral-400">No pinned facts yet — these persist until you remove them.</p>
        )}
        {workspace.pinned_facts.map(item => (
          <RemovableChip key={item} label={item} onRemove={() => onRemoveFact(item)} />
        ))}
      </div>
      <div className="mt-2.5 flex gap-2">
        <input
          value={factDraft}
          onChange={event => onFactDraftChange(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter' && factDraft.trim()) {
              event.preventDefault()
              onAddFact()
            }
          }}
          placeholder="Add a fact Fronei should always know here…"
          className="min-w-0 flex-1 rounded-lg border border-neutral-200 bg-neutral-50 px-3 py-1.5 text-xs text-neutral-900 outline-none dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-100"
        />
      </div>
    </div>
  )
}
```

Wire it at the call site (~line 314-339), mirroring the existing `workspacePriorityDrafts` state pattern with a sibling `workspaceFactDrafts` map:

```tsx
{(profile.workspaces || []).map(workspace => (
  <WorkspaceCard
    key={workspace.id}
    workspace={workspace}
    draft={workspacePriorityDrafts[workspace.id] || ''}
    onDraftChange={value => setWorkspacePriorityDrafts(prev => ({ ...prev, [workspace.id]: value }))}
    onAddPriority={() => {
      const value = (workspacePriorityDrafts[workspace.id] || '').trim()
      if (!value) return
      void profile.updateWorkspacePriorities(workspace.id, [...workspace.priorities, value])
      setWorkspacePriorityDrafts(prev => ({ ...prev, [workspace.id]: '' }))
    }}
    onRemovePriority={item => void profile.removeWorkspacePriority(workspace.id, item)}
    factDraft={workspaceFactDrafts[workspace.id] || ''}
    onFactDraftChange={value => setWorkspaceFactDrafts(prev => ({ ...prev, [workspace.id]: value }))}
    onAddFact={() => {
      const value = (workspaceFactDrafts[workspace.id] || '').trim()
      if (!value) return
      void profile.updateWorkspaceFacts(workspace.id, [...workspace.pinned_facts, value])
      setWorkspaceFactDrafts(prev => ({ ...prev, [workspace.id]: '' }))
    }}
    onRemoveFact={item => void profile.removeWorkspaceFact(workspace.id, item)}
  />
))}
```

Add `const [workspaceFactDrafts, setWorkspaceFactDrafts] = useState<Record<string, string>>({})` next to the existing `workspacePriorityDrafts` state declaration.

## Out of scope for this pass

- **Admin visibility**: `apps/api/app/routers/admin.py` already surfaces `priorities_json` somewhere (found via grep, not read in detail here) — worth a quick look to decide if pinned facts should appear there too, but not required for the feature to work.
- **Conversation-level or global pinning**: this is workspace-scoped only, matching the existing priorities/consolidation scope boundary. If a fact genuinely belongs to every workspace, the existing `User.profile_json["preferences"]` path (via `PATCH /profile/preferences`) is the closer fit already — don't conflate the two.
- **Auto-suggestion**: no "Fronei suggests pinning this" UX — purely user-initiated, by design, so nothing here can silently drift the way nightly-consolidated priorities can.

## Testing plan

- Migration: run `alembic upgrade head` against a copy of the dev DB, confirm `pinned_facts_json` appears with default `"[]"` on existing rows, `alembic downgrade -1` cleanly reverses it.
- `_workspace_pinned_facts`: unit test empty/None workspace, malformed JSON, over-cap list (25 items → capped at 12), over-length item (300 chars → capped at 200).
- `_render_context`: build a `workspace_ctx` with `pinned_facts` set, assert the rendered text contains "Pinned facts for this workspace" and each fact, and that it appears regardless of `include_workspace_history`.
- `PATCH /profile/workspaces/{id}/facts`: 404 for a workspace not owned by the caller (mirror the existing priorities-endpoint test), successful update returns the cleaned list, re-`GET /profile/workspaces` reflects it.
- Regression: confirm `profile_consolidator.py`'s nightly job never writes to `pinned_facts_json` (it shouldn't reference the new column at all — that's the whole point) by grepping the consolidator source after implementation, not just trusting the design.
- Manual: pin a fact, start a new conversation in that workspace, confirm the model's answer reflects it (e.g. pin "always respond in bullet points for this workspace" and check the next turn honors it) — the actual end-to-end proof this closes the gap that motivated the feature.
