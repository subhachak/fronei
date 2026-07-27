"""Blog endpoints: admin authoring (draft/preview/publish) and public reads.

Two routers, same module: admin_router (RequireAdmin, sees drafts) and
public_router (no auth, published posts only). The frontend's draft-preview
gate on /blog/[slug] hits admin_get_post_by_slug when the visitor is an
authenticated admin and the public lookup 404s, so a post renders identically
in preview and once published without a separate preview template to keep in
sync.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import AdminPrincipal, RequireAdmin
from app.db.models import BlogPost, SessionLocal
from app.services.agent.models import new_id

logger = logging.getLogger(__name__)

BLOG_DRAFT_PROMPT = """You are Fronei's blog drafting assistant.

You are given a scratch note or piece of research from the author -- raw,
unstructured, possibly just a paragraph of half-formed thoughts, bullet
points, or copy-pasted research. Turn it into a structured personal blog
post draft: a title, a one-sentence excerpt, 2-5 topical tags, and a
cleaned-up Markdown body.

Rules:
- Preserve the author's actual opinions, claims, and voice. Do not add
  claims, facts, or conclusions that aren't in the notes -- if the notes are
  thin on a point, keep the post thin there too rather than inventing detail.
- Write in first person ("I think...", "my take is..."), not corporate "we."
- The body should read like a finished blog post -- proper paragraphs and
  headings where useful, code fences for code -- not a bullet-point summary
  of the notes.
- Tags should be short, lowercase, topical single words or short phrases
  (e.g. "ai", "predictions", "projects"), not full sentences.

Return only JSON:
{"title": "...", "excerpt": "...", "tags": ["...", "..."], "body_markdown": "..."}
"""

BLOG_EDIT_PROMPT = """You are Fronei's blog editing assistant.

You are given a blog post draft's current title, excerpt, tags, and Markdown
body, plus an editing instruction from the author -- generic ("tighten this
up", "make the title more dramatic") or pointed ("rewrite the second
paragraph", "cut the bit about pricing"). Apply the instruction to whichever
field(s) it actually concerns and return the full post: every field, not
just the one(s) you changed, and the full body, not just the changed
portion.

Rules:
- Only touch the field(s) the instruction concerns. An instruction about the
  title means edit the title and return excerpt/tags/body unchanged; an
  instruction about the body means edit the body and return title/excerpt/
  tags unchanged. Don't rewrite or "improve" a field the instruction didn't
  ask about.
- Preserve the author's voice, opinions, and claims. Don't add new claims, or
  soften or strengthen an opinion beyond what the instruction asks for.
- If the instruction is vague, use reasonable editorial judgment but stay
  conservative -- prefer trimming and clarifying over adding new content.
- Tags are short, lowercase, topical words or short phrases, not sentences.

Return only JSON:
{
  "title": "<current or edited title>",
  "excerpt": "<current or edited excerpt>",
  "tags": ["<current or edited tags>"],
  "body_markdown": "<current or edited full post body>",
  "changes": ["<one short, specific, human-readable description per distinct
    change you actually made, e.g. 'Cut the third paragraph about pricing'
    or 'Made the title more dramatic'. If the instruction doesn't apply to
    anything in the post, explain that instead of guessing.>"]
}
"""

MAX_REVISIONS = 20


def _clean_or_preserve(original: str, candidate: str) -> str:
    """Avoid a false content diff from the model's whitespace normalization:
    if a field's content is the same once trimmed, keep the original string
    exactly (byte-for-byte) rather than the model's cleaned copy, so a field
    the instruction didn't ask about doesn't show up as "changed" over
    nothing but leading/trailing whitespace."""
    stripped = candidate.strip()
    return original if stripped == original.strip() else stripped


def _push_revision(post: BlogPost, *, label: str, changes: list[str] | None = None) -> None:
    """Snapshot post's CURRENT title/excerpt/tags/body as a revision, before
    the caller overwrites any of them. Called for both manual saves and LLM
    edits, so undo covers either source -- not just AI-driven changes."""
    revisions = post.revisions
    revisions.append({
        "id": new_id("rev"),
        "title": post.title,
        "excerpt": post.excerpt,
        "tags": post.tags,
        "body_markdown": post.body_markdown,
        "label": label,
        "changes": changes,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    post.revisions_json = json.dumps(revisions[-MAX_REVISIONS:])


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "post"


def _unique_slug(db, base_slug: str, *, exclude_id: str | None = None) -> str:
    slug = base_slug
    suffix = 2
    while True:
        query = db.query(BlogPost).filter(BlogPost.slug == slug)
        if exclude_id:
            query = query.filter(BlogPost.id != exclude_id)
        if not query.first():
            return slug
        slug = f"{base_slug}-{suffix}"
        suffix += 1


def _serialize(post: BlogPost) -> dict:
    return {
        "id": post.id,
        "slug": post.slug,
        "title": post.title,
        "excerpt": post.excerpt,
        "body_markdown": post.body_markdown,
        "tags": post.tags,
        "author": post.author,
        "voice": post.voice,
        "status": post.status,
        "cover_image_url": post.cover_image_url,
        "published_at": post.published_at.isoformat() if post.published_at else None,
        "created_at": post.created_at.isoformat(),
        "updated_at": post.updated_at.isoformat(),
    }


def _serialize_detail(post: BlogPost) -> dict:
    """_serialize() plus revision history -- for single-post admin views
    (the editor), not the list view or public endpoints, so those stay
    lightweight and drafts' edit history never leaks publicly."""
    return {**_serialize(post), "revisions": post.revisions}


class BlogPostIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    slug: str | None = Field(default=None, max_length=160)
    excerpt: str = ""
    body_markdown: str = ""
    tags: list[str] = Field(default_factory=list)
    author: str = "Subh Chakraborty"
    voice: Literal["personal", "product"] = "personal"
    cover_image_url: str | None = None


class BlogDraftFromNotesIn(BaseModel):
    notes: str = Field(min_length=1, max_length=20_000)


class BlogDraftSuggestion(BaseModel):
    title: str
    excerpt: str
    tags: list[str]
    body_markdown: str


class BlogEditInstructionIn(BaseModel):
    instruction: str = Field(min_length=1, max_length=2_000)


class BlogPostUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, max_length=160)
    excerpt: str | None = None
    body_markdown: str | None = None
    tags: list[str] | None = None
    author: str | None = None
    voice: Literal["personal", "product"] | None = None
    cover_image_url: str | None = None


# ---------------------------------------------------------------------------
# Admin: draft / edit / publish. Sees every post regardless of status.
# ---------------------------------------------------------------------------

admin_router = APIRouter(prefix="/admin/blog", tags=["admin"])


@admin_router.get("/posts")
def admin_list_posts(admin: AdminPrincipal = RequireAdmin) -> dict:
    db = SessionLocal()
    try:
        posts = db.query(BlogPost).order_by(BlogPost.updated_at.desc()).all()
        return {"items": [_serialize(p) for p in posts]}
    finally:
        db.close()


@admin_router.post("/posts")
def admin_create_post(payload: BlogPostIn, admin: AdminPrincipal = RequireAdmin) -> dict:
    db = SessionLocal()
    try:
        slug = _unique_slug(db, _slugify(payload.slug or payload.title))
        post = BlogPost(
            id=new_id("blog"),
            slug=slug,
            title=payload.title,
            excerpt=payload.excerpt,
            body_markdown=payload.body_markdown,
            tags_json=json.dumps(payload.tags),
            author=payload.author,
            voice=payload.voice,
            status="draft",
            cover_image_url=payload.cover_image_url,
        )
        db.add(post)
        db.commit()
        db.refresh(post)
        return _serialize_detail(post)
    finally:
        db.close()


@admin_router.post("/posts/draft-from-notes")
def admin_draft_from_notes(
    payload: BlogDraftFromNotesIn, admin: AdminPrincipal = RequireAdmin,
) -> BlogDraftSuggestion:
    """Turn a pasted scratch note into structured draft fields for the editor
    to prefill. Doesn't save anything -- the admin reviews/edits the
    suggestion before hitting Save, same as any other form fill."""
    from app.services.agent import model_client
    from app.services.agent.prompt_library import resolve_prompt
    from app.services.agent.research_planner import _longform_timeout_s
    from app.services.agent.research_utils import _parse_json

    prompt = resolve_prompt(
        "agent.blog.draft_from_notes.default",
        agent_id="blog_draft",
        fallback_system_prompt=BLOG_DRAFT_PROMPT,
        variables=["notes"],
    )
    try:
        response = model_client.complete(
            [
                {"role": "system", "content": prompt.system_prompt},
                {"role": "user", "content": payload.notes},
            ],
            role="blog_draft",
            max_tokens=4000,
            timeout_s=_longform_timeout_s(),
        )
        data = _parse_json(response.text)
    except Exception as exc:
        logger.warning("blog draft-from-notes failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Could not generate a draft from these notes. Try again or write it manually.",
        ) from exc

    tags = [str(t).strip().lower() for t in (data.get("tags") or []) if str(t).strip()]
    return BlogDraftSuggestion(
        title=str(data.get("title", ""))[:255],
        excerpt=str(data.get("excerpt", ""))[:2000],
        tags=tags[:8],
        body_markdown=str(data.get("body_markdown", "")),
    )


@admin_router.get("/posts/by-slug/{slug}")
def admin_get_post_by_slug(slug: str, admin: AdminPrincipal = RequireAdmin) -> dict:
    db = SessionLocal()
    try:
        post = db.query(BlogPost).filter(BlogPost.slug == slug).first()
        if post is None:
            raise HTTPException(status_code=404, detail="Post not found.")
        return _serialize_detail(post)
    finally:
        db.close()


@admin_router.get("/posts/{post_id}")
def admin_get_post(post_id: str, admin: AdminPrincipal = RequireAdmin) -> dict:
    db = SessionLocal()
    try:
        post = db.get(BlogPost, post_id)
        if post is None:
            raise HTTPException(status_code=404, detail="Post not found.")
        return _serialize_detail(post)
    finally:
        db.close()


@admin_router.patch("/posts/{post_id}")
def admin_update_post(post_id: str, payload: BlogPostUpdate, admin: AdminPrincipal = RequireAdmin) -> dict:
    db = SessionLocal()
    try:
        post = db.get(BlogPost, post_id)
        if post is None:
            raise HTTPException(status_code=404, detail="Post not found.")
        data = payload.model_dump(exclude_unset=True)
        content_changed = any([
            "title" in data and data["title"] != post.title,
            "excerpt" in data and data["excerpt"] != post.excerpt,
            "tags" in data and data["tags"] != post.tags,
            "body_markdown" in data and data["body_markdown"] != post.body_markdown,
        ])
        if content_changed:
            _push_revision(post, label="Manual edit")
        if "slug" in data or "title" in data:
            base = _slugify(data.get("slug") or data.get("title") or post.slug)
            post.slug = _unique_slug(db, base, exclude_id=post.id)
            data.pop("slug", None)
        if "tags" in data:
            post.tags_json = json.dumps(data.pop("tags"))
        for key, value in data.items():
            setattr(post, key, value)
        db.commit()
        db.refresh(post)
        return _serialize_detail(post)
    finally:
        db.close()


@admin_router.delete("/posts/{post_id}")
def admin_delete_post(post_id: str, admin: AdminPrincipal = RequireAdmin) -> dict:
    db = SessionLocal()
    try:
        post = db.get(BlogPost, post_id)
        if post is None:
            raise HTTPException(status_code=404, detail="Post not found.")
        db.delete(post)
        db.commit()
        return {"deleted": True}
    finally:
        db.close()


@admin_router.post("/posts/{post_id}/publish")
def admin_publish_post(post_id: str, admin: AdminPrincipal = RequireAdmin) -> dict:
    db = SessionLocal()
    try:
        post = db.get(BlogPost, post_id)
        if post is None:
            raise HTTPException(status_code=404, detail="Post not found.")
        post.status = "published"
        if post.published_at is None:
            post.published_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(post)
        return _serialize_detail(post)
    finally:
        db.close()


@admin_router.post("/posts/{post_id}/unpublish")
def admin_unpublish_post(post_id: str, admin: AdminPrincipal = RequireAdmin) -> dict:
    db = SessionLocal()
    try:
        post = db.get(BlogPost, post_id)
        if post is None:
            raise HTTPException(status_code=404, detail="Post not found.")
        post.status = "draft"
        db.commit()
        db.refresh(post)
        return _serialize_detail(post)
    finally:
        db.close()


@admin_router.post("/posts/{post_id}/edit-with-instruction")
def admin_edit_with_instruction(
    post_id: str, payload: BlogEditInstructionIn, admin: AdminPrincipal = RequireAdmin,
) -> dict:
    """Apply an LLM edit to a draft under an explicit instruction -- title,
    excerpt, tags, and/or body, whichever the instruction concerns. Applies
    the change directly (not a propose-then-confirm flow) but always
    snapshots the pre-edit state first, so it's always undoable -- fully via
    /revisions/{id}/restore, or effectively partially by restoring an earlier
    point in the history and re-applying only the instructions you want."""
    from app.services.agent import model_client
    from app.services.agent.prompt_library import resolve_prompt
    from app.services.agent.research_planner import _longform_timeout_s
    from app.services.agent.research_utils import _parse_json

    db = SessionLocal()
    try:
        post = db.get(BlogPost, post_id)
        if post is None:
            raise HTTPException(status_code=404, detail="Post not found.")

        prompt = resolve_prompt(
            "agent.blog.edit_with_instruction.default",
            agent_id="blog_edit",
            fallback_system_prompt=BLOG_EDIT_PROMPT,
            variables=["instruction", "title", "excerpt", "tags", "body_markdown"],
        )
        try:
            response = model_client.complete(
                [
                    {"role": "system", "content": prompt.system_prompt},
                    {
                        "role": "user",
                        "content": json.dumps({
                            "instruction": payload.instruction,
                            "title": post.title,
                            "excerpt": post.excerpt,
                            "tags": post.tags,
                            "body_markdown": post.body_markdown,
                        }),
                    },
                ],
                role="blog_edit",
                max_tokens=4000,
                timeout_s=_longform_timeout_s(),
            )
            data = _parse_json(response.text)
            new_title = _clean_or_preserve(post.title, str(data.get("title", post.title))) or post.title
            new_excerpt = _clean_or_preserve(post.excerpt, str(data.get("excerpt", post.excerpt)))
            new_tags = [
                str(t).strip().lower() for t in (data.get("tags", post.tags) or []) if str(t).strip()
            ]
            new_body = _clean_or_preserve(post.body_markdown, str(data.get("body_markdown", post.body_markdown)))
            changes = [str(c).strip() for c in (data.get("changes") or []) if str(c).strip()]
            if not new_body.strip():
                raise ValueError("model returned an empty body_markdown")
        except Exception as exc:
            logger.warning("blog edit-with-instruction failed: %s", exc)
            raise HTTPException(
                status_code=502,
                detail="Could not apply that edit. Try rephrasing the instruction or edit manually.",
            ) from exc

        _push_revision(post, label=payload.instruction, changes=changes)
        post.title = new_title
        post.excerpt = new_excerpt
        post.tags_json = json.dumps(new_tags)
        post.body_markdown = new_body
        db.commit()
        db.refresh(post)
        return {**_serialize_detail(post), "changes": changes}
    finally:
        db.close()


@admin_router.post("/posts/{post_id}/revisions/{revision_id}/restore")
def admin_restore_revision(post_id: str, revision_id: str, admin: AdminPrincipal = RequireAdmin) -> dict:
    """Restore title/excerpt/tags/body to an earlier revision. Snapshots the
    current state first, so restoring is itself undoable (no dead ends).
    Fields absent from an older-shaped revision (pre-dating title/excerpt/
    tags snapshotting) are left as-is rather than cleared."""
    db = SessionLocal()
    try:
        post = db.get(BlogPost, post_id)
        if post is None:
            raise HTTPException(status_code=404, detail="Post not found.")
        target = next((r for r in post.revisions if r.get("id") == revision_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="Revision not found.")
        _push_revision(post, label="Before restoring an earlier version")
        if "title" in target:
            post.title = target["title"]
        if "excerpt" in target:
            post.excerpt = target["excerpt"]
        if "tags" in target:
            post.tags_json = json.dumps(target["tags"])
        post.body_markdown = target["body_markdown"]
        db.commit()
        db.refresh(post)
        return _serialize_detail(post)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Public: published posts only, no auth.
# ---------------------------------------------------------------------------

public_router = APIRouter(prefix="/blog", tags=["blog"])


@public_router.get("/posts")
def public_list_posts(
    tag: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    db = SessionLocal()
    try:
        posts = (
            db.query(BlogPost)
            .filter(BlogPost.status == "published")
            .order_by(BlogPost.published_at.desc())
            .all()
        )
        # Tag filtering happens in Python, not SQL -- tags_json isn't indexed
        # and a personal blog's post count never gets large enough for that
        # to matter.
        if tag:
            posts = [p for p in posts if tag in p.tags]
        total = len(posts)
        page = posts[offset : offset + limit]
        return {"items": [_serialize(p) for p in page], "total": total}
    finally:
        db.close()


@public_router.get("/posts/{slug}")
def public_get_post(slug: str) -> dict:
    db = SessionLocal()
    try:
        post = (
            db.query(BlogPost)
            .filter(BlogPost.slug == slug, BlogPost.status == "published")
            .first()
        )
        if post is None:
            raise HTTPException(status_code=404, detail="Post not found.")
        return _serialize(post)
    finally:
        db.close()
