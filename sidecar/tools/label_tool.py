#!/usr/bin/env python3
"""
Минимальный CLI для сбора собственного датасета под дообучение детектора.

Зачем: COCO-класс "cell phone" ловит телефон как объект, но не отличает
«телефон лежит на столе» от «телефон поднят и наведён на экран». Чтобы дообучить
классификатор/детектор на нашем кейсе, нужно быстро набрать свои кадры —
этот скрипт позволяет команде собрать 300 кадров за 20 минут.

Кадры пишутся в dataset/<class>/<class>_<YYYYmmdd-HHMMSS>_<nnnn>.jpg,
рядом ведётся dataset/manifest.csv (путь, класс, время, размер) — его удобно
скормить скрипту обучения.

Запуск:
    python3 sidecar/tools/label_tool.py
    python3 sidecar/tools/label_tool.py --classes phone_aimed,phone_raised,phone_table,clean
    python3 sidecar/tools/label_tool.py --interval 0.4 --target 300        # автосъёмка
    python3 sidecar/tools/label_tool.py --headless --interval 0.5 --class phone_aimed

Управление в окне просмотра (окно cv2, подписи латиницей — cv2 не умеет кириллицу):
    1..9    выбрать класс
    ПРОБЕЛ  снять один кадр
    a       включить/выключить автосъёмку по таймеру (--interval)
    u       удалить последний снятый кадр
    m       зеркалить превью (съёмка идёт в исходной ориентации)
    q / ESC выход

Без GUI (ssh, headless) запускайте с --headless: тогда идёт только автосъёмка
по таймеру с обратным отсчётом в консоли.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Классы по умолчанию — ровно те, что нужны эвристике «телефон наведён на экран».
DEFAULT_CLASSES = ["phone_aimed", "phone_raised", "phone_table", "clean"]

#: Пояснения к классам (печатаются в консоли при старте).
CLASS_HINTS = {
    "phone_aimed": "телефон поднят вертикально и наведён камерой на экран",
    "phone_raised": "телефон поднят к лицу, но не нацелен на экран (смотрит в него)",
    "phone_table": "телефон лежит на столе / в руке внизу кадра",
    "clean": "чистый кадр: студент работает, телефона нет",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="label_tool",
        description="Сбор кадров с вебкамеры в dataset/<class>/ для дообучения детектора.",
    )
    parser.add_argument("--classes", default=",".join(DEFAULT_CLASSES),
                        help="список классов через запятую (по умолчанию: %(default)s)")
    parser.add_argument("--class", dest="start_class", default=None,
                        help="класс, активный при старте (по умолчанию первый из списка)")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "dataset"),
                        help="каталог датасета (по умолчанию: <корень>/dataset)")
    parser.add_argument("--camera", type=int, default=0, help="индекс камеры (по умолчанию 0)")
    parser.add_argument("--width", type=int, default=1280, help="запрашиваемая ширина кадра")
    parser.add_argument("--height", type=int, default=720, help="запрашиваемая высота кадра")
    parser.add_argument("--interval", type=float, default=0.5,
                        help="период автосъёмки в секундах (по умолчанию 0.5)")
    parser.add_argument("--target", type=int, default=0,
                        help="остановить автосъёмку, когда в активном классе столько кадров (0 = без лимита)")
    parser.add_argument("--quality", type=int, default=92, help="качество JPEG, 1..100")
    parser.add_argument("--autostart", action="store_true",
                        help="включить автосъёмку сразу при запуске")
    parser.add_argument("--headless", action="store_true",
                        help="без окна просмотра: только автосъёмка по таймеру")
    parser.add_argument("--warmup", type=float, default=1.0,
                        help="сколько секунд прогревать камеру перед съёмкой")
    return parser.parse_args(argv)


class DatasetWriter:
    """Пишет кадры по классам и ведёт manifest.csv."""

    def __init__(self, root: Path, classes: list[str], quality: int) -> None:
        self.root = root
        self.classes = classes
        self.quality = max(1, min(100, int(quality)))
        self.counts: dict[str, int] = {}
        self.session_tag = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.history: list[Path] = []
        self.manifest = root / "manifest.csv"

        root.mkdir(parents=True, exist_ok=True)
        for name in classes:
            (root / name).mkdir(parents=True, exist_ok=True)
            # Продолжаем нумерацию, если в каталоге уже есть кадры прошлых прогонов.
            self.counts[name] = len(list((root / name).glob("*.jpg")))

        if not self.manifest.exists():
            with self.manifest.open("w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(["path", "class", "ts", "width", "height"])

    def save(self, cv2, frame, cls: str) -> Path | None:
        idx = self.counts.get(cls, 0) + 1
        path = self.root / cls / f"{cls}_{self.session_tag}_{idx:04d}.jpg"
        try:
            ok = cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
        except Exception as exc:
            print(f"не удалось записать {path}: {exc}", file=sys.stderr)
            return None
        if not ok:
            print(f"не удалось записать {path}", file=sys.stderr)
            return None
        self.counts[cls] = idx
        self.history.append(path)
        try:
            with self.manifest.open("a", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow([
                    str(path.relative_to(self.root)), cls, f"{time.time():.3f}",
                    int(frame.shape[1]), int(frame.shape[0]),
                ])
        except Exception as exc:
            print(f"кадр записан, но manifest.csv не обновлён: {exc}", file=sys.stderr)
        return path

    def undo(self) -> Path | None:
        """Удалить последний снятый кадр (строка в manifest.csv остаётся помеченной)."""
        while self.history:
            path = self.history.pop()
            try:
                if path.exists():
                    path.unlink()
                cls = path.parent.name
                self.counts[cls] = max(0, self.counts.get(cls, 1) - 1)
                return path
            except Exception as exc:
                print(f"не удалось удалить {path}: {exc}", file=sys.stderr)
        return None

    def total(self) -> int:
        return sum(self.counts.values())

    def summary(self) -> str:
        return ", ".join(f"{name}={self.counts.get(name, 0)}" for name in self.classes)


def open_camera(cv2, index: int, width: int, height: int):
    """Открыть камеру. На macOS пробуем AVFoundation, затем дефолтный бэкенд."""
    attempts = []
    if sys.platform == "darwin" and hasattr(cv2, "CAP_AVFOUNDATION"):
        attempts.append(cv2.CAP_AVFOUNDATION)
    if sys.platform == "win32" and hasattr(cv2, "CAP_DSHOW"):
        attempts.append(cv2.CAP_DSHOW)
    attempts.append(None)

    for backend in attempts:
        try:
            cap = cv2.VideoCapture(index) if backend is None else cv2.VideoCapture(index, backend)
        except Exception:
            continue
        if cap is not None and cap.isOpened():
            try:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(width))
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
            except Exception:
                pass
            return cap
        if cap is not None:
            cap.release()
    return None


def draw_hud(cv2, frame, cls: str, classes: list[str], writer: DatasetWriter,
             auto: bool, interval: float, fps: float, flash: float) -> None:
    """HUD поверх превью. Только ASCII: cv2.putText не рисует кириллицу."""
    height, width = frame.shape[:2]
    bar = 86
    try:
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, 0), (width, bar), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.55, frame, 0.45, 0, frame)
    except Exception:
        pass

    font = cv2.FONT_HERSHEY_SIMPLEX
    cv2.putText(frame, f"CLASS: {cls}  [{writer.counts.get(cls, 0)}]", (12, 28),
                font, 0.8, (0, 255, 120), 2, cv2.LINE_AA)
    mode = f"AUTO {interval:.2f}s" if auto else "MANUAL (SPACE)"
    cv2.putText(frame, f"{mode}   total={writer.total()}   fps={fps:.1f}", (12, 56),
                font, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
    keys = "  ".join(f"{i + 1}:{name}" for i, name in enumerate(classes[:9]))
    cv2.putText(frame, keys, (12, 78), font, 0.5, (200, 200, 200), 1, cv2.LINE_AA)
    cv2.putText(frame, "a:auto  u:undo  m:mirror  q:quit", (12, height - 14),
                font, 0.5, (200, 200, 200), 1, cv2.LINE_AA)
    if flash > 0:
        cv2.rectangle(frame, (2, 2), (width - 3, height - 3), (0, 0, 255), 4)


def run_gui(cv2, cap, args: argparse.Namespace, classes: list[str],
            writer: DatasetWriter, active: str) -> int:
    window = "label_tool"
    auto = bool(args.autostart)
    mirror = True
    last_shot = 0.0
    flash_until = 0.0
    fps = 0.0
    prev = time.time()

    try:
        cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    except Exception as exc:
        print(f"окно просмотра недоступно ({exc}). Перезапустите с --headless.", file=sys.stderr)
        return 2

    print("Окно открыто. Горячие клавиши — в заголовке окна и в докстринге скрипта.")
    while True:
        ok, frame = cap.read()
        if not ok or frame is None:
            print("камера не отдала кадр, повтор...", file=sys.stderr)
            time.sleep(0.05)
            continue

        now = time.time()
        dt = now - prev
        prev = now
        if dt > 0:
            fps = 0.9 * fps + 0.1 * (1.0 / dt) if fps else 1.0 / dt

        shoot = False
        if auto and args.interval > 0 and (now - last_shot) >= args.interval:
            shoot = True

        key = -1
        try:
            preview = frame[:, ::-1].copy() if mirror else frame.copy()
            draw_hud(cv2, preview, active, classes, writer, auto, args.interval, fps,
                     max(0.0, flash_until - now))
            cv2.imshow(window, preview)
            key = cv2.waitKey(1) & 0xFF
        except Exception as exc:
            print(f"ошибка отрисовки ({exc}). Перезапустите с --headless.", file=sys.stderr)
            return 2

        if key in (ord("q"), 27):
            break
        if key == ord(" "):
            shoot = True
        elif key == ord("a"):
            auto = not auto
            last_shot = now
            print(f"автосъёмка: {'включена' if auto else 'выключена'}")
        elif key == ord("m"):
            mirror = not mirror
        elif key == ord("u"):
            removed = writer.undo()
            print(f"удалён кадр: {removed}" if removed else "удалять нечего")
        elif ord("1") <= key <= ord("9"):
            idx = key - ord("1")
            if idx < len(classes):
                active = classes[idx]
                last_shot = now
                print(f"активный класс: {active} ({CLASS_HINTS.get(active, 'свой класс')})")

        if shoot:
            path = writer.save(cv2, frame, active)
            last_shot = now
            if path is not None:
                flash_until = now + 0.12
                print(f"[{writer.counts[active]:4d}] {active}: {path.name}")
            if args.target and writer.counts.get(active, 0) >= args.target:
                auto = False
                print(f"цель достигнута: {args.target} кадров в классе {active}. "
                      f"Переключите класс цифрой или выходите (q).")

    try:
        cv2.destroyWindow(window)
    except Exception:
        pass
    return 0


def run_headless(cv2, cap, args: argparse.Namespace, writer: DatasetWriter, active: str) -> int:
    interval = args.interval if args.interval > 0 else 0.5
    target = args.target if args.target > 0 else 100
    print(f"Режим без окна: снимаю класс '{active}' каждые {interval:.2f} с, "
          f"цель {target} кадров. Ctrl+C — остановить.")
    taken = 0
    last = 0.0
    try:
        while taken < target:
            ok, frame = cap.read()
            if not ok or frame is None:
                time.sleep(0.05)
                continue
            now = time.time()
            if (now - last) < interval:
                time.sleep(min(0.02, interval))
                continue
            last = now
            path = writer.save(cv2, frame, active)
            if path is not None:
                taken += 1
                print(f"[{taken:4d}/{target}] {path.name}")
    except KeyboardInterrupt:
        print("\nостановлено пользователем")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    classes = [c.strip() for c in str(args.classes).split(",") if c.strip()]
    if not classes:
        print("ОШИБКА: список классов пуст", file=sys.stderr)
        return 2
    active = args.start_class or classes[0]
    if active not in classes:
        classes.insert(0, active)

    try:
        import cv2  # ленивый импорт: без камеры скрипт просто не нужен
    except Exception as exc:
        print(f"ОШИБКА: нужен opencv-python ({exc}).", file=sys.stderr)
        print("Установите: python3 -m pip install opencv-python", file=sys.stderr)
        return 3

    out_root = Path(args.out).expanduser()
    writer = DatasetWriter(out_root, classes, args.quality)

    print("=== Сбор датасета для детектора телефона ===")
    print(f"Каталог: {out_root}")
    print("Классы:")
    for i, name in enumerate(classes, start=1):
        hint = CLASS_HINTS.get(name, "свой класс")
        print(f"  {i}. {name:<14} — {hint} (уже снято: {writer.counts.get(name, 0)})")
    print("Совет: снимайте с разным светом, расстоянием и углом, обеими руками, "
          "в чехле и без — модель должна видеть вариативность.")

    cap = open_camera(cv2, args.camera, args.width, args.height)
    if cap is None:
        print(f"ОШИБКА: не удалось открыть камеру {args.camera}. "
              f"Проверьте разрешение для терминала в настройках приватности.", file=sys.stderr)
        return 4

    if args.warmup > 0:
        deadline = time.time() + args.warmup
        while time.time() < deadline:
            cap.read()

    try:
        if args.headless:
            code = run_headless(cv2, cap, args, writer, active)
        else:
            code = run_gui(cv2, cap, args, classes, writer, active)
    finally:
        try:
            cap.release()
        except Exception:
            pass
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass

    print(f"Итого кадров: {writer.total()} ({writer.summary()})")
    print(f"Манифест: {writer.manifest}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
