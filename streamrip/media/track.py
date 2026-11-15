import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, replace

from mutagen import File

from .. import converter
from ..client import Client, Downloadable
from ..client.downloadable import get_quality_description
from ..config import Config
from ..console import console
from ..db import Database
from ..exceptions import NonStreamableError, SkipTrackError
from ..filepath_utils import clean_filename, clean_filepath
from ..metadata import AlbumMetadata, Covers, TrackMetadata, tag_file
from ..progress import add_title, get_progress_callback, remove_title
from ..utils.audio_validator import validate_audio_file
from .artwork import artwork_tempdir, download_artwork
from .media import Media, Pending

logger = logging.getLogger("streamrip")


@dataclass(slots=True)
class Track(Media):
    meta: TrackMetadata
    downloadable: Downloadable
    config: Config
    folder: str
    # Is None if a cover doesn't exist for the track
    cover_path: str | None
    db: Database
    # change?
    download_path: str = ""
    is_single: bool = False
    organize_by_album: bool = False

    async def rip(self):
        await self.preprocess()
        try:
            if self.config.session.cli.dry_run:
                await self.download()
                return
            destination = self.download_path
            initial_folder = self.folder
            # Keep the old file until validation, tags and conversion all succeed.
            staging = tempfile.mkdtemp(prefix=".streamrip-", dir=self.folder)
            completed = False
            try:
                self.download_path = os.path.join(staging, os.path.basename(destination))
                await self.download()
                await self.postprocess()
                staged_path = self.download_path
                extension = os.path.splitext(staged_path)[1][1:]
                if self.organize_by_album:
                    self._set_album_folder_from_audio()
                os.makedirs(self.folder, exist_ok=True)
                self.meta.info.container = extension.upper()
                self._set_download_path(extension)
                os.replace(staged_path, self.download_path)
                completed = True
            finally:
                if completed or self.config.session.downloads.delete_invalid_files:
                    shutil.rmtree(staging)
                    if completed and initial_folder != self.folder:
                        try:
                            os.rmdir(initial_folder)
                        except OSError:
                            pass  # Other tracks may still be using this directory.
                else:
                    logger.warning("Kept incomplete download in %s", staging)
            self.db.set_downloaded(self.meta.info.id, self.downloadable.source)
        finally:
            if self.is_single:
                remove_title(self.meta.title)

    def _set_album_folder_from_audio(self):
        audio = File(self.download_path)
        if audio is None:
            raise ValueError("Cannot determine downloaded audio properties")
        container = os.path.splitext(self.download_path)[1][1:].upper()
        lossless = container == "FLAC" or getattr(audio.info, "codec", None) == "alac"
        info = replace(
            self.meta.album.info,
            container=container,
            bit_depth=(getattr(audio.info, "bits_per_sample", None) or None) if lossless else None,
            sampling_rate=audio.info.sample_rate / 1000,
        )
        album = replace(self.meta.album, info=info)
        config = self.config.session
        parent = config.downloads.folder
        if config.downloads.source_subdirectories:
            parent = os.path.join(parent, self.downloadable.source.capitalize())
        folder = os.path.join(parent, clean_filepath(
            album.format_folder_path(config.filepaths.folder_format),
            config.filepaths.restrict_characters,
        ))
        os.makedirs(folder, exist_ok=True)
        if config.artwork.save_artwork and not album.covers.empty():
            _, _, saved_cover = album.covers.largest()
            target = os.path.join(folder, "cover.jpg")
            if saved_cover and os.path.abspath(saved_cover) != os.path.abspath(target):
                shutil.copyfile(saved_cover, target)
        if config.downloads.disc_subdirectories and album.disctotal > 1:
            folder = os.path.join(folder, f"Disc {self.meta.discnumber}")
        self.folder = folder

    async def preprocess(self):
        self._set_download_path()
        if not self.config.session.cli.dry_run:
            os.makedirs(self.folder, exist_ok=True)
        if self.is_single:
            add_title(self.meta.title)

    async def download(self):
        if self.config.session.cli.dry_run:
            await self._print_dry_run_info()
            return

        with get_progress_callback(
            self.config.session.cli.progress_bars,
            await self.downloadable.size(),
            f"Track {self.meta.tracknumber}",
        ) as callback:
            await self.downloadable.download(self.download_path, callback)

    async def _print_dry_run_info(self):
        """Print track information for dry run mode."""
        try:
            size_mb = (await self.downloadable.size()) / (1024 * 1024)
            size_str = f"{size_mb:.1f} MB"
        except:
            size_str = "Unknown size"

        quality_info = f"Quality: {self.meta.info.quality}"
        if hasattr(self.meta.info, 'bit_depth') and hasattr(self.meta.info, 'sampling_rate'):
            if self.meta.info.bit_depth and self.meta.info.sampling_rate:
                quality_info += f" ({self.meta.info.bit_depth}-bit/{self.meta.info.sampling_rate}kHz)"

        console.print(f"[cyan]Would download:[/cyan] [bold]{self.meta.title}[/bold]")
        console.print(f"  Artist: {self.meta.artist}")
        console.print(f"  Album: {self.meta.album.album}")
        console.print(f"  Track: {self.meta.tracknumber}/{self.meta.album.tracktotal}")
        if self.meta.album.disctotal > 1:
            console.print(f"  Disc: {self.meta.discnumber}/{self.meta.album.disctotal}")
        console.print(f"  Source: {self.downloadable.source}")
        console.print(f"  {quality_info}")
        console.print(f"  Format: {self.meta.info.container}")
        console.print(f"  Size: {size_str}")
        console.print(f"  Path: [dim]{self.download_path}[/dim]")
        console.print("")

    async def postprocess(self):
        if self.config.session.cli.dry_run:
            return

        # Validate audio file if enabled
        if self.config.session.downloads.validate_audio:
            await self._validate_audio_file()

        await tag_file(self.download_path, self.meta, self.cover_path, self.config.session.metadata.exclude)
        if self.config.session.conversion.enabled:
            await self._convert()


    async def _validate_audio_file(self):
        """Validate the downloaded audio file for corruption."""
        if not os.path.exists(self.download_path):
            raise Exception(f"Audio file not found for validation: {self.download_path}")

        logger.debug(f"Validating audio file: {self.download_path}")
        validation_result = await validate_audio_file(self.download_path)

        if not validation_result.is_valid:
            error_msg = f"Audio validation failed for '{self.meta.title}' by {self.meta.artist}"
            if validation_result.error_message:
                error_msg += f": {validation_result.error_message}"
            if validation_result.validation_method:
                error_msg += f" (method: {validation_result.validation_method})"

            logger.error(error_msg)

            # Delete invalid file if configured
            if self.config.session.downloads.delete_invalid_files:
                try:
                    os.remove(self.download_path)
                    logger.debug(f"Deleted invalid audio file: {self.download_path}")
                except OSError as e:
                    logger.warning(f"Failed to delete invalid file {self.download_path}: {e}")

            if not self.config.session.downloads.retry_on_validation_failure:
                raise SkipTrackError(error_msg)
            raise RuntimeError(error_msg)
        else:
            quality_desc = get_quality_description(self.downloadable.source, self.downloadable.quality)
            logger.info(f"Audio validation passed for '{self.meta.title}' by {self.meta.artist} [{quality_desc}] (method: {validation_result.validation_method})")


    async def _convert(self):
        c = self.config.session.conversion
        engine_class = converter.get(c.codec)
        engine = engine_class(
            filename=self.download_path,
            sampling_rate=c.sampling_rate,
            bit_depth=c.bit_depth,
            remove_source=True,  # always going to delete the old file
        )
        await engine.convert()
        self.download_path = engine.final_fn  # because the extension changed

    def _set_download_path(self, extension: str | None = None):
        c = self.config.session.filepaths
        formatter = c.track_format
        track_path = clean_filename(
            self.meta.format_track_path(formatter),
            restrict=c.restrict_characters,
        )
        if c.truncate_to > 0 and len(track_path) > c.truncate_to:
            track_path = track_path[: c.truncate_to]

        extension = f".{extension or self.downloadable.extension}"
        track_path = track_path.encode("utf-8")[:255 - len(extension.encode("utf-8"))].decode("utf-8", errors="ignore")
        self.download_path = os.path.join(self.folder, f"{track_path}{extension}")


@dataclass(slots=True)
class PendingTrack(Pending):
    id: str
    album: AlbumMetadata
    client: Client
    config: Config
    folder: str
    db: Database
    # cover_path is None <==> Artwork for this track doesn't exist in API
    cover_path: str | None

    async def resolve(self) -> Track | None:
        if self.db.downloaded(self.id, self.client.source):
            logger.info(
                f"Skipping track {self.id}. Marked as downloaded in the database.",
            )
            return None

        source = self.client.source
        try:
            resp = await self.client.get_metadata(self.id, "track")
        except NonStreamableError as e:
            logger.error(f"Track {self.id} not available for stream on {source}: {e}")
            return None

        try:
            meta = TrackMetadata.from_resp(self.album, source, resp)
        except Exception as e:
            logger.error(f"Error building track metadata for {self.id}: {e}")
            return None

        # Check if track is streamable
        if not meta.info.streamable:
            error_msg = f"Track '{meta.title}' by {meta.artist} (Album: {meta.album.album}) [{self.id}] not available for stream on {source}"
            if source == "deezer":
                error_msg += " (possible geo-restriction - track may show as available in web UI but API blocks downloads)"
            logger.error(error_msg)
            self.db.set_failed(source, "track", self.id)
            return None

        # Check quality requirements and select appropriate quality
        source_config = self.config.session.get_source(source)
        requested_quality = source_config.quality
        lower_quality_fallback = getattr(source_config, 'lower_quality_if_not_available', False)
        
        # meta.info.quality contains the highest available quality for this track
        # Now select the actual quality to download
        if self.client.source != "tidal" and meta.info.quality < requested_quality and not lower_quality_fallback:
            logger.error(f"Track '{meta.title}' by {meta.artist} (Album: {meta.album.album}) [{self.id}]: Quality {meta.info.quality} available but {requested_quality} requested - skipping due to lower_quality_if_not_available=false")
            self.db.set_failed(source, "track", self.id)
            raise SkipTrackError(f"Quality {meta.info.quality} available but {requested_quality} requested")

        # Select the quality to download: min of requested and available
        quality = requested_quality if self.client.source == "tidal" else min(requested_quality, meta.info.quality)
        
        # Get downloadable info for the determined quality
        try:
            downloadable = await self.client.get_downloadable(self.id, quality)

        except NonStreamableError as e:
            logger.error(
                f"Error getting downloadable data for track {meta.tracknumber} '{meta.title}' by {meta.artist} (Album: {meta.album.album}) [{self.id}]: {e}"
            )
            self.db.set_failed(source, "track", self.id)
            return None

        # Update container format based on actual downloadable format
        meta.info.container = downloadable.extension.upper()
        if self.client.source == "tidal":
            meta.info.quality = downloadable.quality
            meta.info.bit_depth = downloadable.bit_depth
            meta.info.sampling_rate = downloadable.sampling_rate

        downloads_config = self.config.session.downloads
        if downloads_config.disc_subdirectories and self.album.disctotal > 1:
            folder = os.path.join(self.folder, f"Disc {meta.discnumber}")
        else:
            folder = self.folder

        return Track(
            meta,
            downloadable,
            self.config,
            folder,
            self.cover_path,
            self.db,
            organize_by_album=True,
        )


@dataclass(slots=True)
class PendingSingle(Pending):
    """Whereas PendingTrack is used in the context of an album, where the album metadata
    and cover have been resolved, PendingSingle is used when a single track is downloaded.

    This resolves the Album metadata and downloads the cover to pass to the Track class.
    """

    id: str
    client: Client
    config: Config
    db: Database

    async def resolve(self) -> Track | None:
        if self.db.downloaded(self.id, self.client.source):
            logger.info(
                f"Skipping track {self.id}. Marked as downloaded in the database.",
            )
            return None

        try:
            resp = await self.client.get_metadata(self.id, "track")
        except NonStreamableError as e:
            logger.error(f"Error fetching track {self.id}: {e}")
            return None
        # Patch for soundcloud
        try:
            if self.client.source == "tidal":
                album_resp = await self.client.get_metadata(str(resp["album"]["id"]), "album")
                album = AlbumMetadata.from_album_resp(album_resp, "tidal")
            else:
                album = AlbumMetadata.from_track_resp(resp, self.client.source)
        except Exception as e:
            logger.error(f"Error building album metadata for track {id=}: {e}")
            return None

        if album is None:
            self.db.set_failed(self.client.source, "track", self.id)
            logger.error(
                f"Cannot stream track (am) ({self.id}) on {self.client.source}",
            )
            return None

        try:
            meta = TrackMetadata.from_resp(album, self.client.source, resp)
        except Exception as e:
            logger.error(f"Error building track metadata for track {id=}: {e}")
            return None

        # Check if track is streamable
        if not meta.info.streamable:
            logger.error(f"Track '{meta.title}' by {meta.artist} (Album: {meta.album.album}) [{self.id}] not available for stream on {self.client.source}")
            self.db.set_failed(self.client.source, "track", self.id)
            return None

        # Check quality requirements and select appropriate quality
        source_config = self.config.session.get_source(self.client.source)
        requested_quality = source_config.quality
        lower_quality_fallback = getattr(source_config, 'lower_quality_if_not_available', False)
        
        # meta.info.quality contains the highest available quality for this track
        # Now select the actual quality to download
        if self.client.source != "tidal" and meta.info.quality < requested_quality and not lower_quality_fallback:
            logger.error(f"Track '{meta.title}' by {meta.artist} (Album: {meta.album.album}) [{self.id}]: Quality {meta.info.quality} available but {requested_quality} requested - skipping due to lower_quality_if_not_available=false")
            self.db.set_failed(self.client.source, "track", self.id)
            return None

        # Select the quality to download: min of requested and available
        quality = requested_quality if self.client.source == "tidal" else min(requested_quality, meta.info.quality)
        
        downloadable = await self.client.get_downloadable(self.id, quality)
        meta.info.container = downloadable.extension.upper()
        if self.client.source == "tidal":
            meta.info.quality = album.info.quality = downloadable.quality
            meta.info.bit_depth = album.info.bit_depth = downloadable.bit_depth
            meta.info.sampling_rate = album.info.sampling_rate = downloadable.sampling_rate
            album.info.container = meta.info.container

        config = self.config.session
        folder = self._format_folder(album) if config.filepaths.add_singles_to_folder else config.downloads.folder
        os.makedirs(folder, exist_ok=True)
        embedded_cover_path = await self._download_cover(album.covers, folder)

        return Track(
            meta,
            downloadable,
            self.config,
            folder,
            embedded_cover_path,
            self.db,
            is_single=True,
            organize_by_album=self.config.session.filepaths.add_singles_to_folder,
        )

    def _format_folder(self, meta: AlbumMetadata) -> str:
        c = self.config.session
        parent = c.downloads.folder
        formatter = c.filepaths.folder_format
        if c.downloads.source_subdirectories:
            parent = os.path.join(parent, self.client.source.capitalize())

        return os.path.join(parent, clean_filepath(meta.format_folder_path(formatter), c.filepaths.restrict_characters))

    async def _download_cover(self, covers: Covers, folder: str) -> str | None:
        embed_path, _ = await download_artwork(
            self.client.session,
            artwork_tempdir() if self.config.session.filepaths.add_singles_to_folder else folder,
            covers,
            self.config.session.artwork,
            for_playlist=False,
        )
        return embed_path
