"""
Детекция телефона и запрещённых предметов в кадре (п. 2.1 ТЗ).

Модуль полностью самодостаточен: не импортирует соседние детекторы, не ходит в сеть,
не бросает исключений наружу. Отсутствие модели или библиотеки => available() == False,
сайдкар продолжает работу в деградированном режиме.

Бэкенды (выбираются автоматически, порядок приоритета):
  1. onnxruntime + models/yolov8n.onnx — предпочтительный, быстрый на CPU, минимум зависимостей.
     Препроцессинг и постобработка реализованы здесь целиком (letterbox, NMS на numpy).
  2. ultralytics YOLO — если установлен И локальный .pt-файл уже лежит на диске.
     Файл НЕ скачивается: сеть в рантайме запрещена, веса кладёт scripts/fetch_models.sh.
  3. ничего — available() == False.

Главная ценность модуля — эвристики PHONE_RAISED и PHONE_AIMED_AT_SCREEN: мало просто
увидеть телефон, нужно отличить «лежит на столе» от «поднят и наведён на экран».
См. докстринги _eval_raised() и _eval_aimed().

Модуль возвращает НАБЛЮДЕНИЯ (Detection + результаты эвристик), события порождает
только Event Engine — так требует docs/CONTRACT.md.

Схема конфига (все ключи необязательны, значения ниже — дефолты):

    {"objects": {
        "enabled": true,
        "backend": "auto",                    # auto | onnx | ultralytics | none
        "model_path": "models/yolov8n.onnx",
        "weights_pt": "models/yolov8n.pt",
        "imgsz": 640,
        "conf": 0.35,                         # базовый порог уверенности
        "class_conf": {"cell phone": 0.30},   # пороги по классам (переопределяют conf)
        "nms_iou": 0.45,
        "max_det": 50,
        "providers": ["CPUExecutionProvider"],
        "classes": [0, 62, 63, 65, 67, 73],   # COCO id, всё прочее отбрасывается
        "track": {"iou": 0.30, "max_missed": 5, "smooth": 0.5},
        "heuristics": {
            "raised_center_ratio": 0.50,
            "face_margin": 0.60,
            "raised_min_conf": 0.30,
            "aspect_min": 1.30,
            "area_growth": 1.12,
            "area_window": 1.50,
            "near_area_ratio": 0.030,
            "min_area_ratio": 0.0015,
            "hold_sec": 1.00,
            "min_track_age": 3
        }
    }}
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------------
# COCO-классы, которые нас интересуют. Остальные отбрасываются в постобработке.
# --------------------------------------------------------------------------
CLASS_PERSON = 0
CLASS_TV = 62
CLASS_LAPTOP = 63
CLASS_REMOTE = 65
CLASS_CELL_PHONE = 67
CLASS_BOOK = 73

#: id -> каноническое имя COCO (оно же Detection.label — стабильный ключ для других модулей)
INTERESTING_CLASSES: dict[int, str] = {
    CLASS_PERSON: "person",
    CLASS_TV: "tv",
    CLASS_LAPTOP: "laptop",
    CLASS_REMOTE: "remote",
    CLASS_CELL_PHONE: "cell phone",
    CLASS_BOOK: "book",
}

#: Человекочитаемые названия для отчёта (по-русски).
LABELS_RU: dict[str, str] = {
    "person": "человек",
    "tv": "монитор или телевизор",
    "laptop": "ноутбук",
    "remote": "пульт (легко спутать с телефоном)",
    "cell phone": "телефон",
    "book": "книга или конспект",
}

#: Запрещённые предметы (кроме телефона — у него собственные события и эвристики).
#: person сюда не входит: второе лицо — зона ответственности face_mesh/identity.
FORBIDDEN_LABELS: frozenset[str] = frozenset({"book", "laptop", "tv", "remote"})

PHONE_LABEL = "cell phone"

#: Порог уверенности по классам: телефон мелкий и часто частично перекрыт — опускаем порог,
#: «пульт» наоборот поднимаем, он даёт больше всего ложных срабатываний на кружках и ручках.
DEFAULT_CLASS_CONF: dict[str, float] = {
    "cell phone": 0.30,
    "book": 0.40,
    "laptop": 0.40,
    "tv": 0.40,
    "remote": 0.50,
    "person": 0.50,
}

DEFAULT_HEURISTICS: dict[str, float] = {
    # --- raised ---
    "raised_center_ratio": 0.50,   # центр телефона выше этой доли высоты кадра => поднят
    "face_margin": 0.60,           # bbox лица, расширенный на 60%, — «зона у лица»
    "raised_min_conf": 0.30,       # ниже этой уверенности детекции про raised не говорим
    # --- aimed ---
    "aspect_min": 1.30,            # h/w бокса: вертикальная ориентация телефона
    "area_growth": 1.12,           # площадь выросла в N раз за окно => приближается
    "area_window": 1.50,           # окно анализа площади, сек
    "near_area_ratio": 0.030,      # телефон уже крупный (>=3% кадра) — «рост» не требуется
    "min_area_ratio": 0.0015,      # мусорные боксы меньше 0.15% кадра игнорируем
    "hold_sec": 1.00,              # условие должно удерживаться не меньше секунды
    "min_track_age": 3,            # и трек должен быть виден минимум 3 кадра подряд
}

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# Структуры наблюдений
# --------------------------------------------------------------------------
@dataclass
class Detection:
    """Один объект в кадре.

    Обязательные поля по контракту: label, conf, bbox (x, y, w, h в пикселях
    исходного кадра), area_ratio (площадь бокса / площадь кадра).
    Остальное — для эвристик, отчёта и отладки.
    """

    label: str
    conf: float
    bbox: tuple[float, float, float, float]
    area_ratio: float
    class_id: int = -1
    label_ru: str = ""
    track_id: int = -1
    track_age: int = 0          # сколько кадров подряд объект виден
    age_sec: float = 0.0        # сколько секунд виден
    aspect: float = 0.0         # h / w сглаженного бокса
    area_growth: float = 1.0    # во сколько раз выросла площадь за окно анализа
    raised: bool = False
    aimed: bool = False

    @property
    def center(self) -> tuple[float, float]:
        x, y, w, h = self.bbox
        return x + w / 2.0, y + h / 2.0

    @property
    def is_phone(self) -> bool:
        return self.label == PHONE_LABEL

    @property
    def is_forbidden(self) -> bool:
        return self.label in FORBIDDEN_LABELS

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "label_ru": self.label_ru,
            "conf": round(float(self.conf), 3),
            "bbox": [round(float(v), 1) for v in self.bbox],
            "area_ratio": round(float(self.area_ratio), 5),
            "class_id": int(self.class_id),
            "track_id": int(self.track_id),
            "track_age": int(self.track_age),
            "age_sec": round(float(self.age_sec), 2),
            "aspect": round(float(self.aspect), 2),
            "area_growth": round(float(self.area_growth), 2),
            "raised": bool(self.raised),
            "aimed": bool(self.aimed),
        }


@dataclass
class PhoneHeuristicResult:
    """Результат эвристики: флаг + уверенность + объяснение по-русски для отчёта."""

    active: bool = False
    confidence: float = 0.0
    explain: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:  # удобно писать `if det.phone_raised(...):`
        return bool(self.active)

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": bool(self.active),
            "confidence": round(float(self.confidence), 3),
            "explain": self.explain,
            "detail": self.detail,
        }


# --------------------------------------------------------------------------
# Геометрия (чистые функции, без numpy — вызываются по десятку боксов на кадр)
# --------------------------------------------------------------------------
def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else float(v))


def _frames_ru(n: int) -> str:
    """«1 кадр» / «2 кадра» / «5 кадров» — строки идут в отчёт, склейки «1 кадров» не нужны."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} кадр"
    if n % 10 in (2, 3, 4) and not (11 <= n % 100 <= 14):
        return f"{n} кадра"
    return f"{n} кадров"


def iou_xywh(a: Sequence[float], b: Sequence[float]) -> float:
    """IoU двух боксов в формате (x, y, w, h)."""
    ax1, ay1, aw, ah = a[0], a[1], a[2], a[3]
    bx1, by1, bw, bh = b[0], b[1], b[2], b[3]
    ax2, ay2 = ax1 + aw, ay1 + ah
    bx2, by2 = bx1 + bw, by1 + bh
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = ix2 - ix1, iy2 - iy1
    if iw <= 0.0 or ih <= 0.0:
        return 0.0
    inter = iw * ih
    union = max(aw, 0.0) * max(ah, 0.0) + max(bw, 0.0) * max(bh, 0.0) - inter
    return float(inter / union) if union > 0.0 else 0.0


def _expand(bbox: Sequence[float], margin: float) -> tuple[float, float, float, float]:
    """Расширить бокс на margin (доля от размера) в каждую сторону."""
    x, y, w, h = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
    dx, dy = w * margin, h * margin
    return x - dx, y - dy, w + 2 * dx, h + 2 * dy


def _inside(point: Sequence[float], bbox: Sequence[float]) -> bool:
    x, y, w, h = bbox[0], bbox[1], bbox[2], bbox[3]
    return bool(x <= point[0] <= x + w and y <= point[1] <= y + h)


def _as_xywh(bbox: Any) -> tuple[float, float, float, float] | None:
    """Привести произвольный bbox лица к (x, y, w, h). None, если формат непонятен."""
    if bbox is None:
        return None
    try:
        if isinstance(bbox, dict):
            if all(k in bbox for k in ("x", "y", "w", "h")):
                vals = (bbox["x"], bbox["y"], bbox["w"], bbox["h"])
            elif all(k in bbox for k in ("x1", "y1", "x2", "y2")):
                x1, y1, x2, y2 = bbox["x1"], bbox["y1"], bbox["x2"], bbox["y2"]
                vals = (x1, y1, x2 - x1, y2 - y1)
            else:
                return None
        else:
            seq = list(bbox)
            if len(seq) < 4:
                return None
            vals = (seq[0], seq[1], seq[2], seq[3])
        x, y, w, h = (float(v) for v in vals)
    except Exception:
        return None
    if w <= 0.0 or h <= 0.0:
        return None
    return x, y, w, h


# --------------------------------------------------------------------------
# Детектор
# --------------------------------------------------------------------------
class ObjectDetector:
    """YOLO-детектор объектов + трекинг + эвристики по телефону.

    Публичный интерфейс по контракту: __init__(config), available(), detect(frame_bgr).
    Дополнительно: phone_raised(), phone_aimed_at_screen(), observations(), reset().
    """

    def __init__(self, config: dict | None = None) -> None:
        cfg_all = config if isinstance(config, dict) else {}
        cfg = cfg_all.get("objects", cfg_all)
        if not isinstance(cfg, dict):
            cfg = {}
        self._cfg = cfg

        self.enabled: bool = bool(cfg.get("enabled", True))
        self.imgsz: int = int(cfg.get("imgsz") or 640)
        self.conf_thr: float = float(cfg.get("conf", 0.35))
        self.nms_iou: float = float(cfg.get("nms_iou", 0.45))
        self.max_det: int = int(cfg.get("max_det", 50))

        self.class_conf: dict[str, float] = dict(DEFAULT_CLASS_CONF)
        for key, val in (cfg.get("class_conf") or {}).items():
            try:
                self.class_conf[str(key)] = float(val)
            except Exception:
                continue

        wanted = cfg.get("classes") or list(INTERESTING_CLASSES)
        self.classes: tuple[int, ...] = tuple(
            int(c) for c in wanted if int(c) in INTERESTING_CLASSES
        ) or tuple(INTERESTING_CLASSES)

        track_cfg = cfg.get("track") or {}
        self.track_iou: float = float(track_cfg.get("iou", 0.30))
        self.track_max_missed: int = int(track_cfg.get("max_missed", 5))
        self.track_smooth: float = _clamp01(float(track_cfg.get("smooth", 0.5)))

        self.h: dict[str, float] = dict(DEFAULT_HEURISTICS)
        for key, val in (cfg.get("heuristics") or {}).items():
            if key in self.h:
                try:
                    self.h[key] = float(val)
                except Exception:
                    continue

        # --- состояние ---
        self._np = None
        self._cv2 = None
        self._session = None          # onnxruntime.InferenceSession
        self._input_name = ""
        self._yolo = None             # ultralytics.YOLO
        self._backend: str | None = None
        self._backend_info: str = "не инициализирован"
        self._last_error: str = ""
        self._fail_streak = 0
        self._max_fail = int(cfg.get("max_fail", 10))
        self._tracks: list[dict[str, Any]] = []
        self._next_track_id = 1
        self._frame_shape: tuple[int, int] | None = None
        self._face_bbox: tuple[float, float, float, float] | None = None
        self._frame_idx = 0
        self._last_detections: list[Detection] = []
        self._last_latency_ms = 0.0

        if self.enabled:
            self._init_backend(str(cfg.get("backend") or "auto").lower())

    # ------------------------------------------------------------------
    # Инициализация бэкенда
    # ------------------------------------------------------------------
    def _resolve(self, raw: str) -> Path | None:
        """Найти файл модели: как указано, затем относительно корня репозитория и cwd."""
        if not raw:
            return None
        p = Path(str(raw)).expanduser()
        candidates = [p] if p.is_absolute() else [PROJECT_ROOT / p, Path.cwd() / p, p]
        for c in candidates:
            try:
                if c.is_file():
                    return c
            except OSError:
                continue
        return None

    def _init_backend(self, requested: str) -> None:
        """Подобрать доступный бэкенд. Любая ошибка => деградация, не исключение."""
        try:
            import numpy as _np  # numpy нужен и для препроцессинга, и для NMS

            self._np = _np
        except Exception as exc:
            self._backend_info = "numpy недоступен, детекция объектов отключена"
            self._last_error = f"{type(exc).__name__}: {exc}"
            return

        try:
            import cv2 as _cv2  # нужен только для качественного resize, не обязателен

            self._cv2 = _cv2
        except Exception:
            self._cv2 = None

        if requested == "none":
            self._backend_info = "бэкенд отключён в конфиге"
            return

        if requested in ("auto", "onnx") and self._init_onnx():
            return
        if requested in ("auto", "ultralytics") and self._init_ultralytics():
            return

        if self._backend is None and requested == "auto" and not self._last_error:
            self._backend_info = (
                "модель не найдена: запустите scripts/fetch_models.sh, "
                "чтобы получить models/yolov8n.onnx"
            )

    def _init_onnx(self) -> bool:
        model = self._resolve(self._cfg.get("model_path") or "models/yolov8n.onnx")
        if model is None:
            self._backend_info = "ONNX-модель не найдена (models/yolov8n.onnx)"
            return False
        try:
            import onnxruntime as ort  # ленивый импорт тяжёлой зависимости
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._backend_info = "onnxruntime не установлен"
            return False
        try:
            available = set(ort.get_available_providers())
            wanted = self._cfg.get("providers") or ["CPUExecutionProvider"]
            providers = [p for p in wanted if p in available] or ["CPUExecutionProvider"]
            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            opts.intra_op_num_threads = int(self._cfg.get("threads", 2))
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self._session = ort.InferenceSession(str(model), opts, providers=providers)
            self._input_name = self._session.get_inputs()[0].name
            shape = self._session.get_inputs()[0].shape
            # Если модель экспортирована с фиксированным размером — уважаем его.
            if isinstance(shape, (list, tuple)) and len(shape) == 4:
                side = shape[2] if isinstance(shape[2], int) else None
                if isinstance(side, int) and side > 0:
                    self.imgsz = side
            self._backend = "onnx"
            self._backend_info = f"onnxruntime {ort.__version__} ({'+'.join(providers)}), {model.name}"
            return True
        except Exception as exc:
            self._session = None
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._backend_info = f"не удалось открыть {model.name}: {exc}"
            return False

    def _init_ultralytics(self) -> bool:
        weights = self._resolve(self._cfg.get("weights_pt") or "models/yolov8n.pt")
        if weights is None:
            # Конструктор ultralytics.YOLO скачивает веса из сети — это запрещено,
            # поэтому без локального файла бэкенд просто не поднимаем.
            msg = "models/yolov8n.pt отсутствует, ultralytics не поднят"
            if self._backend_info and self._backend_info != "не инициализирован":
                msg = f"{self._backend_info}; {msg}"
            self._backend_info = msg
            return False
        try:
            os.environ.setdefault("YOLO_OFFLINE", "1")  # без проверок обновлений
            from ultralytics import YOLO  # ленивый импорт тяжёлой зависимости

            self._yolo = YOLO(str(weights))
            self._backend = "ultralytics"
            self._backend_info = f"ultralytics YOLO, {weights.name}"
            return True
        except Exception as exc:
            self._yolo = None
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._backend_info = f"ultralytics недоступен: {exc}"
            return False

    # ------------------------------------------------------------------
    # Публичные свойства
    # ------------------------------------------------------------------
    def available(self) -> bool:
        return bool(self.enabled and self._backend is not None)

    @property
    def backend(self) -> str:
        return self._backend or "none"

    def describe(self) -> dict[str, Any]:
        """Короткая сводка состояния — для hello/диагностики."""
        return {
            "available": self.available(),
            "backend": self.backend,
            "info": self._backend_info,
            "imgsz": self.imgsz,
            "classes": [INTERESTING_CLASSES[c] for c in self.classes],
            "last_error": self._last_error,
            "latency_ms": round(self._last_latency_ms, 1),
        }

    def reset(self) -> None:
        """Сбросить треки (новая сессия / после паузы)."""
        self._tracks.clear()
        self._last_detections = []
        self._frame_idx = 0

    def set_face_bbox(self, face_bbox: Any) -> None:
        """Подсказать детектору текущий bbox лица (из FaceAnalyzer).

        Таймеры удержания эвристик крутятся внутри detect(), поэтому для варианта
        «телефон у лица» лицо нужно сообщить до кадра: либо этим методом, либо
        аргументом detect(frame, face_bbox=...). Без подсказки удержание считается
        только по порогу высоты кадра.
        """
        face = _as_xywh(face_bbox)
        if face is not None:
            self._face_bbox = face

    # ------------------------------------------------------------------
    # Основной метод
    # ------------------------------------------------------------------
    def detect(self, frame_bgr, face_bbox: Any = None) -> list[Detection]:
        """Детекция объектов в кадре. НИКОГДА не бросает исключений наружу.

        Возвращает список Detection в координатах исходного кадра, отсортированный
        по уверенности. Пустой список — если бэкенда нет, кадр невалиден или
        произошла ошибка инференса.

        face_bbox — необязательная подсказка с текущим bbox лица: только detect()
        двигает таймеры удержания эвристик (он вызывается ровно раз на кадр),
        поэтому лицо лучше передавать сюда. Если не передать, будет использован
        последний известный bbox (его запоминают phone_raised/phone_aimed_at_screen).
        """
        if not self.available() or frame_bgr is None:
            return []
        self.set_face_bbox(face_bbox)
        started = time.perf_counter()
        try:
            np = self._np
            frame = np.asarray(frame_bgr)
            if frame.ndim != 3 or frame.shape[0] < 8 or frame.shape[1] < 8 or frame.shape[2] < 3:
                return []
            height, width = int(frame.shape[0]), int(frame.shape[1])
            self._frame_shape = (height, width)
            now = time.time()

            if self._backend == "onnx":
                raw = self._infer_onnx(frame)
            else:
                raw = self._infer_ultralytics(frame)

            raw = self._filter_and_nms(raw, width, height)
            dets = self._update_tracks(raw, now, width, height)
            self._last_detections = dets
            self._frame_idx += 1
            self._fail_streak = 0
            return dets
        except Exception as exc:  # никакой ошибкой наружу не светим
            self._fail_streak += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            if self._fail_streak >= self._max_fail:
                # Стабильно падаем — уходим в деградированный режим, но не роняем сайдкар.
                self._backend = None
                self._session = None
                self._yolo = None
                self._backend_info = f"бэкенд отключён после {self._fail_streak} ошибок: {exc}"
            return []
        finally:
            self._last_latency_ms = (time.perf_counter() - started) * 1000.0

    # ------------------------------------------------------------------
    # Препроцессинг
    # ------------------------------------------------------------------
    def _resize(self, img, new_w: int, new_h: int):
        """Resize через cv2, при его отсутствии — ближайший сосед на numpy."""
        if self._cv2 is not None:
            interp = self._cv2.INTER_AREA if new_w < img.shape[1] else self._cv2.INTER_LINEAR
            return self._cv2.resize(img, (new_w, new_h), interpolation=interp)
        np = self._np
        ys = (np.arange(new_h) * (img.shape[0] / float(new_h))).astype(np.int32)
        xs = (np.arange(new_w) * (img.shape[1] / float(new_w))).astype(np.int32)
        ys = np.clip(ys, 0, img.shape[0] - 1)
        xs = np.clip(xs, 0, img.shape[1] - 1)
        return img[ys][:, xs]

    def _letterbox(self, frame) -> tuple[Any, float, int, int]:
        """Вписать кадр в квадрат imgsz с сохранением пропорций, серая заливка 114.

        Возвращает (canvas, ratio, pad_x, pad_y) — ratio и паддинги нужны для
        обратного масштабирования боксов.
        """
        np = self._np
        size = int(self.imgsz)
        h, w = int(frame.shape[0]), int(frame.shape[1])
        ratio = min(size / float(h), size / float(w))
        new_w = max(1, min(size, int(round(w * ratio))))
        new_h = max(1, min(size, int(round(h * ratio))))
        resized = self._resize(frame[:, :, :3], new_w, new_h)
        canvas = np.full((size, size, 3), 114, dtype=np.uint8)
        pad_x = (size - new_w) // 2
        pad_y = (size - new_h) // 2
        canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized.astype(np.uint8, copy=False)
        return canvas, ratio, pad_x, pad_y

    def _blob(self, canvas):
        """BGR->RGB, /255, HWC->NCHW, float32, непрерывный буфер."""
        np = self._np
        rgb = canvas[:, :, ::-1].astype(np.float32) / 255.0
        return np.ascontiguousarray(rgb.transpose(2, 0, 1)[None, ...], dtype=np.float32)

    # ------------------------------------------------------------------
    # Инференс
    # ------------------------------------------------------------------
    def _infer_onnx(self, frame):
        """ONNX-инференс + постобработка YOLOv8 (1, 84, 8400) -> (M, 6) xyxy/conf/cls."""
        np = self._np
        canvas, ratio, pad_x, pad_y = self._letterbox(frame)
        outputs = self._session.run(None, {self._input_name: self._blob(canvas)})
        arr = np.asarray(outputs[0], dtype=np.float32)

        if arr.ndim == 3:
            arr = arr[0]
        if arr.ndim != 2:
            return np.zeros((0, 6), dtype=np.float32)
        # Выход YOLOv8 — (84, 8400): признаки по строкам. Транспонируем в (8400, 84).
        if arr.shape[0] < arr.shape[1]:
            arr = arr.T

        ncol = arr.shape[1]
        if ncol < 6:
            return np.zeros((0, 6), dtype=np.float32)
        if ncol == 6:
            # уже NMS-ed выход (end2end-экспорт): x1,y1,x2,y2,conf,cls
            boxes = arr[:, :4].copy()
            scores = arr[:, 4]
            cls_ids = arr[:, 5]
        else:
            boxes_cxcywh = arr[:, :4]
            if ncol >= 85 and (ncol - 5) >= 80:
                # YOLOv5-подобный выход: objectness * class score
                obj = arr[:, 4:5]
                cls_scores = arr[:, 5:] * obj
            else:
                cls_scores = arr[:, 4:]
            cls_ids = cls_scores.argmax(axis=1)
            scores = cls_scores[np.arange(cls_scores.shape[0]), cls_ids]
            # cxcywh -> xyxy
            boxes = np.empty((boxes_cxcywh.shape[0], 4), dtype=np.float32)
            boxes[:, 0] = boxes_cxcywh[:, 0] - boxes_cxcywh[:, 2] / 2.0
            boxes[:, 1] = boxes_cxcywh[:, 1] - boxes_cxcywh[:, 3] / 2.0
            boxes[:, 2] = boxes_cxcywh[:, 0] + boxes_cxcywh[:, 2] / 2.0
            boxes[:, 3] = boxes_cxcywh[:, 1] + boxes_cxcywh[:, 3] / 2.0

        # Грубый отсев до NMS: минимальный порог по всем интересным классам.
        min_conf = min([self.conf_thr] + [
            self.class_conf.get(INTERESTING_CLASSES[c], self.conf_thr) for c in self.classes
        ])
        keep_cls = np.isin(cls_ids.astype(np.int32), np.array(self.classes, dtype=np.int32))
        mask = (scores >= float(min_conf)) & keep_cls
        if not mask.any():
            return np.zeros((0, 6), dtype=np.float32)
        boxes = boxes[mask]
        scores = scores[mask]
        cls_ids = cls_ids[mask].astype(np.float32)

        # Обратное масштабирование в координаты исходного кадра.
        boxes[:, [0, 2]] -= float(pad_x)
        boxes[:, [1, 3]] -= float(pad_y)
        boxes /= float(ratio) if ratio > 0 else 1.0

        out = np.concatenate(
            [boxes, scores.reshape(-1, 1).astype(np.float32), cls_ids.reshape(-1, 1)], axis=1
        )
        return out.astype(np.float32)

    def _infer_ultralytics(self, frame):
        """Инференс через ultralytics. Возвращает (M, 6) xyxy/conf/cls."""
        np = self._np
        results = self._yolo.predict(
            source=frame[:, :, :3],
            imgsz=int(self.imgsz),
            conf=float(min([self.conf_thr] + list(self.class_conf.values()))),
            iou=float(self.nms_iou),
            classes=list(self.classes),
            max_det=int(self.max_det),
            device="cpu",
            verbose=False,
        )
        rows: list[list[float]] = []
        for res in results or []:
            boxes = getattr(res, "boxes", None)
            if boxes is None or len(boxes) == 0:
                continue
            xyxy = np.asarray(boxes.xyxy.cpu().numpy() if hasattr(boxes.xyxy, "cpu") else boxes.xyxy)
            conf = np.asarray(boxes.conf.cpu().numpy() if hasattr(boxes.conf, "cpu") else boxes.conf)
            cls = np.asarray(boxes.cls.cpu().numpy() if hasattr(boxes.cls, "cpu") else boxes.cls)
            for i in range(xyxy.shape[0]):
                rows.append([
                    float(xyxy[i][0]), float(xyxy[i][1]), float(xyxy[i][2]), float(xyxy[i][3]),
                    float(conf[i]), float(cls[i]),
                ])
        if not rows:
            return np.zeros((0, 6), dtype=np.float32)
        return np.asarray(rows, dtype=np.float32)

    # ------------------------------------------------------------------
    # NMS и фильтрация
    # ------------------------------------------------------------------
    def _nms(self, boxes, scores) -> list[int]:
        """Свой NMS на numpy (xyxy). Без torch/torchvision — лишних зависимостей не тянем."""
        np = self._np
        order = scores.argsort()[::-1]
        x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
        areas = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
        keep: list[int] = []
        while order.size > 0 and len(keep) < self.max_det:
            i = int(order[0])
            keep.append(i)
            if order.size == 1:
                break
            rest = order[1:]
            ix1 = np.maximum(x1[i], x1[rest])
            iy1 = np.maximum(y1[i], y1[rest])
            ix2 = np.minimum(x2[i], x2[rest])
            iy2 = np.minimum(y2[i], y2[rest])
            iw = np.clip(ix2 - ix1, 0, None)
            ih = np.clip(iy2 - iy1, 0, None)
            inter = iw * ih
            union = areas[i] + areas[rest] - inter
            iou = np.where(union > 0, inter / np.maximum(union, 1e-9), 0.0)
            order = rest[iou <= float(self.nms_iou)]
        return keep

    def _filter_and_nms(self, raw, width: int, height: int) -> list[tuple[int, float, tuple[float, float, float, float]]]:
        """Отсечь неинтересные классы и слабые боксы, прижать к кадру, применить NMS по классам."""
        np = self._np
        if raw is None or len(raw) == 0:
            return []
        arr = np.asarray(raw, dtype=np.float32).reshape(-1, 6)
        # прижимаем к границам кадра
        arr[:, 0] = np.clip(arr[:, 0], 0, width - 1)
        arr[:, 1] = np.clip(arr[:, 1], 0, height - 1)
        arr[:, 2] = np.clip(arr[:, 2], 0, width - 1)
        arr[:, 3] = np.clip(arr[:, 3], 0, height - 1)

        frame_area = float(width * height)
        min_area = float(self.h["min_area_ratio"]) * frame_area
        results: list[tuple[int, float, tuple[float, float, float, float]]] = []

        for cls_id in sorted({int(c) for c in arr[:, 5].tolist()}):
            if cls_id not in INTERESTING_CLASSES or cls_id not in self.classes:
                continue
            label = INTERESTING_CLASSES[cls_id]
            thr = float(self.class_conf.get(label, self.conf_thr))
            sub = arr[(arr[:, 5].astype(np.int32) == cls_id) & (arr[:, 4] >= thr)]
            if sub.shape[0] == 0:
                continue
            w = sub[:, 2] - sub[:, 0]
            h = sub[:, 3] - sub[:, 1]
            sub = sub[(w > 1) & (h > 1) & ((w * h) >= min_area)]
            if sub.shape[0] == 0:
                continue
            for idx in self._nms(sub[:, :4], sub[:, 4]):
                x1, y1, x2, y2 = (float(v) for v in sub[idx, :4])
                results.append((cls_id, float(sub[idx, 4]), (x1, y1, x2 - x1, y2 - y1)))

        results.sort(key=lambda r: r[1], reverse=True)
        return results[: self.max_det]

    # ------------------------------------------------------------------
    # Трекинг
    # ------------------------------------------------------------------
    def _update_tracks(self, dets, now: float, width: int, height: int) -> list[Detection]:
        """Сопоставление с треками по IoU (жадно, внутри класса) + EMA-сглаживание бокса.

        Сглаживание убирает дрожание бокса, track_age даёт число подряд видимых кадров —
        на нём держатся эвристики: одиночное ложное срабатывание YOLO не поднимет событие.
        """
        frame_area = float(width * height) or 1.0
        for track in self._tracks:
            track["matched"] = False

        pairs: list[tuple[float, int, int]] = []
        for di, (cls_id, conf, bbox) in enumerate(dets):
            for ti, track in enumerate(self._tracks):
                if track["class_id"] != cls_id:
                    continue
                score = iou_xywh(bbox, track["bbox"])
                if score >= self.track_iou:
                    pairs.append((score, di, ti))
        pairs.sort(reverse=True)

        det_to_track: dict[int, int] = {}
        used_tracks: set[int] = set()
        for _score, di, ti in pairs:
            if di in det_to_track or ti in used_tracks:
                continue
            det_to_track[di] = ti
            used_tracks.add(ti)

        alpha = self.track_smooth
        out: list[Detection] = []
        for di, (cls_id, conf, bbox) in enumerate(dets):
            ti = det_to_track.get(di)
            if ti is None:
                track = {
                    "id": self._next_track_id,
                    "class_id": cls_id,
                    "bbox": tuple(bbox),
                    "conf": conf,
                    "age": 0,
                    "missed": 0,
                    "first_seen": now,
                    "last_seen": now,
                    "area_hist": [],
                    "raised_since": None,
                    "aimed_since": None,
                    "matched": True,
                }
                self._next_track_id += 1
                self._tracks.append(track)
            else:
                track = self._tracks[ti]
                prev = track["bbox"]
                track["bbox"] = tuple(
                    alpha * float(prev[k]) + (1.0 - alpha) * float(bbox[k]) for k in range(4)
                )
                track["conf"] = alpha * float(track["conf"]) + (1.0 - alpha) * conf
                track["missed"] = 0
                track["matched"] = True
            track["age"] = int(track["age"]) + 1
            track["last_seen"] = now

            sx, sy, sw, sh = track["bbox"]
            area_ratio = max(sw, 0.0) * max(sh, 0.0) / frame_area
            hist = track["area_hist"]
            hist.append((now, area_ratio))
            window = float(self.h["area_window"]) * 2.0 + 1.0
            track["area_hist"] = [item for item in hist if now - item[0] <= window][-120:]
            track["area_ratio"] = area_ratio
            track["aspect"] = (sh / sw) if sw > 0 else 0.0
            track["growth"] = self._area_growth(track, now)[0]

            # Таймеры удержания эвристик двигаются ровно здесь — один раз на кадр.
            # Чтение через phone_raised()/phone_aimed_at_screen() состояние не меняет.
            is_raised = is_aimed = False
            if cls_id == CLASS_CELL_PHONE:
                res = self._eval_aimed(track, self._face_bbox, now, mutate=True)
                is_raised = bool(res["raised"]["ok"])
                is_aimed = bool(res["ok"])

            label = INTERESTING_CLASSES.get(cls_id, str(cls_id))
            out.append(Detection(
                label=label,
                conf=float(track["conf"]),
                bbox=(float(sx), float(sy), float(sw), float(sh)),
                area_ratio=float(area_ratio),
                class_id=int(cls_id),
                label_ru=LABELS_RU.get(label, label),
                track_id=int(track["id"]),
                track_age=int(track["age"]),
                age_sec=float(now - track["first_seen"]),
                aspect=float(track["aspect"]),
                area_growth=float(track["growth"]),
                raised=is_raised,
                aimed=is_aimed,
            ))

        # Пропавшие треки: держим max_missed кадров (переживают мигание детектора),
        # но age обнуляем — «подряд видимых кадров» больше нет.
        alive: list[dict[str, Any]] = []
        for track in self._tracks:
            if track.get("matched"):
                alive.append(track)
                continue
            track["missed"] = int(track["missed"]) + 1
            track["age"] = 0
            track["raised_since"] = None
            track["aimed_since"] = None
            if track["missed"] <= self.track_max_missed:
                alive.append(track)
        self._tracks = alive

        out.sort(key=lambda d: d.conf, reverse=True)
        return out

    def _area_growth(self, track: dict[str, Any], now: float) -> tuple[float, bool]:
        """Во сколько раз выросла площадь бокса за окно. (growth, достоверно ли)."""
        window = float(self.h["area_window"])
        hist = [(ts, a) for ts, a in track.get("area_hist", []) if now - ts <= window]
        if len(hist) < 4:
            return 1.0, False
        half = len(hist) // 2
        first = sum(a for _ts, a in hist[:half]) / float(half)
        last = sum(a for _ts, a in hist[half:]) / float(len(hist) - half)
        if first <= 1e-9:
            return 1.0, False
        return float(last / first), True

    # ------------------------------------------------------------------
    # Эвристики по телефону
    # ------------------------------------------------------------------
    def _phone_tracks(self) -> list[dict[str, Any]]:
        return [t for t in self._tracks if t["class_id"] == CLASS_CELL_PHONE and t.get("missed", 0) == 0]

    def _eval_raised(self, track: dict[str, Any], face: tuple[float, float, float, float] | None,
                     now: float, mutate: bool = False) -> dict[str, Any]:
        """PHONE_RAISED: телефон поднят к лицу / в верхнюю половину кадра.

        Условие (ИЛИ): центр бокса выше `raised_center_ratio` высоты кадра
        ЛИБО центр попадает в bbox лица, расширенный на `face_margin`.
        Второй вариант важен, когда студент сидит низко и его лицо само в нижней
        половине кадра: «телефон у лица» там не ловится порогом по высоте.

        mutate=True двигает таймер удержания (только из detect(), раз на кадр).
        """
        shape = self._frame_shape
        if shape is None:
            return {"ok": False, "conf": 0.0, "explain": "нет кадра", "by": "none"}
        height, width = shape
        x, y, w, h = track["bbox"]
        cx, cy = x + w / 2.0, y + h / 2.0
        conf_det = float(track["conf"])
        if conf_det < float(self.h["raised_min_conf"]):
            return {
                "ok": False, "conf": 0.0, "by": "none",
                "explain": f"уверенность детекции телефона {conf_det:.2f} ниже порога "
                           f"{self.h['raised_min_conf']:.2f}",
            }

        ratio = float(self.h["raised_center_ratio"])
        y_norm = cy / float(height)
        by_height = y_norm < ratio
        margin_h = max(0.0, (ratio - y_norm) / max(ratio, 1e-6))  # 0..1, насколько высоко

        by_face = False
        face_dist_norm = None
        if face is not None:
            zone = _expand(face, float(self.h["face_margin"]))
            by_face = _inside((cx, cy), zone)
            fx, fy, fw, fh = face
            diag = (fw ** 2 + fh ** 2) ** 0.5 or 1.0
            dist = (((cx - (fx + fw / 2.0)) ** 2 + (cy - (fy + fh / 2.0)) ** 2) ** 0.5)
            face_dist_norm = dist / diag

        ok = bool(by_height or by_face)
        if mutate:
            track["raised_since"] = (track.get("raised_since") or now) if ok else None
        since = track.get("raised_since")
        held = (now - float(since)) if since else 0.0

        geom = 0.0
        if by_face and face_dist_norm is not None:
            geom = max(geom, _clamp01(1.2 - face_dist_norm))
        if by_height:
            geom = max(geom, _clamp01(0.45 + margin_h))
        conf = _clamp01(0.55 * conf_det + 0.45 * geom) if ok else 0.0

        if ok and by_face and by_height:
            why = (f"центр телефона на {y_norm:.2f} высоты кадра (выше середины) "
                   f"и в зоне лица (дистанция {face_dist_norm:.2f} диагонали лица)")
            by = "face+height"
        elif ok and by_face:
            why = f"центр телефона в зоне лица (дистанция {face_dist_norm:.2f} диагонали лица)"
            by = "face"
        elif ok:
            why = f"центр телефона на {y_norm:.2f} высоты кадра, порог {ratio:.2f}"
            by = "height"
        else:
            why = (f"телефон внизу кадра (центр на {y_norm:.2f} высоты)"
                   + ("" if face is None else " и вне зоны лица"))
            by = "none"

        return {
            "ok": ok, "conf": conf, "by": by, "held": held,
            "explain": ("Телефон поднят: " if ok else "Телефон не поднят: ") + why + ".",
            "y_norm": y_norm, "face_dist": face_dist_norm,
        }

    def _eval_aimed(self, track: dict[str, Any], face: tuple[float, float, float, float] | None,
                    now: float, mutate: bool = False) -> dict[str, Any]:
        """PHONE_AIMED_AT_SCREEN: телефон развёрнут вертикально и наведён на экран.

        Это и есть «фотографирует задание» — самая дорогая по весу риска картинка.
        Условия (И):
          1. трек живёт >= min_track_age кадров (отсев одиночных ложных боксов YOLO);
          2. бокс вертикальный: h/w >= aspect_min (телефон держат портретно,
             камерой к экрану; лежащий на столе телефон почти всегда горизонтальный);
          3. телефон поднят (та же эвристика _eval_raised);
          4. площадь бокса растёт (growth >= area_growth) ЛИБО телефон уже крупный
             (area_ratio >= near_area_ratio) — приближение к камере/экрану уже завершилось;
          5. всё перечисленное удерживается >= hold_sec секунд — мгновенные
             «поднял и убрал» не считаются наведением.

        mutate=True двигает таймер удержания (только из detect(), раз на кадр).
        """
        min_age = int(self.h["min_track_age"])
        aspect_min = float(self.h["aspect_min"])
        growth_min = float(self.h["area_growth"])
        near = float(self.h["near_area_ratio"])
        hold = float(self.h["hold_sec"])

        raised = self._eval_raised(track, face, now, mutate=mutate)
        aspect = float(track.get("aspect") or 0.0)
        area_ratio = float(track.get("area_ratio") or 0.0)
        growth, growth_known = self._area_growth(track, now)
        age = int(track.get("age") or 0)

        c_age = age >= min_age
        c_aspect = aspect >= aspect_min
        c_raised = bool(raised["ok"])
        c_near = area_ratio >= near
        c_growth = (growth >= growth_min and growth_known) or c_near

        all_ok = c_age and c_aspect and c_raised and c_growth
        if mutate:
            track["aimed_since"] = (track.get("aimed_since") or now) if all_ok else None
        since = track.get("aimed_since")
        held = (now - float(since)) if since else 0.0
        ok = bool(all_ok and held >= hold)

        # Уверенность: детекция + запасы по каждому геометрическому условию + выдержка.
        conf = 0.0
        if all_ok:
            m_aspect = _clamp01((aspect - aspect_min) / 0.7)
            m_growth = _clamp01((growth - 1.0) / max(growth_min - 1.0, 1e-6)) if growth_known else 0.0
            m_near = _clamp01(area_ratio / max(near, 1e-6))
            m_hold = _clamp01(held / max(hold, 1e-6))
            conf = _clamp01(
                0.35 * float(track["conf"])
                + 0.20 * float(raised["conf"])
                + 0.15 * m_aspect
                + 0.15 * max(m_growth, m_near)
                + 0.15 * m_hold
            )

        if ok:
            # Объяснение собирается по частям — так его читабельно вставлять в отчёт.
            parts = [f"бокс вертикальный (h/w={aspect:.2f})",
                     raised["explain"].split(": ", 1)[-1].rstrip(".")]
            if growth_known and growth >= growth_min:
                parts.append(f"площадь бокса выросла на {(growth - 1.0) * 100:.0f}% "
                             f"за {self.h['area_window']:.1f} с (приближается к экрану)")
            if c_near:
                parts.append(f"телефон занимает {area_ratio * 100:.1f}% кадра (крупный план)")
            parts.append(f"условие удерживается {held:.1f} с")
            explain = "Телефон наведён на экран: " + "; ".join(parts) + "."
        else:
            missing: list[str] = []
            if not c_age:
                missing.append(f"трек слишком короткий ({age} кадров < {min_age})")
            if not c_aspect:
                missing.append(f"бокс не вертикальный (h/w={aspect:.2f} < {aspect_min:.2f})")
            if not c_raised:
                missing.append("телефон не поднят")
            if not c_growth:
                missing.append(
                    f"не приближается (рост площади {growth:.2f} < {growth_min:.2f}, "
                    f"площадь {area_ratio * 100:.1f}% < {near * 100:.1f}%)"
                )
            if all_ok and held < hold:
                missing.append(f"удерживается {held:.1f} с < {hold:.1f} с")
            explain = "Наведения на экран нет: " + ("; ".join(missing) or "условия не выполнены") + "."

        return {
            "ok": ok, "conf": conf, "explain": explain, "held": held,
            "aspect": aspect, "growth": growth, "growth_known": growth_known,
            "area_ratio": area_ratio, "age": age, "raised": raised,
        }

    def phone_raised(self, face_bbox: Any = None, detections: Iterable[Detection] | None = None,
                     now: float | None = None) -> PhoneHeuristicResult:
        """Эвристика PHONE_RAISED по последнему кадру.

        face_bbox — bbox лица из FaceAnalyzer (x, y, w, h) или None, тогда работает
        только порог по высоте кадра. Возвращает флаг + уверенность + объяснение.
        Метод ничего не меняет в состоянии треков, кроме запоминания подсказки о лице
        для следующего кадра.
        """
        try:
            now = time.time() if now is None else float(now)
            face = _as_xywh(face_bbox)
            self.set_face_bbox(face)
            tracks = self._phone_tracks()
            if not tracks:
                return PhoneHeuristicResult(False, 0.0, "Телефон в кадре не обнаружен.", {})
            best: tuple[dict[str, Any], dict[str, Any]] | None = None
            for track in tracks:
                res = self._eval_raised(track, face, now)
                if best is None or (res["ok"], res["conf"]) > (best[1]["ok"], best[1]["conf"]):
                    best = (track, res)
            track, res = best  # type: ignore[misc]
            if res["ok"]:
                track_ids = [t["id"] for t in tracks]
            else:
                track_ids = [track["id"]]
            return PhoneHeuristicResult(
                active=bool(res["ok"]),
                confidence=float(res["conf"]),
                explain=str(res["explain"]),
                detail={
                    "track_id": int(track["id"]),
                    "track_age": int(track.get("age") or 0),
                    "bbox": [round(float(v), 1) for v in track["bbox"]],
                    "det_conf": round(float(track["conf"]), 3),
                    "trigger": res["by"],
                    "held_sec": round(float(res.get("held") or 0.0), 2),
                    "center_y_norm": round(float(res.get("y_norm") or 0.0), 3),
                    "face_dist_norm": (None if res.get("face_dist") is None
                                       else round(float(res["face_dist"]), 3)),
                    "face_bbox": (None if face is None else [round(float(v), 1) for v in face]),
                    "phone_tracks": track_ids,
                    "thresholds": {
                        "raised_center_ratio": self.h["raised_center_ratio"],
                        "face_margin": self.h["face_margin"],
                        "raised_min_conf": self.h["raised_min_conf"],
                    },
                },
            )
        except Exception as exc:
            return PhoneHeuristicResult(False, 0.0, "Эвристика недоступна.",
                                        {"error": f"{type(exc).__name__}: {exc}"})

    def phone_aimed_at_screen(self, face_bbox: Any = None,
                              detections: Iterable[Detection] | None = None,
                              now: float | None = None) -> PhoneHeuristicResult:
        """Эвристика PHONE_AIMED_AT_SCREEN по последнему кадру (см. _eval_aimed).

        Таймер удержания крутит detect(), здесь он только читается, поэтому вызывать
        метод можно сколько угодно раз на кадр — результат не поедет.
        """
        try:
            now = time.time() if now is None else float(now)
            face = _as_xywh(face_bbox)
            self.set_face_bbox(face)
            tracks = self._phone_tracks()
            if not tracks:
                return PhoneHeuristicResult(False, 0.0, "Телефон в кадре не обнаружен.", {})
            best: tuple[dict[str, Any], dict[str, Any]] | None = None
            for track in tracks:
                res = self._eval_aimed(track, face, now)
                if best is None or (res["ok"], res["conf"]) > (best[1]["ok"], best[1]["conf"]):
                    best = (track, res)
            track, res = best  # type: ignore[misc]
            return PhoneHeuristicResult(
                active=bool(res["ok"]),
                confidence=float(res["conf"]),
                explain=str(res["explain"]),
                detail={
                    "track_id": int(track["id"]),
                    "track_age": int(res["age"]),
                    "bbox": [round(float(v), 1) for v in track["bbox"]],
                    "det_conf": round(float(track["conf"]), 3),
                    "aspect": round(float(res["aspect"]), 2),
                    "area_ratio": round(float(res["area_ratio"]), 5),
                    "area_growth": round(float(res["growth"]), 2),
                    "growth_known": bool(res["growth_known"]),
                    "held_sec": round(float(res["held"]), 2),
                    "raised": bool(res["raised"]["ok"]),
                    "raised_explain": res["raised"]["explain"],
                    "face_bbox": (None if face is None else [round(float(v), 1) for v in face]),
                    "thresholds": {
                        "aspect_min": self.h["aspect_min"],
                        "area_growth": self.h["area_growth"],
                        "area_window": self.h["area_window"],
                        "near_area_ratio": self.h["near_area_ratio"],
                        "hold_sec": self.h["hold_sec"],
                        "min_track_age": self.h["min_track_age"],
                    },
                },
            )
        except Exception as exc:
            return PhoneHeuristicResult(False, 0.0, "Эвристика недоступна.",
                                        {"error": f"{type(exc).__name__}: {exc}"})

    # ------------------------------------------------------------------
    # Сводка наблюдений для Event Engine
    # ------------------------------------------------------------------
    def observations(self, face_bbox: Any = None, now: float | None = None) -> dict[str, Any]:
        """Готовые наблюдения по последнему кадру. События из них делает Event Engine.

        Ключи phone_in_frame / phone_raised / phone_aimed_at_screen / forbidden_object
        соответствуют EventKind.PHONE_IN_FRAME, PHONE_RAISED, PHONE_AIMED_AT_SCREEN,
        FORBIDDEN_OBJECT — но сам модуль события не создаёт.
        """
        try:
            now = time.time() if now is None else float(now)
            dets = list(self._last_detections)
            phones = [d for d in dets if d.is_phone]
            forbidden = [d for d in dets if d.is_forbidden]
            raised = self.phone_raised(face_bbox, now=now)
            aimed = self.phone_aimed_at_screen(face_bbox, now=now)
            for det in dets:
                if det.is_phone:
                    det.raised = bool(raised.active and raised.detail.get("track_id") == det.track_id)
                    det.aimed = bool(aimed.active and aimed.detail.get("track_id") == det.track_id)

            phone_conf = max((d.conf for d in phones), default=0.0)
            best_forbidden = max(forbidden, key=lambda d: d.conf, default=None)
            return {
                "available": self.available(),
                "backend": self.backend,
                "frame_idx": self._frame_idx,
                "latency_ms": round(self._last_latency_ms, 1),
                "detections": [d.to_dict() for d in dets],
                "person_count": sum(1 for d in dets if d.label == "person"),
                "phone_in_frame": bool(phones),
                "phone_conf": round(float(phone_conf), 3),
                "phone_count": len(phones),
                "phone_explain": (
                    f"В кадре телефон (уверенность {phone_conf:.2f}, "
                    f"{_frames_ru(phones[0].track_age)} подряд)." if phones
                    else "Телефон в кадре не обнаружен."
                ),
                "phone_raised": raised.to_dict(),
                "phone_aimed_at_screen": aimed.to_dict(),
                "forbidden_object": bool(forbidden),
                "forbidden_labels": sorted({d.label for d in forbidden}),
                "forbidden_explain": (
                    f"В кадре посторонний предмет: {best_forbidden.label_ru} "
                    f"(уверенность {best_forbidden.conf:.2f})." if best_forbidden is not None
                    else "Посторонних предметов не видно."
                ),
            }
        except Exception as exc:
            return {
                "available": self.available(),
                "backend": self.backend,
                "detections": [],
                "phone_in_frame": False,
                "forbidden_object": False,
                "error": f"{type(exc).__name__}: {exc}",
            }


__all__ = [
    "ObjectDetector",
    "Detection",
    "PhoneHeuristicResult",
    "INTERESTING_CLASSES",
    "FORBIDDEN_LABELS",
    "LABELS_RU",
    "PHONE_LABEL",
    "iou_xywh",
]
