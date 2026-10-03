"""
Avatar handling.

Uploads land on local disk and are served by the app. That's fine for one
machine — for anything horizontally scaled, swap `store_avatar` for an S3 / R2
put and return the public URL. Nothing else needs to change.
"""

import asyncio
import uuid
from io import BytesIO
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from PIL import Image, UnidentifiedImageError

from app.core.config import settings

# Decoding is what makes this safe: a file that only claims to be an image
# fails here, so nothing unexpected ever reaches the disk.
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}
MAX_DIMENSION = 512
# Read from the header before decoding: a few kilobytes of PNG can claim a
# canvas that takes gigabytes to unpack
MAX_PIXELS = 40_000_000


def _avatar_dir() -> Path:
    path = Path(settings.UPLOAD_DIR) / "avatars"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def store_avatar(file: UploadFile, user_id: uuid.UUID) -> str:
    """Validates, normalises and writes the image. Returns its public URL."""
    # One byte past the limit is enough to know — never the whole upload in memory
    raw = await file.read(settings.MAX_AVATAR_BYTES + 1)

    if len(raw) > settings.MAX_AVATAR_BYTES:
        limit_mb = settings.MAX_AVATAR_BYTES // (1024 * 1024)
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Image must be smaller than {limit_mb} MB",
        )

    # Decoding and re-encoding is real work; off the event loop it holds up
    # nobody else's request
    encoded = await asyncio.to_thread(_encode, raw)

    # New filename each time so caches and CDNs pick the change up immediately
    filename = f"{user_id}-{uuid.uuid4().hex[:8]}.webp"
    destination = _avatar_dir() / filename

    remove_previous(user_id, keep=filename)
    destination.write_bytes(encoded)

    return f"{settings.UPLOAD_URL_PREFIX}/avatars/{filename}"


def _encode(raw: bytes) -> bytes:
    """The upload as a small WebP, or a 400 saying why it can't be one."""
    try:
        image = Image.open(BytesIO(raw))
        too_large = image.width * image.height > MAX_PIXELS
        if not too_large:
            image.verify()  # cheap structural check
            image = Image.open(BytesIO(raw))  # verify() exhausts the file object
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That file is not a readable image",
        ) from exc

    if too_large:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That image is too large — try a smaller photo.",
        )

    if image.format not in ALLOWED_FORMATS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported format. Use {', '.join(sorted(ALLOWED_FORMATS))}.",
        )

    image.draft("RGB", (MAX_DIMENSION, MAX_DIMENSION))
    try:
        # Flatten transparency onto white — WebP keeps alpha, but a stray
        # alpha channel on a dark avatar reads as a hole in the UI.
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            background = Image.new("RGBA", image.size, (255, 255, 255, 255))
            image = Image.alpha_composite(background, image).convert("RGB")
        else:
            image = image.convert("RGB")
    except (OSError, Image.DecompressionBombError) as exc:
        # The header read fine but the pixels behind it are broken
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That file is not a readable image",
        ) from exc

    image.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
    out = BytesIO()
    image.save(out, format="WEBP", quality=85, method=4)
    return out.getvalue()


def remove_previous(user_id: uuid.UUID, keep: str | None = None) -> None:
    """Old avatars are dead weight — the URL is only ever stored on the user."""
    for existing in _avatar_dir().glob(f"{user_id}-*.webp"):
        if existing.name != keep:
            existing.unlink(missing_ok=True)
