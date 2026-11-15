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
        side_effect=[{"id": "123"}, {"items": []}, {"subtitles": "synced lyrics"}]
    )

    metadata = await client.get_metadata("123", "track")

    assert metadata["lyrics"] == ("synced lyrics" if fetch_lyrics else "")
    expected_calls = [call("tracks/123"), call("tracks/123/contributors")]
    if fetch_lyrics:
        expected_calls.append(call("tracks/123/lyrics", base="https://tidal.com/v1"))
    assert client._api_request.call_args_list == expected_calls


async def test_unavailable_lyrics_do_not_prevent_track_download():
    config = Config.defaults()
    config.session.tidal.fetch_lyrics = True
    client = TidalClient(config)
    client._api_request = AsyncMock(
        side_effect=[{"id": "123"}, {"items": []}, NonStreamableError("No lyrics")]
    )

    assert await client.get_metadata("123", "track") == {"id": "123", "contributors": [], "lyrics": ""}


async def test_search_keeps_one_result():
    client = TidalClient(Config.defaults())
    client._api_request = AsyncMock(return_value={'items': [{'id': '123'}]})
    assert await client.search('track', 'one result', limit=1) == [{'items': [{'id': '123'}]}]


@pytest.mark.parametrize('fallback', [True, False])
async def test_actual_quality_is_reported_or_rejected(fallback, caplog):
    import base64
    import json

    cfg = Config.defaults()
    cfg.session.tidal.lower_quality_if_not_available = fallback
    client = TidalClient(cfg)
    client.session = None
    manifest = base64.b64encode(json.dumps({'urls': ['https://example.org/audio'], 'codecs': 'mp4a.40.2'}).encode()).decode()
    client._api_request = AsyncMock(return_value={'manifest': manifest, 'audioQuality': 'HIGH'})
    if fallback:
        downloadable = await client.get_downloadable('123', 3)
        assert downloadable.quality == 1
        assert downloadable.extension == 'm4a'
        assert 'HIGH instead of HI_RES_LOSSLESS' in caplog.text
    else:
        with pytest.raises(NonStreamableError, match='HIGH instead of HI_RES_LOSSLESS'):
            await client.get_downloadable('123', 3)


async def test_refresh_persists_rotated_credentials(tmp_path):
    import shutil

    from streamrip.config import BLANK_CONFIG_PATH

    path = tmp_path / 'config.toml'
    shutil.copyfile(BLANK_CONFIG_PATH, path)
    cfg = Config(str(path))
    client = TidalClient(cfg)
    client.refresh_token = 'old'
    client._api_post = AsyncMock(return_value={'access_token': 'new-access', 'refresh_token': 'new-refresh', 'expires_in': 3600})
    client._update_authorization_from_config = lambda: None
    await client._refresh_access_token()
    saved = Config(str(path)).file.tidal
    assert saved.access_token == 'new-access'
    assert saved.refresh_token == client.refresh_token == 'new-refresh'
    assert saved.token_expiry == cfg.session.tidal.token_expiry


async def test_dash_chooses_highest_bandwidth_and_includes_initialization():
    import base64

    client = TidalClient(Config.defaults())
    xml = '''<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period><AdaptationSet>
    <Representation bandwidth="100" codecs="flac"><SegmentTemplate initialization="low-init" media="low-$Number$" startNumber="1"><SegmentTimeline><S d="1" r="1"/></SegmentTimeline></SegmentTemplate></Representation>
    <Representation bandwidth="200" codecs="flac"><SegmentTemplate initialization="high-init" media="high-$Number$" startNumber="1"><SegmentTimeline><S d="1" r="1"/></SegmentTimeline></SegmentTemplate></Representation>
    </AdaptationSet></Period></MPD>'''
    manifest = await client._parse_dash_manifest(base64.b64encode(xml.encode()).decode())
    assert manifest['urls'] == ['high-init', 'high-1', 'high-2']
