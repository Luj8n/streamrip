import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock

from streamrip.client.downloadable import SoundcloudDownloadable


async def test_segments_are_concatenated_in_playlist_order(monkeypatch, tmp_path):
    response = MagicMock()
    response.text = AsyncMock(
        return_value="#EXTM3U\n#EXTINF:1,\nhttps://example.com/0.mp3\n"
        "#EXTINF:1,\nhttps://example.com/1.mp3\n#EXT-X-ENDLIST\n"
    )
    session = MagicMock()
    session.get.return_value.__aenter__.return_value = response
    downloadable = SoundcloudDownloadable(
        session, {"type": "mp3", "url": "https://example.com/playlist.m3u8"}
    )
    second_finished = asyncio.Event()
    completion_order = []

    async def download_segment(index, uri):
        if index == 0:
            await second_finished.wait()
        else:
            second_finished.set()
        completion_order.append(index)
        return index, str(tmp_path / f"{index}.mp3")

    monkeypatch.setattr(downloadable, "_download_segment", download_segment)
    concatenate = AsyncMock()
    monkeypatch.setattr("streamrip.client.downloadable.concat_audio_files", concatenate)
    progress = Mock()
    output = str(tmp_path / "track.mp3")

    await downloadable.download(output, progress)

    assert completion_order == [1, 0]
    concatenate.assert_awaited_once_with(
        [str(tmp_path / "0.mp3"), str(tmp_path / "1.mp3")], output, "mp3"
    )
    assert progress.call_count == 2
