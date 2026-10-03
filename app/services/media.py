"""
Avatar handling.

Uploads land on local disk and are served by the app. That's fine for one
machine — for anything horizontally scaled, swap `store_avatar` for an S3 / R2
put and return the public URL. Nothing else needs to change.
"""

import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from PIL import Image, UnidentifiedImageError

from app.core.config import settings

# Decoding is what makes this safe: a file that only claims to be an image
# fails here, so nothing unexpected ever reaches the disk.
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}
MAX_DIMENSION = 512


def _avatar_dir() -> Path:
    path = Path(settings.UPLOAD_DIR) / "avatars"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def store_avatar(file: UploadFile, user_id: uuid.UUID) -> str:
    """Validates, normalises and writes the image. Returns its public URL."""
    raw = await file.read()

    if len(raw) > settings.MAX_AVATAR_BYTES:
        limit_mb = settings.MAX_AVATAR_BYTES // (1024 * 1024)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Image must be smaller than {limit_mb} MB",
        )

    from io import BytesIO

    try:
        image = Image.open(BytesIO(raw))
        image.verify()  # cheap structural check
        image = Image.open(BytesIO(raw))  # verify() exhausts the file object
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That file is not a readable image",
        ) from exc

    if image.format not in ALLOWED_FORMATS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported format. Use {', '.join(sorted(ALLOWED_FORMATS))}.",
        )

    # Flatten transparency onto white — WebP keeps alpha, but a stray alpha
    # channel on a dark avatar reads as a hole in the UI.
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        background = Image.new("RGBA", image.size, (255, 255, 255, 255))
        image = Image.alpha_composite(background, image).convert("RGB")
    else:
        image = image.convert("RGB")

    image.thumbnail((MAX_DIMENSION, MAX_DIMENSION))

    # New filename each time so caches and CDNs pick the change up immediately
    filename = f"{user_id}-{uuid.uuid4().hex[:8]}.webp"
    destination = _avatar_dir() / filename

    remove_previous(user_id, keep=filename)
    image.save(destination, format="WEBP", quality=85, method=4)

    return f"{settings.UPLOAD_URL_PREFIX}/avatars/{filename}"


def remove_previous(user_id: uuid.UUID, keep: str | None = None) -> None:
    """Old avatars are dead weight — the URL is only ever stored on the user."""
    for existing in _avatar_dir().glob(f"{user_id}-*.webp"):
        if existing.name != keep:
            existing.unlink(missing_ok=True)
