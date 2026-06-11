# Fronei — Product Roadmap & Implementation Guide

> Vision: a digital personal assistant where users never have to think about which AI model to use
> or worry about cost overrun. Just ask. Fronei handles the rest.

> Status note: this file contains historical implementation prompts as well as roadmap ideas.
> Several early phases have already been implemented. For the current architecture, read
> `docs/architecture.md`; for the current engineering roadmap, read `docs/review-and-roadmap.md`.

---

## The Core Shift

The current build already hides most routing details behind developer mode and presents a
personal workbench by default. The next product shift is to make the personalization,
research evidence, and workspace organization feel native rather than bolted onto chat.

| Current | Next target |
|---------|-------------|
| Clean chat with developer mode for routing internals | Keep internals available without making them central |
| Quick / Smart / Thorough model intent labels | Smarter automatic mode selection based on task risk |
| Clerk auth and per-user conversation ownership | Stronger production audience enforcement and admin controls |
| Conversation summaries plus persistent memories | User-controllable memory review, retention, and project scoping |
| Twin profile from writing samples | More explicit voice controls and feedback loops |
| Deep research with sources, claims, findings, and verifier notes | More ergonomic evidence navigation, export, and follow-up |
| Single-user workbench orientation | Project/workspace organization and eventual team mode |

---

## Phase 1 — Rebrand to Fronei
**Effort: 2–4 hours | No backend changes**

What changes: every UI string, the logo icon, the page metadata, and the empty state copy.
The routing intelligence stays identical — only the presentation changes.

---

### Prompt 1A — Rename throughout

```
Rename the application from "Fronei" to "Fronei" everywhere in the frontend and backend.

apps/web/app/layout.tsx
- metadata.title: "Fronei"
- metadata.description: "Your AI personal assistant"

apps/web/app/page.tsx
- .logo-name text: "Fronei"
- Logo icon: change ti-route to ti-sparkles in both .logo-mark instances
- Empty state headline: "What can I help you with today?"
- Empty state subtitle: "Fronei finds the best AI for every task — you just ask."
- The topbar title for a new conversation: "New chat with Fronei"
- composer placeholder: "Ask Fronei anything…"

apps/web/app/dashboard/page.tsx
- .logo-name text: "Fronei"
- Logo icon: ti-sparkles
- Topbar title: "Usage analytics"
- Eyebrow text: keep "Analytics"

apps/api/app/main.py
- FastAPI title: "Fronei API"

apps/api/pyproject.toml
- name: "fronei-api"
- description: "Fronei — personal AI assistant API"

apps/web/package.json
- name: "fronei-web"

Do not change: variable names, CSS class names, API routes, localStorage keys,
database table names, or any Python identifiers.
```

---

### Prompt 1B — Developer mode toggle

```
Add a "Developer mode" toggle to the Settings section in the sidebar.
When off (default), all routing metadata is hidden and Fronei feels like a clean personal assistant.
When on, the full routing transparency UI appears — model badges, execution log, latency, cost.

apps/web/app/page.tsx changes:

1. Add state (after existing layout state):
   const [devMode, setDevMode] = useState(false)

2. In the init useEffect, load from localStorage:
   const dm = localStorage.getItem('md-dev-mode')
   if (dm) setDevMode(dm === '1')

3. Add a persist effect:
   useEffect(() => {
     try { localStorage.setItem('md-dev-mode', devMode ? '1' : '0') } catch {}
   }, [devMode])

4. In the Settings section body, add this row after the theme row:
   <div className="settings-row">
     <span className="settings-row-label">Developer mode</span>
     <div className="theme-btn-group">
       <button
         className={`theme-btn-opt${!devMode ? ' active' : ''}`}
         onClick={() => { setDevMode(false); setRightPanelOpen(false) }}
       >Off</button>
       <button
         className={`theme-btn-opt${devMode ? ' active' : ''}`}
         onClick={() => setDevMode(true)}
       >On</button>
     </div>
   </div>

5. Gate these UI elements behind devMode:

   In the turn-header for assistant messages:
   - model-badge span: only render when devMode is true
   - turn-type-badge span: only render when devMode is true

   In turn-actions for assistant messages:
   - latency-tag span: only render when devMode is true
   - exec-log-btn button: only render when devMode is true

   In the topbar-controls:
   - The exec log panel toggle button (ti-activity): only render when devMode is true
   - When devMode flips to false, also call setRightPanelOpen(false)

   In the composer-toolbar:
   - The force_model <select>: only render when devMode is true

6. Apply the same devMode state (loaded from the same 'md-dev-mode' localStorage key)
   to apps/web/app/dashboard/page.tsx. No conditional rendering needed on the dashboard.
```

---

### Prompt 1C — Simplified profile language

```
Replace the technical routing profile labels with intent-based language.
The backend values (cost_saver, balanced, best_quality) are unchanged —
only what the user sees changes.

In apps/web/app/page.tsx:

1. Update the PROFILES constant:
   const PROFILES: { value: Profile; label: string }[] = [
     { value: 'cost_saver',   label: 'Quick'    },
     { value: 'balanced',     label: 'Smart'    },
     { value: 'best_quality', label: 'Thorough' },
   ]

2. The profile <select> options already use p.label — no further change needed there.

3. In the topbar chip, show the simplified label:
   Replace: {profile.replace(/_/g, ' ')}
   With: {PROFILES.find(p => p.value === profile)?.label ?? 'Smart'}

4. Set the default profile state to 'balanced' (already is — confirm it stays).

No backend changes. The API receives cost_saver/balanced/best_quality as before.
```

---

## Phase 2 — Onboarding & Personalization
**Effort: 1–2 days | Minimal backend changes**

On first launch, Fronei asks who you are and what you do.
This personalises the greeting and injects context into the planner so
routing decisions are better calibrated from the very first message.

---

### Prompt 2A — First-run onboarding modal

```
Add a first-run onboarding modal that collects the user's name and work domain.
It appears once on first launch, can be skipped, and stores prefs to localStorage.

apps/web/app/page.tsx:

1. Add state:
   const [showOnboarding, setShowOnboarding] = useState(false)
   const [userName, setUserName] = useState('')
   const [userDomain, setUserDomain] = useState('')

2. In the init useEffect, after loading other prefs:
   const onboarded = localStorage.getItem('md-onboarded')
   if (!onboarded) setShowOnboarding(true)
   const savedName = localStorage.getItem('md-user-name')
   const savedDomain = localStorage.getItem('md-user-domain')
   if (savedName) setUserName(savedName)
   if (savedDomain) setUserDomain(savedDomain)

3. Add completeOnboarding():
   function completeOnboarding() {
     try {
       localStorage.setItem('md-onboarded', '1')
       if (userName.trim()) localStorage.setItem('md-user-name', userName.trim())
       if (userDomain.trim()) localStorage.setItem('md-user-domain', userDomain.trim())
     } catch {}
     setShowOnboarding(false)
   }

4. Render the modal when showOnboarding is true. Place it just before </div> that closes .shell:
   A fixed full-screen overlay (inset 0, z-index 60, bg rgba(0,0,0,0.72))
   containing a centered white card (max-width 440px, padding 32px, border-radius 20px,
   bg var(--bg-s1), border 1px solid var(--bd2)):

   Contents:
   - Logo mark (ti-sparkles) + "Hi, I'm Fronei." heading (font-size 22px)
   - Subtitle: "Let me learn a little about you so I can be more helpful from the start."
   - Label + input: "What should I call you?" — bound to userName
   - Label + input: "What kind of work do you do?" placeholder "e.g. software engineer, writer, researcher" — bound to userDomain
   - "Get started →" primary button — calls completeOnboarding()
   - Small "Skip for now" text link below the button — also calls completeOnboarding()
   - On Enter in either input, focus moves to the next; Enter in second input calls completeOnboarding()
   - Both fields are optional — the button is always enabled

5. Update the empty state greeting:
   const firstName = userName.split(' ')[0]
   Headline: firstName ? `What can I help you with today, ${firstName}?` : "What can I help you with today?"

6. Inject user context into new conversations.
   In the submit() function, when wasNew is true and userName or userDomain is set,
   prepend a hidden context line to the message sent to the API (not displayed in the UI):

   const userCtx = [
     userName && `User: ${userName}`,
     userDomain && `Domain: ${userDomain}`,
   ].filter(Boolean).join(' | ')

   const apiMessage = (wasNew && userCtx)
     ? `[Context: ${userCtx}]\n\n${sent}`
     : sent

   Use apiMessage in the fetch body but keep sent as the displayed message text.
   This gives the planner user context on the first turn without showing it in the UI.
```

---

### Prompt 2B — Preferences in Settings

```
Add a Preferences section to the Settings panel where users can update
their name, domain, and default routing preference after onboarding.

apps/web/app/page.tsx:

1. Add a "Preferences" sub-section inside the Settings section body,
   above the Theme row:

   <div className="settings-row" style={{ flexDirection: 'column', alignItems: 'flex-start', gap: 8 }}>
     <span className="settings-row-label">Your name</span>
     <input
       className="conv-search-input"
       style={{ width: '100%' }}
       value={userName}
       onChange={e => setUserName(e.target.value)}
       onBlur={() => { try { localStorage.setItem('md-user-name', userName) } catch {} }}
       placeholder="Optional"
     />
   </div>

   <div className="settings-row" style={{ flexDirection: 'column', alignItems: 'flex-start', gap: 8 }}>
     <span className="settings-row-label">Your work domain</span>
     <input
       className="conv-search-input"
       style={{ width: '100%' }}
       value={userDomain}
       onChange={e => setUserDomain(e.target.value)}
       onBlur={() => { try { localStorage.setItem('md-user-domain', userDomain) } catch {} }}
       placeholder="e.g. software engineer"
     />
   </div>

   A nav-divider then the existing Theme row, then the existing Developer mode row.

2. Apply the same userName and userCtx injection to the dashboard's
   sidebar settings (userName/domain display only — no chat functionality on dashboard).
```

---

## Phase 3 — Cross-Conversation Memory
**Effort: 3–5 days | Backend + frontend**

The most important gap between a chatbot and a personal assistant.
After each conversation, Fronei extracts key facts about you and stores them.
These are injected into every new conversation so Fronei always knows your context.

---

### Prompt 3A — Memory data model and API

```
Add a persistent user memory system to the Fronei API.

apps/api/app/db/models.py:

1. Add a new UserMemory model:
   class UserMemory(Base):
       __tablename__ = "user_memories"
       id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
       content: Mapped[str] = mapped_column(Text)          # the fact, e.g. "Uses Python and FastAPI"
       category: Mapped[str] = mapped_column(String(64), default="general")  # work, preference, project, etc.
       source_conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
       created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
       updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

2. Add GET /memory endpoint in a new apps/api/app/routers/memory.py:
   - GET /memory — returns list[UserMemory] ordered by updated_at desc
   - POST /memory — create a memory manually { content: str, category: str }
   - DELETE /memory/{id} — delete a specific memory
   - DELETE /memory — clear all memories (with a confirm query param: ?confirm=true)

3. Register the memory router in apps/api/app/main.py.

4. Add get_all_memories(db) helper to models.py that returns
   all memory content strings as a single formatted block:
   def get_all_memories(db) -> str:
       mems = db.query(UserMemory).order_by(UserMemory.updated_at.desc()).all()
       if not mems: return ""
       return "\n".join(f"- [{m.category}] {m.content}" for m in mems)

5. Create apps/api/app/services/memory_extractor.py:
   A background service (daemon thread, like memory_writer.py) that runs after
   each completed conversation turn and extracts memorable facts.

   Function: extract_and_store(conv_id, user_message, assistant_answer, existing_memories)
   
   Calls a cheap model (gemini/gemini-2.5-flash) with a prompt:
   "You are extracting persistent facts about the user from this conversation turn.
    Only extract facts that are likely to be useful in future conversations:
    preferences, domain expertise, ongoing projects, tools they use, constraints they mentioned.
    Do not extract facts about the topic being discussed.
    Return a JSON array of {content, category} objects where category is one of:
    work, preference, project, tool, constraint, personal.
    Return [] if no persistent facts are present. Output only valid JSON."
   
   Input: user message + assistant response excerpt (max 600 chars each)
   On success: for each extracted fact, INSERT into user_memories
   On failure: silent — no memory is lost, future turns will catch it
   
   Call this from conversations.py after committing each assistant message,
   alongside memory_writer.schedule().
```

---

### Prompt 3B — Inject memory into every conversation

```
Inject stored user memories into the planner context for every new conversation.

apps/api/app/routers/conversations.py:

1. Import get_all_memories from app.db.models.

2. In the chat() handler, after _resolve_conversation() and before run_planner():
   memory_context = get_all_memories(db)

3. Pass memory_context to run_planner():
   plan = run_planner(
       req.message, history, settings.planner_model,
       running_summary=running_summary,
       active_task=active_task,
       user_memory=memory_context,   # new param
   )

4. In apps/api/app/services/planner.py, add user_memory param to run_planner():
   def run_planner(..., user_memory: str = "") -> Plan:
   
   Inject user_memory into the system messages before history:
   if user_memory:
       state_parts.append(f"USER MEMORY (persistent facts about this user):\n{user_memory}")

5. Apply the same injection to the streaming path in event_generator().

6. Update the PLANNER_SYSTEM_PROMPT in prompts.py to reference user memory:
   Add at the end: "user memory — if present, use it to personalise context_summary
   and enriched_prompt. Reference the user's known domain, tools, and preferences
   when they are relevant to the current task."
```

---

### Prompt 3C — Memory management UI

```
Add a Memory section to the Settings panel where users can see and delete
what Fronei has learned about them.

apps/web/app/page.tsx:

1. Add state:
   const [memories, setMemories] = useState<{id: number; content: string; category: string}[]>([])
   const [memoriesLoaded, setMemoriesLoaded] = useState(false)

2. When settingsOpen becomes true (useEffect on settingsOpen), fetch memories:
   if (settingsOpen && !memoriesLoaded) {
     fetch(`${API_BASE}/memory`)
       .then(r => r.ok ? r.json() : [])
       .then(m => { setMemories(m); setMemoriesLoaded(true) })
       .catch(() => {})
   }

3. Add a "What Fronei remembers" sub-section at the top of the Settings body:
   - Section label: "What Fronei remembers"
   - If memories.length === 0 and memoriesLoaded: show dim text "Nothing yet."
   - For each memory: a row with the content text and a small × delete button
     - On click: DELETE /memory/{id}, remove from local state
   - A "Clear all" link at the bottom (only if memories.length > 0)
     - On click: DELETE /memory?confirm=true, clear local state
   - Max height 200px with overflow-y: auto so it doesn't blow out the sidebar

4. Category pills: prefix each memory row with a small pill showing m.category
   using the existing exec-pill CSS class.
```

---

## Phase 4 — Progressive Response Mode
**Effort: 2–3 days | Frontend-only**

For longer answers, Fronei shows a "thinking" phase before streaming begins —
giving the user feedback that something intelligent is happening, not just latency.
It also introduces follow-up suggestions after each assistant response.

---

### Prompt 4A — Thinking state & follow-up suggestions

```
Add two UX enhancements that make Fronei feel more like an active assistant:
a "thinking" label during the planner phase, and 2–3 follow-up suggestions
after each assistant response.

apps/web/app/page.tsx:

1. The streaming path already has a loading state before the first token arrives.
   In the loading (not yet streaming) state, replace the plain typing-dot with:
   <div className="thinking-state">
     <div className="typing-dot"><span /><span /><span /></div>
     <span className="thinking-label">Fronei is thinking…</span>
   </div>
   CSS for .thinking-state: display flex; align-items center; gap 8px
   CSS for .thinking-label: font-size 12px; color var(--t5); font-style italic

2. After each complete assistant message (when streaming stops and execution_log
   is available), show 2–3 follow-up suggestions below the turn-actions row.
   
   Generate them client-side from the planner intent field:
   - Only show when devMode is false (regular users) and the message is the last one
   - Suggestions are derived from the plan's sub_queries (if decompose) or
     a simple set of generic follow-ups based on task_type:
     coding → ["Explain this in more detail", "Add error handling", "Write tests for this"]
     architecture → ["What are the trade-offs?", "How would this scale?", "Show me the sequence diagram"]
     writing → ["Make it more concise", "Adjust the tone", "Expand on the key points"]
     research → ["Go deeper on this topic", "Compare alternatives", "Summarise for an executive"]
     default → ["Tell me more", "Give me an example", "What should I do next?"]
   
   Render as small ghost buttons below the last assistant message:
   <div className="followup-chips">
     {suggestions.map(s => (
       <button key={s} className="followup-chip" onClick={() => { setMessage(s); taRef.current?.focus() }}>
         {s}
       </button>
     ))}
   </div>
   
   CSS for .followup-chips: display flex; gap 6px; flex-wrap wrap; padding 4px 0
   CSS for .followup-chip: font-size 12px; padding 4px 10px; border-radius 20px;
     border 1px solid var(--bd); background transparent; color var(--t4);
     cursor pointer; font-family inherit; transition border-color 0.15s color 0.15s
   CSS for .followup-chip:hover: border-color var(--ac-bd); color var(--ac-text)
```

---

## Phase 5 — PWA & Mobile
**Effort: 1–2 days | Frontend-only**

Make Fronei installable as a Progressive Web App and genuinely usable on mobile.

---

### Prompt 5A — PWA manifest and icons

```
Add PWA support so Fronei can be installed as a desktop or mobile app.

1. Create apps/web/public/manifest.json:
   {
     "name": "Fronei",
     "short_name": "Fronei",
     "description": "Your AI personal assistant",
     "start_url": "/",
     "display": "standalone",
     "background_color": "#09090b",
     "theme_color": "#7c3aed",
     "icons": [
       { "src": "/icon-192.png", "sizes": "192x192", "type": "image/png" },
       { "src": "/icon-512.png", "sizes": "512x512", "type": "image/png" }
     ]
   }

2. Create placeholder icon files (192×192 and 512×512 purple square with a sparkles
   icon centred) in apps/web/public/. Use a canvas-based script or any SVG→PNG tool.
   For a quick placeholder, a solid #7c3aed square with white "S" text works.

3. In apps/web/app/layout.tsx, add to <head>:
   <link rel="manifest" href="/manifest.json" />
   <meta name="theme-color" content="#7c3aed" />
   <meta name="apple-mobile-web-app-capable" content="yes" />
   <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
   <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
```

---

### Prompt 5B — Mobile navigation

```
Add a mobile bottom navigation bar so Fronei is usable on phones.

apps/web/app/globals.css — add at the bottom of the responsive section:

@media (max-width: 640px) {
  html, body { overflow: hidden; }

  .sidenav { display: none !important; }

  .mobile-nav {
    position: fixed;
    bottom: 0; left: 0; right: 0;
    height: 56px;
    background: var(--bg-s1);
    border-top: 1px solid var(--bd);
    display: flex;
    z-index: 40;
  }

  .mobile-nav-btn {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 3px;
    border: none;
    background: transparent;
    color: var(--t5);
    font-size: 10px;
    font-family: inherit;
    cursor: pointer;
    text-decoration: none;
  }
  .mobile-nav-btn.active { color: var(--ac-text); }
  .mobile-nav-btn i { font-size: 20px; }

  .sidebar-overlay {
    position: fixed;
    inset: 0;
    z-index: 30;
    background: rgba(0,0,0,0.55);
  }

  .sidenav.mobile-open {
    display: flex !important;
    position: fixed;
    left: 0; top: 0; bottom: 0;
    width: 280px !important;
    min-width: 280px !important;
    z-index: 35;
    animation: slideInLeft 0.22s ease both;
  }

  @keyframes slideInLeft {
    from { transform: translateX(-100%); }
    to   { transform: translateX(0); }
  }

  .workspace, .composer-wrap { padding-bottom: 60px; }
  .chat-thread { padding: 14px 16px; }

  .composer-toolbar { flex-wrap: wrap; }
  .c-select { flex: 1 1 120px; }
  .send-btn { width: 100%; justify-content: center; }
}

apps/web/app/page.tsx — add the mobile nav bar:

1. Add state: const [mobileMenuOpen, setMobileMenuOpen] = useState(false)

2. Render before the closing </div> of .shell:

   {mobileMenuOpen && (
     <div className="sidebar-overlay" onClick={() => setMobileMenuOpen(false)} />
   )}

   <nav className="mobile-nav" aria-label="Mobile navigation">
     <button className="mobile-nav-btn" onClick={() => { setMobileMenuOpen(true) }} aria-label="Menu">
       <i className="ti ti-menu-2" aria-hidden="true" />
       Menu
     </button>
     <button className="mobile-nav-btn active" onClick={() => { setMobileMenuOpen(false) }} aria-label="Chat">
       <i className="ti ti-message" aria-hidden="true" />
       Chat
     </button>
     <button className="mobile-nav-btn" onClick={newConversation} aria-label="New chat">
       <i className="ti ti-plus" aria-hidden="true" />
       New
     </button>
     <Link href="/dashboard" className="mobile-nav-btn" aria-label="Dashboard">
       <i className="ti ti-chart-bar" aria-hidden="true" />
       Analytics
     </Link>
   </nav>

3. Add class mobile-open to sidenav when mobileMenuOpen is true.
   Close it when loadConversation or newConversation is called.
```

---

## Phase 6 — Conversation Intelligence
**Effort: 3–5 days | Backend + frontend**

Pattern detection and proactive suggestions across conversations —
the features that move Fronei from reactive to genuinely assistant-like.

---

### Prompt 6A — Per-conversation cost in sidebar

```
Show the total cost of each conversation next to its title in the sidebar.
Quick win that surfaces the value of intelligent routing.

apps/api/app/schemas.py:
- Add total_cost_usd: float = 0.0 to ConversationSummary

apps/api/app/routers/conversations.py:
- In list_conversations(), for each conversation, compute total cost:
  from sqlalchemy import func
  total = db.query(func.sum(ConversationMessage.estimated_cost_usd))\
             .filter(ConversationMessage.conversation_id == conv.id)\
             .filter(ConversationMessage.role == 'assistant')\
             .scalar() or 0.0
  Include in the summary response.

  For efficiency, do this in one query using a subquery or join rather than
  N+1 per conversation.

apps/web/app/page.tsx:
- Add total_cost_usd?: number to the ConversationSummary type.
- In the conv-item, after the conv-item-text span, add:
  {c.total_cost_usd != null && c.total_cost_usd > 0 && (
    <span style={{ fontSize: 10, color: 'var(--t6)', flexShrink: 0, marginRight: 2 }}>
      ${c.total_cost_usd.toFixed(3)}
    </span>
  )}
  Place it between the title and the action buttons.
```

---

### Prompt 6B — Message editing

```
Allow users to edit a previously sent message and re-run the conversation from that point.

apps/web/app/page.tsx:

1. Add state: const [editingId, setEditingId] = useState<number | null>(null)
              const [editText, setEditText] = useState('')

2. On user message turn-actions, add an edit button (only when not loading/streaming):
   <button
     className="action-btn"
     onClick={() => { setEditingId(m.id); setEditText(m.content) }}
     title="Edit message"
   >
     <i className="ti ti-edit" />
   </button>

3. When editingId === m.id, render the user bubble as an inline textarea instead
   of plain text, with Save and Cancel buttons below:
   <textarea
     className="composer-ta"
     value={editText}
     onChange={e => setEditText(e.target.value)}
     onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); saveEdit() } }}
     autoFocus
   />
   <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end', marginTop: 4 }}>
     <button className="toggle-chip" onClick={() => setEditingId(null)}>Cancel</button>
     <button className="send-btn" style={{ padding: '5px 14px' }} onClick={saveEdit}>Save & re-run</button>
   </div>

4. saveEdit():
   - Truncate messages to everything before this message (exclusive)
   - If activeConvId: delete all messages from this point forward via
     DELETE /conversations/{id}/messages/from/{editingId} (add this endpoint)
     or simply start a new conversation seeded with prior history
   - Simpler MVP approach: keep all messages up to (not including) this turn,
     set activeConvId to the same conv, and call submit(editText)
   - setEditingId(null)

   Backend endpoint to add (apps/api/app/routers/conversations.py):
   DELETE /conversations/{conv_id}/messages/from/{message_id}
   Deletes the message with that ID and all subsequent messages in the conversation.
   Recomputes message_count. Returns 204.
```

---

## Sequencing Summary

| Phase | What it delivers | Who benefits | Build time |
|-------|-----------------|--------------|------------|
| 1A — Rename | Brand alignment | Everyone | 1–2 hrs |
| 1B — Dev mode | Clean UX for non-technical users | End users | 2–3 hrs |
| 1C — Profile labels | Removes jargon from composer | End users | 30 mins |
| 2A — Onboarding | First-run personalisation | New users | 3–4 hrs |
| 2B — Preferences in settings | Ongoing personalisation | All users | 1–2 hrs |
| 3A — Memory model + API | Cross-session intelligence backend | Foundation | 1 day |
| 3B — Memory injection | Planner knows who you are | All users | 2–3 hrs |
| 3C — Memory UI | User can see/manage what Fronei knows | Trust & control | 2–3 hrs |
| 4A — Thinking state + follow-ups | Feels like an active assistant | All users | 3–4 hrs |
| 5A — PWA manifest | Installable on desktop/mobile | Mobile users | 1 hr |
| 5B — Mobile nav | Usable on phones | Mobile users | 3–4 hrs |
| 6A — Conv cost in sidebar | Surfacing routing value | Power users | 2 hrs |
| 6B — Message editing | Core chat capability | All users | 1 day |

**Do Phase 1 first** — it reframes the product immediately and costs almost nothing.
**Phase 3 (memory) is the highest-leverage** feature for the personal assistant vision.
Without it, Fronei resets every session and isn't really an assistant — it's a chat interface.
