import logging
import os
from enum import Enum

import aiofiles
from mutagen import id3
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,  # type: ignore
    ID3,
    ID3NoHeaderError,
)
from mutagen.mp4 import MP4, MP4Cover

from .track import TrackMetadata

logger = logging.getLogger("streamrip")

FLAC_MAX_BLOCKSIZE = 16777215  # 16.7 MB

MP4_KEYS = (
    "\xa9nam",
    "\xa9ART",
    "----:com.apple.iTunes:ARTISTS",
    "\xa9alb",
    r"aART",
    "\xa9wrt",  # composer
    "----:com.apple.iTunes:AUTHOR",  # author/songwriter
    "\xa9cmt",
    "desc",
    "purd",
    "\xa9grp",
    "\xa9gen",
    "\xa9lyr",
    "\xa9too",
    "cprt",
    "cpil",
    "trkn",
    "disk",
    None,
    None,
    None,
    "----:com.apple.iTunes:ISRC",
    "©pub",  # label/publisher
    "tmpo",  # bpm
    "----:com.apple.iTunes:UPC",
    "----:com.apple.iTunes:RELEASETYPE",  # was RECORD_TYPE
    "----:com.apple.iTunes:TRACK_ARTIST_CREDIT",
    "----:com.apple.iTunes:ALBUM_ARTIST_CREDIT",
    "----:com.apple.iTunes:ORIGINALDATE",  # was ORIGINAL_RELEASE_DATE
    "----:com.apple.iTunes:MEDIA_TYPE",
)

MP3_KEYS = (
    id3.TIT2,  # type: ignore
    id3.TPE1,  # type: ignore
    None,  # artists (handled as TXXX)
    id3.TALB,  # type: ignore
    id3.TPE2,  # type: ignore
    id3.TCOM,  # type: ignore
    id3.TEXT,  # author/lyricist/songwriter
    id3.COMM,  # type: ignore
    id3.TIT1,  # description (content group)
    None,  # purchase_date (handled as TXXX)
    id3.GP1,  # type: ignore
    id3.TCON,  # type: ignore
    id3.USLT,  # type: ignore
    id3.TEN,  # type: ignore
    id3.TCOP,  # type: ignore
    id3.TCMP,  # type: ignore
    id3.TRCK,  # type: ignore
    id3.TPOS,  # type: ignore
    None,
    None,
    None,
    id3.TSRC,
    id3.TPUB,  # label/publisher 
    id3.TBPM,  # bpm
    None,  # upc (handled as TXXX)
    None,  # releasetype (handled as TXXX)
    None,  # track_artist_credit (handled as TXXX)
    None,  # album_artist_credit (handled as TXXX)
    id3.TDOR,  # originaldate
    None,  # media_type (handled as TXXX)
)
METADATA_TYPES = (
    "title",
    "artist",
    "artists",
    "album",
    "albumartist",
    "composer",
    "author",
    "comment",
    "description",
    "purchase_date",
    "grouping",
    "genre",
    "lyrics",
    "encoder",
    "copyright",
    "compilation",
    "tracknumber",
    "discnumber",
    "tracktotal",
    "disctotal", 
    "date",
    "isrc",
    "label",
    "bpm",
    "upc",
    "releasetype",
    "track_artist_credit",
    "album_artist_credit",
    "originaldate",
    "media_type",
)


FLAC_KEY = {v: v.upper() for v in METADATA_TYPES}
MP4_KEY = dict(zip(METADATA_TYPES, MP4_KEYS))
MP3_KEY = dict(zip(METADATA_TYPES, MP3_KEYS))


FLAC_KEY.update({name: name.upper() for name in ("albumartists", "work", "credits", "explicit")})
MP4_KEY.update({
    "date": "\xa9day",
    "label": "----:com.apple.iTunes:LABEL",
    "albumartists": "----:com.apple.iTunes:ALBUMARTISTS",
    "work": "----:com.apple.iTunes:WORK",
    "credits": "----:com.apple.iTunes:CREDITS",
    "explicit": "rtng",
})
MP3_KEY.update({
    "date": id3.TDRC,
    "description": None,
    "albumartists": None,
    "work": None,
    "credits": None,
    "explicit": None,
})


class Container(Enum):
    FLAC = 1
    AAC = 2
    MP3 = 3

    def get_mutagen_class(self, path: str):
        if self == Container.FLAC:
            return FLAC(path)
        elif self == Container.AAC:
            return MP4(path)
        elif self == Container.MP3:
            try:
                return ID3(path)
            except ID3NoHeaderError:
                return ID3()
        # unreachable
        return {}

    def get_tag_pairs(self, meta) -> list[tuple]:
        if self == Container.FLAC:
            return self._tag_flac(meta)
        elif self == Container.MP3:
            return self._tag_mp3(meta)
        elif self == Container.AAC:
            return self._tag_mp4(meta)
        # unreachable
        return []

    def _tag_flac(self, meta: TrackMetadata) -> list[tuple]:
        out = []
        for name, key in FLAC_KEY.items():
            value = self._attr_from_meta(meta, name)
            if value is None or value == "" or value == []:
                continue
            if name in {"tracknumber", "discnumber", "tracktotal", "disctotal"}:
                value = f"{int(value):02}"
            elif isinstance(value, bool):
                value = str(int(value))
            elif not isinstance(value, list):
                value = str(value)
            out.append((key, value))
        return out

    def _tag_mp3(self, meta: TrackMetadata):
        out = []
        for name, frame in MP3_KEY.items():
            if name in {"tracktotal", "disctotal"}:
                continue
            value = self._attr_from_meta(meta, name)
            if name == "tracknumber":
                value = f"{meta.tracknumber}/{meta.album.tracktotal}"
            elif name == "discnumber":
                value = f"{meta.discnumber}/{meta.album.disctotal}"
            if value is None or value == "" or value == []:
                continue
            if isinstance(value, bool):
                value = str(int(value))
            if frame is None:
                out.append((f"TXXX:{name.upper()}", value))
            else:
                out.append((frame.__name__, frame(encoding=3, text=value)))
        return out

    def _tag_mp4(self, meta: TrackMetadata):
        out = []
        for name, key in MP4_KEY.items():
            if key is None:
                continue
            value = self._attr_from_meta(meta, name)
            if value is None or value == "" or value == []:
                continue
            if name == "tracknumber":
                value = [(meta.tracknumber, meta.album.tracktotal)]
            elif name == "discnumber":
                value = [(meta.discnumber, meta.album.disctotal)]
            elif name == "bpm":
                value = [int(float(value))]
            elif name == "explicit":
                value = [1 if value else 2]
            elif name == "compilation":
                value = str(value).lower() in {"true", "1"}
            else:
                values = value if isinstance(value, list) else [value]
                value = [str(v).encode("utf-8") if key.startswith("----:") else str(v) for v in values]
            out.append((key, value))
        return out

    def _attr_from_meta(self, meta: TrackMetadata, attr: str):
        if attr == "album":
            return meta.album.album
        if attr == "label":
            return meta.album.info.label
        if attr in {"work", "explicit"}:
            return getattr(meta.info, attr)
        if attr in {
            "title", "artist", "artists", "tracknumber", "discnumber",
            "composer", "author", "isrc", "lyrics", "bpm",
            "track_artist_credit", "media_type", "credits",
        }:
            return getattr(meta, attr)
        return getattr(meta.album, attr)

    def tag_audio(self, audio, tags: list[tuple]):
        for key, value in tags:
            if key.startswith("TXXX:"):
                audio.add(id3.TXXX(encoding=3, desc=key.split(":", 1)[1], text=value))
            elif self == Container.MP3:
                audio.add(value)
            else:
                audio[key] = value

    async def embed_cover(self, audio, cover_path):
        if self == Container.FLAC:
            size = os.path.getsize(cover_path)
            if size > FLAC_MAX_BLOCKSIZE:
                raise Exception("Cover art too big for FLAC")
            cover = Picture()
            cover.type = 3
            cover.mime = "image/jpeg"
            async with aiofiles.open(cover_path, "rb") as img:
                cover.data = await img.read()
            audio.add_picture(cover)
        elif self == Container.MP3:
            cover = APIC()
            cover.type = 3
            cover.mime = "image/jpeg"
            async with aiofiles.open(cover_path, "rb") as img:
                cover.data = await img.read()
            audio.add(cover)
        elif self == Container.AAC:
            async with aiofiles.open(cover_path, "rb") as img:
                cover = MP4Cover(await img.read(), imageformat=MP4Cover.FORMAT_JPEG)
            audio["covr"] = [cover]

    def save_audio(self, audio, path):
        if self == Container.FLAC:
            audio.save()
        elif self == Container.AAC:
            audio.save()
        elif self == Container.MP3:
            audio.save(path, v2_version=4)


async def tag_file(path: str, meta: TrackMetadata, cover_path: str | None, exclude=()):
    ext = path.split(".")[-1].lower()
    if ext == "flac":
        container = Container.FLAC
    elif ext == "m4a":
        container = Container.AAC
    elif ext == "mp3":
        container = Container.MP3
    else:
        raise Exception(f"Invalid extension {ext}")

    audio = container.get_mutagen_class(path)
    tags = container.get_tag_pairs(meta)
    logger.debug("Tagging with %s", tags)
    excluded = {name.lower() for name in exclude}
    keys = {Container.FLAC: FLAC_KEY, Container.AAC: MP4_KEY, Container.MP3: MP3_KEY}[container]
    excluded_keys = set()
    for name in excluded:
        if name not in keys:
            continue
        key = keys[name]
        excluded_keys.add((key.__name__ if key else f"TXXX:{name.upper()}") if container == Container.MP3 else key)
    container.tag_audio(audio, [(key, value) for key, value in tags if key not in excluded_keys])
    if cover_path is not None:
        await container.embed_cover(audio, cover_path)
    container.save_audio(audio, path)
