from unittest.mock import AsyncMock, call

import pytest

from streamrip.client.tidal import TidalClient
from streamrip.config import Config
from streamrip.exceptions import NonStreamableError


@pytest.mark.parametrize("fetch_lyrics", [True, False])
async def test_track_lyrics_endpoint_and_opt_out(fetch_lyrics):
    config = Config.defaults()
    config.session.tidal.fetch_lyrics = fetch_lyrics
    client = TidalClient(config)
    client._api_request = AsyncMock(
        side_effect=[{"id": "123"}, {"subtitles": "synced lyrics"}]
    )

    metadata = await client.get_metadata("123", "track")

    assert metadata["lyrics"] == ("synced lyrics" if fetch_lyrics else "")
    expected_calls = [call("tracks/123")]
    if fetch_lyrics:
        expected_calls.append(call("tracks/123/lyrics", base="https://tidal.com/v1"))
    assert client._api_request.call_args_list == expected_calls


async def test_unavailable_lyrics_do_not_prevent_track_download():
    config = Config.defaults()
    config.session.tidal.fetch_lyrics = True
    client = TidalClient(config)
    client._api_request = AsyncMock(
        side_effect=[{"id": "123"}, NonStreamableError("No lyrics")]
    )

    assert await client.get_metadata("123", "track") == {"id": "123", "lyrics": ""}
