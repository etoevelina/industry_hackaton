"""
Запись доказательств: кольцевой буфер кадров, снимки и клипы вокруг инцидента.

Идея. Инцидент фиксируется постфактум (окно подтверждения в EventEngine — от
0.6 до 3 с), поэтому «кадр момента» уже уехал. Чтобы доказательство было
осмысленным, последние N секунд видео постоянно держатся в памяти в виде
JPEG-кадров, а на диск попадает только то, что окружает подтверждённый
инцидент: кроп, полный кадр с рамкой и клип «до/после».

Клип нельзя собрать в момент события: «после» ещё не произошло. Поэтому
`save_clip()` только регистрирует отложенную задачу: кадры «до» снимаются из
буфера сразу (bytes неизменяемы, копирования нет), кадры «после» добираются
из `push()` по мере поступления, а запись файла уходит в фоновый поток, чтобы
не тормозить конвейер камеры.

Контроль памяти. Буфер ограничен и по времени (`buffer_seconds`), и по объёму
(`max_memory_mb`); кадр перед упаковкой уменьшается до `buffer_max_width`.
Переполнение вытесняет самые старые кадры — буфер никогда не растёт бесконечно,
даже если диск или кодек отвалились.

ПОЛИТИКА ХРАНЕНИЯ ПЕРСОНАЛЬНЫХ ДАННЫХ
-------------------------------------
1. Видео целиком не записывается нигде: кадры живут только в оперативном
   кольцевом буфере и стираются при `close()`. На диск попадают исключительно
   фрагменты вокруг зафиксированных инцидентов, каждый из которых привязан к
   записи hash-chain (см. `storage/db.py`).
2. Кроп делается только по bbox предмета-повода (телефон, книга, монитор).
   Отдельные кропы лиц не сохраняются никогда — ни лица студента, ни тем
   более посторонних: для инцидентов `SECOND_FACE` / `IDENTITY_MISMATCH`
   сохраняется только полный кадр, он и является доказательством «в комнате
   есть второй человек».
3. Лица людей, не являющихся субъектом экзамена, размываются на сохраняемом
   кадре, если вызывающая сторона передала их регионы в `blur_regions`
   (`EventEngine` знает bbox лишних лиц). Это осознанный компромисс: факт
   присутствия второго человека фиксируется, его биометрия — нет.
4. Эмбеддинги лиц и голосов третьих лиц не вычисляются и не сохраняются.
   Эталон хранится только для самого студента и только на время сессии.
5. Все файлы лежат внутри каталога сессии. Наружу ничего не отправляется.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

#: Значения по умолчанию, если в конфиге ничего не передали.
DEFAULT_BUFFER_SECONDS = 20.0
DEFAULT_BUFFER_MB = 96.0
DEFAULT_FPS = 15.0
DEFAULT_JPEG_QUALITY = 85
DEFAULT_BUFFER_MAX_WIDTH = 640
DEFAULT_CLIP_SECONDS = 15.0
DEFAULT_MAX_PENDING = 8


def _pick(config: dict[str, Any], section: str, *keys: str, default: Any = None) -> Any:
    """Достать значение по схеме config[section][key] -> config[key] -> default."""
    node = config.get(section) if isinstance(config.get(section), dict) else {}
    for key in keys:
        if isinstance(node, dict) and key in node and node[key] is not None:
            return node[key]
    for key in keys:
        if key in config and config[key] is not None:
            return config[key]
    return default


def _f(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class _BufferedFrame:
    """Упакованный кадр буфера. jpeg — неизменяемые байты, копий не делаем."""
    ts: float
    jpeg: bytes
    width: int
    height: int

    @property
    def nbytes(self) -> int:
        return len(self.jpeg)


@dataclass
class _PendingClip:
    """Отложенная дозапись клипа: «до» уже есть, «после» ещё набирается."""
    event_id: str
    out_path: Path
    rel_path: str
    event_ts: float
    deadline: float
    pre: list[_BufferedFrame]
    post: list[_BufferedFrame] = field(default_factory=list)
    max_post: int = 600
    done: bool = False

    def ready(self, now: float) -> bool:
        return now >= self.deadline or len(self.post) >= self.max_post


class EvidenceRecorder:
    """Кольцевой буфер кадров + сохранение снимков и клипов.

    Использование из конвейера камеры::

        rec = EvidenceRecorder(config)
        rec.set_session_dir(session.dir)
        ...
        rec.push(frame)                         # каждый кадр
        rec.save_snapshot(frame, bbox, path)    # на подтверждённом инциденте
        rec.save_clip(event.id)                 # вернёт путь, файл появится позже
        ...
        rec.close()                             # дописать всё и очистить буфер
    """

    def __init__(self, config: dict[str, Any] | None = None,
                 session_dir: str | Path | None = None) -> None:
        cfg = dict(config or {})
        self._cfg = cfg

        self.buffer_seconds = max(_f(_pick(cfg, "storage", "buffer_seconds",
                                           "evidence_buffer_seconds",
                                           default=DEFAULT_BUFFER_SECONDS),
                                     DEFAULT_BUFFER_SECONDS), 1.0)
        self.max_bytes = int(max(_f(_pick(cfg, "storage", "buffer_mb", "max_memory_mb",
                                          "evidence_buffer_mb",
                                          default=DEFAULT_BUFFER_MB),
                                    DEFAULT_BUFFER_MB), 4.0) * 1024 * 1024)
        self.fps = max(_f(_pick(cfg, "capture", "fps", "target_fps", default=DEFAULT_FPS),
                          DEFAULT_FPS), 1.0)
        self.jpeg_quality = min(max(_i(_pick(cfg, "storage", "jpeg_quality",
                                             "evidence_jpeg_quality",
                                             default=DEFAULT_JPEG_QUALITY),
                                       DEFAULT_JPEG_QUALITY), 40), 95)
        self.buffer_max_width = max(_i(_pick(cfg, "storage", "buffer_max_width",
                                             "evidence_buffer_max_width",
                                             default=DEFAULT_BUFFER_MAX_WIDTH),
                                       DEFAULT_BUFFER_MAX_WIDTH), 160)
        self.clip_seconds = max(_f(_pick(cfg, "storage", "clip_seconds",
                                         "evidence_clip_seconds",
                                         default=DEFAULT_CLIP_SECONDS),
                                   DEFAULT_CLIP_SECONDS), 2.0)
        self.evidence_subdir = str(_pick(cfg, "storage", "evidence_subdir",
                                         default="evidence") or "evidence")
        self.max_pending = max(_i(_pick(cfg, "storage", "max_pending_clips",
                                        default=DEFAULT_MAX_PENDING),
                                  DEFAULT_MAX_PENDING), 1)

        self._lock = threading.RLock()
        self._frames: list[_BufferedFrame] = []
        self._bytes = 0
        self._dropped = 0
        self._pending: list[_PendingClip] = []
        self._session_dir: Path | None = None
        self._errors: list[str] = []
        self._written: list[dict[str, Any]] = []

        # фоновая запись файлов, чтобы не блокировать поток камеры
        self._jobs: "queue.Queue[_PendingClip | None]" = queue.Queue(maxsize=32)
        self._writer: threading.Thread | None = None
        self._stop = threading.Event()

        self._cv: Any = None
        self._np: Any = None
        self._cv_tried = False

        if session_dir is not None:
            self.set_session_dir(session_dir)

    # ----------------------------------------------------------- зависимости
    def _deps(self) -> tuple[Any, Any]:
        """Ленивый импорт cv2/numpy: без них модуль деградирует, но не падает."""
        if self._cv_tried:
            return self._cv, self._np
        self._cv_tried = True
        try:
            import cv2  # type: ignore
            import numpy  # type: ignore
            self._cv, self._np = cv2, numpy
        except Exception as exc:
            self._cv, self._np = None, None
            self._note_error(f"кадры не записываются: нет opencv/numpy ({exc})")
        return self._cv, self._np

    def available(self) -> bool:
        """True, если кадры вообще можно упаковать и записать."""
        cv, np = self._deps()
        return cv is not None and np is not None

    # --------------------------------------------------------------- сессия
    def set_session_dir(self, session_dir: str | Path | None) -> None:
        """Переключить каталог сессии. Старый буфер сбрасывается."""
        with self._lock:
            self.flush(wait=True)
            self._frames.clear()
            self._bytes = 0
            self._session_dir = Path(str(session_dir)) if session_dir else None
            if self._session_dir is not None:
                try:
                    (self._session_dir / self.evidence_subdir).mkdir(parents=True, exist_ok=True)
                except Exception as exc:
                    self._note_error(f"каталог доказательств недоступен: {exc}")

    @property
    def session_dir(self) -> Path | None:
        return self._session_dir

    # --------------------------------------------------------------- буфер
    def push(self, frame_bgr: Any, ts: float | None = None) -> None:
        """Положить кадр в буфер и добрать «после» для отложенных клипов."""
        if frame_bgr is None:
            return
        now = time.time() if ts is None else float(ts)
        packed = self._pack(frame_bgr, now)
        if packed is None:
            return
        with self._lock:
            self._frames.append(packed)
            self._bytes += packed.nbytes
            self._trim(now)
            for clip in self._pending:
                if not clip.done and len(clip.post) < clip.max_post and now >= clip.event_ts:
                    clip.post.append(packed)
            self._promote_ready(now)

    def tick(self, now: float | None = None) -> None:
        """Вызывать из основного цикла: отправляет дозревшие клипы на запись.

        Нужен, если кадры перестали приходить (камера отвалилась) — иначе клип
        ждал бы «после», которого уже не будет.
        """
        with self._lock:
            self._promote_ready(time.time() if now is None else float(now))

    def _pack(self, frame_bgr: Any, ts: float) -> _BufferedFrame | None:
        cv, _np = self._deps()
        if cv is None:
            return None
        try:
            h, w = int(frame_bgr.shape[0]), int(frame_bgr.shape[1])
            small = frame_bgr
            if w > self.buffer_max_width:
                scale = self.buffer_max_width / float(w)
                small = cv.resize(frame_bgr, (self.buffer_max_width, max(int(h * scale), 2)),
                                  interpolation=cv.INTER_AREA)
                h, w = int(small.shape[0]), int(small.shape[1])
            ok, buf = cv.imencode(".jpg", small,
                                  [int(cv.IMWRITE_JPEG_QUALITY), int(self.jpeg_quality)])
            if not ok:
                return None
            return _BufferedFrame(ts=ts, jpeg=bytes(buf.tobytes()), width=w, height=h)
        except Exception as exc:
            self._note_error(f"кадр не упакован: {exc}")
            return None

    def _trim(self, now: float) -> None:
        """Вытеснить старое: сначала по времени, потом по объёму памяти."""
        horizon = now - self.buffer_seconds
        idx = 0
        for idx, item in enumerate(self._frames):
            if item.ts >= horizon:
                break
        else:
            idx = len(self._frames)
        if idx:
            for item in self._frames[:idx]:
                self._bytes -= item.nbytes
            del self._frames[:idx]
            self._dropped += idx
        while self._bytes > self.max_bytes and len(self._frames) > 1:
            item = self._frames.pop(0)
            self._bytes -= item.nbytes
            self._dropped += 1

    def stats(self) -> dict[str, Any]:
        with self._lock:
            span = 0.0
            if len(self._frames) >= 2:
                span = round(self._frames[-1].ts - self._frames[0].ts, 2)
            return {
                "frames": len(self._frames),
                "bytes": self._bytes,
                "mb": round(self._bytes / (1024 * 1024), 2),
                "limit_mb": round(self.max_bytes / (1024 * 1024), 2),
                "span_sec": span,
                "dropped": self._dropped,
                "pending_clips": sum(1 for c in self._pending if not c.done),
                "written": len(self._written),
                "errors": list(self._errors[-5:]),
                "available": self.available(),
            }

    # ------------------------------------------------------------- снимки
    def save_snapshot(self, frame_bgr: Any, bbox: Any, out_path: str | Path,
                      label: str = "", blur_regions: list[Any] | None = None,
                      ts: float | None = None) -> dict[str, Any] | None:
        """Сохранить доказательство-кадр: полный кадр с рамкой + кроп по bbox.

        `out_path` — путь полного кадра; кроп пишется рядом с суффиксом
        `_crop`. `blur_regions` — прямоугольники (лица посторонних), которые
        размываются перед записью (см. политику хранения в докстринге модуля).
        Возвращает словарь путей и фактический bbox либо None, если записать
        не удалось.
        """
        cv, _np = self._deps()
        if cv is None or frame_bgr is None:
            return None
        out = Path(str(out_path))
        stamp = time.time() if ts is None else float(ts)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            full = frame_bgr.copy()
            fh, fw = int(full.shape[0]), int(full.shape[1])

            for region in (blur_regions or []):
                box = self._norm_bbox(region, fw, fh)
                if box is None:
                    continue
                x, y, w, h = box
                roi = full[y:y + h, x:x + w]
                if roi.size:
                    k = max(3, (min(w, h) // 4) * 2 + 1)
                    full[y:y + h, x:x + w] = cv.GaussianBlur(roi, (k, k), 0)

            box = self._norm_bbox(bbox, fw, fh)
            crop_path: Path | None = None
            if box is not None:
                x, y, w, h = box
                pad = max(8, int(0.08 * max(w, h)))
                cx0, cy0 = max(x - pad, 0), max(y - pad, 0)
                cx1, cy1 = min(x + w + pad, fw), min(y + h + pad, fh)
                crop = full[cy0:cy1, cx0:cx1]
                if crop.size:
                    crop_path = out.with_name(f"{out.stem}_crop{out.suffix or '.jpg'}")
                    cv.imwrite(str(crop_path), crop,
                               [int(cv.IMWRITE_JPEG_QUALITY), int(self.jpeg_quality)])
                cv.rectangle(full, (x, y), (x + w, y + h), (0, 0, 255), 2)

            caption = label or ""
            text = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M:%S")
            if caption:
                text = f"{text}  {caption}"
            self._draw_caption(cv, full, text)

            cv.imwrite(str(out), full,
                       [int(cv.IMWRITE_JPEG_QUALITY), int(self.jpeg_quality)])
        except Exception as exc:
            self._note_error(f"снимок не сохранён: {exc}")
            return None

        result = {
            "frame_path": str(out),
            "crop_path": str(crop_path) if crop_path else "",
            "bbox": list(box) if box else None,
            "ts": stamp,
        }
        if self._session_dir is not None:
            result["frame_rel"] = self._rel(out)
            result["crop_rel"] = self._rel(crop_path) if crop_path else ""
        return result

    @staticmethod
    def _draw_caption(cv: Any, image: Any, text: str) -> None:
        """Метка времени поверх кадра — читаемая на любом фоне."""
        if not text:
            return
        org = (8, max(18, int(image.shape[0] * 0.05)))
        cv.putText(image, text, org, cv.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3,
                   cv.LINE_AA)
        cv.putText(image, text, org, cv.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1,
                   cv.LINE_AA)

    @staticmethod
    def _norm_bbox(bbox: Any, width: int, height: int) -> tuple[int, int, int, int] | None:
        """Привести bbox к целым пикселям внутри кадра. Поддержаны (x,y,w,h) и доли."""
        if bbox is None:
            return None
        try:
            values = [float(v) for v in list(bbox)[:4]]
        except (TypeError, ValueError):
            return None
        if len(values) < 4:
            return None
        x, y, w, h = values
        if max(values) <= 1.5:  # нормированные координаты
            x, y, w, h = x * width, y * height, w * width, h * height
        xi, yi = int(round(x)), int(round(y))
        wi, hi = int(round(w)), int(round(h))
        if wi <= 0 or hi <= 0:
            return None
        xi = min(max(xi, 0), max(width - 2, 0))
        yi = min(max(yi, 0), max(height - 2, 0))
        wi = min(wi, width - xi)
        hi = min(hi, height - yi)
        if wi <= 1 or hi <= 1:
            return None
        return xi, yi, wi, hi

    # -------------------------------------------------------------- клипы
    def save_clip(self, event_id: str, pre_sec: float | None = None,
                  post_sec: float | None = None, out_path: str | Path | None = None,
                  event_ts: float | None = None) -> str:
        """Зарегистрировать клип вокруг инцидента. Возвращает относительный путь.

        Файл появляется не сразу: часть «после» дописывается по мере прихода
        кадров, затем задача уходит в фоновый поток записи. Повторный вызов с
        тем же `event_id` возвращает путь уже запланированного клипа.
        """
        pre = self.clip_seconds * (2.0 / 3.0) if pre_sec is None else max(float(pre_sec), 0.0)
        post = self.clip_seconds * (1.0 / 3.0) if post_sec is None else max(float(post_sec), 0.0)
        now = time.time() if event_ts is None else float(event_ts)
        safe_id = "".join(ch for ch in str(event_id) if ch.isalnum() or ch in "-_")[:40] or "clip"

        with self._lock:
            for clip in self._pending:
                if clip.event_id == safe_id and not clip.done:
                    return clip.rel_path
            if self._session_dir is None:
                self._note_error("клип не записан: каталог сессии не задан")
                return ""
            if out_path is not None:
                target = Path(str(out_path))
            else:
                name = f"{int(now * 1000)}_{safe_id}.mp4"
                target = self._session_dir / self.evidence_subdir / name
            rel = self._rel(target)

            horizon = now - pre
            pre_frames = [f for f in self._frames if f.ts >= horizon]
            clip = _PendingClip(
                event_id=safe_id, out_path=target, rel_path=rel, event_ts=now,
                deadline=now + post, pre=pre_frames,
                max_post=int(max(post, 0.0) * self.fps) + 5,
            )
            self._pending.append(clip)
            # слишком много незакрытых клипов — старейший дописываем немедленно
            alive = [c for c in self._pending if not c.done]
            if len(alive) > self.max_pending:
                self._promote(alive[0])
        return rel

    def _promote_ready(self, now: float) -> None:
        for clip in list(self._pending):
            if not clip.done and clip.ready(now):
                self._promote(clip)
        self._pending = [c for c in self._pending if not c.done]

    def _promote(self, clip: _PendingClip) -> None:
        """Отправить клип в фоновую запись (под локом)."""
        clip.done = True
        self._ensure_writer()
        try:
            self._jobs.put_nowait(clip)
        except queue.Full:
            self._note_error("очередь записи клипов переполнена, пишу синхронно")
            self._write_clip(clip)

    def _ensure_writer(self) -> None:
        if self._writer is not None and self._writer.is_alive():
            return
        self._stop.clear()
        self._writer = threading.Thread(target=self._writer_loop, name="evidence-writer",
                                        daemon=True)
        self._writer.start()

    def _writer_loop(self) -> None:
        while True:
            try:
                job = self._jobs.get(timeout=0.5)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            try:
                if job is None:
                    return
                self._write_clip(job)
            finally:
                self._jobs.task_done()

    def _write_clip(self, clip: _PendingClip) -> dict[str, Any] | None:
        """Собрать mp4 (fallback — avi/MJPG) из буферизованных кадров."""
        cv, np = self._deps()
        frames = list(clip.pre) + [f for f in clip.post if f.ts > (clip.pre[-1].ts if clip.pre else 0.0)]
        if cv is None or np is None or not frames:
            self._note_error(f"клип {clip.event_id}: нет кадров для записи")
            return None
        width = max(f.width for f in frames)
        height = max(f.height for f in frames)
        span = max(frames[-1].ts - frames[0].ts, 1e-3)
        fps = min(max(len(frames) / span, 4.0), 30.0)

        attempts = ((clip.out_path, "mp4v"), (clip.out_path.with_suffix(".avi"), "MJPG"))
        for path, codec in attempts:
            writer = None
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fourcc = cv.VideoWriter_fourcc(*codec)
                writer = cv.VideoWriter(str(path), fourcc, float(fps), (width, height))
                if not writer.isOpened():
                    raise RuntimeError(f"кодек {codec} недоступен")
                for item in frames:
                    img = cv.imdecode(np.frombuffer(item.jpeg, dtype=np.uint8),
                                      cv.IMREAD_COLOR)
                    if img is None:
                        continue
                    if img.shape[0] != height or img.shape[1] != width:
                        img = cv.resize(img, (width, height), interpolation=cv.INTER_AREA)
                    writer.write(img)
                writer.release()
                writer = None
                if path.is_file() and path.stat().st_size > 0:
                    info = {
                        "event_id": clip.event_id,
                        "path": str(path),
                        "rel": self._rel(path),
                        "frames": len(frames),
                        "fps": round(fps, 2),
                        "seconds": round(span, 2),
                        "codec": codec,
                    }
                    with self._lock:
                        self._written.append(info)
                    return info
                raise RuntimeError("файл не создан")
            except Exception as exc:
                self._note_error(f"клип {clip.event_id} ({codec}): {exc}")
                if writer is not None:
                    try:
                        writer.release()
                    except Exception:
                        pass
                try:
                    if path.is_file() and path.stat().st_size == 0:
                        os.unlink(path)
                except Exception:
                    pass
        return None

    def written_clips(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._written)

    # ------------------------------------------------------------- финализация
    def flush(self, wait: bool = True, timeout: float = 10.0) -> None:
        """Дописать все отложенные клипы тем, что есть. Вызывается при закрытии сессии."""
        with self._lock:
            for clip in list(self._pending):
                if not clip.done:
                    self._promote(clip)
            self._pending = [c for c in self._pending if not c.done]
        if not wait:
            return
        deadline = time.time() + max(timeout, 0.1)
        while time.time() < deadline:
            if self._jobs.unfinished_tasks == 0:
                return
            time.sleep(0.05)

    def close(self) -> None:
        """Завершить запись, остановить фоновый поток, очистить буфер."""
        self.flush(wait=True)
        self._stop.set()
        writer = self._writer
        if writer is not None and writer.is_alive():
            try:
                self._jobs.put_nowait(None)
            except queue.Full:
                pass
            writer.join(timeout=3.0)
        self._writer = None
        with self._lock:
            self._frames.clear()
            self._bytes = 0
            self._pending.clear()

    # --------------------------------------------------------------- служебное
    def _rel(self, path: Path | None) -> str:
        """Путь относительно каталога сессии — именно такой кладётся в Evidence."""
        if path is None:
            return ""
        if self._session_dir is None:
            return str(path)
        try:
            return path.resolve().relative_to(self._session_dir.resolve()).as_posix()
        except Exception:
            return path.name

    def _note_error(self, message: str) -> None:
        with self._lock:
            self._errors.append(message)
            if len(self._errors) > 50:
                del self._errors[:-50]


__all__ = ["EvidenceRecorder"]
