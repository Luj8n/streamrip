import json

import pytest

from streamrip.metadata import AlbumMetadata, TrackMetadata

with open("tests/qobuz_album_resp.json") as f:
    qobuz_album_resp = json.load(f)

with open("tests/qobuz_track_resp.json") as f:
    qobuz_track_resp = json.load(f)


def test_album_metadata_qobuz():
    m = AlbumMetadata.from_qobuz(qobuz_album_resp)
    info = m.info
    assert info.id == "0603497941032"
    assert info.quality == 3
    assert info.container == "FLAC"
    assert info.label == "Rhino - Warner Records"
    assert info.explicit is False
    assert info.sampling_rate == 96
    assert info.bit_depth == 24
    assert info.booklets is None

    assert m.album == "Rumours (2001 Remaster)"
    assert m.albumartist == "Fleetwood Mac"
    assert m.year == "1977"
    assert m.genre == ["Rock"]
    assert not m.covers.empty()

    assert m.albumcomposer == "Various Composers"
    assert m.comment is None
    assert m.compilation is None
    assert (
        m.copyright
        == "© 1977 Warner Records Inc. ℗ 1977 Warner Records Inc. Marketed by Rhino Entertainment Company, A Warner Music Group Company."
    )
    assert m.date == "1977-02-04"
    assert m.description == ""
    assert m.disctotal == 1
    assert m.encoder is None
    assert m.grouping is None
    assert m.lyrics is None
    assert m.purchase_date is None
    assert m.tracktotal == 11


def test_track_metadata_qobuz():
    a = AlbumMetadata.from_qobuz(qobuz_track_resp["album"])
    t = TrackMetadata.from_qobuz(a, qobuz_track_resp)
    info = t.info
    assert info.id == "216020864"
    assert info.quality == 3
    assert info.bit_depth == 24
    assert info.sampling_rate == 96
    assert info.work is None

    assert t.title == "Water Tower"
    assert t.album == a
    assert t.artist == "The Mountain Goats"
    assert t.tracknumber == 9
    assert t.discnumber == 1
    assert t.composer == ["John Darnielle"]


@pytest.mark.parametrize(
    ("genre", "genres_list", "expected"),
    [
        ({"name": "Classical"}, ["Classique"], {"Classical"}),
        (None, ["Pop/Rock"], {"Pop/Rock"}),
        ({}, ["Pop/Rock"], {"Pop/Rock"}),
        ({"name": ""}, ["Pop/Rock"], {"Pop/Rock"}),
        (None, [], set()),
    ],
)
def test_qobuz_genre_name_and_fallback(genre, genres_list, expected):
    response = dict(qobuz_album_resp, genre=genre, genres_list=genres_list)
    assert set(AlbumMetadata.from_qobuz(response).genre) == expected


def test_qobuz_missing_genre():
    response = qobuz_album_resp.copy()
    response.pop("genre", None)
    response.pop("genres_list", None)
    assert AlbumMetadata.from_qobuz(response).genre == []


def test_track_formatter_supports_id():
    album = AlbumMetadata.from_qobuz(qobuz_track_resp["album"])
    track = TrackMetadata.from_qobuz(album, qobuz_track_resp)
    assert track.format_track_path("{id} - {title}") == "216020864 - Water Tower"


def test_qobuz_null_optional_metadata():
    response = dict(qobuz_album_resp, release_type=None)
    album = AlbumMetadata.from_qobuz(response)
    assert not album.releasetype
    track = TrackMetadata.from_qobuz(album, dict(qobuz_track_resp, composer=None))
    assert track.title == qobuz_track_resp['title']


def test_qobuz_preserves_artist_names_work_and_disc_count():
    from streamrip.client.qobuz import QobuzClient

    credits = 'Earth, Wind & Fire, MainArtist - Surname, Given, ComposerLyricist'
    response = dict(qobuz_track_resp, title='Aria', work='Goldberg Variations', parental_warning=True,
                    performer={'name': 'Earth, Wind & Fire'}, composer={'name': 'Surname, Given'}, performers=credits)
    response['_parsed_performer_roles'] = QobuzClient.parse_performers(credits, ['Earth, Wind & Fire', 'Surname, Given'])
    album_response = dict(response['album'], media_count=4, artists=[{'name': 'Artist A', 'roles': ['main-artist']}, {'name': 'Artist B', 'roles': ['main-artist']}])
    album_response.pop('tracks', None)
    album = AlbumMetadata.from_qobuz(album_response)
    track = TrackMetadata.from_qobuz(album, response)
    assert album.disctotal == 4
    assert album.albumartists == ['Artist A', 'Artist B']
    assert album.albumartist == 'Artist A, Artist B'
    assert track.title == 'Aria'
    assert track.info.work == 'Goldberg Variations'
    assert track.info.explicit is True
    assert track.artists == ['Earth, Wind & Fire']
    assert track.composer == track.author == ['Surname, Given']
    assert track.credits == credits


def test_tidal_does_not_duplicate_version_or_invent_label():
    response = {'id': '1', 'cover': None, 'title': 'Album', 'releaseDate': '2024-01-02', 'artists': [{'name': 'A'}, {'name': 'B'}],
                'copyright': '(P) 2024 Some Rights Holder', 'audioQuality': 'LOSSLESS'}
    album = AlbumMetadata.from_tidal(response)
    assert album.info.label is None
    assert album.albumartists == ['A', 'B']
    track = TrackMetadata.from_tidal(album, {'id': '2', 'title': 'Song (Live)', 'version': 'Live', 'isrc': 'TEST',
                                           'artists': [{'id': 1, 'name': 'A'}, {'id': 2, 'name': 'B'}],
                                           'contributors': [{'name': 'C', 'role': 'Composer'}, {'name': 'D', 'role': 'Writer'}]})
    assert track.title == 'Song (Live)'
    assert track.artist == 'A, B'
    assert track.composer == ['C']
    assert track.author == ['D']


def test_long_album_folder_is_byte_safe_without_changing_tags(tmp_path):
    from streamrip.metadata import AlbumInfo, Covers

    title = '語' * 130
    artist = 'A' * 180
    album = AlbumMetadata(AlbumInfo('1', 2, 'FLAC'), title, artist, '2024', [], Covers(), 1)
    folder = album.format_folder_path('{albumartist} - {title} ({year})')
    assert len(folder.encode()) <= 255
    (tmp_path / folder).mkdir()
    assert album.album == title
    assert album.albumartist == artist


@pytest.mark.parametrize('rate,expected', [(96, '96'), (96.0, '96'), (44.1, '44.1')])
def test_equivalent_sample_rates_have_same_folder_name(rate, expected):
    from streamrip.metadata import AlbumInfo, Covers

    album = AlbumMetadata(AlbumInfo('1', 3, 'FLAC', sampling_rate=rate), 'Album', 'Artist', '2024', [], Covers(), 1)
    assert album.format_folder_path('{sampling_rate}kHz') == expected + 'kHz'
