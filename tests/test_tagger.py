import os
import shutil

import pytest
from mutagen.flac import FLAC
from util import arun

from streamrip.metadata import (
    AlbumInfo,
    AlbumMetadata,
    Covers,
    TrackInfo,
    TrackMetadata,
    tag_file,
)

TEST_FLAC_ORIGINAL = "tests/silence.flac"
TEST_FLAC_COPY = "tests/silence_copy.flac"
test_cover = "tests/1x1_pixel.jpg"


def wipe_test_flac():
    audio = FLAC(TEST_FLAC_COPY)
    # Remove all tags
    audio.delete()
    audio.save()


@pytest.fixture()
def sample_metadata() -> TrackMetadata:
    return TrackMetadata(
        TrackInfo(
            id="12345",
            quality=3,
            bit_depth=24,
            explicit=True,
            sampling_rate=96,
            work=None,
        ),
        "testtitle",
        AlbumMetadata(
            AlbumInfo("5678", 4, "flac"),
            "testalbum",
            "testalbumartist",
            "1999",
            ["rock", "pop"],
            Covers(),
            14,
            3,
            "testalbumcomposer",
            "testcomment",
            compilation="testcompilation",
            copyright="(c) stuff (p) other stuff",
            date="1998-02-13",
            description="testdesc",
            encoder="ffmpeg",
            grouping="testgroup",
            lyrics="ye ye ye",
            purchase_date=None,
        ),
        "testartist",
        3,
        1,
        "testcomposer",
        "testauthor",  # Added required author field
    )


def test_tag_flac_no_cover(sample_metadata):
    shutil.copy(TEST_FLAC_ORIGINAL, TEST_FLAC_COPY)
    wipe_test_flac()
    arun(tag_file(TEST_FLAC_COPY, sample_metadata, None))
    file = FLAC(TEST_FLAC_COPY)
    assert file["title"][0] == "testtitle"
    assert file["album"][0] == "testalbum"
    assert file["composer"][0] == "testcomposer"
    assert file["comment"][0] == "testcomment"
    assert file["artist"][0] == "testartist"
    assert file["albumartist"][0] == "testalbumartist"
    assert file["genre"][0] == "rock"
    assert file["genre"][1] == "pop"
    assert file["tracknumber"][0] == "03"
    assert file["discnumber"][0] == "01"
    assert file["copyright"][0] == "(c) stuff (p) other stuff"
    assert file["tracktotal"][0] == "14"
    assert file["date"][0] == "1998-02-13"
    assert "purchase_date" not in file, file["purchase_date"]
    os.remove(TEST_FLAC_COPY)


def test_tag_flac_cover(sample_metadata):
    shutil.copy(TEST_FLAC_ORIGINAL, TEST_FLAC_COPY)
    wipe_test_flac()
    arun(tag_file(TEST_FLAC_COPY, sample_metadata, test_cover))
    file = FLAC(TEST_FLAC_COPY)
    assert file["title"][0] == "testtitle"
    assert file["album"][0] == "testalbum"
    assert file["composer"][0] == "testcomposer"
    assert file["comment"][0] == "testcomment"
    assert file["artist"][0] == "testartist"
    assert file["albumartist"][0] == "testalbumartist"
    assert file["genre"][0] == "rock"
    assert file["genre"][1] == "pop"
    assert file["tracknumber"][0] == "03"
    assert file["discnumber"][0] == "01"
    assert file["copyright"][0] == "(c) stuff (p) other stuff"
    assert file["tracktotal"][0] == "14"
    assert file["date"][0] == "1998-02-13"
    with open(test_cover, "rb") as img:
        assert file.pictures[0].data == img.read()
    assert "purchase_date" not in file, file["purchase_date"]
    os.remove(TEST_FLAC_COPY)


@pytest.mark.parametrize('extension,codec', [('flac','flac'),('m4a','aac'),('mp3','libmp3lame')])
async def test_metadata_round_trip_all_formats(tmp_path, sample_metadata, extension, codec):
    import subprocess
    from mutagen import File

    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg unavailable')
    path = tmp_path / f'audio.{extension}'
    subprocess.run(['ffmpeg','-v','error','-i',TEST_FLAC_ORIGINAL,'-map','0:a:0','-map_metadata','-1','-c:a',codec,str(path)],check=True)
    meta = sample_metadata
    meta.title = '名字 / Café: Live (2024)'
    meta.artist = 'Earth, Wind & Fire, Beyoncé'
    meta.artists = ['Earth, Wind & Fire', 'Beyoncé']
    meta.album.albumartist = 'Lady Gaga, Bruno Mars'
    meta.album.albumartists = ['Lady Gaga', 'Bruno Mars']
    meta.composer = ['Surname, Given Name', 'Another Composer']
    meta.author = ['Writer One', 'Writer Two']
    meta.album.originaldate = '1981-11-14'
    meta.album.compilation = True
    meta.info.work = 'Work: Part I'
    meta.credits = 'Earth, Wind & Fire, MainArtist - Writer One, Author'
    await tag_file(str(path),meta,None)
    tags = File(path).tags
    if extension == 'flac':
        assert tags['artists'] == meta.artists
        assert tags['albumartists'] == meta.album.albumartists
        assert tags['composer'] == meta.composer
        assert tags['date'] == ['1998-02-13']
        assert tags['originaldate'] == ['1981-11-14']
        assert tags['title'] == [meta.title]
        assert tags['copyright'] == [meta.album.copyright]
    elif extension == 'm4a':
        assert [v.decode() for v in tags['----:com.apple.iTunes:ARTISTS']] == meta.artists
        assert [v.decode() for v in tags['----:com.apple.iTunes:ALBUMARTISTS']] == meta.album.albumartists
        assert tags['©wrt'] == meta.composer
        assert tags['©day'] == ['1998-02-13']
        assert tags['©nam'] == [meta.title]
        assert tags['cpil'] is True
        assert tags['rtng'] == [1]
    else:
        assert tags['TXXX:ARTISTS'].text == meta.artists
        assert tags['TXXX:ALBUMARTISTS'].text == meta.album.albumartists
        assert tags['TCOM'].text == meta.composer
        assert str(tags['TDRC']) == '1998-02-13'
        assert str(tags['TDOR']) == '1981-11-14'
        assert tags['TIT2'].text == [meta.title]
        assert tags['TXXX:WORK'].text == ['Work: Part I']


async def test_metadata_exclusions_are_applied(tmp_path, sample_metadata):
    path = tmp_path / 'audio.flac'
    shutil.copyfile(TEST_FLAC_ORIGINAL, path)
    audio = FLAC(path)
    audio.delete()
    await tag_file(str(path), sample_metadata, None, exclude=['COMPOSER', 'copyright'])
    tags = FLAC(path)
    assert 'composer' not in tags
    assert 'copyright' not in tags
    assert tags['title'] == ['testtitle']


@pytest.mark.parametrize('extension,codec', [('flac', 'flac'), ('m4a', 'aac')])
async def test_album_folder_uses_downloaded_audio(tmp_path, sample_metadata, extension, codec):
    import subprocess
    from unittest.mock import AsyncMock, MagicMock
    from mutagen import File
    from streamrip.config import Config
    from streamrip.media.track import Track

    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg unavailable')
    source = tmp_path / f'source.{extension}'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=duration=0.1','-c:a',codec,str(source)],check=True)
    cfg = Config.defaults()
    cfg.session.downloads.folder = str(tmp_path / 'music')
    cfg.session.downloads.source_subdirectories = True
    cfg.session.downloads.disc_subdirectories = True
    cfg.session.downloads.validate_audio = False
    cfg.session.conversion.enabled = False
    cfg.session.cli.progress_bars = False
    cfg.session.artwork.save_artwork = False
    cfg.session.filepaths.folder_format = '{title} [{container}] [{bit_depth}B-{sampling_rate}kHz]'
    sample_metadata.album.info.container = 'FLAC'
    sample_metadata.album.info.bit_depth = 24
    sample_metadata.album.info.sampling_rate = 192
    sample_metadata.album.disctotal = 2
    sample_metadata.discnumber = 2
    sample_metadata.album.compilation = None
    initial = tmp_path / 'music' / 'Tidal' / 'wrong-quality' / 'Disc 2'
    downloadable = MagicMock(extension=extension, source='tidal')
    downloadable.size = AsyncMock(return_value=source.stat().st_size)

    async def download(path, callback):
        shutil.copyfile(source, path)

    downloadable.download = download
    track = Track(sample_metadata, downloadable, cfg, str(initial), None, MagicMock(), organize_by_album=True)
    await track.rip()
    from pathlib import Path
    target = Path(track.download_path)
    assert target.is_file()
    assert target.parent.name == 'Disc 2'
    expected_bits = File(source).info.bits_per_sample if extension == 'flac' else 'Unknown'
    assert f'[{extension.upper()}] [{expected_bits}B-44.1kHz]' in target.parent.parent.name
    assert sample_metadata.album.info.sampling_rate == 192
    assert sample_metadata.album.info.bit_depth == 24
    assert not initial.exists()
