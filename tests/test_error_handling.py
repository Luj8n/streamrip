import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from streamrip.media.album import Album
from streamrip.media.playlist import Playlist


class TestErrorHandling:
    """Test error handling in playlist and album downloads."""

    @pytest.mark.asyncio
    async def test_playlist_handles_failed_track(self):
        """Test that a playlist download continues even if one track fails."""
        mock_config = MagicMock()
        mock_client = MagicMock()

        mock_track_success = MagicMock()
        mock_track_success.resolve = AsyncMock(return_value=MagicMock())
        mock_track_success.resolve.return_value.rip = AsyncMock()

        mock_track_failure = MagicMock()
        mock_track_failure.resolve = AsyncMock(
            side_effect=json.JSONDecodeError("Expecting value", "", 0)
        )

        playlist = Playlist(
            name="Test Playlist",
            config=mock_config,
            client=mock_client,
            tracks=[mock_track_success, mock_track_failure],
        )

        await playlist.download()

        mock_track_success.resolve.assert_called_once()
        mock_track_success.resolve.return_value.rip.assert_called_once()
        mock_track_failure.resolve.assert_called_once()

    @pytest.mark.asyncio
    async def test_album_download_is_noop(self):
        """Test that Album.download() is now a no-op (tracks handled by Main queue)."""
        mock_config = MagicMock()
        mock_db = MagicMock()
        mock_meta = MagicMock()

        # Create mock tracks
        mock_track_success = MagicMock()
        mock_track_failure = MagicMock()

        album = Album(
            meta=mock_meta,
            config=mock_config,
            tracks=[mock_track_success, mock_track_failure],
            folder="/test/folder",
            db=mock_db,
        )

        # Album.download() should now be a no-op
        await album.download()

        # Tracks should NOT be processed - they're handled by Main's queue system
        mock_track_success.resolve.assert_not_called()
        mock_track_failure.resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_main_stream_process_pending_handles_failed_pending(self):
        """Test that Main.stream_process_pending handles failed pending items."""
        from streamrip.rip.main import Main

        mock_config = MagicMock()
        mock_config.session.downloads.requests_per_minute = 0
        mock_config.session.downloads.max_connections = 1  # For worker pool
        mock_config.session.database.downloads_enabled = False
        mock_config.session.database.failed_downloads_enabled = False

        with (
            patch("streamrip.rip.main.QobuzClient"),
            patch("streamrip.rip.main.TidalClient"),
            patch("streamrip.rip.main.DeezerClient"),
            patch("streamrip.rip.main.SoundcloudClient"),
        ):
            main = Main(mock_config)

            # Create mock pending items that resolve to media
            mock_pending_success = MagicMock()
            mock_media_success = MagicMock(spec=['rip'])  # Only spec 'rip', not 'tracks'
            mock_media_success.rip = AsyncMock()
            mock_pending_success.resolve = AsyncMock(return_value=mock_media_success)

            mock_pending_failure = MagicMock()
            mock_pending_failure.resolve = AsyncMock(
                side_effect=Exception("Resolve failed")
            )

            # Add pending items (not media) - this is the new architecture
            main.pending = [mock_pending_success, mock_pending_failure]

            from click import ClickException

            with pytest.raises(ClickException, match="1 item"):
                await main.stream_process_pending()

            # Verify the successful item was processed
            mock_pending_success.resolve.assert_called_once()
            mock_media_success.rip.assert_called_once()

            # Verify the failed item was attempted
            mock_pending_failure.resolve.assert_called_once()


@pytest.mark.parametrize('failure', ['connection', 'http'])
async def test_failed_transfer_preserves_existing_file(tmp_path, failure):
    import aiohttp
    from streamrip.client.downloadable import fast_async_download

    path = tmp_path / 'audio.flac'
    path.write_bytes(b'existing audio')
    session = MagicMock()
    response = MagicMock()
    response.__aenter__.return_value = response
    response.raise_for_status.side_effect = aiohttp.ClientError('503')
    session.get.return_value = response
    if failure == 'connection':
        session.get.side_effect = aiohttp.ClientConnectionError('offline')
    with pytest.raises(aiohttp.ClientError):
        await fast_async_download(str(path), 'https://example.org/audio', session, lambda _: None)
    assert path.read_bytes() == b'existing audio'
    assert list(tmp_path.iterdir()) == [path]


async def test_transfer_accepts_more_than_100_response_headers(tmp_path):
    import asyncio
    import aiohttp
    from streamrip.client.downloadable import fast_async_download

    async def respond(reader, writer):
        await reader.readuntil(b'\r\n\r\n')
        headers = ''.join(f'X-Test-{i}: value\r\n' for i in range(150))
        writer.write(f'HTTP/1.1 200 OK\r\n{headers}Content-Length: 3\r\nConnection: close\r\n\r\nabc'.encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(respond, '127.0.0.1', 0)
    async with server, aiohttp.ClientSession() as session:
        port = server.sockets[0].getsockname()[1]
        path = tmp_path / 'audio.flac'
        await fast_async_download(str(path), f'http://127.0.0.1:{port}/audio', session, lambda _: None)
        assert path.read_bytes() == b'abc'


@pytest.mark.parametrize('tag_fails', [True, False])
async def test_track_is_published_only_after_postprocessing(tmp_path, tag_fails):
    from streamrip.config import Config
    from streamrip.media.track import Track

    cfg = Config.defaults()
    cfg.session.downloads.validate_audio = False
    cfg.session.conversion.enabled = False
    cfg.session.cli.progress_bars = False
    cfg.session.filepaths.track_format = '{title}'
    meta = MagicMock()
    meta.format_track_path.return_value = 'track'
    meta.info.id = '123'
    downloadable = MagicMock(source='tidal', extension='flac')
    downloadable.size = AsyncMock(return_value=3)

    async def download(path, callback):
        from pathlib import Path
        Path(path).write_bytes(b'new')

    downloadable.download = download
    db = MagicMock()
    track = Track(meta, downloadable, cfg, str(tmp_path), None, db)
    target = tmp_path / 'track.flac'
    target.write_bytes(b'old')
    with patch('streamrip.media.track.tag_file', new_callable=AsyncMock) as tag:
        if tag_fails:
            tag.side_effect = RuntimeError('tag failure')
            with pytest.raises(RuntimeError, match='tag failure'):
                await track.rip()
            db.set_downloaded.assert_not_called()
        else:
            await track.rip()
            db.set_downloaded.assert_called_once_with('123', 'tidal')
    assert target.read_bytes() == (b'old' if tag_fails else b'new')
    assert list(tmp_path.iterdir()) == [target]


async def test_worker_records_terminal_failure_and_exits(tmp_path):
    import asyncio

    from click import ClickException

    from streamrip.config import Config
    from streamrip.media.track import PendingSingle
    from streamrip.rip.main import Main

    cfg = Config.defaults()
    cfg.session.downloads.max_connections = 1
    cfg.session.database.downloads_path = str(tmp_path / 'downloads.db')
    cfg.session.database.failed_downloads_path = str(tmp_path / 'failed.db')
    main = Main(cfg)
    main.pending = [PendingSingle('123', MagicMock(source='tidal'), cfg, main.database)]
    with patch.object(PendingSingle, 'resolve', new_callable=AsyncMock) as resolve, patch('streamrip.rip.main.asyncio.sleep', new_callable=AsyncMock):
        resolve.side_effect = RuntimeError('transfer failed')
        with pytest.raises(ClickException, match='1 item'):
            await asyncio.wait_for(main.stream_process_pending(), timeout=2)
    assert resolve.await_count == 4
    assert main.database.get_failed_downloads() == [('tidal', 'track', '123')]
    assert main.download_queue.empty()
    assert not main.worker_tasks


async def test_playlist_label_and_artist_share_queue(tmp_path):
    from streamrip.config import Config
    from streamrip.media import (
        PendingAlbum,
        PendingArtist,
        PendingLabel,
        PendingPlaylist,
    )
    from streamrip.media.label import Label
    from streamrip.rip.main import Main

    cfg = Config.defaults()
    cfg.session.database.downloads_enabled = False
    cfg.session.database.failed_downloads_enabled = False
    cfg.session.cli.dry_run = True
    main = Main(cfg)
    client = MagicMock(source='tidal')
    meta = MagicMock(album='Album')
    track = MagicMock(spec=['rip'])
    track.rip = AsyncMock()
    album = Album(meta, [track], cfg, str(tmp_path), main.database)
    playlist = Playlist('Playlist', cfg, client, [track])
    pending_album = PendingAlbum('1', client, cfg, main.database)
    label = Label('Label', [pending_album], client, cfg, '2', main.database)
    main.pending = [PendingPlaylist('3', client, cfg, main.database), PendingLabel('2', client, cfg, main.database), PendingArtist('4', client, cfg, main.database)]

    async def albums(_):
        yield album

    with patch.object(PendingAlbum, 'resolve', AsyncMock(return_value=album)), patch.object(PendingPlaylist, 'resolve', AsyncMock(return_value=playlist)), patch.object(PendingLabel, 'resolve', AsyncMock(return_value=label)), patch.object(PendingArtist, 'stream_albums', albums), patch.object(Album, 'preprocess', AsyncMock()), patch.object(Album, 'postprocess', AsyncMock()) as finalize:
        await main.stream_process_pending()
    assert track.rip.await_count == 3
    assert finalize.await_count == 2
    assert not main.collections


async def test_concurrent_conversion_keeps_same_named_tracks_separate(tmp_path):
    import asyncio
    import shutil
    import subprocess

    from mutagen import File

    from streamrip.converter import FLAC

    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg unavailable')
    inputs = []
    for duration in (1, 2):
        folder = tmp_path / str(duration)
        folder.mkdir()
        source = folder / 'same-name.wav'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'sine=duration={duration}', str(source)], check=True)
        inputs.append(source)
    await asyncio.gather(*(FLAC(str(path), remove_source=True).convert() for path in inputs))
    for duration, source in enumerate(inputs, 1):
        assert not source.exists()
        assert File(source.with_suffix('.flac')).info.length == pytest.approx(duration)
    assert not list(tmp_path.rglob('.streamrip-convert-*'))


def test_long_unicode_filename_keeps_extension_and_metadata(tmp_path):
    from streamrip.config import Config
    from streamrip.media.track import Track
    from streamrip.metadata import AlbumInfo, AlbumMetadata, Covers, TrackInfo, TrackMetadata

    cfg = Config.defaults()
    cfg.session.filepaths.track_format = '{discnumber:02}-{tracknumber:02}. {title}'
    cfg.session.filepaths.truncate_to = 0
    title = '名字 / Café: ' + '語' * 150
    album = AlbumMetadata(AlbumInfo('1', 2, 'FLAC'), 'Album', 'Artist', '2024', [], Covers(), 1)
    meta = TrackMetadata(TrackInfo('2', 2), title, album, 'Artist', 1, 2, None, None)
    track = Track(meta, MagicMock(extension='flac'), cfg, str(tmp_path), None, MagicMock())
    track._set_download_path()
    from pathlib import Path
    path = Path(track.download_path)
    assert len(path.name.encode()) <= 255
    assert path.suffix == '.flac'
    assert path.name.startswith('02-01.')
    assert meta.title == title
    path.touch()


@pytest.mark.parametrize('playlist_tags', [False, True])
async def test_playlist_metadata_settings_are_explicit(tmp_path, playlist_tags):
    import json
    from streamrip.config import Config
    from streamrip.db import Database, Dummy
    from streamrip.media.playlist import PendingPlaylistTrack

    response = json.load(open('tests/qobuz_track_resp.json'))
    cfg = Config.defaults()
    cfg.session.metadata.renumber_playlist_tracks = playlist_tags
    cfg.session.metadata.set_playlist_to_album = playlist_tags
    cfg.session.qobuz.lower_quality_if_not_available = True
    client = MagicMock(source='qobuz')
    client.get_metadata = AsyncMock(return_value=response)
    client.get_downloadable = AsyncMock(return_value=MagicMock(extension='flac'))
    pending = PendingPlaylistTrack('123', client, cfg, str(tmp_path), 'Playlist', 20, Database(Dummy(), Dummy(), Dummy()), playlist_total=30)
    with patch.object(PendingPlaylistTrack, '_download_cover', AsyncMock(return_value=None)):
        track = await pending.resolve()
    if playlist_tags:
        assert track.meta.album.album == 'Playlist'
        assert track.meta.tracknumber == 20
        assert track.meta.album.tracktotal == 30
        assert track.meta.discnumber == track.meta.album.disctotal == 1
    else:
        assert track.meta.album.album.startswith(response['album']['title'])
        assert track.meta.tracknumber == response['track_number']
        assert track.meta.album.tracktotal == response['album']['tracks_count']


async def test_incomplete_http_body_preserves_existing_file(tmp_path):
    import asyncio
    import aiohttp
    from streamrip.client.downloadable import fast_async_download

    async def respond(reader, writer):
        await reader.readuntil(b'\r\n\r\n')
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 20000\r\nConnection: close\r\n\r\npartial')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(respond, '127.0.0.1', 0)
    path = tmp_path / 'audio.flac'
    path.write_bytes(b'previous successful download')
    async with server, aiohttp.ClientSession() as session:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(aiohttp.ClientPayloadError):
            await fast_async_download(str(path), f'http://127.0.0.1:{port}/audio', session, lambda _: None)
    assert path.read_bytes() == b'previous successful download'
    assert list(tmp_path.iterdir()) == [path]
