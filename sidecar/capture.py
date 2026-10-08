"""
Захват камеры в отдельном потоке с «всегда свежим» кадром.

Зачем поток: cv2.VideoCapture.read() блокирующий, а внутренний буфер драйвера
копит кадры. Если читать его из того же цикла, где работают детекторы, лаг
растёт неограниченно — к концу сессии проктор смотрит в прошлое. Поэтому:
поток-читатель забирает кадры максимально быстро и держит только ПОСЛЕДНИЙ,
старые выбрасываются. Потребитель получает самый свежий кадр и ровно один раз.

cv2 импортируется лениво: без opencv класс просто сообщает available() == False,
сайдкар работает в headless-режиме и не падает.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from collections import deque
from typing import Any, Callable

log = logging.getLogger("sidecar.capture")


def _backend(cv2_mod: Any) -> int:
    """Нативный бэкенд под платформу: на macOS AVFoundation открывается надёжнее."""
    if sys.platform == "darwin":
        return int(getattr(cv2_mod, "CAP_AVFOUNDATION", 0))
    if sys.platform == "win32":
        return int(getattr(cv2_mod, "CAP_DSHOW", 0))
    return int(getattr(cv2_mod, "CAP_ANY", 0))


class CameraCapture:
    """Поток-читатель камеры с очередью на один кадр.

    read() отдаёт кадр только если он новее предыдущего отданного: так цикл
    обработки никогда не считает один и тот же кадр дважды и естественно
    синхронизируется с реальным FPS камеры.
    """

    def __init__(
        self,
        index: int = 0,
        width: int = 640,
        height: int = 480,
        fps: int = 15,
        reopen_delay: float = 1.5,
        max_read_failures: int = 15,
        read_timeout: float = 1.0,
        on_state_change: Callable[[bool, str], None] | None = None,
    ) -> None:
        self.index = int(index)
        self.width = int(width)
        self.height = int(height)
        self.requested_fps = int(fps)
        self.reopen_delay = float(reopen_delay)
        self.max_read_failures = int(max_read_failures)
        self.read_timeout = float(read_timeout)
        self.on_state_change = on_state_change

        self._cv2: Any | None = None
        self._cap: Any | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._cond = threading.Condition()

        self._frame: Any | None = None
        self._frame_ts: float = 0.0
        self._seq: int = 0
        self._consumed_seq: int = 0

        self._ts_window: deque[float] = deque(maxlen=30)
        #: Насколько старый последний кадр ещё считается «поток идёт».
        #: Минимум 1.5 с: на 15 к/с это больше двадцати пропущенных кадров.
        self._stale_after = max(1.5, 4.0 / max(self.requested_fps, 1))
        self._opened = False
        self._last_error: str = ""
        self.frames_total = 0
        self.frames_dropped = 0
        self.reopen_count = 0

    # ------------------------------------------------------------------ сервис
    def _import_cv2(self) -> Any | None:
        if self._cv2 is not None:
            return self._cv2
        try:  # тяжёлый импорт — лениво и под try
            import cv2  # type: ignore
        except Exception as exc:
            self._last_error = f"opencv недоступен: {exc}"
            log.warning("opencv недоступен, захват камеры отключён: %s", exc)
            return None
        self._cv2 = cv2
        return cv2

    def available(self) -> bool:
        """Можно ли вообще работать с камерой (есть opencv и поток жив)."""
        if self._cv2 is None and self._import_cv2() is None:
            return False
        return True

    @property
    def opened(self) -> bool:
        return self._opened

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def fps(self) -> float:
        """Фактический FPS — скользящее среднее по последним кадрам.

        Если кадры перестали приходить (камеру выдернули, виртуальная камера
        встала), окно таймстемпов остаётся заполненным старыми значениями и
        среднее по нему врёт. Поэтому устаревшее окно даёт честный 0: HUD и
        предполётная проверка не должны показывать живой поток на мёртвой камере.
        """
        with self._cond:
            if len(self._ts_window) < 2:
                return 0.0
            last = self._ts_window[-1]
            span = last - self._ts_window[0]
            if span <= 0:
                return 0.0
            if time.time() - last > self._stale_after:
                return 0.0
            return round((len(self._ts_window) - 1) / span, 2)

    # -------------------------------------------------------------- жизненный цикл
    def _prime_authorization(self) -> None:
        """
        Разовое открытие камеры на ВЫЗЫВАЮЩЕМ потоке — только macOS.

        Зачем: AVFoundation выдаёт разрешение на камеру лишь тогда, когда запрос
        идёт с главного потока. Наш рабочий поток его сделать не может, OpenCV
        прямо сообщает: «can not spin main run loop from other thread». Если первое
        открытие камеры произойдёт в потоке, системный диалог не появится НИКОГДА,
        и канал зрения останется пустым без единой внятной ошибки — ровно это
        и случилось 07.10 при первом живом запуске.

        Поэтому start() сначала открывает устройство здесь, на главном потоке:
        запрос уходит системе, пользователь отвечает, разрешение запоминается.
        Дальше рабочий поток открывает камеру уже беспрепятственно.

        Не бросает исключений и ничего не ломает: это подготовка, а не проверка.
        """
        if sys.platform != "darwin":
            return
        cv2 = self._cv2
        if cv2 is None:
            return
        try:
            cap = cv2.VideoCapture(self.index)
            opened = cap.isOpened()
            cap.release()
        except Exception as exc:
            self._last_error = f"подготовка доступа к камере: {exc}"
            return
        if opened:
            return
        # Разрешения нет. Сообщаем так, чтобы человек понял, что делать.
        self._last_error = (
            f"камера {self.index} недоступна: нет разрешения у приложения, "
            "запустившего сайдкар. Системные настройки -> Конфиденциальность "
            "и безопасность -> Камера -> включить это приложение. "
            "Если приложения нет в списке, запустите сайдкар из Terminal.app "
            "(scripts/run-sidecar-terminal.command) — система спросит доступ."
        )

    def start(self) -> bool:
        """Запустить поток чтения. False — opencv нет, работаем без камеры."""
        if self._thread is not None and self._thread.is_alive():
            return True
        if self._import_cv2() is None:
            return False
        # Запрос разрешения обязан уйти с главного потока, см. _prime_authorization.
        self._prime_authorization()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="camera", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """Корректно остановить поток и освободить устройство."""
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        self._thread = None
        self._release()

    def _release(self) -> None:
        cap, self._cap = self._cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        self._opened = False

    def _open(self) -> bool:
        cv2 = self._cv2
        if cv2 is None:
            return False
        self._release()
        try:
            backend = _backend(cv2)
            cap = cv2.VideoCapture(self.index, backend) if backend else cv2.VideoCapture(self.index)
            if not cap.isOpened():
                cap.release()
                # вторая попытка без указания бэкенда — некоторые сборки капризны
                cap = cv2.VideoCapture(self.index)
            if not cap.isOpened():
                cap.release()
                self._last_error = f"камера {self.index} не открывается"
                return False
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            if self.requested_fps > 0:
                cap.set(cv2.CAP_PROP_FPS, self.requested_fps)
            # буфер в один кадр: нам нужна свежесть, а не полнота
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            self._cap = cap
            self._opened = True
            self._last_error = ""
            return True
        except Exception as exc:
            self._last_error = f"ошибка открытия камеры: {exc}"
            self._opened = False
            return False

    # ------------------------------------------------------------------- поток
    def _loop(self) -> None:
        failures = 0
        notified_lost = False
        min_period = 1.0 / max(self.requested_fps, 1) * 0.5  # не крутим CPU быстрее нужного

        while not self._stop.is_set():
            if not self._opened:
                if not self._open():
                    if not notified_lost:
                        self._notify(False, self._last_error or "камера недоступна")
                        notified_lost = True
                    self.reopen_count += 1
                    self._stop.wait(self.reopen_delay)
                    continue
                failures = 0
                if notified_lost:
                    self._notify(True, "камера снова доступна")
                    notified_lost = False
                log.info("камера %s открыта (%sx%s)", self.index, self.width, self.height)

            t0 = time.time()
            ok, frame = False, None
            try:
                ok, frame = self._cap.read()  # type: ignore[union-attr]
            except Exception as exc:
                self._last_error = f"ошибка чтения кадра: {exc}"
                ok = False

            if not ok or frame is None:
                failures += 1
                if failures >= self.max_read_failures:
                    log.warning("камера %s потеряна, переоткрываю", self.index)
                    self._last_error = "камера перестала отдавать кадры"
                    self._release()
                    failures = 0
                    if not notified_lost:
                        self._notify(False, self._last_error)
                        notified_lost = True
                    self._stop.wait(self.reopen_delay)
                else:
                    self._stop.wait(0.02)
                continue

            failures = 0
            ts = time.time()
            with self._cond:
                if self._seq != self._consumed_seq:
                    # предыдущий кадр так и не забрали — выбрасываем его
                    self.frames_dropped += 1
                self._frame = frame
                self._frame_ts = ts
                self._seq += 1
                self.frames_total += 1
                self._ts_window.append(ts)
                self._cond.notify_all()

            elapsed = time.time() - t0
            if elapsed < min_period:
                self._stop.wait(min_period - elapsed)

        self._release()

    def _notify(self, ok: bool, message: str) -> None:
        cb = self.on_state_change
        if cb is None:
            return
        try:
            cb(ok, message)
        except Exception:  # колбэк потребителя не должен ронять поток камеры
            log.exception("ошибка в колбэке состояния камеры")

    # --------------------------------------------------------------- потребление
    def read(self, timeout: float | None = None) -> tuple[bool, Any | None, float]:
        """Забрать самый свежий кадр.

        Блокируется до появления НОВОГО кадра либо таймаута.
        Возврат: (ok, frame_bgr, ts). (False, None, 0.0) — свежего кадра нет.
        """
        wait = self.read_timeout if timeout is None else timeout
        deadline = time.time() + max(wait, 0.0)
        with self._cond:
            while self._seq == self._consumed_seq and not self._stop.is_set():
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False, None, 0.0
                self._cond.wait(remaining)
            if self._seq == self._consumed_seq:
                return False, None, 0.0
            self._consumed_seq = self._seq
            return True, self._frame, self._frame_ts

    def peek(self) -> tuple[Any | None, float]:
        """Последний кадр без отметки «потреблён» — для снапшотов по команде."""
        with self._cond:
            return self._frame, self._frame_ts

    def status(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "opened": self._opened,
            "fps": self.fps,
            "width": self.width,
            "height": self.height,
            "frames": self.frames_total,
            "dropped": self.frames_dropped,
            "reopens": self.reopen_count,
            "error": self._last_error,
        }

    # ------------------------------------------------------------------ утилиты
    @staticmethod
    def list_cameras(max_index: int = 5) -> list[dict[str, Any]]:
        """Перебрать индексы и вернуть те, что реально отдают кадр.

        Вызывается только по запросу (--list-cameras): на macOS открытие
        камеры может поднимать системный запрос доступа.
        """
        try:
            import cv2  # type: ignore
        except Exception:
            return []
        found: list[dict[str, Any]] = []
        for idx in range(max(max_index, 0) + 1):
            cap = None
            try:
                backend = _backend(cv2)
                cap = cv2.VideoCapture(idx, backend) if backend else cv2.VideoCapture(idx)
                if not cap.isOpened():
                    continue
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                h, w = frame.shape[:2]
                found.append({"index": idx, "width": int(w), "height": int(h)})
            except Exception:
                continue
            finally:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
        return found


__all__ = ["CameraCapture"]
