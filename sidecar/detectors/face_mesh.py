"""
Анализ лица: поза головы, направление взгляда, моргания, открытие рта.

Пункт 2.2 ТЗ — самая чувствительная к ложным срабатываниям часть, поэтому модуль
возвращает ТОЛЬКО наблюдения (FaceObservation). Решение «это инцидент» принимает
EventEngine: он держит окно подтверждения, гистерезис и cooldown.

Как считается:
  1. MediaPipe FaceMesh (refine_landmarks=True -> 478 точек, включая радужки),
     max_num_faces=2, чтобы заметить второе лицо в кадре.
  2. Поза головы — cv2.solvePnP (SOLVEPNP_ITERATIVE) по 6 каноническим точкам
     (нос, подбородок, внешние углы глаз, углы рта). Модель 3D задана в системе
     координат камеры OpenCV (x вправо, y вниз, z от камеры), поэтому фронтальное
     лицо даёт R ≈ I и углы около нуля — это убирает классические скачки на ±180
     при разложении Родригеса в углы Эйлера. Дополнительно углы заворачиваются в
     (-180, 180] и сглаживаются с учётом перехода через границу.
  3. Взгляд считается ОТДЕЛЬНО от позы: центр радужки (468 — правый глаз,
     473 — левый) относительно середины углов глазницы, спроецированный на оси
     самой глазницы (ось «наружный угол -> внутренний угол» и перпендикуляр к ней),
     нормированный на половину ширины/высоты глаза. Проекция на оси глазницы делает
     оценку инвариантной к наклону головы (roll). Глаз с низкой видимостью
     (сильный поворот головы, закрытый глаз, развалившаяся радужка) отбрасывается.
  4. Моргание — eye aspect ratio с АДАПТИВНОЙ базой (у разных людей разрез глаз
     разный) и гистерезисом. Нужен для liveness: отсутствие морганий >20 с —
     подозрение на подсунутое видео/фото.
  5. mouth_open_ratio — для проверки «звук есть, губы не двигаются» (другой модуль).
  6. Все углы и ratio сглаживаются: медиана по окну 5 (убивает выбросы landmarks)
     + экспоненциальное среднее (убивает дрожание). EAR сглаживается слабее,
     иначе моргание размазывается и перестаёт детектиться.

Зона взгляда (gaze_zone) считается ОТНОСИТЕЛЬНО КАЛИБРОВКИ, а не абсолютных чисел.
Калибровку делает sidecar/engine/calibration.py и передаёт сюда словарём
через apply_calibration(). Формат словаря (schema v1) — общий для двух модулей,
см. CALIBRATION_SCHEMA_VERSION ниже. Модуль намеренно не импортирует calibration.py
(детектор обязан быть самодостаточным), поэтому вычисление полинома дублируется
в _eval_screen_map().

Деградация: нет mediapipe/cv2/numpy -> available() == False, analyze() возвращает
пустое наблюдение с полем error и ничего не ломает.
"""
from __future__ import annotations

import math
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Индексы landmarks MediaPipe FaceMesh (478 точек, refine_landmarks=True)
# --------------------------------------------------------------------------
IDX_NOSE_TIP = 1
IDX_CHIN = 152
IDX_R_EYE_OUTER = 33      # внешний угол ПРАВОГО глаза субъекта (слева в кадре)
IDX_R_EYE_INNER = 133
IDX_R_EYE_UPPER = 159
IDX_R_EYE_LOWER = 145
IDX_L_EYE_OUTER = 263     # внешний угол ЛЕВОГО глаза субъекта (справа в кадре)
IDX_L_EYE_INNER = 362
IDX_L_EYE_UPPER = 386
IDX_L_EYE_LOWER = 374
IDX_MOUTH_LEFT = 61       # левый в кадре угол рта
IDX_MOUTH_RIGHT = 291
IDX_LIP_UPPER_IN = 13
IDX_LIP_LOWER_IN = 14

#: Радужки. 468 — центр правой (глаз с точками 33/133), 473 — центр левой.
IDX_R_IRIS_CENTER = 468
IDX_R_IRIS_RING = (469, 470, 471, 472)
IDX_L_IRIS_CENTER = 473
IDX_L_IRIS_RING = (474, 475, 476, 477)

#: Точки для EAR в порядке p1..p6: EAR = (|p2-p6| + |p3-p5|) / (2|p1-p4|)
EAR_RIGHT = (33, 160, 158, 133, 153, 144)
EAR_LEFT = (362, 385, 387, 263, 380, 373)

#: Точки для solvePnP (порядок должен совпадать с MODEL_POINTS_3D)
POSE_IDX = (IDX_NOSE_TIP, IDX_CHIN, IDX_R_EYE_OUTER, IDX_L_EYE_OUTER,
            IDX_MOUTH_LEFT, IDX_MOUTH_RIGHT)

#: Канонические 3D-точки лица, мм. Система координат камеры OpenCV:
#: x — вправо по кадру, y — ВНИЗ по кадру, z — от камеры в глубину.
#: Благодаря такому выбору фронтальное лицо -> углы ≈ 0 (без скачков на ±180).
MODEL_POINTS_3D = (
    (0.0, 0.0, 0.0),          # кончик носа
    (0.0, 330.0, 65.0),       # подбородок
    (-225.0, -170.0, 135.0),  # внешний угол правого глаза (слева в кадре)
    (225.0, -170.0, 135.0),   # внешний угол левого глаза (справа в кадре)
    (-150.0, 150.0, 125.0),   # левый в кадре угол рта
    (150.0, 150.0, 125.0),    # правый в кадре угол рта
)

#: Версия формата калибровочного словаря, общего с engine/calibration.py
CALIBRATION_SCHEMA_VERSION = 1

GAZE_ZONES = ("center", "up", "down", "left", "right", "off_screen")

# --------------------------------------------------------------------------
# Конфиг. Значения читаются из config: сначала из секций, потом из корня,
# иначе берётся дефолт отсюда. Так модуль не зависит от того, как именно
# sidecar/config.py разложит ключи по секциям.
# --------------------------------------------------------------------------
_CFG_SECTIONS = ("face_mesh", "face", "gaze", "vision")

DEFAULTS: dict[str, Any] = {
    # mediapipe
    "max_num_faces": 2,
    "min_detection_confidence": 0.5,
    "min_tracking_confidence": 0.5,
    "static_image_mode": False,

    # сглаживание
    "median_window": 5,          # медианный фильтр по окну 5 кадров
    "smoothing_alpha": 0.35,     # EMA для yaw/pitch/roll
    "gaze_alpha": 0.25,          # взгляд шумнее позы — сглаживаем сильнее
    "mouth_alpha": 0.5,          # рту нужна реакция (проверка артикуляции)
    "ear_alpha": 0.7,            # EAR почти не сглаживаем, иначе теряем моргания
    "ear_median_window": 3,
    "smoother_reset_sec": 1.0,   # лицо пропало дольше — сбрасываем фильтры

    # поза
    "max_reproj_ratio": 0.07,    # ошибка репроекции > 7% ширины лица -> поза невалидна
    "max_reproj_px": 6.0,        # но не строже 6 px (мелкое лицо)
    "max_abs_roll": 150.0,       # |roll| больше — считаем, что решение развалилось

    # взгляд
    "min_eye_visibility": 0.35,
    "eye_width_face_ratio": 0.20,    # ожидаемая ширина глаза / ширина лица
    "iris_radius_eye_ratio": 0.20,   # ожидаемый радиус радужки / ширина глаза
    "eye_height_floor_ratio": 0.30,  # пол высоты глаза (от ширины) при моргании

    # пороги по умолчанию (используются, ПОКА НЕТ калибровки)
    "gaze_h_floor": 0.22,        # ед. = доля половины ширины глаза
    "gaze_up_floor": 0.18,
    "gaze_down_floor": 0.26,     # вниз порог шире: на ноутбуке вниз смотрят законно
    "head_yaw_floor": 18.0,      # градусы
    "head_pitch_up_floor": 13.0,
    "head_pitch_down_floor": 18.0,
    "off_screen_factor": 1.8,    # во столько раз превысить порог = вне экрана
    "off_screen_margin": 0.12,   # запас вокруг [0,1] экрана при наличии карты
    "screen_center_box": 0.30,   # полуширина «центральной» зоны экрана

    # моргания / liveness
    "ear_baseline_init": 0.30,
    "ear_baseline_alpha": 0.05,
    "ear_close_ratio": 0.72,     # ниже 72% базы — глаз закрыт
    "ear_open_ratio": 0.85,      # выше 85% базы — глаз открыт (гистерезис)
    "blink_min_frames": 1,       # кадров закрытия (по медиане EAR, а она уже
                                 # требует 2 сырых кадра подряд -> глитч не пройдёт)
    "blink_min_sec": 0.0,        # по времени не отсекаем: на 30 fps моргание ~2 кадра
    "blink_max_sec": 0.5,        # дольше — это не моргание, а закрытые глаза
    "eyes_closed_sec": 0.8,
    "blink_rate_window": 60.0,
    "blink_rate_min_window": 10.0,   # пока сессия короче — не раздуваем частоту
    "no_blink_seconds": 20.0,    # >20 с без морганий -> подозрение на видео

    # калибровка (для calibrate_center по контракту)
    "k_sigma": 3.0,
    "threshold_ceiling_factor": 2.5,
    "min_center_samples": 15,
}


def _as_dict(config: Any) -> dict[str, Any]:
    """Привести конфиг к словарю: принимаем и dict, и dataclass из config.py."""
    if isinstance(config, dict):
        return config
    if config is None:
        return {}
    try:
        import dataclasses  # noqa: PLC0415
        if dataclasses.is_dataclass(config) and not isinstance(config, type):
            return dataclasses.asdict(config)
    except Exception:
        pass
    for attr in ("to_dict", "as_dict", "dict"):
        fn = getattr(config, attr, None)
        if callable(fn):
            try:
                d = fn()
                if isinstance(d, dict):
                    return d
            except Exception:
                pass
    return dict(getattr(config, "__dict__", {}) or {})


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else (hi if x > hi else x)


def _finite(x: Any) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _wrap_deg(a: float) -> float:
    """Завернуть угол в (-180, 180]."""
    a = math.fmod(a + 180.0, 360.0)
    if a <= 0.0:
        a += 360.0
    return a - 180.0


def _robust_stats(values: list[float]) -> tuple[float, float]:
    """Медиана и робастная сигма (1.4826*MAD, но не меньше обычного std/4).

    MAD устойчив к выбросам (один дёрнувшийся кадр не раздует порог),
    но у очень статичного человека MAD может быть нулевым — тогда берём std.
    """
    vals = [float(v) for v in values if _finite(v)]
    if not vals:
        return 0.0, 0.0
    med = statistics.median(vals)
    if len(vals) < 3:
        return med, 0.0
    mad = statistics.median([abs(v - med) for v in vals])
    sigma_mad = 1.4826 * mad
    try:
        sigma_std = statistics.stdev(vals)
    except statistics.StatisticsError:
        sigma_std = 0.0
    return med, max(sigma_mad, sigma_std * 0.25)


class _Smoother:
    """Медианный фильтр по окну + EMA. Для углов учитывает переход через ±180."""

    def __init__(self, alpha: float, window: int, angular: bool = False) -> None:
        self.alpha = _clamp(float(alpha), 0.01, 1.0)
        self.buf: deque[float] = deque(maxlen=max(1, int(window)))
        self.value: float | None = None
        self.angular = bool(angular)

    def push(self, x: Any) -> float | None:
        if not _finite(x):
            return self.value
        x = float(x)
        if self.angular and self.value is not None:
            # разворачиваем относительно текущего значения, чтобы медиана и EMA
            # не прыгали на границе ±180
            x = self.value + _wrap_deg(x - self.value)
        self.buf.append(x)
        med = statistics.median(self.buf)
        if self.value is None:
            self.value = med
        else:
            self.value += self.alpha * (med - self.value)
        if self.angular:
            self.value = _wrap_deg(self.value)
        return self.value

    def reset(self) -> None:
        self.buf.clear()
        self.value = None


@dataclass
class FaceObservation:
    """Наблюдение по одному кадру. НЕ событие — события порождает EventEngine.

    Нормированные девиации (gaze_dev_h/gaze_dev_v/head_dev_yaw/head_dev_pitch)
    выражены в «долях персонального порога»: |dev| > 1 означает выход за порог,
    посчитанный по калибровке этого человека. EventEngine сравнивает с 1.0 и не
    должен знать ни про градусы, ни про геометрию глаза.
    """
    ts: float = 0.0
    ok: bool = False                       # кадр обработан и лицо найдено
    error: str | None = None

    face_count: int = 0
    face_bbox: tuple[float, float, float, float] | None = None   # x, y, w, h в px
    faces: list[tuple[float, float, float, float]] = field(default_factory=list)
    second_face_bbox: tuple[float, float, float, float] | None = None
    face_area_ratio: float = 0.0

    # поза головы, градусы (сглаженные)
    yaw: float = 0.0        # >0 — лицо повёрнуто в правую половину кадра
    pitch: float = 0.0      # >0 — голова поднята
    roll: float = 0.0       # >0 — наклон к правому в кадре плечу
    pose_ok: bool = False
    reproj_error: float = 0.0

    # взгляд в глазнице, условные единицы (доля половины ширины/высоты глаза)
    gaze_yaw: float = 0.0   # >0 — радужка смещена вправо по кадру
    gaze_pitch: float = 0.0  # >0 — взгляд вверх
    gaze_ok: bool = False
    eye_visibility: tuple[float, float] = (0.0, 0.0)   # (правый, левый)

    gaze_zone: str = "unknown"          # center|up|down|left|right|off_screen
    gaze_region: str = "unknown"        # уточнение по карте экрана
    gaze_point: tuple[float, float] | None = None   # норм. координаты экрана
    gaze_off_direction: str | None = None
    gaze_dev_h: float = 0.0
    gaze_dev_v: float = 0.0
    head_dev_yaw: float = 0.0
    head_dev_pitch: float = 0.0

    eye_aspect_ratio: float = 0.0
    ear_right: float = 0.0
    ear_left: float = 0.0
    mouth_open_ratio: float = 0.0

    blink: bool = False                 # в этом кадре завершилось моргание
    blink_count: int = 0
    blink_rate: float = 0.0             # морганий в минуту (окно 60 с)
    seconds_since_blink: float = 0.0
    blink_stale: bool = False           # морганий нет дольше no_blink_seconds
    eyes_closed: bool = False

    motion_score: float = 0.0           # средний сдвиг landmarks / ширина лица

    calibrated: bool = False
    screen_map: bool = False

    landmarks: Any = None               # (478,3) px: ndarray или list[tuple]
    raw: dict[str, float] = field(default_factory=dict)   # несглаженные значения

    def to_dict(self) -> dict[str, Any]:
        """Компактный словарь без landmarks — пригоден для JSON/логов."""
        return {
            "ts": self.ts, "ok": self.ok, "error": self.error,
            "face_count": self.face_count, "face_bbox": self.face_bbox,
            "face_area_ratio": self.face_area_ratio,
            "yaw": self.yaw, "pitch": self.pitch, "roll": self.roll,
            "pose_ok": self.pose_ok, "reproj_error": self.reproj_error,
            "gaze_yaw": self.gaze_yaw, "gaze_pitch": self.gaze_pitch,
            "gaze_ok": self.gaze_ok, "gaze_zone": self.gaze_zone,
            "gaze_region": self.gaze_region, "gaze_point": self.gaze_point,
            "gaze_off_direction": self.gaze_off_direction,
            "gaze_dev_h": self.gaze_dev_h, "gaze_dev_v": self.gaze_dev_v,
            "head_dev_yaw": self.head_dev_yaw, "head_dev_pitch": self.head_dev_pitch,
            "eye_aspect_ratio": self.eye_aspect_ratio,
            "mouth_open_ratio": self.mouth_open_ratio,
            "blink": self.blink, "blink_count": self.blink_count,
            "blink_rate": self.blink_rate,
            "seconds_since_blink": self.seconds_since_blink,
            "blink_stale": self.blink_stale, "eyes_closed": self.eyes_closed,
            "motion_score": self.motion_score,
            "eye_visibility": list(self.eye_visibility),
            "calibrated": self.calibrated, "screen_map": self.screen_map,
        }


class FaceAnalyzer:
    """MediaPipe FaceMesh + поза головы + взгляд + моргания + артикуляция."""

    def __init__(self, config: dict | None = None) -> None:
        self._cfg = _as_dict(config)
        self._np = None
        self._cv2 = None
        self._mesh = None
        self._init_error: str | None = None

        w = int(self._c("median_window"))
        a = float(self._c("smoothing_alpha"))
        ga = float(self._c("gaze_alpha"))
        self._sm = {
            "yaw": _Smoother(a, w, angular=True),
            "pitch": _Smoother(a, w, angular=True),
            "roll": _Smoother(a, w, angular=True),
            "gaze_yaw": _Smoother(ga, w),
            "gaze_pitch": _Smoother(ga, w),
            "mouth": _Smoother(float(self._c("mouth_alpha")), 3),
            "ear": _Smoother(float(self._c("ear_alpha")),
                             int(self._c("ear_median_window"))),
        }
        # медиана EAR без EMA — на ней работает детектор морганий
        self._ear_buf: deque[float] = deque(maxlen=int(self._c("ear_median_window")))

        # состояние детектора морганий
        self._ear_baseline = float(self._c("ear_baseline_init"))
        self._eye_closed = False
        self._close_start = 0.0
        self._closed_frames = 0
        self._blink_count = 0
        self._blink_ts: deque[float] = deque()
        self._last_blink_ts: float | None = None
        self._first_seen_ts: float | None = None

        self._last_face_ts = 0.0
        self._prev_landmarks = None
        self._rvec = None
        self._tvec = None
        self._model_np = None
        self._calib: dict[str, Any] = {}

        self._lazy_init()

    # ------------------------------------------------------------------
    # конфиг
    # ------------------------------------------------------------------
    def _c(self, key: str) -> Any:
        for sec in _CFG_SECTIONS:
            node = self._cfg.get(sec)
            if isinstance(node, dict) and key in node:
                return node[key]
        if key in self._cfg:
            return self._cfg[key]
        return DEFAULTS[key]

    # ------------------------------------------------------------------
    # инициализация
    # ------------------------------------------------------------------
    def _lazy_init(self) -> None:
        """Ленивый импорт тяжёлых зависимостей. Любая ошибка -> деградация."""
        try:
            import numpy as np  # noqa: PLC0415
            self._np = np
        except Exception as exc:                      # pragma: no cover
            self._init_error = f"numpy недоступен: {exc}"
            return
        try:
            import cv2  # noqa: PLC0415
            self._cv2 = cv2
        except Exception as exc:                      # pragma: no cover
            self._init_error = f"OpenCV недоступен: {exc}"
            return
        try:
            import mediapipe as mp  # noqa: PLC0415
            self._mesh = mp.solutions.face_mesh.FaceMesh(
                static_image_mode=bool(self._c("static_image_mode")),
                max_num_faces=max(1, int(self._c("max_num_faces"))),
                refine_landmarks=True,
                min_detection_confidence=float(self._c("min_detection_confidence")),
                min_tracking_confidence=float(self._c("min_tracking_confidence")),
            )
        except Exception as exc:
            self._mesh = None
            self._init_error = f"MediaPipe FaceMesh недоступен: {exc}"
            return
        self._model_np = self._np.array(MODEL_POINTS_3D, dtype="float64")
        self._init_error = None

    def available(self) -> bool:
        return self._mesh is not None

    def status(self) -> dict[str, Any]:
        return {"available": self.available(), "error": self._init_error,
                "calibrated": bool(self._calib),
                "screen_map": self._has_screen_map()}

    def close(self) -> None:
        if self._mesh is not None:
            try:
                self._mesh.close()
            except Exception:
                pass
        self._mesh = None

    # ------------------------------------------------------------------
    # калибровка
    # ------------------------------------------------------------------
    def apply_calibration(self, data: dict | None) -> bool:
        """Принять калибровку (словарь schema v1 из engine/calibration.py)."""
        if not isinstance(data, dict):
            return False
        if int(data.get("version", 0)) != CALIBRATION_SCHEMA_VERSION:
            return False
        if not isinstance(data.get("thresholds"), dict):
            return False
        self._calib = data
        return True

    def clear_calibration(self) -> None:
        self._calib = {}

    def calibration(self) -> dict[str, Any]:
        return dict(self._calib)

    def calibrate_center(self, samples: Any) -> dict[str, Any]:
        """Быстрая калибровка центра по контракту.

        samples — список FaceObservation или словарей. Берём медиану как базу,
        робастную сигму как разброс, порог = база ± k*sigma с полом (чтобы у
        статичного человека порог не выродился в нуль) и потолком (чтобы у
        вертлявого человека порог не стал бесконечным).

        Полноценная калибровка (включая 9-точечную сетку) живёт в
        engine/calibration.py; формат результата у них одинаковый.
        """
        keys = ("gaze_yaw", "gaze_pitch", "yaw", "pitch", "roll",
                "eye_aspect_ratio", "mouth_open_ratio")
        acc: dict[str, list[float]] = {k: [] for k in keys}
        used = 0
        for s in samples or []:
            if not _sample_get(s, "gaze_ok", False) and not _sample_get(s, "pose_ok", False):
                continue
            used += 1
            for k in keys:
                v = _sample_get(s, k, None)
                if _finite(v):
                    acc[k].append(float(v))
        stats = {k: _robust_stats(v) for k, v in acc.items()}
        base = {k: stats[k][0] for k in keys}
        sigma = {k: stats[k][1] for k in keys}
        k_sigma = float(self._c("k_sigma"))
        ceil_f = float(self._c("threshold_ceiling_factor"))

        def thr(floor_key: str, sigma_key: str) -> float:
            floor = float(self._c(floor_key))
            return _clamp(k_sigma * sigma.get(sigma_key, 0.0), floor, floor * ceil_f)

        data = {
            "version": CALIBRATION_SCHEMA_VERSION,
            "ts": time.time(),
            "samples": {"center": used, "grid": {}},
            "base": {
                "gaze_yaw": base["gaze_yaw"], "gaze_pitch": base["gaze_pitch"],
                "head_yaw": base["yaw"], "head_pitch": base["pitch"],
                "head_roll": base["roll"],
                "ear": base["eye_aspect_ratio"], "mouth": base["mouth_open_ratio"],
            },
            "sigma": {
                "gaze_yaw": sigma["gaze_yaw"], "gaze_pitch": sigma["gaze_pitch"],
                "head_yaw": sigma["yaw"], "head_pitch": sigma["pitch"],
                "head_roll": sigma["roll"],
                "ear": sigma["eye_aspect_ratio"], "mouth": sigma["mouth_open_ratio"],
            },
            "thresholds": {
                "gaze_h": thr("gaze_h_floor", "gaze_yaw"),
                "gaze_up": thr("gaze_up_floor", "gaze_pitch"),
                "gaze_down": thr("gaze_down_floor", "gaze_pitch"),
                "head_yaw": thr("head_yaw_floor", "yaw"),
                "head_pitch_up": thr("head_pitch_up_floor", "pitch"),
                "head_pitch_down": thr("head_pitch_down_floor", "pitch"),
                "off_screen_factor": float(self._c("off_screen_factor")),
            },
            "screen_map": {"kind": None},
            "quality": {
                "grade": "good" if used >= int(self._c("min_center_samples")) else "poor",
                "center_samples": used,
                "message": ("Базовая калибровка центра выполнена."
                            if used >= int(self._c("min_center_samples"))
                            else "Мало кадров калибровки центра, переснимите."),
            },
        }
        self.apply_calibration(data)
        return data

    # ------------------------------------------------------------------
    # основной проход
    # ------------------------------------------------------------------
    def analyze(self, frame_bgr: Any) -> FaceObservation:
        """Обработать кадр. Никогда не бросает исключений."""
        ts = time.time()
        if self._first_seen_ts is None:
            self._first_seen_ts = ts
        if self._mesh is None:
            return FaceObservation(ts=ts, error=self._init_error or "FaceMesh не инициализирован")
        try:
            return self._analyze_impl(frame_bgr, ts)
        except Exception as exc:                      # защита демо от любого сбоя
            return FaceObservation(ts=ts, error=f"сбой анализа лица: {exc}")

    def _analyze_impl(self, frame_bgr: Any, ts: float) -> FaceObservation:
        np, cv2 = self._np, self._cv2
        if frame_bgr is None or getattr(frame_bgr, "ndim", 0) != 3:
            return FaceObservation(ts=ts, error="пустой кадр")
        h, w = int(frame_bgr.shape[0]), int(frame_bgr.shape[1])
        if h < 16 or w < 16:
            return FaceObservation(ts=ts, error="слишком мелкий кадр")

        # сброс фильтров после длинной паузы без лица
        if self._last_face_ts and ts - self._last_face_ts > float(self._c("smoother_reset_sec")):
            for sm in self._sm.values():
                sm.reset()
            self._ear_buf.clear()
            self._prev_landmarks = None
            self._rvec = self._tvec = None

        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        res = self._mesh.process(rgb)
        faces = getattr(res, "multi_face_landmarks", None) or []
        if not faces:
            self._prev_landmarks = None
            obs = FaceObservation(ts=ts, ok=False, face_count=0)
            obs.calibrated = bool(self._calib)
            obs.screen_map = self._has_screen_map()
            obs.seconds_since_blink = ts - (self._last_blink_ts or self._first_seen_ts or ts)
            obs.blink_count = self._blink_count
            return obs

        # все лица -> пиксельные массивы, сортировка по площади bbox
        entries = []
        for fl in faces:
            pts = np.array([[lm.x * w, lm.y * h, lm.z * w] for lm in fl.landmark],
                           dtype="float64")
            bbox = _bbox_of(pts)
            entries.append((bbox[2] * bbox[3], pts, bbox))
        entries.sort(key=lambda e: e[0], reverse=True)
        _, pts, bbox = entries[0]

        obs = FaceObservation(ts=ts, ok=True, face_count=len(entries))
        obs.faces = [tuple(float(v) for v in e[2]) for e in entries]
        obs.face_bbox = obs.faces[0]
        obs.second_face_bbox = obs.faces[1] if len(obs.faces) > 1 else None
        obs.face_area_ratio = float(bbox[2] * bbox[3] / (w * h)) if w * h else 0.0
        obs.landmarks = pts
        self._last_face_ts = ts

        face_w = max(1e-6, float(bbox[2]))

        # --- поза головы ---
        pose = self._solve_pose(pts, w, h, face_w)
        raw: dict[str, float] = {}
        if pose is not None:
            yaw_r, pitch_r, roll_r, reproj = pose
            raw.update(yaw=yaw_r, pitch=pitch_r, roll=roll_r)
            obs.reproj_error = reproj
            obs.pose_ok = True
            obs.yaw = float(self._sm["yaw"].push(yaw_r) or 0.0)
            obs.pitch = float(self._sm["pitch"].push(pitch_r) or 0.0)
            obs.roll = float(self._sm["roll"].push(roll_r) or 0.0)
        else:
            obs.pose_ok = False
            for k in ("yaw", "pitch", "roll"):
                v = self._sm[k].value
                setattr(obs, k, float(v) if v is not None else 0.0)

        # --- EAR и моргания ---
        ear_r = _ear(pts, EAR_RIGHT)
        ear_l = _ear(pts, EAR_LEFT)
        ears = [e for e in (ear_r, ear_l) if e is not None]
        ear = sum(ears) / len(ears) if ears else None
        obs.ear_right = float(ear_r or 0.0)
        obs.ear_left = float(ear_l or 0.0)
        if ear is not None:
            raw["ear"] = ear
            self._ear_buf.append(ear)
            ear_med = statistics.median(self._ear_buf)
            obs.eye_aspect_ratio = float(self._sm["ear"].push(ear) or ear)
            self._update_blink(ear_med, ts, obs)
        else:
            v = self._sm["ear"].value
            obs.eye_aspect_ratio = float(v) if v is not None else 0.0
            self._update_blink(None, ts, obs)

        # --- открытие рта ---
        mar = _mouth_ratio(pts)
        if mar is not None:
            raw["mouth"] = mar
            obs.mouth_open_ratio = float(self._sm["mouth"].push(mar) or mar)
        else:
            v = self._sm["mouth"].value
            obs.mouth_open_ratio = float(v) if v is not None else 0.0

        # --- взгляд ---
        gaze = self._gaze(pts, face_w, self._ear_baseline, ear_r, ear_l)
        if gaze is not None:
            g_yaw, g_pitch, vis_r, vis_l = gaze
            raw.update(gaze_yaw=g_yaw, gaze_pitch=g_pitch)
            obs.eye_visibility = (vis_r, vis_l)
            obs.gaze_ok = True
            obs.gaze_yaw = float(self._sm["gaze_yaw"].push(g_yaw) or 0.0)
            obs.gaze_pitch = float(self._sm["gaze_pitch"].push(g_pitch) or 0.0)
        else:
            obs.gaze_ok = False
            for k in ("gaze_yaw", "gaze_pitch"):
                v = self._sm[k].value
                setattr(obs, k, float(v) if v is not None else 0.0)

        # --- движение landmarks (для liveness: фото даёт почти нуль) ---
        obs.motion_score = self._motion(pts, face_w)

        obs.raw = raw
        obs.calibrated = bool(self._calib)
        obs.screen_map = self._has_screen_map()
        self._classify_zone(obs)
        return obs

    # ------------------------------------------------------------------
    # поза
    # ------------------------------------------------------------------
    def _solve_pose(self, pts: Any, w: int, h: int,
                    face_w: float) -> tuple[float, float, float, float] | None:
        """solvePnP -> (yaw, pitch, roll, ошибка репроекции в px) либо None."""
        np, cv2 = self._np, self._cv2
        try:
            img = np.ascontiguousarray(pts[list(POSE_IDX), :2], dtype="float64")
        except Exception:
            return None
        focal = float(w)                      # грубая оценка фокуса ≈ ширина кадра
        cam = np.array([[focal, 0.0, w / 2.0],
                        [0.0, focal, h / 2.0],
                        [0.0, 0.0, 1.0]], dtype="float64")
        dist = np.zeros((4, 1), dtype="float64")

        ok, rvec, tvec = False, None, None
        if self._rvec is not None and self._tvec is not None:
            try:
                ok, rvec, tvec = cv2.solvePnP(
                    self._model_np, img, cam, dist,
                    self._rvec.copy(), self._tvec.copy(), True,
                    cv2.SOLVEPNP_ITERATIVE)
            except Exception:
                ok = False
        if not ok:
            try:
                ok, rvec, tvec = cv2.solvePnP(self._model_np, img, cam, dist,
                                              flags=cv2.SOLVEPNP_ITERATIVE)
            except Exception:
                return None
        if not ok or rvec is None:
            return None

        # ошибка репроекции: фильтр от развалившихся решений
        try:
            proj, _ = cv2.projectPoints(self._model_np, rvec, tvec, cam, dist)
            err = float(np.mean(np.linalg.norm(proj.reshape(-1, 2) - img, axis=1)))
        except Exception:
            err = 0.0
        limit = max(float(self._c("max_reproj_px")),
                    float(self._c("max_reproj_ratio")) * face_w)
        if not math.isfinite(err) or err > limit:
            self._rvec = self._tvec = None
            return None

        self._rvec, self._tvec = rvec, tvec
        try:
            rmat, _ = cv2.Rodrigues(rvec)
        except Exception:
            return None
        yaw, pitch, roll = _euler_from_matrix(rmat)
        if abs(roll) > float(self._c("max_abs_roll")):
            return None
        return yaw, pitch, roll, err

    # ------------------------------------------------------------------
    # взгляд
    # ------------------------------------------------------------------
    def _gaze(self, pts: Any, face_w: float, ear_base: float,
              ear_r: float | None, ear_l: float | None
              ) -> tuple[float, float, float, float] | None:
        """Взгляд в глазнице, усреднённый по двум глазам с весами видимости."""
        right = self._eye_gaze(pts, IDX_R_EYE_OUTER, IDX_R_EYE_INNER,
                               IDX_R_EYE_UPPER, IDX_R_EYE_LOWER,
                               IDX_R_IRIS_CENTER, IDX_R_IRIS_RING,
                               face_w, +1.0, ear_base, ear_r)
        left = self._eye_gaze(pts, IDX_L_EYE_OUTER, IDX_L_EYE_INNER,
                              IDX_L_EYE_UPPER, IDX_L_EYE_LOWER,
                              IDX_L_IRIS_CENTER, IDX_L_IRIS_RING,
                              face_w, -1.0, ear_base, ear_l)
        min_vis = float(self._c("min_eye_visibility"))
        good = [e for e in (right, left) if e is not None and e[2] >= min_vis]
        vis_r = right[2] if right else 0.0
        vis_l = left[2] if left else 0.0
        if not good:
            return None
        wsum = sum(e[2] for e in good)
        if wsum <= 1e-9:
            return None
        g_yaw = sum(e[0] * e[2] for e in good) / wsum
        g_pitch = sum(e[1] * e[2] for e in good) / wsum
        if not (_finite(g_yaw) and _finite(g_pitch)):
            return None
        return float(g_yaw), float(g_pitch), float(vis_r), float(vis_l)

    def _eye_gaze(self, pts: Any, outer: int, inner: int, upper: int, lower: int,
                  iris_c: int, iris_ring: tuple[int, ...], face_w: float,
                  side: float, ear_base: float, ear_eye: float | None
                  ) -> tuple[float, float, float] | None:
        """(gaze_h, gaze_v, видимость) для одного глаза.

        Оси берутся от самой глазницы: u — от внешнего угла к внутреннему,
        приведённая к направлению «вправо по кадру» множителем side;
        v — перпендикуляр к u, направленный «вниз по кадру». Поэтому наклон
        головы (roll) не искажает оценку.
        """
        if pts.shape[0] <= max(iris_c, max(iris_ring)):
            return None                      # radужек нет: refine_landmarks выключен
        o = pts[outer]; i = pts[inner]
        ux, uy = float(i[0] - o[0]), float(i[1] - o[1])
        width = math.hypot(ux, uy)
        if width < 2.0:
            return None
        ux, uy = side * ux / width, side * uy / width
        vx, vy = -uy, ux                      # «вниз по кадру»
        mid_x, mid_y = (float(o[0] + i[0]) / 2.0, float(o[1] + i[1]) / 2.0)
        ic = pts[iris_c]
        dx, dy = float(ic[0]) - mid_x, float(ic[1]) - mid_y

        h_off = (dx * ux + dy * uy) / (width / 2.0)

        up, lo = pts[upper], pts[lower]
        eye_h = abs(float(lo[0] - up[0]) * vx + float(lo[1] - up[1]) * vy)
        denom_v = max(eye_h, float(self._c("eye_height_floor_ratio")) * width) / 2.0
        v_off = (dx * vx + dy * vy) / denom_v
        gaze_v = -v_off                       # вверх = +

        # --- видимость глаза (берём минимум из трёх независимых признаков) ---
        exp_ratio = float(self._c("eye_width_face_ratio"))
        r = (width / face_w) / exp_ratio if exp_ratio > 0 else 0.0
        vis_geom = _clamp((r - 0.45) / 0.35, 0.0, 1.0)   # сильный поворот -> 0

        if ear_eye is not None and ear_base > 1e-6:
            vis_open = _clamp((ear_eye - 0.5 * ear_base) / (0.3 * ear_base), 0.0, 1.0)
        else:
            vis_open = 0.5

        exp_r = float(self._c("iris_radius_eye_ratio")) * width
        radii = [math.hypot(float(pts[k][0]) - float(ic[0]),
                            float(pts[k][1]) - float(ic[1])) for k in iris_ring]
        iris_r = sum(radii) / len(radii) if radii else 0.0
        vis_iris = _clamp(1.0 - abs(iris_r / exp_r - 1.0) / 0.6, 0.0, 1.0) \
            if exp_r > 1e-6 else 0.0

        vis = min(vis_geom, vis_open, vis_iris)
        if not (_finite(h_off) and _finite(gaze_v)):
            return None
        # отсечение физически невозможных смещений (развалившиеся landmarks)
        if abs(h_off) > 2.0 or abs(gaze_v) > 3.0:
            return None
        return float(h_off), float(gaze_v), float(vis)

    # ------------------------------------------------------------------
    # моргания
    # ------------------------------------------------------------------
    def _update_blink(self, ear_med: float | None, ts: float,
                      obs: FaceObservation) -> None:
        """Гистерезисный детектор морганий с адаптивной базой EAR."""
        if ear_med is not None:
            # база = типичный EAR ОТКРЫТОГО глаза; кадры моргания в неё не попадают
            if not self._eye_closed and ear_med >= self._ear_baseline * 0.85:
                self._ear_baseline += float(self._c("ear_baseline_alpha")) * \
                    (ear_med - self._ear_baseline)
            self._ear_baseline = _clamp(self._ear_baseline, 0.12, 0.45)

            close_thr = self._ear_baseline * float(self._c("ear_close_ratio"))
            open_thr = self._ear_baseline * float(self._c("ear_open_ratio"))
            if not self._eye_closed:
                if ear_med < close_thr:
                    self._eye_closed = True
                    self._close_start = ts
                    self._closed_frames = 1
            elif ear_med > open_thr:              # гистерезис: открылся только выше open_thr
                dur = ts - self._close_start
                frames = self._closed_frames
                self._eye_closed = False
                self._closed_frames = 0
                if frames >= int(self._c("blink_min_frames")) and \
                        float(self._c("blink_min_sec")) <= dur <= float(self._c("blink_max_sec")):
                    self._blink_count += 1
                    self._last_blink_ts = ts
                    self._blink_ts.append(ts)
                    obs.blink = True
            else:
                self._closed_frames += 1

        window = float(self._c("blink_rate_window"))
        while self._blink_ts and ts - self._blink_ts[0] > window:
            self._blink_ts.popleft()
        elapsed = min(window, max(float(self._c("blink_rate_min_window")),
                                  ts - (self._first_seen_ts or ts)))
        obs.blink_count = self._blink_count
        obs.blink_rate = len(self._blink_ts) * 60.0 / elapsed
        obs.eyes_closed = self._eye_closed and \
            (ts - self._close_start) > float(self._c("eyes_closed_sec"))
        since = ts - (self._last_blink_ts or self._first_seen_ts or ts)
        obs.seconds_since_blink = float(since)
        obs.blink_stale = since > float(self._c("no_blink_seconds"))

    # ------------------------------------------------------------------
    # движение landmarks
    # ------------------------------------------------------------------
    def _motion(self, pts: Any, face_w: float) -> float:
        np = self._np
        sub = pts[::8, :2]
        prev = self._prev_landmarks
        self._prev_landmarks = sub.copy()
        if prev is None or prev.shape != sub.shape or face_w <= 1e-6:
            return 0.0
        try:
            d = float(np.mean(np.linalg.norm(sub - prev, axis=1))) / face_w
        except Exception:
            return 0.0
        return d if math.isfinite(d) else 0.0

    # ------------------------------------------------------------------
    # зона взгляда
    # ------------------------------------------------------------------
    def _thresholds(self) -> dict[str, float]:
        t = self._calib.get("thresholds") if isinstance(self._calib, dict) else None
        if not isinstance(t, dict):
            t = {}
        return {
            "gaze_h": float(t.get("gaze_h", self._c("gaze_h_floor"))),
            "gaze_up": float(t.get("gaze_up", self._c("gaze_up_floor"))),
            "gaze_down": float(t.get("gaze_down", self._c("gaze_down_floor"))),
            "head_yaw": float(t.get("head_yaw", self._c("head_yaw_floor"))),
            "head_pitch_up": float(t.get("head_pitch_up", self._c("head_pitch_up_floor"))),
            "head_pitch_down": float(t.get("head_pitch_down",
                                           self._c("head_pitch_down_floor"))),
            "off_screen_factor": float(t.get("off_screen_factor",
                                             self._c("off_screen_factor"))),
        }

    def _base(self) -> dict[str, float]:
        b = self._calib.get("base") if isinstance(self._calib, dict) else None
        if not isinstance(b, dict):
            b = {}
        return {k: float(b.get(k, 0.0)) for k in
                ("gaze_yaw", "gaze_pitch", "head_yaw", "head_pitch", "head_roll")}

    def _has_screen_map(self) -> bool:
        m = self._calib.get("screen_map") if isinstance(self._calib, dict) else None
        return isinstance(m, dict) and m.get("kind") in ("poly2", "affine")

    def _eval_screen_map(self, g_yaw: float,
                         g_pitch: float) -> tuple[float, float] | None:
        """Полиномиальное отображение взгляда в координаты экрана.

        Дублирует формулу из engine/calibration.py намеренно: детектор не должен
        импортировать движок. Порядок признаков фиксирован схемой v1:
        poly2  -> [1, u, v, u^2, u*v, v^2];  affine -> [1, u, v],
        где u = (gaze_yaw - c0)/s, v = (gaze_pitch - c1)/s.
        """
        m = self._calib.get("screen_map")
        if not isinstance(m, dict):
            return None
        kind = m.get("kind")
        cx, cy = m.get("coef_x"), m.get("coef_y")
        center = m.get("center") or [0.0, 0.0]
        scale = float(m.get("scale") or 1.0) or 1.0
        if kind not in ("poly2", "affine") or not cx or not cy:
            return None
        u = (float(g_yaw) - float(center[0])) / scale
        v = (float(g_pitch) - float(center[1])) / scale
        phi = [1.0, u, v, u * u, u * v, v * v] if kind == "poly2" else [1.0, u, v]
        if len(cx) != len(phi) or len(cy) != len(phi):
            return None
        x = sum(c * f for c, f in zip(cx, phi))
        y = sum(c * f for c, f in zip(cy, phi))
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        # квадратичная экстраполяция далеко за сеткой бессмысленна — ограничим
        return _clamp(x, -1.5, 2.5), _clamp(y, -1.5, 2.5)

    def _classify_zone(self, obs: FaceObservation) -> None:
        """Зона взгляда от калибровочной базы, а не от абсолютных значений.

        Девиации взгляда и головы СКЛАДЫВАЮТСЯ в нормированных единицах:
        направление взгляда в пространстве = направление головы + взгляд в
        глазнице. Поэтому «голова вправо, но глаза вернулись на экран» гасит
        само себя, а «голова вправо и глаза вправо» усиливается — это физически
        верно и именно так уходит основная масса ложных GAZE_SIDE.
        """
        thr = self._thresholds()
        base = self._base()

        dev_h = dev_v = 0.0
        if obs.gaze_ok:
            d = obs.gaze_yaw - base["gaze_yaw"]
            dev_h += d / max(1e-6, thr["gaze_h"])
            dv = obs.gaze_pitch - base["gaze_pitch"]
            dev_v += dv / max(1e-6, thr["gaze_up"] if dv >= 0 else thr["gaze_down"])
        if obs.pose_ok:
            dyaw = obs.yaw - base["head_yaw"]
            obs.head_dev_yaw = dyaw / max(1e-6, thr["head_yaw"])
            dpitch = obs.pitch - base["head_pitch"]
            obs.head_dev_pitch = dpitch / max(
                1e-6, thr["head_pitch_up"] if dpitch >= 0 else thr["head_pitch_down"])
            dev_h += obs.head_dev_yaw
            dev_v += obs.head_dev_pitch
        obs.gaze_dev_h = float(dev_h)
        obs.gaze_dev_v = float(dev_v)

        if not obs.gaze_ok and not obs.pose_ok:
            obs.gaze_zone = "unknown"
            obs.gaze_region = "unknown"
            return

        # --- есть карта экрана: «смотрит куда-то в свой монитор» = норма ---
        if obs.gaze_ok and self._has_screen_map():
            pt = self._eval_screen_map(obs.gaze_yaw, obs.gaze_pitch)
            if pt is not None:
                x, y = pt
                obs.gaze_point = (float(x), float(y))
                margin = float(self._c("off_screen_margin"))
                out_x = x < -margin or x > 1.0 + margin
                out_y = y < -margin or y > 1.0 + margin
                if out_x or out_y:
                    # направление наибольшего выхода за границу экрана
                    ox = (-margin - x) if x < -margin else (x - 1.0 - margin if out_x else 0.0)
                    oy = (-margin - y) if y < -margin else (y - 1.0 - margin if out_y else 0.0)
                    if ox >= oy:
                        obs.gaze_off_direction = "left" if x < 0.5 else "right"
                    else:
                        obs.gaze_off_direction = "up" if y < 0.5 else "down"
                    obs.gaze_zone = "off_screen"
                    obs.gaze_region = "off_screen"
                    return
                box = float(self._c("screen_center_box"))
                if abs(x - 0.5) <= box and abs(y - 0.5) <= box:
                    obs.gaze_region = "center"
                elif abs(x - 0.5) >= abs(y - 0.5):
                    obs.gaze_region = "left" if x < 0.5 else "right"
                else:
                    obs.gaze_region = "up" if y < 0.5 else "down"
                # точка внутри экрана -> нарушения нет, как бы ни косил глаз
                obs.gaze_zone = "center"
                return

        # --- карты нет: пороговая логика по персональным порогам ---
        off = thr["off_screen_factor"]
        mag_h, mag_v = abs(dev_h), abs(dev_v)
        if max(mag_h, mag_v) >= off:
            obs.gaze_zone = "off_screen"
            if mag_h >= mag_v:
                obs.gaze_off_direction = "right" if dev_h > 0 else "left"
            else:
                obs.gaze_off_direction = "up" if dev_v > 0 else "down"
            obs.gaze_region = "off_screen"
            return
        if max(mag_h, mag_v) < 1.0:
            obs.gaze_zone = "center"
            obs.gaze_region = "center"
            return
        if mag_h >= mag_v:
            obs.gaze_zone = "right" if dev_h > 0 else "left"
        else:
            obs.gaze_zone = "up" if dev_v > 0 else "down"
        obs.gaze_region = obs.gaze_zone


# --------------------------------------------------------------------------
# вспомогательные функции уровня модуля
# --------------------------------------------------------------------------
def _sample_get(sample: Any, name: str, default: Any) -> Any:
    """Достать поле из FaceObservation или из обычного словаря."""
    if isinstance(sample, dict):
        return sample.get(name, default)
    return getattr(sample, name, default)


def _bbox_of(pts: Any) -> tuple[float, float, float, float]:
    xs = pts[:, 0]
    ys = pts[:, 1]
    x0, x1 = float(xs.min()), float(xs.max())
    y0, y1 = float(ys.min()), float(ys.max())
    return x0, y0, max(1e-6, x1 - x0), max(1e-6, y1 - y0)


def _dist(pts: Any, a: int, b: int) -> float:
    return math.hypot(float(pts[a][0] - pts[b][0]), float(pts[a][1] - pts[b][1]))


def _ear(pts: Any, idx: tuple[int, int, int, int, int, int]) -> float | None:
    """Eye aspect ratio: (|p2-p6| + |p3-p5|) / (2*|p1-p4|)."""
    if pts.shape[0] <= max(idx):
        return None
    horiz = _dist(pts, idx[0], idx[3])
    if horiz < 1e-6:
        return None
    val = (_dist(pts, idx[1], idx[5]) + _dist(pts, idx[2], idx[4])) / (2.0 * horiz)
    return val if math.isfinite(val) else None


def _mouth_ratio(pts: Any) -> float | None:
    """Открытие рта: расстояние между внутренними губами / ширина рта."""
    if pts.shape[0] <= max(IDX_LIP_LOWER_IN, IDX_MOUTH_RIGHT):
        return None
    width = _dist(pts, IDX_MOUTH_LEFT, IDX_MOUTH_RIGHT)
    if width < 1e-6:
        return None
    val = _dist(pts, IDX_LIP_UPPER_IN, IDX_LIP_LOWER_IN) / width
    return val if math.isfinite(val) else None


def _euler_from_matrix(rmat: Any) -> tuple[float, float, float]:
    """R (камера<-модель) -> (yaw, pitch, roll) в градусах.

    Разложение R = Rz(roll)*Ry(yaw)*Rx(pitch). Модель задана в системе камеры,
    поэтому фронтальное лицо даёт R ≈ I и все углы около нуля: скачков на ±180
    не возникает. Результат дополнительно заворачивается в (-180, 180].
    """
    r = [[float(rmat[i][j]) for j in range(3)] for i in range(3)]
    sy = math.sqrt(r[0][0] * r[0][0] + r[1][0] * r[1][0])
    if sy > 1e-6:
        pitch = math.atan2(r[2][1], r[2][2])
        yaw = math.atan2(-r[2][0], sy)
        roll = math.atan2(r[1][0], r[0][0])
    else:                                  # вырожденный случай (гимбал-лок)
        pitch = math.atan2(-r[1][2], r[1][1])
        yaw = math.atan2(-r[2][0], sy)
        roll = 0.0
    return (_wrap_deg(math.degrees(yaw)),
            _wrap_deg(math.degrees(pitch)),
            _wrap_deg(math.degrees(roll)))


__all__ = ["FaceAnalyzer", "FaceObservation", "CALIBRATION_SCHEMA_VERSION",
           "GAZE_ZONES", "DEFAULTS"]
