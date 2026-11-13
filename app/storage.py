"""File-based storage for games and their version history.

Layout on disk (inside the container this is /data, on the host ./data):

    games/<slug>/meta.json            game name + list of kept versions
    games/<slug>/versions/000001.html one file per uploaded version
    .lock                             lock file used to serialise writes

Uploaded files are never stored under a user-supplied name: the slug is
validated against a strict pattern and version file names are generated
by the server, so path traversal is impossible.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
MAX_NAME_LEN = 100
MAX_NOTE_LEN = 200


class StorageError(ValueError):
    """Raised for invalid input; the message is safe to show to the admin."""


@dataclass
class Version:
    number: int
    uploaded_at: str
    size: int
    sha256: str
    original_filename: str = ""
    note: str = ""


@dataclass
class Game:
    slug: str
    name: str
    created_at: str
    next_version: int = 1
    versions: list[Version] = field(default_factory=list)

    @property
    def latest(self) -> Version | None:
        return self.versions[-1] if self.versions else None


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:64].strip("-")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _clean_text(value: str, max_len: int) -> str:
    # Strip control characters; templates escape everything else on output.
    value = "".join(ch for ch in value if ch.isprintable())
    return value.strip()[:max_len]


def validate_html(data: bytes, max_bytes: int) -> None:
    if not data:
        raise StorageError("The uploaded file is empty.")
    if len(data) > max_bytes:
        raise StorageError(f"File is larger than the {max_bytes // 1024} KB limit.")
    if b"\x00" in data:
        raise StorageError("File contains binary data; only a text HTML file is allowed.")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise StorageError("File must be UTF-8 encoded text (an .html file).")


class Storage:
    def __init__(self, root: str | os.PathLike, keep_versions: int = 50):
        self.root = Path(root).resolve()
        self.games_dir = self.root / "games"
        self.keep_versions = keep_versions
        self.games_dir.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.root / ".lock"

    # ----- helpers -------------------------------------------------------
    @contextmanager
    def _locked(self):
        with open(self._lock_path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _game_dir(self, slug: str) -> Path:
        if not SLUG_RE.fullmatch(slug or ""):
            raise StorageError("Invalid game id.")
        path = (self.games_dir / slug).resolve()
        if path.parent != self.games_dir:  # defence in depth
            raise StorageError("Invalid game id.")
        return path

    @staticmethod
    def _version_file(game_dir: Path, number: int) -> Path:
        return game_dir / "versions" / f"{int(number):06d}.html"

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o640)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def _save_meta(self, game: Game) -> None:
        payload = {
            "slug": game.slug,
            "name": game.name,
            "created_at": game.created_at,
            "next_version": game.next_version,
            "versions": [v.__dict__ for v in game.versions],
        }
        data = json.dumps(payload, indent=2).encode("utf-8")
        self._atomic_write(self._game_dir(game.slug) / "meta.json", data)

    def _load(self, slug: str) -> Game | None:
        meta = self._game_dir(slug) / "meta.json"
        try:
            raw = json.loads(meta.read_text("utf-8"))
        except FileNotFoundError:
            return None
        return Game(
            slug=raw["slug"],
            name=raw["name"],
            created_at=raw["created_at"],
            next_version=raw.get("next_version", 1),
            versions=[Version(**v) for v in raw.get("versions", [])],
        )

    # ----- read API ------------------------------------------------------
    def get(self, slug: str) -> Game | None:
        try:
            return self._load(slug)
        except StorageError:
            return None

    def list_games(self) -> list[Game]:
        games = []
        for entry in self.games_dir.iterdir():
            if entry.is_dir() and SLUG_RE.fullmatch(entry.name):
                game = self._load(entry.name)
                if game is not None:
                    games.append(game)
        games.sort(key=lambda g: (g.name.casefold(), g.slug))
        return games

    def version_path(self, slug: str, number: int) -> Path | None:
        game = self.get(slug)
        if game is None or not any(v.number == number for v in game.versions):
            return None
        path = self._version_file(self._game_dir(slug), number)
        return path if path.is_file() else None

    def latest_path(self, slug: str) -> Path | None:
        game = self.get(slug)
        if game is None or game.latest is None:
            return None
        return self.version_path(slug, game.latest.number)

    # ----- write API -----------------------------------------------------
    def create_game(self, name: str, slug: str) -> Game:
        name = _clean_text(name, MAX_NAME_LEN)
        if not name:
            raise StorageError("Game name is required.")
        slug = (slug or "").strip().lower() or slugify(name)
        if not SLUG_RE.fullmatch(slug):
            raise StorageError(
                "Game id must be 1-64 characters of lowercase letters, numbers "
                "and dashes, and cannot start or end with a dash."
            )
        with self._locked():
            game_dir = self._game_dir(slug)
            if game_dir.exists():
                raise StorageError(f"A game with id '{slug}' already exists.")
            (game_dir / "versions").mkdir(parents=True)
            game = Game(slug=slug, name=name, created_at=_now())
            self._save_meta(game)
            return game

    def rename_game(self, slug: str, name: str) -> Game:
        name = _clean_text(name, MAX_NAME_LEN)
        if not name:
            raise StorageError("Game name is required.")
        with self._locked():
            game = self._load(slug)
            if game is None:
                raise StorageError("Game not found.")
            game.name = name
            self._save_meta(game)
            return game

    def add_version(
        self, slug: str, data: bytes, max_bytes: int, original_filename: str = "", note: str = ""
    ) -> Version:
        validate_html(data, max_bytes)
        with self._locked():
            game = self._load(slug)
            if game is None:
                raise StorageError("Game not found.")
            game_dir = self._game_dir(slug)
            version = Version(
                number=game.next_version,
                uploaded_at=_now(),
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                original_filename=_clean_text(os.path.basename(original_filename or ""), 100),
                note=_clean_text(note or "", MAX_NOTE_LEN),
            )
            self._atomic_write(self._version_file(game_dir, version.number), data)
            game.next_version += 1
            game.versions.append(version)

            # Keep only the most recent N versions.
            removed = game.versions[: -self.keep_versions]
            game.versions = game.versions[-self.keep_versions :]
            self._save_meta(game)
            for old in removed:
                try:
                    self._version_file(game_dir, old.number).unlink()
                except FileNotFoundError:
                    pass
            return version

    def restore_version(self, slug: str, number: int, max_bytes: int) -> Version:
        path = self.version_path(slug, number)
        if path is None:
            raise StorageError("Version not found.")
        return self.add_version(
            slug, path.read_bytes(), max_bytes, note=f"Restored from version {number}"
        )

    def delete_game(self, slug: str) -> None:
        with self._locked():
            game_dir = self._game_dir(slug)
            if not game_dir.is_dir():
                raise StorageError("Game not found.")
            shutil.rmtree(game_dir)
