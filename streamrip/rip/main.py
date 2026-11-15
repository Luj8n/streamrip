import asyncio
import json
import logging
import platform

import aiofiles
from click import ClickException

from .. import db
from ..client import Client, DeezerClient, QobuzClient, SoundcloudClient, TidalClient
from ..config import Config
from ..console import console
from ..download_task import DownloadTask
from ..exceptions import NonStreamableError, SkipTrackError
from ..media import (
    Album,
    Media,
    Pending,
    PendingAlbum,
    PendingArtist,
    PendingLabel,
    PendingLastfmPlaylist,
    PendingPlaylist,
    PendingPlaylistTrack,
    PendingSingle,
    PendingTrack,
    Playlist,
    Track,
    remove_artwork_tempdirs,
)
from ..metadata import SearchResults
from ..progress import clear_progress
from .parse_url import parse_url
from .prompter import get_prompter

logger = logging.getLogger("streamrip")

if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class Main:
    """Provides all of the functionality called into by the CLI.

    * Logs in to Clients and prompts for credentials
    * Handles output logging
    * Handles downloading Media
    * Handles interactive search

    User input (urls) -> Main --> Download files & Output messages to terminal
    """

    def __init__(self, config: Config):
        # Data pipeline:
        # input URL -> (URL) -> (Pending) -> (Media) -> (Downloadable) -> audio file
        self.pending: list[Pending] = []
        self.media: list[Media] = []
        self.config = config
        self.clients: dict[str, Client] = {
            "qobuz": QobuzClient(config),
            "tidal": TidalClient(config),
            "deezer": DeezerClient(config),
            "soundcloud": SoundcloudClient(config),
        }

        # Global download queue and worker management
        self.download_queue: asyncio.Queue[DownloadTask | None] = asyncio.Queue()
        self.worker_tasks: list[asyncio.Task] = []
        self.max_workers = max(1, config.session.downloads.max_connections)
        self.failures = 0
        self.collections = []

        self.database: db.Database

        c = self.config.session.database
        if c.downloads_enabled:
            downloads_db = db.Downloads(c.downloads_path)
        else:
            downloads_db = db.Dummy()

        if c.failed_downloads_enabled:
            failed_downloads_db = db.Failed(c.failed_downloads_path)
        else:
            failed_downloads_db = db.Dummy()

        # Use same path as downloads for releases table
        if c.downloads_enabled:
            releases_db = db.DownloadedReleases(c.downloads_path.replace('.db', '_releases.db'))
        else:
            releases_db = db.Dummy()

        self.database = db.Database(downloads_db, failed_downloads_db, releases_db)

    async def start_workers(self):
        """Start download worker tasks."""
        logger.debug(f"Starting {self.max_workers} download workers")
        for i in range(self.max_workers):
            worker = asyncio.create_task(self._download_worker(f"worker-{i}"))
            self.worker_tasks.append(worker)

    async def stop_workers(self):
        for _ in self.worker_tasks:
            await self.download_queue.put(None)
        await asyncio.gather(*self.worker_tasks)
        self.worker_tasks.clear()

    async def _download_worker(self, name: str):
        while True:
            task = await self.download_queue.get()
            try:
                if task is None:
                    return
                try:
                    await self._process_download_task(task)
                except Exception as e:
                    if task.retry_count < 3 and not isinstance(e, (SkipTrackError, NonStreamableError)):
                        task.retry_count += 1
                        logger.warning("Download failed (%s); retry %s/3", e, task.retry_count)
                        await asyncio.sleep(task.retry_count * 2)
                        await self.download_queue.put(task)
                    else:
                        self.failures += 1
                        track = task.track
                        if isinstance(track, Track):
                            source, item_id = track.downloadable.source, track.meta.info.id
                        elif isinstance(track, (PendingSingle, PendingTrack, PendingPlaylistTrack)):
                            source, item_id = track.client.source, track.id
                        else:
                            logger.error("Failed download: %s", e)
                            continue
                        try:
                            self.database.set_failed(source, "track", item_id)
                        except Exception as db_error:
                            logger.error("Could not record failed download: %s", db_error)
                        logger.error("Failed %s track %s: %s", source, item_id, e)
            finally:
                self.download_queue.task_done()

    async def _process_download_task(self, task: DownloadTask):
        track = task.track
        if isinstance(track, Pending):
            if self.database.downloaded(track.id, track.client.source):
                return
            track = await track.resolve()
            if track is None:
                raise SkipTrackError("Track is unavailable or its metadata could not be resolved")
        await track.rip()

    async def _queue_album_tracks(self, album):
        # Albums and playlists share the same track queue and finalization.
        await album.preprocess()
        self.collections.append(album)
        for track in album.tracks:
            await self.download_queue.put(DownloadTask(track, album_ref=album))

    async def _queue_pending(self, pending):
        if isinstance(pending, (PendingSingle, PendingTrack, PendingPlaylistTrack)):
            await self.download_queue.put(DownloadTask(pending))
        elif isinstance(pending, PendingArtist):
            async for album in pending.stream_albums():
                await self._queue_album_tracks(album)
        else:
            media = await pending.resolve()
            if media is None:
                if isinstance(pending, PendingAlbum) and not self.database.release_downloaded(
                    pending.id, "album", pending.client.source
                ):
                    raise RuntimeError(f"Could not resolve album {pending.id}")
                return
            if isinstance(media, (Album, Playlist)):
                await self._queue_album_tracks(media)
            elif hasattr(media, "albums"):
                for album in media.albums:
                    try:
                        await self._queue_pending(album)
                    except Exception as e:
                        self.failures += 1
                        logger.error("Could not process album %s: %s", album.id, e)
            else:
                await self.download_queue.put(DownloadTask(media))

    async def _finish_downloads(self):
        await self.download_queue.join()
        for collection in self.collections:
            await collection.postprocess()
        self.collections.clear()
        if self.failures:
            raise ClickException(f"{self.failures} item(s) failed to download; see the errors above.")
        logger.info("All downloads completed")

    async def add(self, url: str):
        """Add url as a pending item.

        Do not `asyncio.gather` calls to this! Use `add_all` for concurrency.
        """
        parsed = parse_url(url)
        if parsed is None:
            raise Exception(f"Unable to parse url {url}")

        client = await self.get_logged_in_client(parsed.source)
        self.pending.append(
            await parsed.into_pending(client, self.config, self.database),
        )
        logger.debug("Added url=%s", url)

    async def add_by_id(self, source: str, media_type: str, id: str):
        client = await self.get_logged_in_client(source)
        self._add_by_id_client(client, media_type, id)

    async def add_all_by_id(self, info: list[tuple[str, str, str]]):
        sources = set(s for s, _, _ in info)
        clients = {s: await self.get_logged_in_client(s) for s in sources}
        for source, media_type, id in info:
            self._add_by_id_client(clients[source], media_type, id)

    def _add_by_id_client(self, client: Client, media_type: str, id: str):
        if media_type == "track":
            item = PendingSingle(id, client, self.config, self.database)
        elif media_type == "album":
            item = PendingAlbum(id, client, self.config, self.database)
        elif media_type == "playlist":
            item = PendingPlaylist(id, client, self.config, self.database)
        elif media_type == "label":
            item = PendingLabel(id, client, self.config, self.database)
        elif media_type == "artist":
            item = PendingArtist(id, client, self.config, self.database)
        else:
            raise Exception(media_type)

        self.pending.append(item)

    async def add_all(self, urls: list[str]):
        """Add multiple urls concurrently as pending items."""
        parsed = [parse_url(url) for url in urls]
        url_client_pairs = []
        for i, p in enumerate(parsed):
            if p is None:
                console.print(
                    f"[red]Found invalid url [cyan]{urls[i]}[/cyan], skipping.",
                )
                continue
            url_client_pairs.append((p, await self.get_logged_in_client(p.source)))

        pendings = await asyncio.gather(
            *[
                url.into_pending(client, self.config, self.database)
                for url, client in url_client_pairs
            ],
        )
        self.pending.extend(pendings)

    async def get_logged_in_client(self, source: str):
        """Return a functioning client instance for `source`."""
        client = self.clients.get(source)
        if client is None:
            raise Exception(
                f"No client named {source} available. Only have {self.clients.keys()}",
            )

        # Use lock to prevent concurrent login attempts to same client
        # This prevents race condition where multiple coroutines check logged_in
        # simultaneously and both call login(), creating orphaned aiohttp sessions
        async with client._login_lock:
            if not client.logged_in:
                prompter = get_prompter(client, self.config)
                if not prompter.has_creds():
                    # Get credentials from user and log into client
                    await prompter.prompt_and_login()
                    prompter.save()
                else:
                    # Log into client using credentials from config
                    await client.login()

        assert client.logged_in
        return client



    async def stream_process_urls(self, urls: list[str]):
        self.failures = 0
        await self.start_workers()
        try:
            for url in urls:
                try:
                    parsed = parse_url(url)
                    if parsed is None:
                        raise ValueError(f"Unable to parse URL {url}")
                    client = await self.get_logged_in_client(parsed.source)
                    pending = await parsed.into_pending(client, self.config, self.database)
                    await self._queue_pending(pending)
                except Exception as e:
                    self.failures += 1
                    logger.error("Could not process %s: %s", url, e)
            await self._finish_downloads()
        finally:
            await self.stop_workers()

    async def stream_process_single_url(self, url: str):
        await self.stream_process_urls([url])

    async def stream_process_pending(self):
        self.failures = 0
        await self.start_workers()
        try:
            for pending in self.pending:
                try:
                    await self._queue_pending(pending)
                except Exception as e:
                    self.failures += 1
                    logger.error("Could not resolve item: %s", e)
            await self._finish_downloads()
        finally:
            self.pending.clear()
            await self.stop_workers()

    async def stream_process_lastfm(self, playlist_url: str):
        """Stream process a Last.fm playlist URL."""
        c = self.config.session.lastfm
        client = await self.get_logged_in_client(c.source)

        if len(c.fallback_source) > 0:
            fallback_client = await self.get_logged_in_client(c.fallback_source)
        else:
            fallback_client = None

        pending_playlist = PendingLastfmPlaylist(
            playlist_url,
            client,
            fallback_client,
            self.config,
            self.database,
        )

        # Track all download tasks to ensure they complete
        download_tasks = set()

        async def process_single_item(media_item):
            """Process a single track"""
            try:
                # Download the item
                await media_item.rip()
                return media_item
            except Exception as e:
                logger.error(f"Error processing item {media_item}: {e}")
                return None

        # Stream tracks from the Last.fm playlist
        if hasattr(pending_playlist, 'stream_tracks'):
            async for track in pending_playlist.stream_tracks():
                # Start processing this track immediately
                task = asyncio.create_task(process_single_item(track))
                download_tasks.add(task)
                # Remove completed tasks to prevent memory buildup
                task.add_done_callback(download_tasks.discard)
        else:
            # Fallback to old method if streaming not available
            playlist = await pending_playlist.resolve()
            if playlist:
                await process_single_item(playlist)

        # Wait for all download tasks to complete
        if download_tasks:
            logger.info(f"Waiting for {len(download_tasks)} downloads to complete...")
            await asyncio.gather(*download_tasks, return_exceptions=True)

        # Wait for download queue to be fully processed
        await self.download_queue.join()
        logger.info("All streaming downloads completed")

    async def search_interactive(self, source: str, media_type: str, query: str):
        client = await self.get_logged_in_client(source)

        with console.status(f"[bold]Searching {source}", spinner="dots"):
            pages = await client.search(media_type, query, limit=100)
            if len(pages) == 0:
                console.print(f"[red]No search results found for query {query}")
                return
            search_results = SearchResults.from_pages(source, media_type, pages)

        if platform.system() == "Windows":  # simple term menu not supported for windows
            from pick import pick

            choices = pick(
                search_results.results,
                title=(
                    f"{source.capitalize()} {media_type} search.\n"
                    "Press SPACE to select, RETURN to download, CTRL-C to exit."
                ),
                multiselect=True,
                min_selection_count=1,
            )
            assert isinstance(choices, list)

            await self.add_all_by_id(
                [(source, media_type, item.id) for item, _ in choices],
            )

        else:
            from simple_term_menu import TerminalMenu

            menu = TerminalMenu(
                search_results.summaries(),
                preview_command=search_results.preview,
                preview_size=0.5,
                title=(
                    f"Results for {media_type} '{query}' from {source.capitalize()}\n"
                    "SPACE - select, ENTER - download, ESC - exit"
                ),
                cycle_cursor=True,
                clear_screen=True,
                multi_select=True,
            )
            chosen_ind = menu.show()
            if chosen_ind is None:
                console.print("[yellow]No items chosen. Exiting.")
            else:
                choices = search_results.get_choices(chosen_ind)
                await self.add_all_by_id(
                    [(source, item.media_type(), item.id) for item in choices],
                )

    async def search_take_first(self, source: str, media_type: str, query: str):
        client = await self.get_logged_in_client(source)
        with console.status(f"[bold]Searching {source}", spinner="dots"):
            pages = await client.search(media_type, query, limit=1)

        if len(pages) == 0:
            console.print(f"[red]No search results found for query {query}")
            return

        search_results = SearchResults.from_pages(source, media_type, pages)
        assert len(search_results.results) > 0
        first = search_results.results[0]
        await self.add_by_id(source, first.media_type(), first.id)

    async def search_output_file(
        self, source: str, media_type: str, query: str, filepath: str, limit: int
    ):
        client = await self.get_logged_in_client(source)
        with console.status(f"[bold]Searching {source}", spinner="dots"):
            pages = await client.search(media_type, query, limit=limit)

        if len(pages) == 0:
            console.print(f"[red]No search results found for query {query}")
            return

        search_results = SearchResults.from_pages(source, media_type, pages)
        file_contents = json.dumps(search_results.as_list(source), indent=4)
        async with aiofiles.open(filepath, "w") as f:
            await f.write(file_contents)

        console.print(
            f"Wrote [purple]{len(search_results.results)}[/purple] results to [cyan]{filepath} as JSON!"
        )

    async def resolve_lastfm(self, playlist_url: str):
        """Resolve a last.fm playlist."""
        c = self.config.session.lastfm
        client = await self.get_logged_in_client(c.source)

        if len(c.fallback_source) > 0:
            fallback_client = await self.get_logged_in_client(c.fallback_source)
        else:
            fallback_client = None

        pending_playlist = PendingLastfmPlaylist(
            playlist_url,
            client,
            fallback_client,
            self.config,
            self.database,
        )
        playlist = await pending_playlist.resolve()

        if playlist is not None:
            self.media.append(playlist)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # Ensure all client sessions are closed
        for client in self.clients.values():
            if hasattr(client, "session"):
                await client.session.close()

        # close global progress bar manager
        clear_progress()
        # We remove artwork tempdirs here because multiple singles
        # may be able to share downloaded artwork in the same `rip` session
        # We don't know that a cover will not be used again until end of execution
        remove_artwork_tempdirs()
