"""Thumbnail loader: decodes in a thread pool, keeps a bounded JPEG-compressed cache in memory."""
from __future__ import annotations

import io
import os
import threading
from collections import OrderedDict

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QImage

from .groups import Group

THUMB = 560          # long edge of preloaded thumbnails
CACHE_MAX = 12000    # thumbnails kept in memory (JPEG-compressed, ~40 KB each)


class _Load(QRunnable):
    def __init__(self, cache: "Thumbs", path: str, size: int):
        super().__init__()
        self.cache, self.path, self.size = cache, path, size

    def run(self):
        from PIL import Image, ImageOps
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass
        data = None
        try:
            with Image.open(self.path) as img:
                img.draft("RGB", (self.size, self.size))
                img = ImageOps.exif_transpose(img.convert("RGB"))
                img.thumbnail((self.size, self.size))
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=88)
                data = buf.getvalue()
        except Exception:
            data = b""
        self.cache._done(self.path, self.size, data)


class Thumbs(QObject):
    ready = Signal(str)

    def __init__(self):
        super().__init__()
        self.mem: OrderedDict[tuple, bytes] = OrderedDict()
        self.busy: set[tuple] = set()
        self.lock = threading.Lock()
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(max(4, (os.cpu_count() or 8) - 2))

    def get(self, path: str, size: int = THUMB) -> QImage | None:
        with self.lock:
            data = self.mem.get((path, size))
            if data is not None:
                self.mem.move_to_end((path, size))
        if data is None:
            self.request(path, size, priority=10)
            return None
        img = QImage.fromData(data)
        return img if not img.isNull() else QImage()

    def request(self, path: str, size: int = THUMB, priority: int = 0):
        k = (path, size)
        with self.lock:
            if k in self.mem or k in self.busy:
                return
            self.busy.add(k)
        self.pool.start(_Load(self, path, size), priority)

    def _done(self, path, size, data):
        with self.lock:
            self.busy.discard((path, size))
            self.mem[(path, size)] = data
            while len(self.mem) > CACHE_MAX:
                self.mem.popitem(last=False)
        try:
            self.ready.emit(path)
        except RuntimeError:          # window already closed while a thumbnail was loading
            pass

    def preload(self, groups: list[Group], start: int):
        """Everything from the current group on, nearest first."""
        for gi, g in enumerate(groups[start:] + groups[:start]):
            for p in g.photos:
                self.request(p.path, THUMB, priority=max(0, 5 - gi))
