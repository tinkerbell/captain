"""Terminal progress for transfers and slow steps; silent when not on a terminal.

Non-terminal output (CI, pipes) gets no bars or spinners — callers log a line
before each transfer, and that is the record there.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Generator
from typing import BinaryIO

from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from captain import console

Advance = Callable[[int], None]


@contextlib.contextmanager
def transfer(description: str, total: int | None) -> Generator[Advance]:
    """A byte-transfer bar; yields a callback to report each chunk's size."""
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        disable=not console.is_terminal,
    ) as progress:
        task = progress.add_task(description, total=total or None)

        def advance(n: int) -> None:
            progress.update(task, advance=n)

        yield advance


@contextlib.contextmanager
def status(message: str) -> Generator[None]:
    """A spinner for a slow step with no measurable progress."""
    if not console.is_terminal:
        yield
        return
    with console.status(message):
        yield


class ProgressReader:
    """A read-only file wrapper reporting bytes read, eg. for streaming uploads.

    Seeking back to the start (a retried request) rewinds the reported count.
    """

    def __init__(self, f: BinaryIO, advance: Advance):
        self._f, self._advance, self._done = f, advance, 0

    def read(self, size: int = -1) -> bytes:
        data = self._f.read(size)
        self._done += len(data)
        self._advance(len(data))
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        pos = self._f.seek(offset, whence)
        self._advance(pos - self._done)
        self._done = pos
        return pos

    def tell(self) -> int:
        return self._f.tell()
