"""
Калибровка взгляда: персональные пороги + карта экрана.

Зачем это нужно. Абсолютные пороги по yaw/pitch/взгляду врут почти на каждом
втором кадре: у одного человека камера снизу и он всегда «смотрит вниз», у
другого экран 27", и взгляд в его правый край — это 25° в сторону. Поэтому
система НИЧЕГО не решает по абсолютным значениям, а только по отклонению от
персональной базы и по попаданию точки взгляда в карту экрана.

Две стадии (приходят из оболочки сообщением `calibrate`):

1. `gaze_center` — студент смотрит в центр экрана. Собираем базу
   (медиана gaze_yaw/gaze_pitch/yaw/pitch/roll/EAR) и разброс (робастная сигма
   1.4826*MAD). Персональный порог = k*sigma, зажатый между ПОЛОМ (иначе у
   очень статичного человека порог выродится в нуль и сработает на любом
   микродвижении) и ПОТОЛКОМ (иначе у вертлявого человека порог станет
   бесконечным и система ослепнет).

2. `gaze_grid` — 9 точек на экране. Для каждой точки берём медиану пары
   (gaze_yaw, gaze_pitch) и обучаем полиномиальную регрессию 2-й степени
   (numpy lstsq, без sklearn): признаки [1, u, v, u², uv, v²], где
   u = (gaze_yaw - c0)/s, v = (gaze_pitch - c1)/s. Два независимых полинома
   дают screen_x и screen_y. Это и позволяет отличить «смотрит на второй
   монитор справа» (x > 1) от «смотрит на шпаргалку на столе» (y > 1) и от
   «смотрит в правый край своего экрана» (x ≈ 0.95 — нарушения нет).

Качество считается честно: LOO-кросс-валидация (переобучение без каждой точки
по очереди), потому что на 9 точках и 6 параметрах внутренняя ошибка всегда
выглядит красиво. Плохое качество -> quality()['grade'] == 'poor' и русское
сообщение «калибровка плохая, переснимите».

Результат — словарь schema v1, он же сохраняется в JSON в каталоге сессии и
передаётся детектору через FaceAnalyzer.apply_calibration(). Формат описан в
sidecar/detectors/face_mesh.py (CALIBRATION_SCHEMA_VERSION). Модуль намеренно
не импортирует детектор: порядок признаков полинома зафиксирован схемой.

Модуль не создаёт события. CALIBRATION_DONE при необходимости порождает
EventEngine по результату finish().
"""
from __future__ import annotations

import json
import math
import os
import statistics
import time
from typing import Any

CALIBRATION_SCHEMA_VERSION = 1

STAGE_CENTER = "gaze_center"
STAGE_GRID = "gaze_grid"

#: Стандартная сетка 3x3 в нормированных координатах экрана.
#: Края взяты 0.1/0.9, а не 0.0/1.0: в самый угол люди смотрят неохотно и
#: промахиваются, из-за чего полином выгибает.
DEFAULT_GRID = (
    (0.1, 0.1), (0.5, 0.1), (0.9, 0.1),
    (0.1, 0.5), (0.5, 0.5), (0.9, 0.5),
    (0.1, 0.9), (0.5, 0.9), (0.9, 0.9),
)

_CFG_SECTIONS = ("calibration", "gaze", "face_mesh", "face")

DEFAULTS: dict[str, Any] = {
    # пороги: k*sigma с полом и потолком.
    # Полы дублируют значения из detectors/face_mesh.py DEFAULTS — это одни и
    # те же физические величины; в рантайме важен только итоговый thresholds.
    "k_sigma": 3.0,
    "threshold_ceiling_factor": 2.5,
    "gaze_h_floor": 0.22,            # ед. = доля половины ширины глаза
    "gaze_up_floor": 0.18,
    "gaze_down_floor": 0.26,
    "head_yaw_floor": 18.0,          # градусы
    "head_pitch_up_floor": 13.0,
    "head_pitch_down_floor": 18.0,
    "off_screen_factor": 1.8,

    # стадия центра
    "center_target_samples": 45,     # ~3 с при 15 fps
    "min_center_samples": 15,
    "center_gaze_sigma_max": 0.06,   # больше — человек не смотрел в точку
    "center_head_sigma_max": 6.0,    # градусы

    # стадия сетки
    "grid_target_samples": 15,       # на точку
    "min_grid_samples": 8,
    # poly2 — 6 параметров: LOO (переобучение без точки) осмыслен только при
    # 8+ точках, иначе он вырождается в ошибку на обучении (~0) и «хорошей»
    # становится любая карта. Аффинной (3 параметра) нужно 5+ точек.
    "min_grid_points": 8,            # меньше -> только аффинная карта
    "min_affine_points": 5,          # меньше -> карты нет
    "grid_head_tolerance": 8.0,      # градусы; голова ушла — сэмпл не берём
    "ridge": 1e-6,
    "good_rmse": 0.08,               # доля экрана
    "fair_rmse": 0.16,

    # общие фильтры сэмплов
    "min_visibility": 0.35,
    "blink_reject_ratio": 0.6,       # EAR ниже 60% базы — кадр моргания
    "max_samples": 600,              # защита от бесконечного накопления

    "filename": "calibration.json",
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


def _sample_get(sample: Any, name: str, default: Any = None) -> Any:
    """Поле из FaceObservation либо из обычного словаря (дубль из детектора)."""
    if isinstance(sample, dict):
        return sample.get(name, default)
    return getattr(sample, name, default)


def _gaze_pair(sample: Any) -> tuple[Any, Any]:
    """(gaze_yaw, gaze_pitch) БЕЗ сглаживания детектора, если оно известно.

    Детектор сглаживает взгляд медианой и EMA: после перевода глаз на новую
    точку сглаженное значение доходит до неё за ~1 с, и при 10–12 к/с медиана
    точки съезжает на 10–15 % экрана к предыдущей. Карта, обученная на таком
    взгляде, сжата и смещена по ходу обхода. Сырое значение кадра (`raw`)
    этой инерции не имеет; в установившемся взгляде оба совпадают, поэтому
    карта, обученная на сыром, верна и для сглаженного взгляда на экзамене.
    """
    raw = _sample_get(sample, "raw", None)
    if isinstance(raw, dict) and _finite(raw.get("gaze_yaw")) and _finite(raw.get("gaze_pitch")):
        return raw["gaze_yaw"], raw["gaze_pitch"]
    return _sample_get(sample, "gaze_yaw", None), _sample_get(sample, "gaze_pitch", None)


def _robust_stats(values: list[float]) -> tuple[float, float]:
    """Медиана и робастная сигма: max(1.4826*MAD, std/4)."""
    vals = [float(v) for v in values if _finite(v)]
    if not vals:
        return 0.0, 0.0
    med = statistics.median(vals)
    if len(vals) < 3:
        return med, 0.0
    mad = statistics.median([abs(v - med) for v in vals])
    try:
        std = statistics.stdev(vals)
    except statistics.StatisticsError:
        std = 0.0
    return med, max(1.4826 * mad, std * 0.25)


def _solve_ridge_py(a: list[list[float]], b: list[float],
                    lam: float) -> list[float] | None:
    """Нормальные уравнения (AᵗA + λI)x = Aᵗb, Гаусс с выбором ведущего.

    Резервный путь на случай отсутствия numpy — модуль обязан работать и без
    тяжёлых зависимостей.
    """
    m = len(a)
    if m == 0:
        return None
    n = len(a[0])
    ata = [[sum(a[r][i] * a[r][j] for r in range(m)) for j in range(n)]
           for i in range(n)]
    atb = [sum(a[r][i] * b[r] for r in range(m)) for i in range(n)]
    for i in range(n):
        ata[i][i] += lam
        ata[i].append(atb[i])
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(ata[r][col]))
        if abs(ata[piv][col]) < 1e-12:
            return None
        ata[col], ata[piv] = ata[piv], ata[col]
        pv = ata[col][col]
        for j in range(col, n + 1):
            ata[col][j] /= pv
        for r in range(n):
            if r == col:
                continue
            f = ata[r][col]
            if f:
                for j in range(col, n + 1):
                    ata[r][j] -= f * ata[col][j]
    sol = [ata[i][n] for i in range(n)]
    return sol if all(math.isfinite(v) for v in sol) else None


class GazeCalibration:
    """Сбор калибровки взгляда, обучение карты экрана, оценка качества."""

    def __init__(self, config: dict | None = None,
                 session_dir: str | None = None) -> None:
        self._cfg = _as_dict(config)
        self.session_dir = session_dir
        self._np = None
        try:
            import numpy as np  # noqa: PLC0415
            self._np = np
        except Exception:                       # pragma: no cover
            self._np = None

        self._stage: str | None = None
        self._center: dict[str, list[float]] = {}
        self._base: dict[str, float] = {}
        self._sigma: dict[str, float] = {}
        self._thresholds: dict[str, float] = {}
        self._grid: list[dict[str, Any]] = []
        self._grid_idx = -1
        self._screen_map: dict[str, Any] = {"kind": None}
        self._quality: dict[str, Any] = {"grade": "none",
                                         "message": "Калибровка не выполнена."}
        self._rejected = {"gaze": 0, "blink": 0, "head": 0, "visibility": 0}
        self._center_pending = False
        self._ts = 0.0
        self.reset()

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
    # жизненный цикл
    # ------------------------------------------------------------------
    CENTER_KEYS = ("gaze_yaw", "gaze_pitch", "head_yaw", "head_pitch",
                   "head_roll", "ear", "mouth")

    def reset(self) -> None:
        """Полный сброс (перекалибровка с нуля)."""
        self._stage = None
        self._center = {k: [] for k in self.CENTER_KEYS}
        self._base = {}
        self._sigma = {}
        self._thresholds = {}
        self._grid = []
        self._grid_idx = -1
        self._fit_pts: list[tuple[tuple[float, float], float, float]] = []
        self._screen_map = {"kind": None}
        self._quality = {"grade": "none", "message": "Калибровка не выполнена."}
        self._rejected = {"gaze": 0, "blink": 0, "head": 0, "visibility": 0}
        self._center_pending = False
        self._ts = 0.0

    @property
    def stage(self) -> str | None:
        return self._stage

    def begin_center(self) -> None:
        self._stage = STAGE_CENTER
        self._center = {k: [] for k in self.CENTER_KEYS}
        self._center_pending = False
        # фильтр морганий сравнивает с EAR базы; база прошлой попытки (а то и
        # другого человека с другой формой глаз) отбросила бы все кадры центра
        self._base.pop("ear", None)

    def begin_grid(self, points: Any = None) -> list[tuple[float, float]]:
        """Начать 9-точечную стадию. Возвращает список целей для оболочки."""
        pts = []
        for p in (points or DEFAULT_GRID):
            try:
                x, y = float(p[0]), float(p[1])
            except (TypeError, ValueError, IndexError):
                continue
            pts.append((x, y))
        if not pts:
            pts = [(float(x), float(y)) for x, y in DEFAULT_GRID]
        self._stage = STAGE_GRID
        self._grid = [{"point": p, "samples": []} for p in pts]
        self._grid_idx = -1
        return pts

    def grid_points(self) -> list[tuple[float, float]]:
        return [g["point"] for g in self._grid]

    def begin_point(self, point: Any = None, fresh: bool = False) -> int:
        """Переключиться на точку сетки. point=[x,y] из сообщения `calibrate`
        или None — тогда берётся следующая по порядку.

        fresh=True — оболочка только что ПОКАЗАЛА эту точку (см. visit_point).
        Без fresh (вызов на каждом кадре) точка только выбирается по ближайшей
        координате, накопленное не трогается.
        """
        if fresh and point is not None:
            return self.visit_point(point)
        if not self._grid:
            self.begin_grid()
        if point is None:
            self._grid_idx = min(self._grid_idx + 1, len(self._grid) - 1)
            return self._grid_idx
        try:
            px, py = float(point[0]), float(point[1])
        except (TypeError, ValueError, IndexError):
            self._grid_idx = min(self._grid_idx + 1, len(self._grid) - 1)
            return self._grid_idx
        best, best_d = -1, 1e9
        for i, g in enumerate(self._grid):
            d = (g["point"][0] - px) ** 2 + (g["point"][1] - py) ** 2
            if d < best_d:
                best, best_d = i, d
        if best_d > 0.01:                     # такой точки в сетке нет — добавим
            self._grid.append({"point": (px, py), "samples": []})
            best = len(self._grid) - 1
        self._grid_idx = best
        return best

    def visit_point(self, point: Any) -> int:
        """Оболочка показала точку: она и есть цель регрессии, ровно как есть.

        Сетка оболочки (0.06/0.94) не совпадает с DEFAULT_GRID (0.1/0.9), и
        «прищёлкивание» к ближайшей точке по умолчанию учило бы карту на
        смещённых целях — она сжимала бы экран. Поэтому первый показ после
        другой стадии начинает сетку с нуля и из точек оболочки, а точка
        сопоставляется только по точной координате (иначе на плотной сетке
        новая точка затёрла бы соседнюю). Повторный показ той же точки
        сбрасывает её кадры: повтор этапа не смешивает старый взгляд с новым.
        """
        try:
            px, py = float(point[0]), float(point[1])
        except (TypeError, ValueError, IndexError):
            return self.begin_point(None)
        if self._stage != STAGE_GRID:
            self._stage = STAGE_GRID
            self._grid = []
        for i, g in enumerate(self._grid):
            if abs(g["point"][0] - px) <= 1e-3 and abs(g["point"][1] - py) <= 1e-3:
                self._grid[i] = {"point": (px, py), "samples": []}
                self._grid_idx = i
                return i
        self._grid.append({"point": (px, py), "samples": []})
        self._grid_idx = len(self._grid) - 1
        return self._grid_idx

    # ------------------------------------------------------------------
    # приём сэмплов
    # ------------------------------------------------------------------
    def _usable(self, obs: Any, check_head: bool) -> bool:
        """Фильтр сэмпла: только валидный взгляд, открытые глаза, спокойная голова."""
        if not _sample_get(obs, "gaze_ok", False):
            self._rejected["gaze"] += 1
            return False
        g_yaw, g_pitch = _gaze_pair(obs)
        if not (_finite(g_yaw) and _finite(g_pitch)):
            self._rejected["gaze"] += 1
            return False
        vis = _sample_get(obs, "eye_visibility", None)
        if isinstance(vis, (list, tuple)) and vis:
            if max(float(v) for v in vis) < float(self._c("min_visibility")):
                self._rejected["visibility"] += 1
                return False
        ear = _sample_get(obs, "eye_aspect_ratio", None)
        base_ear = self._base.get("ear", 0.0)
        if _finite(ear) and base_ear > 1e-6:
            if float(ear) < base_ear * float(self._c("blink_reject_ratio")):
                self._rejected["blink"] += 1     # кадр моргания
                return False
        if check_head and "head_yaw" in self._base:
            yaw = _sample_get(obs, "yaw", None)
            pitch = _sample_get(obs, "pitch", None)
            tol = float(self._c("grid_head_tolerance"))
            if _sample_get(obs, "pose_ok", False) and _finite(yaw) and _finite(pitch):
                if abs(float(yaw) - self._base["head_yaw"]) > tol or \
                        abs(float(pitch) - self._base["head_pitch"]) > tol:
                    # карта экрана — функция взгляда В ГЛАЗНИЦЕ; если во время
                    # калибровки крутить головой, карта получится мусорной
                    self._rejected["head"] += 1
                    return False
        return True

    def add_center_sample(self, obs: Any) -> int:
        """Добавить кадр стадии центра. Возвращает число собранных сэмплов."""
        if self._stage != STAGE_CENTER:
            self.begin_center()
        if len(self._center["gaze_yaw"]) >= int(self._c("max_samples")):
            return len(self._center["gaze_yaw"])
        if not self._usable(obs, check_head=False):
            return len(self._center["gaze_yaw"])
        g_yaw, g_pitch = _gaze_pair(obs)
        src = {
            "gaze_yaw": g_yaw,
            "gaze_pitch": g_pitch,
            "head_yaw": _sample_get(obs, "yaw", None),
            "head_pitch": _sample_get(obs, "pitch", None),
            "head_roll": _sample_get(obs, "roll", None),
            "ear": _sample_get(obs, "eye_aspect_ratio", None),
            "mouth": _sample_get(obs, "mouth_open_ratio", None),
        }
        pose_ok = bool(_sample_get(obs, "pose_ok", False))
        for k, v in src.items():
            if k.startswith("head_") and not pose_ok:
                continue
            if _finite(v):
                self._center[k].append(float(v))
        self._center_pending = True
        return len(self._center["gaze_yaw"])

    def add_grid_sample(self, obs: Any, point: Any = None) -> int:
        """Добавить кадр стадии сетки. point задаёт/переключает цель."""
        if self._stage != STAGE_GRID or not self._grid:
            self.begin_grid()
        if point is not None:
            self.begin_point(point)
        if self._grid_idx < 0:
            self.begin_point(None)
        if len(self._grid[self._grid_idx]["samples"]) >= int(self._c("max_samples")):
            return len(self._grid[self._grid_idx]["samples"])
        if not self._usable(obs, check_head=True):
            return len(self._grid[self._grid_idx]["samples"])
        g_yaw, g_pitch = _gaze_pair(obs)
        self._grid[self._grid_idx]["samples"].append((float(g_yaw), float(g_pitch)))
        return len(self._grid[self._grid_idx]["samples"])

    # ------------------------------------------------------------------
    # стадия центра: база, сигма, персональные пороги
    # ------------------------------------------------------------------
    def finish_center(self) -> dict[str, Any]:
        """Посчитать базу, разброс и персональные пороги. Возвращает результат."""
        stats = {k: _robust_stats(v) for k, v in self._center.items()}
        n = len(self._center["gaze_yaw"])
        self._base = {k: stats[k][0] for k in self.CENTER_KEYS}
        self._sigma = {k: stats[k][1] for k in self.CENTER_KEYS}
        self._base["samples"] = float(n)
        self._center_pending = False

        k_sigma = float(self._c("k_sigma"))
        ceil_f = float(self._c("threshold_ceiling_factor"))
        enough = n >= int(self._c("min_center_samples"))
        # Шатание головой и взглядом на центре раздувало бы пороги до потолка
        # (голова до 45°), а «Продолжить» оставляло бы их на весь экзамен.
        # Неустойчивый центр — не норма человека, а провал измерения.
        stable = (max(self._sigma.get("gaze_yaw", 0.0), self._sigma.get("gaze_pitch", 0.0))
                  <= float(self._c("center_gaze_sigma_max"))
                  and max(self._sigma.get("head_yaw", 0.0), self._sigma.get("head_pitch", 0.0))
                  <= float(self._c("center_head_sigma_max")))

        def thr(floor_key: str, sigma_key: str) -> float:
            floor = float(self._c(floor_key))
            if not enough or not stable:
                return floor               # мало или негодные данные — только пол
            return _clamp(k_sigma * self._sigma.get(sigma_key, 0.0),
                          floor, floor * ceil_f)

        self._thresholds = {
            "gaze_h": thr("gaze_h_floor", "gaze_yaw"),
            "gaze_up": thr("gaze_up_floor", "gaze_pitch"),
            "gaze_down": thr("gaze_down_floor", "gaze_pitch"),
            "head_yaw": thr("head_yaw_floor", "head_yaw"),
            "head_pitch_up": thr("head_pitch_up_floor", "head_pitch"),
            "head_pitch_down": thr("head_pitch_down_floor", "head_pitch"),
            "off_screen_factor": float(self._c("off_screen_factor")),
        }
        self._ts = time.time()
        self._grade()
        return self.to_dict()

    # ------------------------------------------------------------------
    # стадия сетки: полиномиальная карта экрана
    # ------------------------------------------------------------------
    def finish_grid(self) -> dict[str, Any]:
        """Обучить карту «взгляд -> экран» по собранным точкам."""
        if not self._base or self._center_pending:
            # оболочка могла уйти с центра раньше, чем набралась цель кадров:
            # база считается по тому, что успели собрать, а не берётся старая
            self.finish_center()
        min_s = int(self._c("min_grid_samples"))
        pts: list[tuple[tuple[float, float], float, float]] = []
        for g in self._grid:
            s = g["samples"]
            if len(s) < min_s:
                continue
            u_med, u_sig = _robust_stats([v[0] for v in s])
            v_med, v_sig = _robust_stats([v[1] for v in s])
            # отбрасываем выбросы внутри точки (студент отвёл глаза)
            kept = [p for p in s
                    if (u_sig <= 1e-9 or abs(p[0] - u_med) <= 2.5 * u_sig)
                    and (v_sig <= 1e-9 or abs(p[1] - v_med) <= 2.5 * v_sig)]
            if len(kept) < min_s:
                kept = s
            u = statistics.median([p[0] for p in kept])
            v = statistics.median([p[1] for p in kept])
            pts.append((g["point"], u, v))

        self._fit_pts = pts
        c0 = float(self._base.get("gaze_yaw", 0.0))
        c1 = float(self._base.get("gaze_pitch", 0.0))
        scale = 1.0
        if pts:
            scale = max(0.05, max(max(abs(u - c0), abs(v - c1)) for _, u, v in pts))

        kind = None
        if len(pts) >= int(self._c("min_grid_points")):
            kind = "poly2"
        elif len(pts) >= int(self._c("min_affine_points")):
            kind = "affine"

        self._screen_map = {"kind": None, "center": [c0, c1], "scale": scale,
                            "points": len(pts)}
        if kind is None:
            self._ts = time.time()
            self._grade(grid_pts=pts)
            return self.to_dict()

        rows = [_features(kind, (u - c0) / scale, (v - c1) / scale)
                for _, u, v in pts]
        bx = [p[0][0] for p in pts]
        by = [p[0][1] for p in pts]
        lam = float(self._c("ridge"))
        coef_x = self._lstsq(rows, bx, lam)
        coef_y = self._lstsq(rows, by, lam)
        if coef_x is None or coef_y is None:
            if kind == "poly2":               # вырожденная сетка -> аффинная карта
                kind = "affine"
                rows = [_features(kind, (u - c0) / scale, (v - c1) / scale)
                        for _, u, v in pts]
                coef_x = self._lstsq(rows, bx, lam)
                coef_y = self._lstsq(rows, by, lam)
        if coef_x is None or coef_y is None:
            self._ts = time.time()
            self._grade(grid_pts=pts)
            return self.to_dict()

        self._screen_map = {
            "kind": kind,
            "center": [c0, c1],
            "scale": scale,
            "coef_x": coef_x,
            "coef_y": coef_y,
            "features": _FEATURE_NAMES[kind],
            "points": len(pts),
            # карта обучена при этой позе головы: дальше grid_head_tolerance
            # от базы детектор ей не верит (face_mesh._classify_zone)
            "head_tolerance": float(self._c("grid_head_tolerance")),
        }
        self._ts = time.time()
        self._grade(grid_pts=pts)
        return self.to_dict()

    def finish(self) -> dict[str, Any]:
        """Завершить калибровку целиком (центр + сетка, если она собиралась)."""
        if not self._base or self._center_pending:
            self.finish_center()
        if any(g["samples"] for g in self._grid):
            return self.finish_grid()
        self._grade()
        return self.to_dict()

    def _lstsq(self, rows: list[list[float]], target: list[float],
               lam: float) -> list[float] | None:
        """numpy.linalg.lstsq с ридж-регуляризацией; без numpy — нормальные уравнения."""
        if not rows or len(rows) != len(target):
            return None
        n = len(rows[0])
        if len(rows) < n and lam <= 0:
            lam = 1e-6
        np = self._np
        if np is not None:
            try:
                a = np.asarray(rows, dtype=float)
                b = np.asarray(target, dtype=float)
                if lam > 0:
                    a = np.vstack([a, math.sqrt(lam) * np.eye(n)])
                    b = np.concatenate([b, np.zeros(n)])
                sol, *_ = np.linalg.lstsq(a, b, rcond=None)
                if np.all(np.isfinite(sol)):
                    return [float(v) for v in sol]
            except Exception:
                pass
        return _solve_ridge_py(rows, target, max(lam, 1e-9))

    # ------------------------------------------------------------------
    # применение
    # ------------------------------------------------------------------
    def map_to_screen(self, gaze_yaw: float,
                      gaze_pitch: float) -> tuple[float, float] | None:
        """Взгляд -> нормированные координаты экрана. None, если карты нет.

        Значения ВНЕ [0,1] — это и есть признак взгляда мимо экрана, поэтому
        результат не зажимается в [0,1]; ограничение [-1.5, 2.5] стоит только
        против бессмысленной экстраполяции квадратичного полинома.
        """
        m = self._screen_map
        kind = m.get("kind")
        if kind not in ("poly2", "affine"):
            return None
        center = m.get("center") or [0.0, 0.0]
        scale = float(m.get("scale") or 1.0) or 1.0
        if not (_finite(gaze_yaw) and _finite(gaze_pitch)):
            return None
        u = (float(gaze_yaw) - float(center[0])) / scale
        v = (float(gaze_pitch) - float(center[1])) / scale
        phi = _features(kind, u, v)
        cx, cy = m.get("coef_x"), m.get("coef_y")
        if not cx or not cy or len(cx) != len(phi) or len(cy) != len(phi):
            return None
        x = sum(c * f for c, f in zip(cx, phi))
        y = sum(c * f for c, f in zip(cy, phi))
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        return _clamp(x, -1.5, 2.5), _clamp(y, -1.5, 2.5)

    def off_screen_direction(self, x: float, y: float,
                             margin: float = 0.12) -> str | None:
        """Куда ушёл взгляд за пределы экрана: left/right/up/down или None."""
        ox = (-margin - x) if x < -margin else (x - 1.0 - margin if x > 1.0 + margin else 0.0)
        oy = (-margin - y) if y < -margin else (y - 1.0 - margin if y > 1.0 + margin else 0.0)
        if ox <= 0.0 and oy <= 0.0:
            return None
        if ox >= oy:
            return "left" if x < 0.5 else "right"
        return "up" if y < 0.5 else "down"

    def thresholds(self) -> dict[str, float]:
        if not self._thresholds:
            return {
                "gaze_h": float(self._c("gaze_h_floor")),
                "gaze_up": float(self._c("gaze_up_floor")),
                "gaze_down": float(self._c("gaze_down_floor")),
                "head_yaw": float(self._c("head_yaw_floor")),
                "head_pitch_up": float(self._c("head_pitch_up_floor")),
                "head_pitch_down": float(self._c("head_pitch_down_floor")),
                "off_screen_factor": float(self._c("off_screen_factor")),
            }
        return dict(self._thresholds)

    def screen_map_usable(self) -> bool:
        """Можно ли детектору верить карте экрана.

        Плохая карта опаснее, чем её отсутствие: внутри неё действует правило
        «точка на экране -> нарушения нет», и сжатая карта прячет взгляд мимо
        экрана. Поэтому при map_grade == 'poor' детектор остаётся на
        персональных порогах, а сама карта сохраняется в JSON для разбора.
        Решает оценка КАРТЫ (LOO), а не общая: короткий центр портит общую
        оценку, но не делает хорошую карту неточной.
        """
        return self._screen_map.get("kind") in ("poly2", "affine") and \
            self._quality.get("map_grade") in ("good", "fair")

    def attach(self, analyzer: Any) -> bool:
        """Отдать калибровку детектору (duck typing, без импорта детектора)."""
        fn = getattr(analyzer, "apply_calibration", None)
        if not callable(fn):
            return False
        data = self.to_dict()
        if not self.screen_map_usable():
            data["screen_map"] = {"kind": None}
        try:
            return bool(fn(data))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # качество
    # ------------------------------------------------------------------
    def _fit_errors(self, pts: list[tuple[tuple[float, float], float, float]]
                    ) -> tuple[float, float, float]:
        """(RMSE на обучении, LOO-RMSE, максимальная ошибка) в долях экрана.

        LOO: модель переобучается без каждой точки по очереди. На 9 точках и 6
        параметрах внутренняя ошибка всегда мала, и только LOO показывает,
        насколько карта реально предсказывает новые направления взгляда.
        """
        kind = self._screen_map.get("kind")
        if kind not in ("poly2", "affine") or not pts:
            return 0.0, 0.0, 0.0
        c0, c1 = self._screen_map["center"]
        scale = float(self._screen_map.get("scale") or 1.0) or 1.0
        lam = float(self._c("ridge"))
        rows = [_features(kind, (u - c0) / scale, (v - c1) / scale) for _, u, v in pts]
        bx = [p[0][0] for p in pts]
        by = [p[0][1] for p in pts]

        errs = []
        for i, row in enumerate(rows):
            px = sum(c * f for c, f in zip(self._screen_map["coef_x"], row))
            py = sum(c * f for c, f in zip(self._screen_map["coef_y"], row))
            errs.append(math.hypot(px - bx[i], py - by[i]))
        rmse = math.sqrt(sum(e * e for e in errs) / len(errs))
        max_err = max(errs)

        n_par = len(rows[0])
        loo = rmse
        if len(rows) >= n_par + 1:
            loo_errs = []
            for i in range(len(rows)):
                sub = [r for j, r in enumerate(rows) if j != i]
                sx = self._lstsq(sub, [bx[j] for j in range(len(rows)) if j != i], lam)
                sy = self._lstsq(sub, [by[j] for j in range(len(rows)) if j != i], lam)
                if sx is None or sy is None:
                    continue
                px = sum(c * f for c, f in zip(sx, rows[i]))
                py = sum(c * f for c, f in zip(sy, rows[i]))
                loo_errs.append(math.hypot(px - bx[i], py - by[i]))
            if loo_errs:
                loo = math.sqrt(sum(e * e for e in loo_errs) / len(loo_errs))
        return rmse, loo, max_err

    def _grade(self, grid_pts: list | None = None) -> None:
        n = int(self._base.get("samples", 0))
        notes: list[str] = []
        center_ok = n >= int(self._c("min_center_samples"))
        if not center_ok:
            notes.append(f"мало кадров центра ({n})")
        sg = max(self._sigma.get("gaze_yaw", 0.0), self._sigma.get("gaze_pitch", 0.0))
        sh = max(self._sigma.get("head_yaw", 0.0), self._sigma.get("head_pitch", 0.0))
        stable = sg <= float(self._c("center_gaze_sigma_max")) and \
            sh <= float(self._c("center_head_sigma_max"))
        if center_ok and not stable:
            notes.append("взгляд не держался в точке")

        rmse = loo = max_err = 0.0
        if grid_pts is None:
            # ровно те точки, на которых карта обучена (finish_grid), а не
            # все с кадрами: иначе оценка смешала бы карту с чужими точками
            grid_pts = list(self._fit_pts) if self._screen_map.get("kind") else []
        if self._screen_map.get("kind") and grid_pts:
            rmse, loo, max_err = self._fit_errors(grid_pts)

        good_r = float(self._c("good_rmse"))
        fair_r = float(self._c("fair_rmse"))
        if not self._screen_map.get("kind") or not grid_pts:
            map_grade = "none"
        elif loo <= good_r:
            map_grade = "good"
        elif loo <= fair_r:
            map_grade = "fair"
        else:
            map_grade = "poor"
        if self._screen_map.get("kind"):
            # детектор расширяет запас границы экрана по ошибке карты
            self._screen_map["loo_rmse"] = round(loo, 5)
        if not self._screen_map.get("kind"):
            if any(g["samples"] for g in self._grid):
                notes.append("карта экрана не построена: точкам сетки не хватило кадров")
            else:
                notes.append("карта экрана не построена (нет 9-точечной калибровки)")
        if not center_ok:
            grade = "poor"
        elif not self._screen_map.get("kind"):
            # карты экрана нет — работаем на персональных порогах
            grade = "fair" if stable else "poor"
        elif loo <= good_r and stable:
            grade = "good"
        elif loo <= fair_r:
            grade = "fair"
        else:
            grade = "poor"
            notes.append(f"ошибка карты экрана {loo:.2f} от размера экрана")

        if grade == "good":
            msg = "Калибровка выполнена, точность хорошая."
        elif grade == "fair":
            msg = "Калибровка приемлемая" + (": " + ", ".join(notes) if notes else ".")
        else:
            msg = "Калибровка плохая, переснимите" + \
                (": " + ", ".join(notes) if notes else ".")

        self._quality = {
            "grade": grade,
            "message": msg,
            "center_samples": n,
            "center_sigma_gaze": round(sg, 5),
            "center_sigma_head_deg": round(sh, 3),
            "center_stable": bool(stable),
            "grid_points": len(grid_pts),
            "grid_samples": sum(len(g["samples"]) for g in self._grid),
            "rmse": round(rmse, 5),
            "loo_rmse": round(loo, 5),
            "max_error": round(max_err, 5),
            "map_grade": map_grade,
            "rejected": dict(self._rejected),
        }

    def quality(self) -> dict[str, Any]:
        return dict(self._quality)

    def is_usable(self) -> bool:
        """Можно ли полагаться на калибровку (пороги хотя бы посчитаны)."""
        return bool(self._thresholds) and self._quality.get("grade") != "poor"

    # ------------------------------------------------------------------
    # прогресс для сообщения `calibration`
    # ------------------------------------------------------------------
    def progress(self) -> dict[str, Any]:
        """Payload для MsgType.CALIBRATION: {stage, progress, done, result}."""
        stage = self._stage or ""
        if self._stage == STAGE_CENTER:
            target = max(1, int(self._c("center_target_samples")))
            n = len(self._center["gaze_yaw"])
            prog = _clamp(n / target, 0.0, 1.0)
            done = n >= target
        elif self._stage == STAGE_GRID:
            target = max(1, int(self._c("grid_target_samples")))
            total = max(1, len(self._grid))
            full = sum(1 for g in self._grid if len(g["samples"]) >= target)
            cur = 0.0
            if 0 <= self._grid_idx < len(self._grid):
                cur = _clamp(len(self._grid[self._grid_idx]["samples"]) / target,
                             0.0, 1.0)
                if len(self._grid[self._grid_idx]["samples"]) >= target:
                    cur = 0.0
            prog = _clamp((full + cur) / total, 0.0, 1.0)
            done = full >= total
        else:
            prog = 1.0 if self._thresholds else 0.0
            done = bool(self._thresholds)
        return {"stage": stage, "progress": round(float(prog), 3),
                "done": bool(done), "result": self.quality()}

    def point_ready(self) -> bool:
        """Собрано ли достаточно кадров для текущей точки сетки."""
        if not (0 <= self._grid_idx < len(self._grid)):
            return False
        return len(self._grid[self._grid_idx]["samples"]) >= \
            int(self._c("grid_target_samples"))

    def center_ready(self) -> bool:
        return len(self._center["gaze_yaw"]) >= int(self._c("center_target_samples"))

    # ------------------------------------------------------------------
    # сериализация
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """Калибровка в формате schema v1 (его понимает FaceAnalyzer)."""
        base = {k: float(self._base.get(k, 0.0)) for k in self.CENTER_KEYS}
        sigma = {k: float(self._sigma.get(k, 0.0)) for k in self.CENTER_KEYS}
        return {
            "version": CALIBRATION_SCHEMA_VERSION,
            "ts": self._ts or time.time(),
            "samples": {
                "center": int(self._base.get("samples", 0)),
                "grid": {f"{g['point'][0]:.2f},{g['point'][1]:.2f}": len(g["samples"])
                         for g in self._grid},
            },
            "base": base,
            "sigma": sigma,
            "thresholds": self.thresholds(),
            "screen_map": dict(self._screen_map),
            # отдана ли карта детектору (см. screen_map_usable): в файле лежит
            # и плохая карта — для разбора, — но работал детектор без неё
            "screen_map_applied": self.screen_map_usable(),
            "quality": self.quality(),
        }

    def from_dict(self, data: dict) -> bool:
        """Загрузить ранее сохранённую калибровку."""
        if not isinstance(data, dict):
            return False
        if int(data.get("version", 0)) != CALIBRATION_SCHEMA_VERSION:
            return False
        thr = data.get("thresholds")
        if not isinstance(thr, dict):
            return False
        base = data.get("base") or {}
        sigma = data.get("sigma") or {}
        self._base = {k: float(base.get(k, 0.0)) for k in self.CENTER_KEYS}
        self._base["samples"] = float((data.get("samples") or {}).get("center", 0))
        self._sigma = {k: float(sigma.get(k, 0.0)) for k in self.CENTER_KEYS}
        self._thresholds = {k: float(v) for k, v in thr.items() if _finite(v)}
        sm = data.get("screen_map")
        self._screen_map = dict(sm) if isinstance(sm, dict) else {"kind": None}
        q = data.get("quality")
        self._quality = dict(q) if isinstance(q, dict) else \
            {"grade": "fair", "message": "Калибровка загружена из файла."}
        self._ts = float(data.get("ts") or time.time())
        self._stage = None
        return True

    def path(self, session_dir: str | None = None) -> str:
        d = session_dir or self.session_dir or "."
        return os.path.join(d, str(self._c("filename")))

    def save(self, session_dir: str | None = None) -> str | None:
        """Сохранить JSON в каталог сессии. Возвращает путь или None."""
        p = self.path(session_dir)
        try:
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
            return p
        except Exception:
            return None

    def load(self, path_or_dir: str | None = None) -> bool:
        """Загрузить калибровку из файла или из каталога сессии."""
        p = path_or_dir or self.path()
        if os.path.isdir(p):
            p = os.path.join(p, str(self._c("filename")))
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return False
        return self.from_dict(data)


_FEATURE_NAMES = {
    "poly2": ["1", "u", "v", "u2", "uv", "v2"],
    "affine": ["1", "u", "v"],
}


def _features(kind: str, u: float, v: float) -> list[float]:
    """Признаки регрессии. Порядок зафиксирован схемой v1 и повторён в
    detectors/face_mesh.py::_eval_screen_map — менять только синхронно."""
    if kind == "poly2":
        return [1.0, u, v, u * u, u * v, v * v]
    return [1.0, u, v]


__all__ = ["GazeCalibration", "CALIBRATION_SCHEMA_VERSION", "STAGE_CENTER",
           "STAGE_GRID", "DEFAULT_GRID", "DEFAULTS"]
