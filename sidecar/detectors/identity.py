"""
Непрерывная верификация личности + признаки живости (анти-спуфинг).

Зачем канал нужен
-----------------
Обычный прокторинг проверяет лицо один раз на старте. Этот модуль проверяет его
непрерывно: подмена человека после калибровки («сел брат, который знает предмет»),
фото перед камерой, зацикленная видеозапись, подставленный видеопоток.

Что делает модуль
-----------------
1. `enroll()` — собирает эталон по кадрам калибровки (усреднённый нормированный
   эмбеддинг insightface/buffalo_sc) с жёсткой отбраковкой плохих кадров.
2. `verify()` — косинусная близость текущего лица к эталону, решение по скользящему
   окну с гистерезисом (одиночный плохой кадр не порождает тревогу).
3. `update_liveness()` — ПРИЗНАКИ живости, а не события: отсутствие морганий,
   статичность области лица, повтор последовательности кадров (loop видеозаписи).

Что модуль НЕ делает
--------------------
* Не порождает `ProctorEvent`. Все наблюдения уходят в EventEngine, который сам
  отвечает за окно подтверждения, cooldown и формулировки:
    - `IdentityObservation.enrolled and not match` -> наблюдение `IDENTITY_MISMATCH`;
    - `LivenessObservation.suspect`                -> наблюдение `LIVENESS_FAIL`.
* Не проверяет виртуальную камеру — это `sidecar/env_checks.py` (`VIRTUAL_CAMERA`),
  метода `check_virtual_camera()` здесь сознательно нет.
* Не ходит в сеть. insightface умеет скачивать модели — поэтому модель берётся
  только из локального каталога, и если файлов `*.onnx` нет, канал просто выключается
  (`available() == False`), а скачивание не инициируется никогда.

Деградация
----------
* Нет insightface/onnxruntime или нет локальных моделей -> `available() == False`,
  верификация личности отключена, остальной сайдкар работает.
* Признаки живости считаются на одном numpy и работают даже при выключенной
  верификации (`liveness_available()`), поэтому фото/loop ловятся и без insightface.
* Нет numpy -> оба канала выключены, исключений не бросается.
"""
from __future__ import annotations

import glob
import importlib
import importlib.util
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

try:  # numpy — единственная обязательная зависимость, но и она не роняет импорт
    import numpy as np
except Exception:  # pragma: no cover - окружение без numpy
    np = None  # type: ignore[assignment]


__all__ = [
    "IdentityVerifier",
    "IdentityObservation",
    "LivenessObservation",
    "DEFAULT_IDENTITY_CONFIG",
]


# --------------------------------------------------------------------------
# Пороги по умолчанию. Все до единого переопределяются через config.
# --------------------------------------------------------------------------
DEFAULT_IDENTITY_CONFIG: dict[str, Any] = {
    # --- модель ---
    "enabled": True,
    "model_name": "buffalo_sc",      # маленький пакет insightface (det_500m + w600k_mbf)
    "model_root": None,              # None -> корень репозитория, т.е. <repo>/models/buffalo_sc
    "det_size": 320,                 # вход детектора; 320 хватает для веб-камеры
    "det_thresh": 0.5,
    # --- решение о совпадении ---
    "threshold": 0.35,               # косинус для buffalo/w600k: >=0.35 — тот же человек
    "check_interval_s": 1.0,         # не чаще раза в секунду — экономим CPU
    "window": 7,                     # длина скользящего окна проверок
    "min_checks": 4,                 # до этого числа проверок решения не принимаем
    "mismatch_ratio": 0.6,           # доля плохих проверок в окне для перехода в mismatch
    "recover_ratio": 0.6,            # доля хороших проверок для возврата в ok (гистерезис)
    # --- отбраковка кадров на калибровке ---
    "enroll_frames": 24,             # сколько эмбеддингов максимум усреднять
    "min_enroll_frames": 5,          # минимум принятых кадров для готового эталона
    "min_face_px": 90,               # меньшая сторона bbox лица, px
    "min_area_ratio": 0.012,         # доля площади кадра
    "min_det_score": 0.6,
    "max_yaw_ratio": 0.22,           # поворот головы по 5 точкам (нос vs середина глаз)
    "max_roll_deg": 20.0,
    "pitch_ratio_range": (0.25, 0.78),  # нос между линией глаз и линией рта
    "min_sharpness": 25.0,           # дисперсия лапласиана кропа лица (анти-смаз)
    "enroll_outlier_cos": 0.5,       # эмбеддинг с таким косинусом к среднему — выброс
}

DEFAULT_LIVENESS_CONFIG: dict[str, Any] = {
    "patch_size": 64,                # размер серого кропа лица для анализа движения
    "hash_grid": 8,                  # сетка 8x8 для хеша кадра
    "no_blink_timeout_s": 25.0,      # нет морганий столько секунд при видимом лице
    "motion_window": 45,             # кадров в окне оценки движения (~3 с при 15 fps)
    "static_mad_threshold": 0.9,     # средний |diff| серого кропа (уровни 0..255)
    "static_min_frames": 30,
    "hash_history": 600,             # кадров истории хешей (~40 с при 15 fps)
    "min_loop_frames": 90,           # минимум истории для поиска цикла
    "min_loop_period": 15,           # минимальный период цикла, кадров
    "loop_tolerance": 0.035,         # нормированная дистанция Хэмминга для «это повтор»
    "min_scene_diversity": 0.05,     # ниже — кадр просто статичен, цикл искать нельзя
    "loop_check_interval_s": 2.0,
    # веса подозрительности; suspect = score >= suspect_threshold
    "w_no_blink": 0.35,
    "w_static": 0.5,
    "w_loop": 0.7,
    "suspect_threshold": 0.6,
}


_MISSING = object()


def _container_get(container: Any, key: str) -> Any:
    """Достать ключ из dict или атрибут из dataclass/объекта конфига."""
    if container is None:
        return _MISSING
    if isinstance(container, dict):
        return container.get(key, _MISSING)
    return getattr(container, key, _MISSING)


def _section(config: Any, name: str) -> Any:
    value = _container_get(config, name)
    return None if value is _MISSING else value


def _cfg(config: Any, section: str, key: str, default: Any) -> Any:
    """config[section][key] -> config[f"{section}_{key}"] -> config[key] -> default.

    Терпимо и к dict, и к dataclass — config.py пишет другой агент, форму не угадать.
    """
    for container, name in (
        (_section(config, section), key),
        (config, f"{section}_{key}"),
        (config, key),
    ):
        value = _container_get(container, name)
        if value is not _MISSING and value is not None:
            return value
    return default


# --------------------------------------------------------------------------
# Структуры наблюдений
# --------------------------------------------------------------------------
@dataclass
class IdentityObservation:
    """Наблюдение канала личности. Контракт: match / similarity / enrolled."""

    match: bool = True
    similarity: float = 0.0
    enrolled: bool = False
    # --- расширения (не ломают контракт, нужны движку и HUD) ---
    available: bool = False     # канал вообще работает (модель загружена)
    checked: bool = False       # на этом кадре реально считался эмбеддинг
    face_found: bool = False    # insightface нашёл лицо для сравнения
    votes: int = 0              # сколько проверок в скользящем окне
    mismatch_ratio: float = 0.0  # доля «чужих» проверок в окне
    threshold: float = 0.0
    quality: float = 0.0        # det_score найденного лица
    reason: str = ""            # по-русски, для HUD/отчёта
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "match": self.match,
            "similarity": round(float(self.similarity), 4),
            "enrolled": self.enrolled,
            "available": self.available,
            "checked": self.checked,
            "face_found": self.face_found,
            "votes": self.votes,
            "mismatch_ratio": round(float(self.mismatch_ratio), 3),
            "threshold": round(float(self.threshold), 3),
            "quality": round(float(self.quality), 3),
            "reason": self.reason,
            "ts": self.ts,
        }


@dataclass
class LivenessObservation:
    """Признаки живости. Событие LIVENESS_FAIL порождает EventEngine, не этот класс."""

    suspect: bool = False            # устойчивый признак подмены (score >= порога)
    score: float = 0.0               # 0..1 суммарная подозрительность
    no_blink: bool = False
    seconds_since_blink: float = 0.0
    blinks_total: int = 0
    static_frame: bool = False
    motion_level: float = 0.0        # средний |diff| серого кропа лица, уровни 0..255
    loop_detected: bool = False
    loop_period_frames: int = 0
    loop_period_s: float = 0.0
    loop_similarity: float = 0.0     # 1 - нормированная дистанция Хэмминга
    scene_diversity: float = 0.0     # насколько кадры вообще отличаются друг от друга
    face_present: bool = False
    frames: int = 0                  # сколько кадров уже проанализировано
    reasons: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "suspect": self.suspect,
            "score": round(float(self.score), 3),
            "no_blink": self.no_blink,
            "seconds_since_blink": round(float(self.seconds_since_blink), 2),
            "blinks_total": self.blinks_total,
            "static_frame": self.static_frame,
            "motion_level": round(float(self.motion_level), 3),
            "loop_detected": self.loop_detected,
            "loop_period_frames": self.loop_period_frames,
            "loop_period_s": round(float(self.loop_period_s), 2),
            "loop_similarity": round(float(self.loop_similarity), 3),
            "scene_diversity": round(float(self.scene_diversity), 3),
            "face_present": self.face_present,
            "frames": self.frames,
            "reasons": list(self.reasons),
            "ts": self.ts,
        }


# --------------------------------------------------------------------------
# Вспомогательные функции над кадром (чистый numpy, без cv2)
# --------------------------------------------------------------------------
def _norm_bbox(bbox: Any, width: int, height: int) -> tuple[int, int, int, int] | None:
    """Привести bbox к (x, y, w, h) в пикселях.

    Контракт отдаёт (x, y, w, h), но допускаем и (x1, y1, x2, y2) — ошибка формата
    на демо дороже четырёх строк эвристики.
    """
    if bbox is None:
        return None
    if isinstance(bbox, dict):
        try:
            bbox = (bbox["x"], bbox["y"], bbox["w"], bbox["h"])
        except Exception:
            return None
    try:
        a, b, c, d = (float(v) for v in tuple(bbox)[:4])
    except Exception:
        return None
    if c <= 0 or d <= 0:
        # (x1,y1,x2,y2) с нулевой/отрицательной «шириной» — значит это углы
        c, d = c - a, d - b
    elif a + c > width + 2 and c > a and d > b:
        # ширина вылезает за кадр, но как углы всё сходится — это углы
        c, d = c - a, d - b
    x = int(max(0, math.floor(a)))
    y = int(max(0, math.floor(b)))
    w = int(min(width - x, math.ceil(c)))
    h = int(min(height - y, math.ceil(d)))
    if w <= 1 or h <= 1:
        return None
    return x, y, w, h


def _to_gray(frame: Any) -> Any:
    """BGR/серый кадр -> float32 серый (0..255). Без cv2."""
    arr = np.asarray(frame)
    if arr.ndim == 3:
        if arr.shape[2] >= 3:
            b = arr[:, :, 0].astype(np.float32)
            g = arr[:, :, 1].astype(np.float32)
            r = arr[:, :, 2].astype(np.float32)
            return 0.114 * b + 0.587 * g + 0.299 * r
        arr = arr[:, :, 0]
    return arr.astype(np.float32)


def _resample(gray: Any, size: int) -> Any:
    """Ближайший сосед до size x size — дешевле cv2 и достаточно для хешей/диффов."""
    h, w = gray.shape[:2]
    yi = np.linspace(0, h - 1, size).astype(np.int32)
    xi = np.linspace(0, w - 1, size).astype(np.int32)
    return gray[yi][:, xi]


def _laplacian_var(gray: Any) -> float:
    """Дисперсия лапласиана — классическая мера резкости (смаз/расфокус)."""
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0
    g = gray.astype(np.float32)
    lap = (
        -4.0 * g[1:-1, 1:-1]
        + g[:-2, 1:-1]
        + g[2:, 1:-1]
        + g[1:-1, :-2]
        + g[1:-1, 2:]
    )
    return float(lap.var())


def _frame_hash(gray: Any, grid: int = 8) -> Any:
    """aHash по сетке grid x grid: средние по блокам, бит = блок светлее общего среднего.

    Сначала даунсемплим до grid*4, затем усредняем блоки 4x4 — так хеш устойчив
    к шуму сенсора и к дрожанию кадра на 1-2 пикселя.
    """
    small = _resample(gray, grid * 4)
    cells = small.reshape(grid, 4, grid, 4).mean(axis=(1, 3))
    return (cells > cells.mean()).astype(np.uint8).reshape(-1)


# --------------------------------------------------------------------------
# Основной класс
# --------------------------------------------------------------------------
class IdentityVerifier:
    """Верификация личности по эмбеддингам лица + признаки живости.

    Ожидаемый сценарий использования в сайдкаре:

        iv = IdentityVerifier(config)
        # калибровка, stage="identity": на каждом кадре
        iv.enroll(frame, face_obs.face_bbox, yaw=face_obs.yaw, pitch=face_obs.pitch)
        ...
        iv.enroll_progress()          # {"accepted": n, "needed": k, "ready": bool}
        # сессия: на каждом кадре
        ident = iv.verify(frame, face_obs.face_bbox)          # троттлится внутри
        live  = iv.update_liveness(frame, face_obs.face_bbox,
                                   blink=face_obs.blink)
    """

    def __init__(self, config: dict | None = None) -> None:
        self.config = config if config is not None else {}

        def ic(key: str) -> Any:
            return _cfg(self.config, "identity", key, DEFAULT_IDENTITY_CONFIG[key])

        def lc(key: str) -> Any:
            # сначала секция liveness, затем identity, затем дефолт
            return _cfg(
                self.config,
                "liveness",
                key,
                _cfg(self.config, "identity", key, DEFAULT_LIVENESS_CONFIG[key]),
            )

        # --- параметры модели ---
        self.enabled = bool(ic("enabled"))
        self.model_name = str(ic("model_name"))
        self._model_root_cfg = ic("model_root")
        self.det_size = int(ic("det_size"))
        self.det_thresh = float(ic("det_thresh"))

        # --- параметры решения ---
        self.threshold = float(ic("threshold"))
        self.check_interval_s = float(ic("check_interval_s"))
        self.window = max(3, int(ic("window")))
        self.min_checks = max(1, min(int(ic("min_checks")), self.window))
        self.mismatch_ratio_cfg = float(ic("mismatch_ratio"))
        self.recover_ratio_cfg = float(ic("recover_ratio"))

        # --- отбраковка на калибровке ---
        self.enroll_frames = int(ic("enroll_frames"))
        self.min_enroll_frames = max(1, int(ic("min_enroll_frames")))
        self.min_face_px = int(ic("min_face_px"))
        self.min_area_ratio = float(ic("min_area_ratio"))
        self.min_det_score = float(ic("min_det_score"))
        self.max_yaw_ratio = float(ic("max_yaw_ratio"))
        self.max_roll_deg = float(ic("max_roll_deg"))
        pr = ic("pitch_ratio_range")
        self.pitch_ratio_range = (float(pr[0]), float(pr[1]))
        self.min_sharpness = float(ic("min_sharpness"))
        self.enroll_outlier_cos = float(ic("enroll_outlier_cos"))

        # --- liveness ---
        self.patch_size = int(lc("patch_size"))
        self.hash_grid = int(lc("hash_grid"))
        self.no_blink_timeout_s = float(lc("no_blink_timeout_s"))
        self.motion_window = int(lc("motion_window"))
        self.static_mad_threshold = float(lc("static_mad_threshold"))
        self.static_min_frames = int(lc("static_min_frames"))
        self.hash_history = int(lc("hash_history"))
        self.min_loop_frames = int(lc("min_loop_frames"))
        self.min_loop_period = max(2, int(lc("min_loop_period")))
        self.loop_tolerance = float(lc("loop_tolerance"))
        self.min_scene_diversity = float(lc("min_scene_diversity"))
        self.loop_check_interval_s = float(lc("loop_check_interval_s"))
        self.w_no_blink = float(lc("w_no_blink"))
        self.w_static = float(lc("w_static"))
        self.w_loop = float(lc("w_loop"))
        self.suspect_threshold = float(lc("suspect_threshold"))

        # --- состояние модели ---
        self._app: Any = None
        self._model_dir: str | None = None
        self._probe_ok: bool | None = None
        self._load_failed = False
        self.last_error: str = ""

        # --- состояние эталона/решения ---
        self._enroll_embeddings: list[Any] = []
        self._enroll_rejected: dict[str, int] = {}
        self._last_reject: str = ""
        self._reference: Any = None
        self._reference_cohesion: float = 0.0
        self._votes: deque[bool] = deque(maxlen=self.window)
        self._state_ok: bool = True
        self._last_check_ts: float = 0.0
        self._last_obs: IdentityObservation = IdentityObservation(
            threshold=self.threshold
        )
        self._similarity_ema: float = 0.0
        self._checks_total: int = 0
        self._mismatch_streak: int = 0

        # --- состояние liveness ---
        self._prev_patch: Any = None
        self._motion: deque[float] = deque(maxlen=max(5, self.motion_window))
        self._hashes: deque[Any] = deque(maxlen=max(30, self.hash_history))
        self._frames_seen = 0
        self._blinks_total = 0
        self._time_since_blink = 0.0
        self._last_frame_ts: float | None = None
        self._last_loop_check = 0.0
        self._loop_cache: tuple[bool, int, float, float] = (False, 0, 0.0, 0.0)
        self._fps_estimate = 15.0

    # ----------------------------------------------------------------- #
    # Доступность
    # ----------------------------------------------------------------- #
    def available(self) -> bool:
        """Канал верификации личности работоспособен.

        Лёгкая проверка (без загрузки ONNX): numpy + insightface + onnxruntime
        установлены, локальный каталог модели существует и содержит *.onnx.
        Если позднее загрузка упала — возвращает False навсегда.
        """
        if not self.enabled or np is None or self._load_failed:
            return False
        if self._app is not None:
            return True
        if self._probe_ok is None:
            self._probe_ok = self._probe()
        return bool(self._probe_ok)

    def liveness_available(self) -> bool:
        """Признаки живости считаются на numpy и не зависят от insightface."""
        return np is not None

    def _probe(self) -> bool:
        for mod in ("insightface", "onnxruntime"):
            try:
                if importlib.util.find_spec(mod) is None:
                    self.last_error = f"нет пакета {mod}"
                    return False
            except Exception as exc:  # pragma: no cover - битый sys.path
                self.last_error = f"не удалось проверить {mod}: {exc}"
                return False
        model_dir = self._resolve_model_dir()
        if model_dir is None:
            self.last_error = (
                f"локальные модели '{self.model_name}' не найдены "
                "(скачивание отключено — система работает офлайн)"
            )
            return False
        self._model_dir = model_dir
        return True

    def _candidate_roots(self) -> list[str]:
        """Где искать пакет моделей. Никаких сетевых источников."""
        here = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.abspath(os.path.join(here, "..", ".."))
        roots: list[str] = []
        if self._model_root_cfg:
            roots.append(os.path.abspath(os.path.expanduser(str(self._model_root_cfg))))
        roots.append(repo_root)
        roots.append(os.path.join(repo_root, "models"))
        env_home = os.environ.get("INSIGHTFACE_HOME")
        if env_home:
            roots.append(os.path.abspath(os.path.expanduser(env_home)))
        roots.append(os.path.expanduser("~/.insightface"))
        seen: set[str] = set()
        uniq: list[str] = []
        for r in roots:
            if r not in seen:
                seen.add(r)
                uniq.append(r)
        return uniq

    def _resolve_model_dir(self) -> str | None:
        """Найти каталог с *.onnx. insightface ждёт <root>/models/<name>."""
        for root in self._candidate_roots():
            for candidate in (
                os.path.join(root, "models", self.model_name),
                os.path.join(root, self.model_name),
            ):
                if os.path.isdir(candidate) and glob.glob(
                    os.path.join(candidate, "*.onnx")
                ):
                    return candidate
        return None

    def warmup(self) -> bool:
        """Загрузить модель заранее (вызывать на калибровке, чтобы не лагало в сессии)."""
        return self._ensure_model() is not None

    def _ensure_model(self) -> Any:
        """Ленивая загрузка insightface. Любая ошибка -> канал выключается навсегда."""
        if self._app is not None:
            return self._app
        if not self.available():
            return None
        model_dir = self._model_dir or self._resolve_model_dir()
        if model_dir is None:
            self._load_failed = True
            return None
        try:
            # root такой, чтобы insightface сам собрал <root>/models/<name>
            # и НЕ пошёл ничего скачивать: каталог уже существует.
            parent = os.path.dirname(model_dir)
            root = os.path.dirname(parent) if os.path.basename(parent) == "models" else parent
            FaceAnalysis = getattr(
                importlib.import_module("insightface.app"), "FaceAnalysis"
            )
            app = FaceAnalysis(
                name=self.model_name,
                root=root,
                allowed_modules=["detection", "recognition"],
                providers=["CPUExecutionProvider"],
            )
            app.prepare(
                ctx_id=-1,  # CPU: на демо-ноутбуке GPU-провайдера нет
                det_thresh=self.det_thresh,
                det_size=(self.det_size, self.det_size),
            )
            if "recognition" not in getattr(app, "models", {}):
                raise RuntimeError("в пакете моделей нет модуля recognition")
            self._app = app
            self.last_error = ""
        except Exception as exc:
            self._app = None
            self._load_failed = True
            self.last_error = f"insightface не загрузился: {exc}"
            return None
        return self._app

    # ----------------------------------------------------------------- #
    # Эмбеддинги
    # ----------------------------------------------------------------- #
    def _extract(self, frame_bgr: Any, face_bbox: Any) -> tuple[Any, dict[str, Any]]:
        """Вернуть (нормированный эмбеддинг | None, метаданные лица).

        Сначала детектируем в расширенном кропе вокруг переданного bbox — это
        и быстрее, и точнее для мелких лиц; если там пусто, пробуем весь кадр.
        """
        app = self._ensure_model()
        meta: dict[str, Any] = {"det_score": 0.0, "bbox": None, "kps": None}
        if app is None or frame_bgr is None:
            return None, meta
        try:
            frame = np.asarray(frame_bgr)
            if frame.ndim != 3 or frame.shape[2] < 3:
                return None, meta
            if frame.dtype != np.uint8:
                frame = np.clip(frame, 0, 255).astype(np.uint8)
            h, w = frame.shape[:2]
            box = _norm_bbox(face_bbox, w, h)

            attempts: list[tuple[Any, int, int]] = []
            if box is not None:
                x, y, bw, bh = box
                pad = 0.6
                x0 = int(max(0, x - pad * bw))
                y0 = int(max(0, y - pad * bh))
                x1 = int(min(w, x + bw + pad * bw))
                y1 = int(min(h, y + bh + pad * bh))
                if x1 - x0 > 32 and y1 - y0 > 32:
                    attempts.append((frame[y0:y1, x0:x1], x0, y0))
            attempts.append((frame, 0, 0))

            for crop, off_x, off_y in attempts:
                faces = app.get(np.ascontiguousarray(crop))
                if not faces:
                    continue
                face = self._pick_face(faces, box, off_x, off_y)
                if face is None:
                    continue
                emb = getattr(face, "normed_embedding", None)
                if emb is None:
                    raw = getattr(face, "embedding", None)
                    if raw is None:
                        continue
                    emb = np.asarray(raw, dtype=np.float32)
                    norm = float(np.linalg.norm(emb))
                    if norm < 1e-6:
                        continue
                    emb = emb / norm
                emb = np.asarray(emb, dtype=np.float32).reshape(-1)
                fb = np.asarray(getattr(face, "bbox", [0, 0, 0, 0]), dtype=np.float32)
                kps = getattr(face, "kps", None)
                meta = {
                    "det_score": float(getattr(face, "det_score", 0.0) or 0.0),
                    "bbox": (
                        float(fb[0] + off_x),
                        float(fb[1] + off_y),
                        float(fb[2] - fb[0]),
                        float(fb[3] - fb[1]),
                    ),
                    "kps": None if kps is None else np.asarray(kps, dtype=np.float32),
                    "frame_shape": (h, w),
                }
                return emb, meta
        except Exception as exc:
            self.last_error = f"ошибка извлечения эмбеддинга: {exc}"
        return None, meta

    @staticmethod
    def _pick_face(faces: list[Any], box: Any, off_x: int, off_y: int) -> Any:
        """Выбрать лицо: максимальный IoU с подсказкой FaceAnalyzer, иначе крупнейшее."""
        best = None
        best_key = -1.0
        for face in faces:
            raw = getattr(face, "bbox", None)
            if raw is None:
                continue
            fb = np.asarray(raw, dtype=np.float32).reshape(-1)
            if fb.size < 4:
                continue
            fx, fy = float(fb[0]) + off_x, float(fb[1]) + off_y
            fw, fh = float(fb[2] - fb[0]), float(fb[3] - fb[1])
            if fw <= 0 or fh <= 0:
                continue
            if box is None:
                key = fw * fh
            else:
                bx, by, bw, bh = box
                ix = max(0.0, min(fx + fw, bx + bw) - max(fx, bx))
                iy = max(0.0, min(fy + fh, by + bh) - max(fy, by))
                inter = ix * iy
                union = fw * fh + bw * bh - inter
                key = (inter / union if union > 0 else 0.0) + 1e-6 * fw * fh
            if key > best_key:
                best_key, best = key, face
        return best

    # ----------------------------------------------------------------- #
    # Отбраковка кадров калибровки
    # ----------------------------------------------------------------- #
    def _quality_reject(
        self,
        frame: Any,
        meta: dict[str, Any],
        yaw: float | None,
        pitch: float | None,
    ) -> str | None:
        """None — кадр годен для эталона, иначе причина отбраковки (по-русски)."""
        bbox = meta.get("bbox")
        if bbox is None:
            return "лицо не найдено"
        x, y, w, h = bbox
        if min(w, h) < self.min_face_px:
            return "лицо слишком мелкое — сядьте ближе"
        shape = meta.get("frame_shape")
        if shape:
            area_ratio = (w * h) / float(max(1, shape[0] * shape[1]))
            if area_ratio < self.min_area_ratio:
                return "лицо занимает слишком малую часть кадра"
        if meta.get("det_score", 0.0) < self.min_det_score:
            return "низкая уверенность детектора лица"

        # Поворот головы: сначала доверяем FaceAnalyzer, если он передал углы.
        if yaw is not None and abs(float(yaw)) > 18.0:
            return "голова повёрнута — смотрите в камеру"
        if pitch is not None and abs(float(pitch)) > 18.0:
            return "голова наклонена — смотрите в камеру"

        kps = meta.get("kps")
        if kps is not None and np.asarray(kps).shape[0] >= 5:
            k = np.asarray(kps, dtype=np.float32)
            le, re, nose, lm, rm = k[0], k[1], k[2], k[3], k[4]
            eye_mid = (le + re) / 2.0
            axis = re - le
            eye_dist = float(np.linalg.norm(axis))
            if eye_dist < 4.0:
                return "глаза не распознаны"
            # Поворот (yaw): смещение носа вдоль линии глаз относительно её середины.
            t = float(np.dot(nose - eye_mid, axis) / (eye_dist * eye_dist))
            if abs(t) > self.max_yaw_ratio:
                return "голова повёрнута в сторону"
            # Наклон (roll): угол линии глаз.
            roll = abs(math.degrees(math.atan2(float(axis[1]), float(axis[0]))))
            roll = min(roll, 180.0 - roll)
            if roll > self.max_roll_deg:
                return "голова наклонена набок"
            # Тангаж (pitch): где нос между линией глаз и линией рта.
            mouth_mid = (lm + rm) / 2.0
            span = float(mouth_mid[1] - eye_mid[1])
            if abs(span) > 4.0:
                ratio = float(nose[1] - eye_mid[1]) / span
                lo, hi = self.pitch_ratio_range
                if not (lo <= ratio <= hi):
                    return "голова опущена или поднята"

        # Смаз/расфокус — на размытом кадре эмбеддинг «уезжает».
        box = _norm_bbox(bbox, frame.shape[1], frame.shape[0])
        if box is not None:
            x0, y0, bw, bh = box
            patch = _to_gray(frame[y0 : y0 + bh, x0 : x0 + bw])
            if _laplacian_var(patch) < self.min_sharpness:
                return "кадр смазан — не двигайтесь"
        return None

    # ----------------------------------------------------------------- #
    # enroll
    # ----------------------------------------------------------------- #
    def enroll(
        self,
        frame_bgr: Any,
        face_bbox: Any = None,
        yaw: float | None = None,
        pitch: float | None = None,
    ) -> bool:
        """Принять один кадр калибровки в эталон.

        Возвращает True, если кадр ПРИНЯТ (прошёл отбраковку и эмбеддинг посчитан),
        False — если кадр отброшен. Причина последней отбраковки доступна в
        `enroll_progress()["reason"]` — оболочка показывает её студенту.
        Как только принятых кадров >= `min_enroll_frames`, эталон пересчитывается,
        и `enrolled` становится True (дальнейшие кадры только уточняют эталон).
        """
        if not self.available():
            self._enroll_rejected["канал недоступен"] = (
                self._enroll_rejected.get("канал недоступен", 0) + 1
            )
            return False
        if len(self._enroll_embeddings) >= self.enroll_frames:
            return False
        try:
            frame = np.asarray(frame_bgr)
        except Exception:
            return False
        emb, meta = self._extract(frame, face_bbox)
        if emb is None:
            self._note_reject("лицо не найдено")
            return False
        reason = self._quality_reject(frame, meta, yaw, pitch)
        if reason is not None:
            self._note_reject(reason)
            return False
        self._enroll_embeddings.append(emb)
        self._rebuild_reference()
        return True

    def _note_reject(self, reason: str) -> None:
        self._enroll_rejected[reason] = self._enroll_rejected.get(reason, 0) + 1
        self._last_reject = reason

    def _rebuild_reference(self) -> None:
        """Эталон = нормированное среднее эмбеддингов с выбросом аутлайеров.

        Аутлайер — эмбеддинг, косинус которого к предварительному среднему ниже
        `enroll_outlier_cos` или ниже (среднее - 2*sigma). Так из эталона вылетают
        кадры, где детектор поймал чужое лицо или сильно смазанное своё.
        """
        if not self._enroll_embeddings or len(self._enroll_embeddings) < self.min_enroll_frames:
            self._reference = None
            self._reference_cohesion = 0.0
            return
        mat = np.stack(self._enroll_embeddings).astype(np.float32)
        mean = mat.mean(axis=0)
        norm = float(np.linalg.norm(mean))
        if norm < 1e-6:
            self._reference = None
            return
        mean /= norm
        cos = mat @ mean
        keep = cos >= self.enroll_outlier_cos
        if cos.size >= 4:
            keep &= cos >= (float(cos.mean()) - 2.0 * float(cos.std()) - 1e-6)
        if int(keep.sum()) >= self.min_enroll_frames:
            mat = mat[keep]
            mean = mat.mean(axis=0)
            n2 = float(np.linalg.norm(mean))
            if n2 > 1e-6:
                mean /= n2
            cos = mat @ mean
        self._reference = mean
        self._reference_cohesion = float(cos.mean())

    def finalize_enroll(self) -> dict[str, Any]:
        """Завершить калибровку личности. Результат уходит в `calibration.result`."""
        self._rebuild_reference()
        return {
            "enrolled": self.enrolled,
            "accepted": len(self._enroll_embeddings),
            "needed": self.min_enroll_frames,
            "cohesion": round(self._reference_cohesion, 4),
            "threshold": self.threshold,
            "rejected": dict(self._enroll_rejected),
            "available": self.available(),
            "error": self.last_error,
        }

    def enroll_progress(self) -> dict[str, Any]:
        accepted = len(self._enroll_embeddings)
        needed = self.min_enroll_frames
        return {
            "accepted": accepted,
            "needed": needed,
            "progress": min(1.0, accepted / float(max(1, needed))),
            "ready": self.enrolled,
            "reason": getattr(self, "_last_reject", ""),
            "available": self.available(),
        }

    @property
    def enrolled(self) -> bool:
        return self._reference is not None

    def reset_enrollment(self) -> None:
        self._enroll_embeddings.clear()
        self._enroll_rejected.clear()
        self._reference = None
        self._reference_cohesion = 0.0
        self._last_reject = ""

    def export_reference(self) -> list[float] | None:
        """Эталон для сохранения в сессии (список float). Фото НЕ сохраняется."""
        if self._reference is None:
            return None
        return [float(v) for v in self._reference]

    def load_reference(self, vector: Any) -> bool:
        """Загрузить эталон из прошлой сессии (например, из профиля студента)."""
        if np is None or vector is None:
            return False
        try:
            emb = np.asarray(list(vector), dtype=np.float32).reshape(-1)
            norm = float(np.linalg.norm(emb))
            if emb.size < 64 or norm < 1e-6:
                return False
            self._reference = emb / norm
            self._reference_cohesion = 1.0
            return True
        except Exception:
            return False

    # ----------------------------------------------------------------- #
    # verify
    # ----------------------------------------------------------------- #
    def verify(self, frame_bgr: Any, face_bbox: Any = None) -> IdentityObservation:
        """Сравнить текущее лицо с эталоном.

        Решение НЕ принимается по одному кадру: каждая проверка кладёт голос в
        скользящее окно длины `window`, и состояние переключается только когда
        доля голосов превышает порог (гистерезис):
          ok -> mismatch:  доля «чужих» >= mismatch_ratio (0.6) при >= min_checks
          mismatch -> ok:  доля «своих»  >= recover_ratio  (0.6)
        Вызовы чаще, чем раз в `check_interval_s`, возвращают прошлое наблюдение
        с `checked=False` — эмбеддинг не считается, CPU не тратится.
        """
        now = time.time()
        if not self.available():
            obs = IdentityObservation(
                match=True,
                enrolled=self.enrolled,
                available=False,
                threshold=self.threshold,
                reason=self.last_error or "канал личности отключён",
                ts=now,
            )
            self._last_obs = obs
            return obs
        if not self.enrolled:
            obs = IdentityObservation(
                match=True,
                enrolled=False,
                available=True,
                threshold=self.threshold,
                reason="эталон личности не снят",
                ts=now,
            )
            self._last_obs = obs
            return obs
        if now - self._last_check_ts < self.check_interval_s:
            cached = self._last_obs
            return IdentityObservation(
                match=cached.match,
                similarity=cached.similarity,
                enrolled=True,
                available=True,
                checked=False,
                face_found=cached.face_found,
                votes=len(self._votes),
                mismatch_ratio=self._current_mismatch_ratio(),
                threshold=self.threshold,
                quality=cached.quality,
                reason=cached.reason,
                ts=now,
            )

        self._last_check_ts = now
        emb, meta = self._extract(frame_bgr, face_bbox)
        if emb is None:
            # Нет лица — это канал NO_FACE, а не подмена: голос в окно не кладём.
            obs = IdentityObservation(
                match=self._state_ok,
                similarity=self._similarity_ema,
                enrolled=True,
                available=True,
                checked=True,
                face_found=False,
                votes=len(self._votes),
                mismatch_ratio=self._current_mismatch_ratio(),
                threshold=self.threshold,
                reason="лицо не найдено — сравнение пропущено",
                ts=now,
            )
            self._last_obs = obs
            return obs

        similarity = float(np.dot(self._reference, emb))
        self._checks_total += 1
        self._similarity_ema = (
            similarity
            if self._checks_total == 1
            else 0.7 * self._similarity_ema + 0.3 * similarity
        )
        good = similarity >= self.threshold
        self._votes.append(good)
        self._mismatch_streak = 0 if good else self._mismatch_streak + 1

        bad_ratio = self._current_mismatch_ratio()
        good_ratio = 1.0 - bad_ratio
        enough = len(self._votes) >= self.min_checks
        if self._state_ok:
            if enough and bad_ratio >= self.mismatch_ratio_cfg:
                self._state_ok = False
        else:
            if enough and good_ratio >= self.recover_ratio_cfg:
                self._state_ok = True

        if self._state_ok:
            reason = f"личность подтверждена (близость {similarity:.2f})"
        else:
            reason = (
                "лицо не совпадает с эталоном калибровки "
                f"(близость {similarity:.2f} < {self.threshold:.2f})"
            )
        obs = IdentityObservation(
            match=self._state_ok,
            similarity=similarity,
            enrolled=True,
            available=True,
            checked=True,
            face_found=True,
            votes=len(self._votes),
            mismatch_ratio=bad_ratio,
            threshold=self.threshold,
            quality=float(meta.get("det_score", 0.0)),
            reason=reason,
            ts=now,
        )
        self._last_obs = obs
        return obs

    def _current_mismatch_ratio(self) -> float:
        if not self._votes:
            return 0.0
        return float(sum(1 for v in self._votes if not v)) / float(len(self._votes))

    # ----------------------------------------------------------------- #
    # Liveness: признаки, не события
    # ----------------------------------------------------------------- #
    def note_blink(self, blinked: bool, ts: float | None = None) -> None:
        """Зарегистрировать моргание (флаг приходит от FaceAnalyzer)."""
        if blinked:
            self._blinks_total += 1
            self._time_since_blink = 0.0
        _ = ts  # время берём из основного цикла update_liveness

    def update_liveness(
        self,
        frame_bgr: Any,
        face_bbox: Any = None,
        blink: bool | None = None,
        ts: float | None = None,
    ) -> LivenessObservation:
        """Посчитать признаки живости по текущему кадру.

        Три независимых признака:

        1. **Нет морганий.** Таймер идёт только когда лицо видно (иначе отсутствие
           человека выглядело бы как фото). `no_blink` = таймер > `no_blink_timeout_s`.
        2. **Статичность области лица.** Средний |разность| серого кропа лица между
           соседними кадрами; медиана по окну ниже `static_mad_threshold` — перед
           камерой фото, стоп-кадр или подставленный статичный поток.
        3. **Повтор последовательности (loop).** aHash кадра по сетке 8x8;
           для каждого лага L считается средняя нормированная дистанция Хэмминга
           между h[i] и h[i-L]; минимальная ниже `loop_tolerance` при достаточном
           разнообразии кадров => видеозапись крутится по кругу, период = L.
           Проверка разнообразия обязательна: на статичном кадре совпадают все лаги,
           и это не loop, а признак №2.

        Итог — `score` (взвешенная сумма) и `suspect` (score >= `suspect_threshold`).
        Движок превращает `suspect` в наблюдение LIVENESS_FAIL со своим окном
        подтверждения; сам модуль событий не создаёт.
        """
        now = time.time() if ts is None else float(ts)
        obs = LivenessObservation(ts=now)
        if np is None:
            obs.reasons.append("numpy недоступен")
            return obs

        dt = 0.0
        if self._last_frame_ts is not None:
            dt = max(0.0, min(1.0, now - self._last_frame_ts))  # защита от стопов цикла
            if dt > 1e-4:
                inst = 1.0 / dt
                self._fps_estimate = 0.9 * self._fps_estimate + 0.1 * inst
        self._last_frame_ts = now

        if blink:
            self.note_blink(True, now)

        try:
            frame = np.asarray(frame_bgr)
            if frame.size == 0:
                obs.reasons.append("пустой кадр")
                return obs
            gray = _to_gray(frame)
        except Exception as exc:
            obs.reasons.append(f"кадр не разобран: {exc}")
            return obs

        h, w = gray.shape[:2]
        box = _norm_bbox(face_bbox, w, h)
        obs.face_present = box is not None
        self._frames_seen += 1
        obs.frames = self._frames_seen
        obs.blinks_total = self._blinks_total

        # --- 1. моргания ---
        if obs.face_present:
            self._time_since_blink += dt
        obs.seconds_since_blink = self._time_since_blink
        obs.no_blink = (
            obs.face_present and self._time_since_blink > self.no_blink_timeout_s
        )

        # --- 2. статичность области лица ---
        if box is not None:
            x, y, bw, bh = box
            region = gray[y : y + bh, x : x + bw]
        else:
            # Лица нет — смотрим центральную часть кадра, чтобы не терять сигнал.
            y0, y1 = int(h * 0.2), int(h * 0.8)
            x0, x1 = int(w * 0.2), int(w * 0.8)
            region = gray[y0:y1, x0:x1]
        patch = _resample(region, self.patch_size)
        if self._prev_patch is not None and self._prev_patch.shape == patch.shape:
            self._motion.append(float(np.abs(patch - self._prev_patch).mean()))
        self._prev_patch = patch
        if len(self._motion) >= self.static_min_frames:
            motion = float(np.median(np.asarray(self._motion, dtype=np.float32)))
            obs.motion_level = motion
            obs.static_frame = motion < self.static_mad_threshold
        elif self._motion:
            obs.motion_level = float(self._motion[-1])

        # --- 3. повтор последовательности ---
        self._hashes.append(_frame_hash(gray, self.hash_grid))
        if now - self._last_loop_check >= self.loop_check_interval_s:
            self._last_loop_check = now
            self._loop_cache = self._detect_loop()
        loop, period, similarity, diversity = self._loop_cache
        obs.loop_detected = loop
        obs.loop_period_frames = period
        obs.loop_similarity = similarity
        obs.scene_diversity = diversity
        obs.loop_period_s = (
            period / self._fps_estimate if period and self._fps_estimate > 0.1 else 0.0
        )

        # --- свод ---
        score = 0.0
        if obs.no_blink:
            score += self.w_no_blink
            obs.reasons.append(
                f"нет морганий {obs.seconds_since_blink:.0f} с при видимом лице"
            )
        if obs.static_frame:
            score += self.w_static
            obs.reasons.append(
                f"изображение лица статично (движение {obs.motion_level:.2f})"
            )
        if obs.loop_detected:
            score += self.w_loop
            obs.reasons.append(
                "кадры повторяются с периодом "
                f"{obs.loop_period_frames} кадров (~{obs.loop_period_s:.1f} с) — "
                "похоже на зацикленную запись"
            )
        obs.score = min(1.0, score)
        obs.suspect = obs.score >= self.suspect_threshold
        return obs

    def _detect_loop(self) -> tuple[bool, int, float, float]:
        """Поиск периода повтора по истории хешей.

        Возвращает (loop, период в кадрах, похожесть 0..1, разнообразие 0..1).
        Разнообразие — средняя нормированная дистанция Хэмминга на большом лаге;
        если оно ниже `min_scene_diversity`, сцена статична и о цикле речи нет.
        """
        n = len(self._hashes)
        if np is None or n < self.min_loop_frames:
            return (False, 0, 0.0, 0.0)
        H = np.stack(self._hashes).astype(np.int8)
        long_lag = max(1, n // 4)
        diversity = float(np.abs(H[long_lag:] - H[:-long_lag]).mean())
        if diversity < self.min_scene_diversity:
            # Всё одинаковое: это признак «статичный кадр», а не цикл.
            return (False, 0, 0.0, diversity)

        # Ищем НАИМЕНЬШИЙ подходящий лаг: кратные ему тоже совпадают, но нас
        # интересует основной период, а не его гармоника.
        max_lag = n // 2
        min_overlap = max(self.min_loop_period, 20)
        best_dist = 1.0
        for lag in range(self.min_loop_period, max_lag + 1):
            if n - lag < min_overlap:
                break
            dist = float(np.abs(H[lag:] - H[:-lag]).mean())
            best_dist = min(best_dist, dist)
            if dist <= self.loop_tolerance:
                return (True, lag, 1.0 - dist, diversity)
        return (False, 0, 1.0 - best_dist, diversity)

    # ----------------------------------------------------------------- #
    # Служебное
    # ----------------------------------------------------------------- #
    def reset_session(self) -> None:
        """Сбросить рантайм-состояние, эталон сохраняется."""
        self._votes.clear()
        self._state_ok = True
        self._last_check_ts = 0.0
        self._similarity_ema = 0.0
        self._checks_total = 0
        self._mismatch_streak = 0
        self._prev_patch = None
        self._motion.clear()
        self._hashes.clear()
        self._frames_seen = 0
        self._blinks_total = 0
        self._time_since_blink = 0.0
        self._last_frame_ts = None
        self._last_loop_check = 0.0
        self._loop_cache = (False, 0, 0.0, 0.0)
        self._last_obs = IdentityObservation(threshold=self.threshold)

    def status(self) -> dict[str, Any]:
        """Короткая сводка для сообщения `status` протокола."""
        return {
            "available": self.available(),
            "enrolled": self.enrolled,
            "identity_ok": bool(self._last_obs.match),
            "similarity": round(float(self._similarity_ema), 3),
            "threshold": self.threshold,
            "cohesion": round(self._reference_cohesion, 3),
            "checks": self._checks_total,
            "fps_estimate": round(self._fps_estimate, 1),
            "model": self.model_name,
            "error": self.last_error,
        }
