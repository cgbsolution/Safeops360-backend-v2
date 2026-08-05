"""Supabase Storage helper. Direct port of `src/lib/storage/supabase-storage.ts`.

Uses the supabase-py service-role client. Browser clients never touch this —
they PUT directly to short-lived signed URLs minted here. Service-role key
must NEVER be exposed to the browser.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from supabase import Client, create_client

from app.core.config import get_settings

settings = get_settings()

_client: Client | None = None


def is_storage_configured() -> bool:
    return bool(settings.supabase_url and settings.supabase_service_role_key)


def _get_client() -> Client:
    global _client
    if not is_storage_configured():
        raise RuntimeError(
            "Supabase Storage is not configured. Set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY in .env."
        )
    if _client is None:
        _client = create_client(settings.supabase_url, settings.supabase_service_role_key)  # type: ignore[arg-type]
    return _client


def build_storage_path(*, incident_id: str, category: str, file_name: str) -> str:
    safe = re.sub(r"[\\/]", "_", file_name)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", safe)[:80]
    short_id = secrets.token_hex(4)
    return f"incidents/{incident_id}/{category.lower()}/{short_id}-{safe}"


def build_risk_storage_path(*, risk_id: str, category: str, file_name: str) -> str:
    safe = re.sub(r"[\\/]", "_", file_name)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", safe)[:80]
    short_id = secrets.token_hex(4)
    return f"risks/{risk_id}/{category.lower()}/{short_id}-{safe}"


def build_control_storage_path(*, control_id: str, category: str, file_name: str) -> str:
    safe = re.sub(r"[\\/]", "_", file_name)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", safe)[:80]
    short_id = secrets.token_hex(4)
    return f"controls/{control_id}/{category.lower()}/{short_id}-{safe}"


def build_permit_storage_path(*, permit_id: str, category: str, file_name: str) -> str:
    """PTW closed-loop attachments (drawings, action-evidence photos,
    return/verification photos)."""
    safe = re.sub(r"[\\/]", "_", file_name)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", safe)[:80]
    short_id = secrets.token_hex(4)
    return f"permits/{permit_id}/{category.lower()}/{short_id}-{safe}"


def build_moc_storage_path(*, moc_id: str, category: str, file_name: str) -> str:
    """MOC supporting documents (drawings, P&IDs, vendor specs, risk docs)."""
    safe = re.sub(r"[\\/]", "_", file_name)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", safe)[:80]
    short_id = secrets.token_hex(4)
    return f"moc/{moc_id}/{category.lower()}/{short_id}-{safe}"


def build_evidence_storage_path(
    *, entity_type: str, entity_id: str, category: str, file_name: str
) -> str:
    """Generic path for the shared Evidence Attachment layer (Stream B §5).
    One builder for every attachable entity, replacing the per-parent
    build_*_storage_path clones for new surfaces:
        evidence/{entityType}/{entityId}/{category}/{shortid}-{safe}
    """
    safe_type = re.sub(r"[^A-Za-z0-9_-]", "_", entity_type)[:40]
    safe = re.sub(r"[\\/]", "_", file_name)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", safe)[:80]
    short_id = secrets.token_hex(4)
    return f"evidence/{safe_type}/{entity_id}/{category.lower()}/{short_id}-{safe}"


def create_signed_upload_url(storage_path: str) -> dict[str, str]:
    """60-second window for the browser to PUT directly. Defensive against
    different supabase-py versions that have used `signed_url` (snake_case)
    vs `signedURL` (camelCase) for the response key."""
    client = _get_client()
    try:
        res = client.storage.from_(settings.supabase_incident_bucket).create_signed_upload_url(storage_path)
    except Exception as e:
        # Surface bucket misconfiguration clearly. Common: bucket doesn't
        # exist, wrong key, or RLS denies the path.
        raise RuntimeError(
            f"Supabase signed-upload call failed for bucket "
            f"'{settings.supabase_incident_bucket}', path '{storage_path}': {e}"
        ) from e
    if not res:
        raise RuntimeError("Supabase returned an empty response for createSignedUploadUrl")
    url = (
        res.get("signed_url")
        or res.get("signedURL")
        or res.get("signedUrl")
    )
    if not url:
        raise RuntimeError(
            f"Supabase response missing signed-url field. Got keys: {list(res.keys())}; "
            f"full response: {res}"
        )
    return {"uploadUrl": url, "token": res.get("token", "")}


def create_signed_download_url(
    storage_path: str, expires_in_sec: int = 300, download: str | None = None
) -> str:
    client = _get_client()
    options: dict[str, Any] = {}
    if download:
        options["download"] = download
    res = client.storage.from_(settings.supabase_incident_bucket).create_signed_url(
        storage_path, expires_in_sec, options=options or None
    )
    url = (res or {}).get("signed_url") or (res or {}).get("signedURL")
    if not url:
        raise RuntimeError(f"createSignedDownloadUrl failed: {res}")
    return url


def delete_storage_object(storage_path: str) -> None:
    client = _get_client()
    res = client.storage.from_(settings.supabase_incident_bucket).remove([storage_path])
    if isinstance(res, dict) and res.get("error"):
        raise RuntimeError(f"deleteStorageObject failed: {res['error']}")


def download_object(storage_path: str) -> bytes:
    """Server-side read (transcription pipeline needs the raw audio bytes)."""
    client = _get_client()
    try:
        return client.storage.from_(settings.supabase_incident_bucket).download(storage_path)
    except Exception as e:
        raise RuntimeError(f"Supabase download failed for path '{storage_path}': {e}") from e


def upload_object(storage_path: str, data: bytes, content_type: str) -> None:
    """Server-side upload (chunked-upload assembly path — Guided Field Capture).
    The browser normally PUTs to a signed URL; here the backend has already
    assembled the bytes, so it pushes directly with the service-role client.
    file_options values must be strings (supabase-py quirk)."""
    client = _get_client()
    try:
        client.storage.from_(settings.supabase_incident_bucket).upload(
            storage_path, data, {"content-type": content_type, "upsert": "true"}
        )
    except Exception as e:
        raise RuntimeError(
            f"Supabase upload failed for bucket '{settings.supabase_incident_bucket}', "
            f"path '{storage_path}': {e}"
        ) from e
