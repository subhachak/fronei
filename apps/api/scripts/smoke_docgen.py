"""
Usage: uv run --extra dev python scripts/smoke_docgen.py --base-url http://localhost:8000 --auth-token <token> --workspace-id <id> --conversation-id <id>

Sends a docx turn, polls until complete, and verifies:
  1. Turn completes without error.
  2. At least one artifact row is surfaced for that turn.
  3. The artifact download endpoint returns 200.
"""
from __future__ import annotations

import argparse
import sys
import time
from urllib.parse import urljoin

import httpx


def _path(api_prefix: str, endpoint: str) -> str:
    prefix = api_prefix.strip("/")
    suffix = endpoint if endpoint.startswith("/") else f"/{endpoint}"
    if not prefix:
        return suffix
    return f"/{prefix}{suffix}"


def _artifact_id(artifact: dict) -> str | None:
    value = artifact.get("id") or artifact.get("artifact_id")
    return str(value) if value else None


def main(
    base_url: str,
    token: str,
    workspace_id: str,
    conversation_id: str,
    api_prefix: str,
) -> None:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    client = httpx.Client(base_url=base_url, headers=headers, timeout=120)

    try:
        resp = client.post(
            _path(api_prefix, "/turns"),
            json={
                "message": "Write a one-paragraph executive summary of AI in 2025.",
                "output_format": "docx",
                "quality_mode": "draft",
                "workspace_id": workspace_id,
                "conversation_id": conversation_id,
            },
        )
        resp.raise_for_status()
        turn_id = resp.json().get("turn_id")
        if not turn_id:
            print("FAIL: no turn_id in response")
            sys.exit(1)
        print(f"Turn started: {turn_id}")

        for attempt in range(60):
            time.sleep(3)
            status_resp = client.get(_path(api_prefix, f"/turns/{turn_id}/status"))
            status_resp.raise_for_status()
            payload = status_resp.json()
            status = payload.get("status")
            print(f"  [{attempt + 1}] status={status}")
            if status == "completed":
                turn = payload.get("turn", {})
                artifacts = turn.get("artifacts") or []
                if not artifacts:
                    print("FAIL: turn completed but no artifacts in response")
                    sys.exit(1)
                print(f"  Artifacts: {[artifact.get('filename') for artifact in artifacts]}")

                artifact_id = _artifact_id(artifacts[0])
                if not artifact_id:
                    print("FAIL: first artifact has no id")
                    sys.exit(1)
                download_path = _path(api_prefix, f"/artifacts/{artifact_id}/download")
                download_url = urljoin(str(client.base_url), download_path.lstrip("/"))
                dl = client.get(download_url, follow_redirects=True)
                if dl.status_code != 200:
                    print(f"FAIL: download returned {dl.status_code}")
                    sys.exit(1)
                print(f"  Download: {dl.status_code} ({len(dl.content)} bytes)")
                print("\nM1 doc-gen gate: PASSED")
                return
            if status in ("failed", "cancelled"):
                print(f"FAIL: turn ended with status={status}")
                err = payload.get("turn", {}).get("answer") or payload.get("error_message") or ""
                if err:
                    print(f"  Error: {str(err)[:300]}")
                sys.exit(1)

        print("FAIL: turn did not complete within 180s")
        sys.exit(1)
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--auth-token", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument(
        "--api-prefix",
        default="",
        help="Optional API path prefix, for deployments that mount agent routes under /api.",
    )
    args = parser.parse_args()
    main(args.base_url, args.auth_token, args.workspace_id, args.conversation_id, args.api_prefix)
