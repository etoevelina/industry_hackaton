"""
Аудио-канал прокторинга: речь в кадре, чужой голос, подсказка через наушник.

КАНАЛ РЕЖИМНЫЙ: РАБОТАЕТ ТОЛЬКО В `exam_mode == "remote"` (решение Р-10)
------------------------------------------------------------------------
Это главное, что нужно знать про модуль. Кейс — ЛОКАЛЬНЫЙ прокторинг, то есть
типовая среда — компьютерный класс, а не тихая комната. В аудитории на 30 человек
анализ звука измеряет не студента, а помещение:

  * `VOICE_OTHER` срабатывает на соседа, на преподавателя и на общий гул —
    «чужой голос» в аудитории звучит непрерывно и по определению;
  * `SPEECH_WITHOUT_LIP_MOTION` устроен так, что срабатывает на ЛЮБУЮ чужую речь
    при сомкнутых губах студента. В классе это состояние — норма, а не признак
    наушника. Главный сигнал канала превращается в постоянное ложное
    срабатывание именно там, где система должна работать.

Плюс обратная сторона: в аудитории студент и сам говорить не станет, так что
ловить нечего. Сигнал не просто шумный — он ещё и пустой.

Поэтому:

  * `exam_mode: "classroom"` (значение по умолчанию) — `available()` возвращает
    False с причиной «отключён в режиме аудитории: высокий уровень ложных
    срабатываний», `start()` ничего не поднимает, микрофон НЕ захватывается
    вообще, ни одного наблюдения наружу не уходит;
  * `exam_mode: "remote"` — канал работает как спроектирован, среда тихая,
    чужой голос в комнате действительно чужой.

Код анализа при этом не удалён и не упрощён: он нужен для `remote`, и отключение
режимом — это один флаг, а не отсутствующая функциональность.

Угроза, ради которой канал заводился (У-02, «наушник со связью»), в аудитории
закрыта другим, детерминированным способом: `env_checks.check_audio_devices()`
периодически перечисляет активные устройства вывода звука и фиксирует
подключённые наушники и Bluetooth-гарнитуры. Устройство либо подключено, либо
нет — шум помещения на это не влияет, ложных нет в принципе.

ПРИВАТНОСТЬ — ЖЁСТКОЕ ТРЕБОВАНИЕ МОДУЛЯ
----------------------------------------
Аудио НЕ СОХРАНЯЕТСЯ НА ДИСК НИ ПРИ КАКИХ УСЛОВИЯХ. Ни wav, ни буферы, ни
«временные файлы для отладки». Сырые сэмплы живут только в оперативном кольцевом
буфере (по умолчанию 2 секунды) и в коротком накопителе речи (<= 1.5 с), после
расчёта признаков немедленно затираются. Наружу уходят только производные:
флаг речи, RMS, доля речевых кадров, косинусная близость к профилю голоса.
По содержанию речи восстановить ничего нельзя — распознавание слов не делается
и не предусмотрено. `stop()` обнуляет буферы явно.

Что отдаёт модуль
-----------------
`poll() -> AudioObservation`: `speech`, `is_owner` (None, если профиля нет),
`rms`, `confidence` плюс служебные поля. Событий модуль не порождает — их делает
EventEngine:
  * `speech and is_owner is False`  -> наблюдение `VOICE_OTHER`;
  * рассинхрон речи и губ           -> наблюдение `SPEECH_WITHOUT_LIP_MOTION`;
  * `available() and not device_ok`  -> `SENSOR_LOST` (микрофон отвалился).

ГЛАВНЫЙ СИГНАЛ КАНАЛА: «речь есть, губы не двигаются»
-----------------------------------------------------
Так выглядит подсказка в наушнике: студент слушает и кивает, но в комнате звучит
голос — либо его собственные тихие ответы шёпотом, либо голос помощника из
динамика/телефона. Связку делает EventEngine, сопоставляя:

    AudioObservation.speech         == True
    AudioObservation.speech_ratio   >= порога (сколько кадров окна — речь)
    AudioObservation.speech_duration>= lip_sync_min_s (речь держится, напр. 1.5 с)
    AudioObservation.rms            > rms_gate (не фоновый шум)
  и одновременно
    FaceObservation.face_count      >= 1            (студент за камерой)
    FaceObservation.mouth_open_ratio <  mouth_open_threshold (губы сомкнуты)
    усреднённая дисперсия mouth_open_ratio за то же окно близка к нулю

Сопоставлять нужно по времени: `AudioObservation.ts` и ts кадра лица, окно
совпадения ~0.5 с. Если губы двигаются (ratio пляшет), событие НЕ порождается —
студент говорит сам, это нормально для режимов с проговариванием.

Деградация
----------
* `exam_mode != "remote"` -> `available() == False`, микрофон не открывается.
* Нет sounddevice или нет микрофона -> `available() == False`, канал выключен.
* Нет webrtcvad -> VAD по адаптивной энергии + ZCR (`vad_backend == "energy"`),
  честно слабее, отражается в `confidence`.
* Нет librosa -> MFCC считаются своей реализацией на numpy (FFT + мел-банк + DCT).
* Нет numpy -> канал выключен, импорт не падает.
"""
from __future__ import annotations

import importlib
import importlib.util
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

try:  # numpy нужен всему каналу, но импорт модуля не роняем
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore[assignment]


__all__ = [
    "AudioMonitor",
    "AudioObservation",
    "DEFAULT_AUDIO_CONFIG",
    "EXAM_MODE_CLASSROOM",
    "EXAM_MODE_REMOTE",
    "CLASSROOM_DISABLED_REASON",
]


# --------------------------------------------------------------------------
# Режим развёртывания
# --------------------------------------------------------------------------
# Значения дублируют `sidecar/config.py` сознательно: детектор по контракту
# самодостаточен и получает только `config: dict`, импортировать конфиг он не
# должен. Строки короткие и зафиксированы в Р-10, расхождения не будет.
EXAM_MODE_CLASSROOM = "classroom"
EXAM_MODE_REMOTE = "remote"

#: Причина отключения, которую видит HUD и читает отчёт. Формулировка по гайду:
#: наблюдаемый факт -> контекст -> что это значит. Без обвинений и без «ошибки».
CLASSROOM_DISABLED_REASON = (
    "отключён в режиме аудитории: высокий уровень ложных срабатываний"
)


# --------------------------------------------------------------------------
# Пороги по умолчанию (все переопределяются через config["audio"])
# --------------------------------------------------------------------------
DEFAULT_AUDIO_CONFIG: dict[str, Any] = {
    "enabled": True,
    # Режим развёртывания. Канал осмыслен только в "remote"; в "classroom"
    # (значение по умолчанию) он выключен целиком — см. докстринг модуля и Р-10.
    "exam_mode": EXAM_MODE_CLASSROOM,
    "sample_rate": 16000,          # webrtcvad умеет 8/16/32/48 кГц
    "frame_ms": 30,                # webrtcvad: только 10/20/30 мс
    "device": None,                # None -> системный вход по умолчанию
    "ring_seconds": 2.0,           # сколько сырых сэмплов живёт в памяти
    "vad_aggressiveness": 2,       # 0..3, 2 — разумный компромисс для комнаты
    "speech_window_s": 1.5,        # окно усреднения решения о речи
    "speech_ratio": 0.35,          # доля речевых кадров в окне для speech=True
    "rms_gate": 0.004,             # абсолютный порог энергии (float32 -1..1)
    "noise_floor_mult": 3.5,       # энергетический VAD: порог = floor * mult
    "noise_floor_ceiling": 0.05,   # пол шума не может подняться выше этого
    "floor_window_s": 5.0,         # окно оценки пола шума (минимум энергии)
    # --- профиль голоса владельца (экспериментально) ---
    "n_mfcc": 13,                  # c1..c13, c0 (энергия) отбрасывается
    "n_mels": 26,
    "fmin": 50.0,
    "fmax": 7600.0,
    "mfcc_chunk_s": 0.5,           # длина куска речи, на котором считаем MFCC
    "owner_threshold": 0.60,       # косинус в отбелённом MFCC-пространстве
    "owner_z_max": 2.5,            # средний |z| отклонения от профиля
    "owner_window": 5,             # сколько последних речевых кусков голосуют
    "owner_min_chunks": 2,         # меньше — is_owner остаётся None
    "enroll_min_speech_s": 2.0,    # минимум речи для профиля
    "enroll_max_wait_s": 3.0,      # запас времени поверх запрошенных секунд
}


_MISSING = object()


def _container_get(container: Any, key: str) -> Any:
    if container is None:
        return _MISSING
    if isinstance(container, dict):
        return container.get(key, _MISSING)
    return getattr(container, key, _MISSING)


def _cfg(config: Any, section: str, key: str, default: Any) -> Any:
    """config[section][key] -> config[f"{section}_{key}"] -> config[key] -> default."""
    sec = _container_get(config, section)
    sec = None if sec is _MISSING else sec
    for container, name in ((sec, key), (config, f"{section}_{key}"), (config, key)):
        value = _container_get(container, name)
        if value is not _MISSING and value is not None:
            return value
    return default


# --------------------------------------------------------------------------
# Наблюдение
# --------------------------------------------------------------------------
@dataclass
class AudioObservation:
    """Наблюдение аудио-канала. Контракт: speech / is_owner / rms / confidence."""

    speech: bool = False
    is_owner: bool | None = None       # None — профиля нет или данных мало
    rms: float = 0.0
    confidence: float = 0.0
    # --- честная маркировка: сравнение голоса слабее остальных каналов ---
    experimental: bool = True
    # --- служебные поля для движка и HUD ---
    available: bool = False
    device_ok: bool = False
    enrolled: bool = False
    speech_ratio: float = 0.0          # доля речевых кадров в окне
    speech_duration: float = 0.0       # сколько секунд речь держится непрерывно
    speech_started_ts: float | None = None
    owner_similarity: float | None = None
    owner_z: float | None = None
    vad_backend: str = "none"          # "webrtcvad" | "energy" | "none"
    noise_floor: float = 0.0
    frames_analyzed: int = 0
    error: str = ""
    #: Режим, в котором снято наблюдение. В отчёте по этому полю видно, что
    #: канал молчал не из-за поломки, а по решению о режиме.
    exam_mode: str = EXAM_MODE_CLASSROOM
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "speech": self.speech,
            "is_owner": self.is_owner,
            "rms": round(float(self.rms), 5),
            "confidence": round(float(self.confidence), 3),
            "experimental": self.experimental,
            "available": self.available,
            "device_ok": self.device_ok,
            "enrolled": self.enrolled,
            "speech_ratio": round(float(self.speech_ratio), 3),
            "speech_duration": round(float(self.speech_duration), 2),
            "speech_started_ts": self.speech_started_ts,
            "owner_similarity": (
                None if self.owner_similarity is None else round(float(self.owner_similarity), 4)
            ),
            "owner_z": None if self.owner_z is None else round(float(self.owner_z), 3),
            "vad_backend": self.vad_backend,
            "noise_floor": round(float(self.noise_floor), 5),
            "frames_analyzed": self.frames_analyzed,
            "error": self.error,
            "exam_mode": self.exam_mode,
            "ts": self.ts,
        }


# --------------------------------------------------------------------------
# Кольцевой буфер сырых сэмплов (единственное место, где живёт звук)
# --------------------------------------------------------------------------
class _Ring:
    """Моно float32 кольцевой буфер фиксированной длины.

    Писатель — callback sounddevice (работает в своём потоке, должен быть быстрым).
    Читатель — рабочий поток, держит свою позицию; при отставании больше ёмкости
    буфера позиция перескакивает вперёд (переполнение фиксируется счётчиком).
    """

    def __init__(self, capacity: int) -> None:
        self._buf = np.zeros(int(capacity), dtype=np.float32)
        self._cap = int(capacity)
        self._written = 0
        self.overruns = 0
        self._lock = threading.Lock()

    def write(self, samples: Any) -> None:
        data = np.asarray(samples, dtype=np.float32).reshape(-1)
        n = data.size
        if n == 0:
            return
        if n >= self._cap:
            # блок длиннее всего буфера (при blocksize 30 мс не бывает) — берём хвост
            data = data[-self._cap :]
            n = data.size
            self.overruns += 1
        with self._lock:
            start = self._written % self._cap
            end = start + n
            if end <= self._cap:
                self._buf[start:end] = data
            else:
                head = self._cap - start
                self._buf[start:] = data[:head]
                self._buf[: end - self._cap] = data[head:]
            self._written += n

    def available(self, pos: int) -> int:
        with self._lock:
            return max(0, self._written - pos)

    def read(self, pos: int, n: int) -> tuple[Any, int]:
        """Прочитать n сэмплов с позиции pos. Возвращает (данные, новая позиция)."""
        with self._lock:
            if self._written - pos > self._cap:
                pos = self._written - self._cap  # отстали — выбрасываем старое
                self.overruns += 1
            n = min(n, self._written - pos)
            if n <= 0:
                return np.zeros(0, dtype=np.float32), pos
            start = pos % self._cap
            end = start + n
            if end <= self._cap:
                out = self._buf[start:end].copy()
            else:
                head = self._cap - start
                out = np.concatenate(
                    (self._buf[start:].copy(), self._buf[: end - self._cap].copy())
                )
            return out, pos + n

    def clear(self) -> None:
        """Явное затирание — требование приватности."""
        with self._lock:
            self._buf[:] = 0.0
            self._written = 0


# --------------------------------------------------------------------------
# MFCC: librosa, если есть; иначе своя реализация на numpy
# --------------------------------------------------------------------------
def _hz_to_mel(f: float) -> float:
    return 2595.0 * math.log10(1.0 + f / 700.0)


def _mel_to_hz(m: Any) -> Any:
    return 700.0 * (10.0 ** (m / 2595.0) - 1.0)


def _mel_filterbank(sr: int, n_fft: int, n_mels: int, fmin: float, fmax: float) -> Any:
    """Треугольный мел-банк (n_mels, n_fft//2+1). Аналог librosa.filters.mel."""
    fmax = min(fmax, sr / 2.0)
    mels = np.linspace(_hz_to_mel(fmin), _hz_to_mel(fmax), n_mels + 2)
    freqs = _mel_to_hz(mels)
    bins = np.floor((n_fft + 1) * freqs / sr).astype(np.int32)
    bins = np.clip(bins, 0, n_fft // 2)
    fb = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
    for m in range(1, n_mels + 1):
        left, center, right = int(bins[m - 1]), int(bins[m]), int(bins[m + 1])
        if right <= left:
            continue
        center = min(max(center, left + 1), right - 1)
        if center > left:
            idx = np.arange(left, center)
            fb[m - 1, left:center] = (idx - left) / float(center - left)
        if right > center:
            idx = np.arange(center, right)
            fb[m - 1, center:right] = (right - idx) / float(right - center)
    return fb


def _dct2_matrix(n_mels: int, n_out: int) -> Any:
    """Ортонормированная DCT-II (n_out, n_mels) — как scipy.fftpack.dct(norm='ortho')."""
    k = np.arange(n_out, dtype=np.float32)[:, None]
    n = np.arange(n_mels, dtype=np.float32)[None, :]
    mat = np.cos(np.pi * k * (2.0 * n + 1.0) / (2.0 * n_mels)).astype(np.float32)
    mat *= math.sqrt(2.0 / n_mels)
    mat[0] *= math.sqrt(0.5)
    return mat


class _MfccExtractor:
    """MFCC c1..cN. librosa при наличии, иначе FFT + мел-банк + DCT на numpy.

    Нулевой коэффициент (c0 — общая энергия) выбрасывается специально: он зависит
    от громкости и расстояния до микрофона, а не от голоса, и только портит
    сравнение говорящих.
    """

    def __init__(
        self,
        sr: int,
        n_mfcc: int = 13,
        n_mels: int = 26,
        fmin: float = 50.0,
        fmax: float = 7600.0,
    ) -> None:
        self.sr = int(sr)
        self.n_mfcc = int(n_mfcc)
        self.n_mels = int(n_mels)
        self.fmin = float(fmin)
        self.fmax = float(fmax)
        self.n_fft = 512
        self.win = 400          # 25 мс при 16 кГц
        self.hop = 160          # 10 мс
        self.backend = "numpy"
        self._librosa: Any = None
        self._fb: Any = None
        self._dct: Any = None
        self._window: Any = None
        if np is not None:
            try:
                if importlib.util.find_spec("librosa") is not None:
                    self._librosa = importlib.import_module("librosa")
                    self.backend = "librosa"
            except Exception:
                self._librosa = None
            if self._librosa is None:
                self._fb = _mel_filterbank(
                    self.sr, self.n_fft, self.n_mels, self.fmin, self.fmax
                )
                self._dct = _dct2_matrix(self.n_mels, self.n_mfcc + 1)
                self._window = np.hamming(self.win).astype(np.float32)

    def __call__(self, samples: Any) -> Any:
        """(n_samples,) float32 -> (n_frames, n_mfcc) float32. Пусто, если данных мало."""
        if np is None:
            return None
        y = np.asarray(samples, dtype=np.float32).reshape(-1)
        if y.size < self.win:
            return np.zeros((0, self.n_mfcc), dtype=np.float32)
        if self._librosa is not None:
            try:
                m = self._librosa.feature.mfcc(
                    y=y,
                    sr=self.sr,
                    n_mfcc=self.n_mfcc + 1,
                    n_fft=self.n_fft,
                    hop_length=self.hop,
                    win_length=self.win,
                    n_mels=self.n_mels,
                    fmin=self.fmin,
                    fmax=min(self.fmax, self.sr / 2.0),
                )
                return np.ascontiguousarray(m[1:, :].T, dtype=np.float32)
            except Exception:
                # librosa сломался — тихо уходим на свою реализацию
                self._librosa = None
                self._fb = _mel_filterbank(
                    self.sr, self.n_fft, self.n_mels, self.fmin, self.fmax
                )
                self._dct = _dct2_matrix(self.n_mels, self.n_mfcc + 1)
                self._window = np.hamming(self.win).astype(np.float32)
                self.backend = "numpy"
        # --- своя реализация ---
        # предыскажение: поднимаем ВЧ, как принято в распознавании речи
        y = np.append(y[0], y[1:] - 0.97 * y[:-1]).astype(np.float32)
        n_frames = 1 + (y.size - self.win) // self.hop
        if n_frames <= 0:
            return np.zeros((0, self.n_mfcc), dtype=np.float32)
        idx = np.arange(self.win)[None, :] + self.hop * np.arange(n_frames)[:, None]
        frames = y[idx] * self._window[None, :]
        spec = np.fft.rfft(frames, n=self.n_fft, axis=1)
        power = (np.abs(spec).astype(np.float32) ** 2) / float(self.n_fft)
        mel = power @ self._fb.T                       # (n_frames, n_mels)
        log_mel = np.log(np.maximum(mel, 1e-10)).astype(np.float32)
        cep = log_mel @ self._dct.T                    # (n_frames, n_mfcc+1)
        return np.ascontiguousarray(cep[:, 1:], dtype=np.float32)


# --------------------------------------------------------------------------
# Основной класс
# --------------------------------------------------------------------------
class AudioMonitor:
    """Мониторинг микрофона: VAD + экспериментальная проверка «это голос студента».

    Сценарий в сайдкаре:

        am = AudioMonitor(config)
        am.start()                       # поднимает поток захвата
        am.enroll_owner(5.0)             # калибровка, stage="voice"
        obs = am.poll()                  # в основном цикле, дёшево и неблокирующе
        am.stop()                        # по session_end — буферы затираются

    Поток данных: callback sounddevice -> кольцевой буфер -> рабочий поток
    (кадры 30 мс: RMS, VAD, накопление речи -> MFCC) -> агрегированное состояние.
    `poll()` только читает агрегат под локом, поэтому не тормозит видео-цикл.
    """

    def __init__(self, config: dict | None = None) -> None:
        self.config = config if config is not None else {}

        def ac(key: str) -> Any:
            return _cfg(self.config, "audio", key, DEFAULT_AUDIO_CONFIG[key])

        self.enabled = bool(ac("enabled"))

        # --- режим развёртывания: главный выключатель канала (Р-10) ---
        # Читается и как `audio.exam_mode`, и как плоский `exam_mode` — порядок
        # поиска задан `_cfg`. Всё, что не "remote", трактуется как аудитория:
        # безопасная сторона — молчащий канал, а не ложные обвинения.
        self.exam_mode = str(ac("exam_mode") or "").strip().lower()
        if self.exam_mode != EXAM_MODE_REMOTE:
            self.exam_mode = EXAM_MODE_CLASSROOM
        self.mode_allows_audio = self.exam_mode == EXAM_MODE_REMOTE
        #: Причина молчания канала. Пустая строка — канал разрешён режимом.
        self.disabled_reason = ""
        if not self.mode_allows_audio:
            configured = str(_cfg(self.config, "audio", "disabled_reason", "") or "").strip()
            self.disabled_reason = configured or CLASSROOM_DISABLED_REASON

        self.sample_rate = int(ac("sample_rate"))
        self.frame_ms = int(ac("frame_ms"))
        if self.frame_ms not in (10, 20, 30):
            self.frame_ms = 30  # ограничение webrtcvad
        self.frame_len = int(self.sample_rate * self.frame_ms / 1000)
        self.device = ac("device")
        self.ring_seconds = float(ac("ring_seconds"))
        self.vad_aggressiveness = max(0, min(3, int(ac("vad_aggressiveness"))))
        self.speech_window_s = float(ac("speech_window_s"))
        self.speech_ratio_threshold = float(ac("speech_ratio"))
        self.rms_gate = float(ac("rms_gate"))
        self.noise_floor_mult = float(ac("noise_floor_mult"))
        self.noise_floor_ceiling = float(ac("noise_floor_ceiling"))
        self.floor_window_s = float(ac("floor_window_s"))

        self.n_mfcc = int(ac("n_mfcc"))
        self.n_mels = int(ac("n_mels"))
        self.fmin = float(ac("fmin"))
        self.fmax = float(ac("fmax"))
        self.mfcc_chunk_s = float(ac("mfcc_chunk_s"))
        self.owner_threshold = float(ac("owner_threshold"))
        self.owner_z_max = float(ac("owner_z_max"))
        self.owner_window = max(1, int(ac("owner_window")))
        self.owner_min_chunks = max(1, int(ac("owner_min_chunks")))
        self.enroll_min_speech_s = float(ac("enroll_min_speech_s"))
        self.enroll_max_wait_s = float(ac("enroll_max_wait_s"))

        # --- состояние ---
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._worker: threading.Thread | None = None
        self._stream: Any = None
        self._ring: _Ring | None = None
        self._read_pos = 0
        self._probe_ok: bool | None = None
        self._started = False
        self.last_error: str = ""
        if not self.mode_allows_audio:
            # Причина известна до первого вызова available(): так её видит и лог
            # загрузки модулей, и HUD, и шапка отчёта.
            self.last_error = f"аудио-канал {self.disabled_reason}"
            self._probe_ok = False

        self._vad: Any = None
        self._vad_backend = "none"
        self._mfcc = _MfccExtractor(
            self.sample_rate, self.n_mfcc, self.n_mels, self.fmin, self.fmax
        ) if np is not None else None

        frames_in_window = max(3, int(self.speech_window_s * 1000 / self.frame_ms))
        self._flags: deque[bool] = deque(maxlen=frames_in_window)
        self._rms_hist: deque[float] = deque(maxlen=frames_in_window)
        self._noise_floor = self.rms_gate
        self._floor_win: deque[float] = deque(
            maxlen=max(20, int(self.floor_window_s * 1000 / self.frame_ms))
        )
        self._frames_analyzed = 0
        self._speech_started_ts: float | None = None
        self._last_speech_ts: float = 0.0
        self._device_ok = False

        # накопитель речи: только для MFCC, живёт <= 1.5 с, затем затирается
        self._speech_buf: list[Any] = []
        self._speech_buf_len = 0
        self._max_speech_buf = int(self.sample_rate * 1.5)
        self._chunk_samples = int(self.sample_rate * self.mfcc_chunk_s)

        self._chunk_means: deque[Any] = deque(maxlen=self.owner_window)
        self._owner_votes: deque[bool] = deque(maxlen=self.owner_window)
        self._profile_mean: Any = None
        self._profile_std: Any = None
        self._profile_frames = 0
        self._last_similarity: float | None = None
        self._last_z: float | None = None

        self._enroll_active = False
        self._enroll_sum: Any = None
        self._enroll_sq: Any = None
        self._enroll_frames = 0

    # ----------------------------------------------------------------- #
    # Доступность
    # ----------------------------------------------------------------- #
    def available(self) -> bool:
        """Канал разрешён режимом, есть numpy и sounddevice, есть вход записи.

        Проверка режима стоит ПЕРВОЙ и до `_probe()` сознательно: в режиме
        аудитории микрофон не должен быть даже опрошен. Причина лежит в
        `last_error` и `disabled_reason` — «отключён в режиме аудитории:
        высокий уровень ложных срабатываний», а не «микрофон недоступен».
        """
        if not self.mode_allows_audio:
            self.last_error = f"аудио-канал {self.disabled_reason}"
            return False
        if not self.enabled or np is None:
            return False
        if self._probe_ok is None:
            self._probe_ok = self._probe()
        return bool(self._probe_ok)

    def _probe(self) -> bool:
        try:
            if importlib.util.find_spec("sounddevice") is None:
                self.last_error = "нет пакета sounddevice — аудио-канал отключён"
                return False
        except Exception as exc:  # pragma: no cover
            self.last_error = f"не удалось проверить sounddevice: {exc}"
            return False
        try:
            sd = importlib.import_module("sounddevice")
            # Проверяем именно вход: наличие пакета без микрофона нам не поможет.
            sd.check_input_settings(
                device=self.device, channels=1, samplerate=self.sample_rate
            )
        except Exception as exc:
            self.last_error = f"микрофон недоступен: {exc}"
            return False
        return True

    @property
    def enrolled(self) -> bool:
        return self._profile_mean is not None

    # ----------------------------------------------------------------- #
    # Запуск / остановка
    # ----------------------------------------------------------------- #
    def start(self) -> None:
        """Поднять захват. Повторный вызов безопасен; ошибки не бросаются наружу.

        В режиме аудитории выходит сразу: ни `sounddevice`, ни поток, ни
        кольцевой буфер не создаются — звук не покидает драйвер.
        """
        if not self.mode_allows_audio:
            return
        if self._started or not self.available():
            return
        try:
            sd = importlib.import_module("sounddevice")
            self._ring = _Ring(max(self.frame_len * 4, int(self.sample_rate * self.ring_seconds)))
            self._read_pos = 0
            self._init_vad()

            def _callback(indata, frames, time_info, status):  # noqa: ANN001
                # Выполняется в потоке звукового драйвера: только копирование.
                if status:
                    self.last_error = f"поток микрофона: {status}"
                try:
                    data = np.asarray(indata, dtype=np.float32)
                    if data.ndim > 1:
                        data = data.mean(axis=1)  # в моно
                    if self._ring is not None:
                        self._ring.write(data)
                except Exception:
                    pass

            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=self.frame_len,
                device=self.device,
                callback=_callback,
            )
            self._stream.start()
            self._stop_evt.clear()
            self._worker = threading.Thread(
                target=self._run, name="audio-monitor", daemon=True
            )
            self._worker.start()
            self._started = True
            self._device_ok = True
            self.last_error = ""
        except Exception as exc:
            self.last_error = f"не удалось открыть микрофон: {exc}"
            self._probe_ok = False
            self._device_ok = False
            self._teardown_stream()

    def _init_vad(self) -> None:
        self._vad = None
        self._vad_backend = "energy"
        try:
            if importlib.util.find_spec("webrtcvad") is not None:
                webrtcvad = importlib.import_module("webrtcvad")
                self._vad = webrtcvad.Vad(self.vad_aggressiveness)
                self._vad_backend = "webrtcvad"
        except Exception as exc:
            self._vad = None
            self._vad_backend = "energy"
            self.last_error = f"webrtcvad недоступен ({exc}) — VAD по энергии"

    def _teardown_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

    def stop(self) -> None:
        """Остановить захват и ЗАТЕРЕТЬ все сырые буферы (требование приватности)."""
        self._stop_evt.set()
        worker, self._worker = self._worker, None
        self._teardown_stream()
        if worker is not None and worker.is_alive():
            worker.join(timeout=1.5)
        with self._lock:
            if self._ring is not None:
                self._ring.clear()
            self._ring = None
            self._speech_buf.clear()
            self._speech_buf_len = 0
            self._enroll_active = False
            self._started = False
            self._device_ok = False

    # ----------------------------------------------------------------- #
    # Рабочий поток
    # ----------------------------------------------------------------- #
    def _run(self) -> None:
        """Разбор кольцевого буфера на кадры 30 мс: RMS -> VAD -> накопление речи."""
        while not self._stop_evt.is_set():
            ring = self._ring
            if ring is None:
                break
            try:
                if ring.available(self._read_pos) < self.frame_len:
                    self._stop_evt.wait(self.frame_ms / 2000.0)
                    continue
                n_frames = min(ring.available(self._read_pos) // self.frame_len, 20)
                data, self._read_pos = ring.read(
                    self._read_pos, n_frames * self.frame_len
                )
                for i in range(n_frames):
                    frame = data[i * self.frame_len : (i + 1) * self.frame_len]
                    if frame.size == self.frame_len:
                        self._process_frame(frame)
                # Сырые сэмплы этого блока больше не нужны.
                del data
            except Exception as exc:  # поток не должен умирать молча
                self.last_error = f"ошибка обработки аудио: {exc}"
                self._stop_evt.wait(0.05)

    def _process_frame(self, frame: Any) -> None:
        now = time.time()
        rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)) + 1e-12)

        # Пол шума — МИНИМУМ энергии за последние floor_window_s секунд. Минимум,
        # а не среднее/процентиль: в живой речи между словами всегда есть провалы,
        # и они дают настоящий уровень комнаты. Сверху ограничиваем потолком —
        # иначе непрерывный громкий звук «заражает» пол и VAD слепнет.
        with self._lock:
            self._floor_win.append(rms)
            floor = min(
                max(min(self._floor_win), 1e-6), self.noise_floor_ceiling
            )
            self._noise_floor = floor

        voiced = self._vad_decide(frame, rms, floor)
        # Жёсткий энергетический гейт: тишину VAD иногда принимает за речь.
        if rms < self.rms_gate:
            voiced = False

        with self._lock:
            self._flags.append(voiced)
            self._rms_hist.append(rms)
            self._frames_analyzed += 1
            if voiced:
                self._last_speech_ts = now
                if self._speech_started_ts is None:
                    self._speech_started_ts = now
            else:
                # Разрыв больше 0.4 с считаем окончанием реплики.
                if (
                    self._speech_started_ts is not None
                    and now - self._last_speech_ts > 0.4
                ):
                    self._speech_started_ts = None

        # --- накопление речи только для MFCC ---
        if voiced:
            self._speech_buf.append(frame.copy())
            self._speech_buf_len += frame.size
            if self._speech_buf_len >= self._chunk_samples:
                self._consume_speech_chunk()
            elif self._speech_buf_len > self._max_speech_buf:
                # Защита от роста памяти: лишнее просто выбрасываем.
                self._speech_buf.clear()
                self._speech_buf_len = 0
        elif self._speech_buf_len >= self.sample_rate // 4:
            # Реплика кончилась, но данных хватает на MFCC — посчитать и затереть.
            self._consume_speech_chunk()
        elif self._speech_buf:
            self._speech_buf.clear()
            self._speech_buf_len = 0

    def _vad_decide(self, frame: Any, rms: float, floor: float) -> bool:
        """Основной сигнал — webrtcvad; без него адаптивная энергия + ZCR."""
        if self._vad is not None:
            try:
                pcm = (np.clip(frame, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
                return bool(self._vad.is_speech(pcm, self.sample_rate))
            except Exception as exc:
                self.last_error = f"webrtcvad отказал ({exc}) — переход на энергию"
                self._vad = None
                self._vad_backend = "energy"
        # Энергетический резерв: громче пола шума + «речевая» частота переходов нуля.
        if rms < max(self.rms_gate, floor * self.noise_floor_mult):
            return False
        signs = np.signbit(frame)
        zcr = float(np.count_nonzero(signs[1:] != signs[:-1])) / max(1, frame.size - 1)
        return 0.01 <= zcr <= 0.35

    def _consume_speech_chunk(self) -> None:
        """Посчитать MFCC по накопленной речи и НЕМЕДЛЕННО затереть сырые сэмплы."""
        if not self._speech_buf or self._mfcc is None:
            self._speech_buf.clear()
            self._speech_buf_len = 0
            return
        try:
            chunk = np.concatenate(self._speech_buf)
        except Exception:
            chunk = None
        finally:
            self._speech_buf.clear()
            self._speech_buf_len = 0
        if chunk is None or chunk.size < self._mfcc.win:
            return
        try:
            mf = self._mfcc(chunk)
        except Exception as exc:
            self.last_error = f"MFCC не посчитались: {exc}"
            mf = None
        finally:
            # Сырой звук уходит из памяти здесь и никогда не попадает на диск.
            chunk = None
        if mf is None or getattr(mf, "shape", (0,))[0] < 3:
            return
        mean = mf.mean(axis=0).astype(np.float32)

        with self._lock:
            if self._enroll_active:
                if self._enroll_sum is None:
                    self._enroll_sum = np.zeros(mean.size, dtype=np.float64)
                    self._enroll_sq = np.zeros(mean.size, dtype=np.float64)
                self._enroll_sum += mf.sum(axis=0, dtype=np.float64)
                self._enroll_sq += (mf.astype(np.float64) ** 2).sum(axis=0)
                self._enroll_frames += int(mf.shape[0])
            self._chunk_means.append(mean)
        self._score_owner()

    # ----------------------------------------------------------------- #
    # Профиль владельца (экспериментальный сигнал)
    # ----------------------------------------------------------------- #
    def enroll_owner(self, seconds: float = 5.0) -> bool:
        """Собрать MFCC-профиль голоса студента за `seconds` секунд речи.

        Профиль = средний вектор MFCC (c1..c13) + подиагональная дисперсия по
        кадрам. Накапливаются только речевые кадры (по VAD), сырой звук не
        хранится. True — профиля хватило (>= enroll_min_speech_s речи).
        """
        if not self.available():
            return False
        if not self._started:
            self.start()
        if not self._started:
            return False
        with self._lock:
            self._enroll_active = True
            self._enroll_sum = None
            self._enroll_sq = None
            self._enroll_frames = 0
        deadline = time.time() + max(1.0, float(seconds)) + self.enroll_max_wait_s
        # хоп MFCC = 10 мс, значит кадров на секунду речи ~100
        need_frames = int(self.enroll_min_speech_s * 100)
        try:
            while time.time() < deadline:
                with self._lock:
                    enough = self._enroll_frames >= need_frames
                    elapsed_ok = time.time() >= deadline - self.enroll_max_wait_s
                if enough and elapsed_ok:
                    break
                time.sleep(0.05)
        finally:
            with self._lock:
                self._enroll_active = False
                frames = self._enroll_frames
                total = self._enroll_sum
                total_sq = self._enroll_sq
                self._enroll_sum = None
                self._enroll_sq = None

        if total is None or frames < max(30, need_frames // 2):
            self.last_error = (
                "профиль голоса не снят: слишком мало речи "
                f"({frames / 100.0:.1f} с) — попросите студента читать текст вслух"
            )
            return False
        mean = (total / float(frames)).astype(np.float32)
        var = np.maximum(total_sq / float(frames) - (total / float(frames)) ** 2, 1e-6)
        std = np.sqrt(var).astype(np.float32)
        with self._lock:
            self._profile_mean = mean
            self._profile_std = np.maximum(std, 1e-3).astype(np.float32)
            self._profile_frames = frames
            self._owner_votes.clear()
        self.last_error = ""
        return True

    def owner_profile(self) -> dict[str, Any]:
        """Результат калибровки голоса для `calibration.result`. Без сырого звука."""
        with self._lock:
            ready = self._profile_mean is not None
            return {
                "enrolled": ready,
                "frames": self._profile_frames,
                "speech_seconds": round(self._profile_frames / 100.0, 2),
                "mfcc_backend": getattr(self._mfcc, "backend", "none"),
                "vad_backend": self._vad_backend,
                "threshold": self.owner_threshold,
                "experimental": True,
                "error": self.last_error,
            }

    def load_owner_profile(self, mean: Any, std: Any, frames: int = 0) -> bool:
        """Загрузить профиль из прошлой сессии."""
        if np is None or mean is None or std is None:
            return False
        try:
            m = np.asarray(list(mean), dtype=np.float32).reshape(-1)
            s = np.asarray(list(std), dtype=np.float32).reshape(-1)
            if m.size != s.size or m.size < 4:
                return False
            with self._lock:
                self._profile_mean = m
                self._profile_std = np.maximum(s, 1e-3)
                self._profile_frames = int(frames)
            return True
        except Exception:
            return False

    def _score_owner(self) -> None:
        """Сравнить последние речевые куски с профилем владельца.

        Косинус считается в ОТБЕЛЁННОМ пространстве: вектор MFCC делится на
        подиагональное СКО профиля. Без отбеливания косинус между средними MFCC
        почти всегда > 0.95 (доминирует общая форма спектра) и ничего не различает.
        Второй показатель — средний |z| отклонения от профиля. Решение = большинство
        голосов по последним `owner_window` кускам (гистерезис).
        """
        with self._lock:
            if self._profile_mean is None or not self._chunk_means:
                return
            prof = self._profile_mean
            std = self._profile_std
            means = list(self._chunk_means)
        try:
            cur = np.mean(np.stack(means[-self.owner_window :]), axis=0).astype(np.float32)
            if cur.size != prof.size:
                return
            a = cur / std
            b = prof / std
            na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
            if na < 1e-9 or nb < 1e-9:
                return
            similarity = float(np.dot(a, b) / (na * nb))
            z = float(np.mean(np.abs((cur - prof) / std)))
        except Exception:
            return
        is_owner = similarity >= self.owner_threshold and z <= self.owner_z_max
        with self._lock:
            self._last_similarity = similarity
            self._last_z = z
            self._owner_votes.append(is_owner)

    # ----------------------------------------------------------------- #
    # poll
    # ----------------------------------------------------------------- #
    def poll(self) -> AudioObservation:
        """Снять текущее наблюдение. Дёшево: только чтение агрегатов под локом.

        `speech` — доля речевых кадров в окне `speech_window_s` не ниже
        `speech_ratio` (по умолчанию 0.35), т.е. короткий стук или щелчок мышью
        речью не считается. `is_owner` — None, если профиля нет, речи нет или
        речевых кусков меньше `owner_min_chunks`.
        """
        now = time.time()
        if not self.available():
            return AudioObservation(
                available=False,
                device_ok=False,
                vad_backend="none",
                confidence=0.0,
                error=self.last_error or "аудио-канал недоступен",
                exam_mode=self.exam_mode,
                ts=now,
            )
        with self._lock:
            flags = list(self._flags)
            rms_hist = list(self._rms_hist)
            noise_floor = self._noise_floor
            frames_analyzed = self._frames_analyzed
            started_ts = self._speech_started_ts
            last_speech = self._last_speech_ts
            votes = list(self._owner_votes)
            enrolled = self._profile_mean is not None
            similarity = self._last_similarity
            z = self._last_z
            chunks = len(self._chunk_means)
            started = self._started
            stream = self._stream

        device_ok = bool(started and stream is not None and getattr(stream, "active", True))
        ratio = (sum(1 for f in flags if f) / float(len(flags))) if flags else 0.0
        rms = float(np.mean(np.asarray(rms_hist, dtype=np.float32))) if rms_hist else 0.0
        speech = ratio >= self.speech_ratio_threshold and rms >= self.rms_gate
        duration = 0.0
        if started_ts is not None and now - last_speech <= 0.6:
            duration = max(0.0, now - started_ts)

        # --- is_owner ---
        is_owner: bool | None = None
        if enrolled and speech and chunks >= self.owner_min_chunks and votes:
            positive = sum(1 for v in votes if v)
            is_owner = positive * 2 >= len(votes)  # большинство, ничья -> в пользу своего

        # --- confidence ---
        # Честно: webrtcvad даёт надёжный флаг речи, сравнение голоса — слабее.
        backend_factor = 1.0 if self._vad_backend == "webrtcvad" else 0.6
        fill = min(1.0, len(flags) / float(max(1, self._flags.maxlen or 1)))
        snr = min(1.0, rms / max(noise_floor * 4.0, self.rms_gate * 2.0))
        if is_owner is None:
            confidence = backend_factor * fill * (0.4 + 0.6 * snr)
        else:
            margin = 0.0
            if similarity is not None:
                margin = min(1.0, abs(similarity - self.owner_threshold) / 0.25)
            vote_unanimity = abs(
                sum(1 for v in votes if v) * 2 - len(votes)
            ) / float(max(1, len(votes)))
            # 0.75 — сознательный штраф: канал голоса экспериментальный
            confidence = 0.75 * backend_factor * (0.3 + 0.4 * margin + 0.3 * vote_unanimity)
        confidence = float(max(0.0, min(1.0, confidence)))

        return AudioObservation(
            speech=speech,
            is_owner=is_owner,
            rms=rms,
            confidence=confidence,
            experimental=True,
            available=True,
            device_ok=device_ok,
            enrolled=enrolled,
            speech_ratio=ratio,
            speech_duration=duration,
            speech_started_ts=started_ts,
            owner_similarity=similarity,
            owner_z=z,
            vad_backend=self._vad_backend,
            noise_floor=noise_floor,
            frames_analyzed=frames_analyzed,
            error="" if device_ok else (self.last_error or "поток микрофона остановлен"),
            exam_mode=self.exam_mode,
            ts=now,
        )

    # ----------------------------------------------------------------- #
    # Служебное
    # ----------------------------------------------------------------- #
    def reset_session(self) -> None:
        """Сбросить рантайм-окна, профиль голоса сохраняется."""
        with self._lock:
            self._flags.clear()
            self._rms_hist.clear()
            self._floor_win.clear()
            self._noise_floor = self.rms_gate
            self._frames_analyzed = 0
            self._speech_started_ts = None
            self._last_speech_ts = 0.0
            self._chunk_means.clear()
            self._owner_votes.clear()
            self._last_similarity = None
            self._last_z = None
            self._speech_buf.clear()
            self._speech_buf_len = 0

    def status(self) -> dict[str, Any]:
        """Короткая сводка для сообщения `status` протокола.

        `exam_mode` и `disabled_reason` нужны HUD: индикатор канала в аудитории
        должен читаться как «выключен по режиму», а не как «сломался микрофон».
        """
        obs = self.poll()
        return {
            "exam_mode": self.exam_mode,
            "mode_allows_audio": self.mode_allows_audio,
            "disabled_reason": self.disabled_reason,
            "available": obs.available,
            "device_ok": obs.device_ok,
            "audio_ok": obs.available and obs.device_ok,
            "speech": obs.speech,
            "is_owner": obs.is_owner,
            "rms": round(obs.rms, 5),
            "vad_backend": obs.vad_backend,
            "mfcc_backend": getattr(self._mfcc, "backend", "none"),
            "enrolled": obs.enrolled,
            "experimental": True,
            "error": obs.error,
        }

    def __del__(self) -> None:  # pragma: no cover - страховка от утечки потока
        try:
            self.stop()
        except Exception:
            pass
