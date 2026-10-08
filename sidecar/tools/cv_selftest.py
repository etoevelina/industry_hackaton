#!/usr/bin/env python3
"""
Самопроверка CV-каналов на реальном кадре.

Отвечает на вопрос, который нельзя закрыть ни одним юнит-тестом: что детекторы
ВИДЯТ на конкретном изображении. Печатает сырые наблюдения — углы, зоны, боксы,
а не вердикты, потому что вердикты рождаются в EventEngine, а не здесь.

Запуск:
    python sidecar/tools/cv_selftest.py --camera 0        # кадр с вебкамеры
    python sidecar/tools/cv_selftest.py --image face.jpg  # готовый кадр
    python sidecar/tools/cv_selftest.py --synthetic       # без камеры: проверка конвейера
    python sidecar/tools/cv_selftest.py --camera 0 --save out.jpg   # с разметкой

Код возврата: 0 — все доступные каналы отработали, 1 — хотя бы один упал.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

try:
    import cv2
except ImportError:
    print("Нужен opencv-python: pip install opencv-python")
    sys.exit(1)


def _line(title: str) -> None:
    print(f"\n{'─' * 72}\n{title}\n{'─' * 72}")


def synthetic_frame(w: int = 640, h: int = 480) -> np.ndarray:
    """Кадр без лица: проверяет, что конвейер не падает на пустом входе."""
    frame = np.full((h, w, 3), 40, dtype=np.uint8)
    cv2.rectangle(frame, (int(w * 0.3), int(h * 0.25)), (int(w * 0.7), int(h * 0.85)), (90, 90, 90), -1)
    cv2.putText(frame, "SYNTHETIC - no face", (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
    return frame


def grab_camera(index: int, warmup: int = 12) -> np.ndarray | None:
    """Снимает кадр, пропустив первые кадры: вебкамере нужно время на экспозицию."""
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        print(f"Камера {index} не открылась. Проверьте разрешения: "
              "Системные настройки → Конфиденциальность → Камера.")
        return None
    frame = None
    for _ in range(warmup):
        ok, f = cap.read()
        if ok:
            frame = f
        time.sleep(0.03)
    cap.release()
    return frame


def main() -> int:
    ap = argparse.ArgumentParser(description="Самопроверка CV-каналов прокторинга")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--camera", type=int, help="индекс камеры")
    src.add_argument("--image", type=str, help="путь к изображению")
    src.add_argument("--synthetic", action="store_true", help="сгенерированный кадр без лица")
    ap.add_argument("--save", type=str, help="сохранить кадр с разметкой")
    args = ap.parse_args()

    # --- получаем кадр ---
    if args.synthetic:
        frame = synthetic_frame()
        source = "синтетический кадр"
    elif args.image:
        frame = cv2.imread(args.image)
        if frame is None:
            print(f"Не удалось прочитать {args.image}")
            return 1
        source = args.image
    else:
        frame = grab_camera(args.camera)
        if frame is None:
            return 1
        source = f"камера {args.camera}"

    h, w = frame.shape[:2]
    print(f"Источник: {source}   Размер кадра: {w}x{h}")

    from config import ProctorConfig  # noqa: E402

    cfg = ProctorConfig()
    cfg_dict = cfg.to_dict() if hasattr(cfg, "to_dict") else {}
    failures = 0
    face_bbox = None

    # ------------------------------------------------------------------ лицо
    _line("КАНАЛ: лицо, поза головы, взгляд (MediaPipe FaceMesh)")
    try:
        from detectors.face_mesh import FaceAnalyzer

        fa = FaceAnalyzer(cfg_dict)
        if not fa.available():
            print("  недоступен — mediapipe не установлен. Канал корректно отключён.")
        else:
            t0 = time.perf_counter()
            obs = fa.analyze(frame)
            ms = (time.perf_counter() - t0) * 1000
            print(f"  время обработки: {ms:.1f} мс")
            print(f"  лиц в кадре: {getattr(obs, 'face_count', '?')}")
            if getattr(obs, "face_count", 0):
                face_bbox = getattr(obs, "face_bbox", None)
                print(f"  поза головы:  yaw {getattr(obs, 'yaw', 0):+.1f}°  "
                      f"pitch {getattr(obs, 'pitch', 0):+.1f}°  roll {getattr(obs, 'roll', 0):+.1f}°")
                print(f"  взгляд:       yaw {getattr(obs, 'gaze_yaw', 0):+.3f}  "
                      f"pitch {getattr(obs, 'gaze_pitch', 0):+.3f}  зона: {getattr(obs, 'gaze_zone', '?')}")
                print(f"  глаза (EAR):  {getattr(obs, 'eye_aspect_ratio', 0):.3f}   "
                      f"рот открыт:   {getattr(obs, 'mouth_open_ratio', 0):.3f}")
                print(f"  bbox лица:    {face_bbox}")
                print("\n  Как читать: yaw/pitch — ПОВОРОТ ГОЛОВЫ в градусах; gaze_* — смещение")
                print("  радужки отдельно от головы, в долях ширины глаза. Зона считается")
                print("  относительно калибровки, без неё она ориентировочная.")
            else:
                print("  лицо не найдено (для синтетического кадра это ожидаемо)")
    except Exception as e:
        print(f"  ОШИБКА: {type(e).__name__}: {e}")
        failures += 1

    # --------------------------------------------------------------- объекты
    _line("КАНАЛ: телефон и посторонние предметы (YOLO)")
    try:
        from detectors.objects import ObjectDetector

        od = ObjectDetector(cfg_dict)
        if not od.available():
            print("  недоступен — нет models/yolov8n.onnx и не установлен ultralytics.")
            print("  Канал корректно отключён. Модель готовится: bash scripts/fetch_models.sh")
        else:
            t0 = time.perf_counter()
            dets = od.detect(frame)
            ms = (time.perf_counter() - t0) * 1000
            print(f"  время обработки: {ms:.1f} мс   найдено объектов: {len(dets)}")
            for d in dets:
                label = d.get("label") if isinstance(d, dict) else getattr(d, "label", "?")
                conf = d.get("conf") if isinstance(d, dict) else getattr(d, "conf", 0)
                bbox = d.get("bbox") if isinstance(d, dict) else getattr(d, "bbox", None)
                print(f"    {label:12} conf {conf:.2f}  bbox {bbox}")
            if not dets:
                print("    объектов из списка интереса в кадре нет")
    except Exception as e:
        print(f"  ОШИБКА: {type(e).__name__}: {e}")
        failures += 1

    # -------------------------------------------------------------- личность
    _line("КАНАЛ: верификация личности (эмбеддинг лица)")
    try:
        from detectors.identity import IdentityVerifier

        iv = IdentityVerifier(cfg_dict)
        if not iv.available():
            print("  недоступен — insightface не установлен. Канал корректно отключён.")
        elif face_bbox is None:
            print("  пропущен: в кадре нет лица для эталона")
        else:
            ok = iv.enroll(frame, face_bbox)
            print(f"  эталон снят: {ok}")
            if ok:
                res = iv.verify(frame, face_bbox)
                sim = getattr(res, "similarity", None)
                print(f"  сверка того же кадра с эталоном: similarity {sim}")
                print("  (на одном и том же кадре близость обязана быть около 1.0 —")
                print("   если нет, сломана нормализация эмбеддинга)")
    except Exception as e:
        print(f"  ОШИБКА: {type(e).__name__}: {e}")
        failures += 1

    # ----------------------------------------------------------------- вывод
    if args.save:
        out = frame.copy()
        if face_bbox:
            x, y, bw, bh = [int(v) for v in face_bbox]
            cv2.rectangle(out, (x, y), (x + bw, y + bh), (0, 255, 198), 2)
        cv2.imwrite(args.save, out)
        print(f"\nКадр с разметкой сохранён: {args.save}")

    _line("ИТОГ")
    if failures:
        print(f"  каналов с ошибкой: {failures}")
    else:
        print("  ошибок нет: доступные каналы отработали, недоступные честно отключены")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
