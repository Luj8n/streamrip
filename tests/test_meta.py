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
    assert info.id == "19512572"
    assert info.quality == 3
    assert info.container == "FLAC"
    assert info.label == "Rhino - Warner Records"
    assert info.explicit is False
    assert info.sampling_rate == 96
    assert info.bit_depth == 24
    assert info.booklets is None

    assert m.album == "Rumours"
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
    assert t.composer == "John Darnielle"


@pytest.mark.parametrize(
    ("genre", "genres_list", "expected"),
    [
        ({"name": "Classical"}, ["Classique"], {"Classical"}),
        (None, ["Pop/Rock"], {"Pop", "Rock"}),
        ({}, ["Pop/Rock"], {"Pop", "Rock"}),
        ({"name": ""}, ["Pop/Rock"], {"Pop", "Rock"}),
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
