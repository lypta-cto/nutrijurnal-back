"""A meal said out loud: the recording is kept on the meal, played back on
request only, and bounded in size and kind."""

import pytest
from httpx import AsyncClient

from tests.helpers import PREFIX, auth_headers, meal

DAY = "2026-09-21"
MAX_VOICE_BYTES = 8 * 1024 * 1024


@pytest.fixture
async def me(client: AsyncClient) -> dict:
    return await auth_headers(client, email="voice@example.com")


@pytest.fixture
async def lunch(client: AsyncClient, me) -> dict:
    return await meal(client, me, DAY, title="Ručak", note="pola pileta i pirinač")


def _send(client: AsyncClient, headers: dict, meal_id: str, content: bytes, kind: str, **form):
    return client.post(
        f"{PREFIX}/eating/meals/{meal_id}/voice",
        files={"file": ("note", content, kind)},
        data=form,
        headers=headers,
    )


async def test_a_recording_is_kept_and_played_back_as_it_came(client: AsyncClient, me, lunch):
    sent = await _send(
        client, me, lunch["id"], b"\x1aE\xdf\xa3webm", "audio/webm;codecs=opus", seconds="12.44"
    )

    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert (body["has_voice"], body["voice_seconds"], body["voice_transcribed"]) == (
        True,
        12.4,
        False,
    )
    # The words already written stay where they were
    assert body["note"] == "pola pileta i pirinač"

    played = await client.get(f"{PREFIX}/eating/meals/{lunch['id']}/voice", headers=me)
    assert played.status_code == 200
    assert played.content == b"\x1aE\xdf\xa3webm"
    assert played.headers["content-type"] == "audio/webm"
    assert played.headers["cache-control"] == "private, max-age=3600"


@pytest.mark.parametrize(
    "kind",
    ["audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/wav", "audio/aac", "AUDIO/MP4"],
)
async def test_every_kind_a_phone_records_is_accepted(client: AsyncClient, me, lunch, kind):
    response = await _send(client, me, lunch["id"], b"sound", kind)

    assert response.status_code == 200, kind
    played = await client.get(f"{PREFIX}/eating/meals/{lunch['id']}/voice", headers=me)
    assert played.headers["content-type"] == kind.lower()


async def test_a_second_recording_replaces_the_first(client: AsyncClient, me, lunch):
    await _send(client, me, lunch["id"], b"first", "audio/ogg", seconds="3", transcribed="true")
    second = await _send(client, me, lunch["id"], b"second", "audio/mp4")

    assert second.json()["voice_seconds"] is None
    assert second.json()["voice_transcribed"] is False
    played = await client.get(f"{PREFIX}/eating/meals/{lunch['id']}/voice", headers=me)
    assert (played.content, played.headers["content-type"]) == (b"second", "audio/mp4")


async def test_a_recording_is_bounded_in_size_and_kind(client: AsyncClient, me, lunch):
    empty = await _send(client, me, lunch["id"], b"", "audio/webm")
    assert empty.status_code == 400 and empty.json()["detail"] == "Empty recording"

    too_long = await _send(client, me, lunch["id"], b"\0" * (MAX_VOICE_BYTES + 1), "audio/webm")
    assert too_long.status_code == 413
    assert too_long.json()["detail"] == "That recording is too long — keep it under two minutes."

    for kind in ("text/plain", "video/webm", "application/octet-stream", "image/png"):
        refused = await _send(client, me, lunch["id"], b"bytes", kind)
        assert refused.status_code == 415, kind
        assert refused.json()["detail"].startswith("Not an audio recording")

    missing = await client.post(f"{PREFIX}/eating/meals/{lunch['id']}/voice", headers=me)
    assert missing.status_code == 422

    # None of that left a recording behind
    played = await client.get(f"{PREFIX}/eating/meals/{lunch['id']}/voice", headers=me)
    assert played.status_code == 404 and played.json()["detail"] == "No recording"

    # Right at the limit is still a recording
    at_limit = await _send(client, me, lunch["id"], b"\0" * MAX_VOICE_BYTES, "audio/webm")
    assert at_limit.status_code == 200


async def test_dropping_the_recording_keeps_the_words(client: AsyncClient, me, lunch):
    await _send(client, me, lunch["id"], b"sound", "audio/ogg", seconds="4.2", transcribed="true")

    dropped = await client.delete(f"{PREFIX}/eating/meals/{lunch['id']}/voice", headers=me)

    assert dropped.status_code == 200
    body = dropped.json()
    assert (body["has_voice"], body["voice_seconds"], body["voice_transcribed"]) == (
        False,
        None,
        False,
    )
    assert body["note"] == "pola pileta i pirinač"
    assert (
        await client.get(f"{PREFIX}/eating/meals/{lunch['id']}/voice", headers=me)
    ).status_code == 404
    # Dropping nothing is not an error
    again = await client.delete(f"{PREFIX}/eating/meals/{lunch['id']}/voice", headers=me)
    assert again.status_code == 200


async def test_the_diary_says_a_recording_exists_without_carrying_it(
    client: AsyncClient, me, lunch
):
    await _send(client, me, lunch["id"], b"x" * 4096, "audio/ogg", seconds="9")

    day = (await client.get(f"{PREFIX}/eating/days/{DAY}", headers=me)).json()

    [listed] = day["meals"]
    assert (listed["has_voice"], listed["voice_seconds"]) == (True, 9)
    assert "voice" not in listed


async def test_a_deleted_meal_takes_its_recording_with_it(client: AsyncClient, me, lunch):
    await _send(client, me, lunch["id"], b"sound", "audio/ogg")

    await client.delete(f"{PREFIX}/eating/meals/{lunch['id']}", headers=me)

    for request in (
        client.get(f"{PREFIX}/eating/meals/{lunch['id']}/voice", headers=me),
        _send(client, me, lunch["id"], b"sound", "audio/ogg"),
        client.delete(f"{PREFIX}/eating/meals/{lunch['id']}/voice", headers=me),
    ):
        assert (await request).status_code == 404


async def test_a_recording_needs_a_signed_in_owner(client: AsyncClient, lunch):
    path = f"{PREFIX}/eating/meals/{lunch['id']}/voice"

    assert (await client.get(path)).status_code == 401
    assert (await client.delete(path)).status_code == 401
    assert (
        await client.post(path, files={"file": ("n", b"sound", "audio/ogg")})
    ).status_code == 401


async def test_a_recordings_length_is_a_real_length(client: AsyncClient, me, lunch):
    for seconds in ("-5", "86400"):
        refused = await _send(client, me, lunch["id"], b"sound", "audio/ogg", seconds=seconds)
        assert refused.status_code == 422, seconds
