#!/usr/bin/env python3
"""
Калибровка взгляда сквозь настоящий код сайдкара — без камеры и без оболочки.

Зачем этот файл существует: до 08.10 карта экрана (9 точек) не построилась НИ В
ОДНОМ живом прогоне. Сайдкар слал done:true после каждой точки сетки, оболочка
читала это как итог этапа и уходила со сетки после первой точки; в
calibration.json оставался только центр, взгляд оценивался по порогам. Ревью
нашло ещё несколько способов тихо получить кривую карту — каждый закрыт
проверкой ниже.

Стенд берёт `main.ProctorSidecar`, `engine.calibration.GazeCalibration` и
`detectors.face_mesh.FaceAnalyzer._classify_zone` как есть, подменяет только
камеру и сокет. Синтетический студент:
  * амплитуда взгляда как в живом прогоне 08.10 (горизонталь ±0.12 на краях
    экрана, вертикаль ±0.06), шум как у живого центра;
  * реакция и саккада ~0.3 с с учётом конвейера камеры;
  * сглаживание взгляда как в детекторе (медиана 5 + EMA 0.25), сырое значение
    кадра лежит в `raw` — калибровка обязана брать его;
  * моргания.
Оболочка ведёт этапы по правилам shell/renderer/calibration.js: центр — до done
или 7 с; точка сетки — до point_done, но 0.9–3 с (молчание сайдкара — 1.5 с);
после обхода calibrate {gaze_grid, point:null} и ожидание итога.

Время реальное (паузы на саккаду и таймауты сайдкара считаются по часам),
сценарии идут параллельно: прогон ~30 с.

Запуск: .venv/bin/python scripts/check_calibration.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import statistics
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sidecar"))

import main as sidecar_main  # noqa: E402
from config import ProctorConfig  # noqa: E402
from detectors.face_mesh import FaceAnalyzer, FaceObservation  # noqa: E402
from engine.calibration import GazeCalibration  # noqa: E402

CENTER_MS = 7000
GRID_MS, POINT_MIN_MS, POINT_MAX_MS = 1500, 900, 3000
GRID_RESULT_WAIT = 4.0
SHELL_GRID = [
    [0.06, 0.08], [0.50, 0.08], [0.94, 0.08],
    [0.94, 0.50], [0.50, 0.50], [0.06, 0.50],
    [0.06, 0.92], [0.50, 0.92], [0.94, 0.92],
]
AMP_YAW, AMP_PITCH = 0.12, 0.06


class Student:
    """Куда смотрит студент -> что видит детектор (взгляд в глазнице)."""

    def __init__(self, seed: int, noise: tuple[float, float], ear: float = 0.38,
                 amp: tuple[float, float] = (AMP_YAW, AMP_PITCH), reaction: float = 0.20) -> None:
        self.reaction = reaction
        self.rng = random.Random(seed)
        self.prev = self.target = (0.5, 0.5)
        self.switched = time.time()
        self.noise = noise
        self.ear = ear
        self.amp = amp
        self._hist: list[tuple[float, float]] = []
        self._ema: tuple[float, float] | None = None

    def look(self, x: float, y: float) -> None:
        self.prev, self.target, self.switched = self.point(), (x, y), time.time()

    def point(self) -> tuple[float, float]:
        dt = time.time() - self.switched - 0.10        # конвейер камеры
        if dt < self.reaction:                         # реакция
            return self.prev
        if dt < self.reaction + 0.06:                  # саккада
            a = (dt - self.reaction) / 0.06
            return (self.prev[0] + a * (self.target[0] - self.prev[0]),
                    self.prev[1] + a * (self.target[1] - self.prev[1]))
        return self.target

    def gaze_of(self, x: float, y: float) -> tuple[float, float]:
        u, v = x - 0.5, y - 0.5        # слегка нелинейно, как радужка у края глазницы
        return (-0.007 + 2 * self.amp[0] * u + 0.3 * self.amp[0] * u * abs(u),
                0.266 - 2 * self.amp[1] * v - 0.2 * self.amp[1] * v * v)

    def _smooth(self, g: tuple[float, float]) -> tuple[float, float]:
        """Как detectors/face_mesh.py: медиана 5 кадров, затем EMA 0.25."""
        self._hist = (self._hist + [g])[-5:]
        med = (statistics.median(h[0] for h in self._hist),
               statistics.median(h[1] for h in self._hist))
        self._ema = med if self._ema is None else (
            self._ema[0] + 0.25 * (med[0] - self._ema[0]),
            self._ema[1] + 0.25 * (med[1] - self._ema[1]))
        return self._ema

    def observe(self) -> dict:
        g_yaw, g_pitch = self.gaze_of(*self.point())
        raw = (g_yaw + self.rng.gauss(0, self.noise[0]), g_pitch + self.rng.gauss(0, self.noise[1]))
        sm = self._smooth(raw)
        blink = self.rng.random() < 0.04
        return {
            "face_count": 1, "gaze_ok": not blink or self.rng.random() < 0.5, "pose_ok": True,
            "gaze_yaw": sm[0], "gaze_pitch": sm[1],
            "raw": {"gaze_yaw": raw[0], "gaze_pitch": raw[1]},
            "yaw": self.rng.gauss(0, 0.6), "pitch": -6.0 + self.rng.gauss(0, 0.3),
            "roll": -0.5, "eye_aspect_ratio": self.ear * (0.3 if blink else 1.0),
            "mouth_open_ratio": 0.002, "eye_visibility": (0.9, 0.9),
        }


class Detector:
    """Вместо FaceAnalyzer: запоминает, что ему отдали."""

    def __init__(self) -> None:
        self.applied: dict | None = None
        self.cleared = 0

    def available(self) -> bool:
        return True

    def apply_calibration(self, data: dict) -> bool:
        self.applied = data
        return True

    def clear_calibration(self) -> None:
        self.applied = None
        self.cleared += 1


def zone_of(calib: dict | None, g_yaw: float, g_pitch: float,
            head_yaw: float = 0.0, head_pitch: float = -6.0) -> str:
    """Решение НАСТОЯЩЕГО детектора по калибровке: зона[:направление]."""
    fa = FaceAnalyzer.__new__(FaceAnalyzer)
    fa._cfg = {}
    fa._calib = calib or {}
    obs = FaceObservation()
    obs.gaze_ok = obs.pose_ok = True
    obs.gaze_yaw, obs.gaze_pitch, obs.yaw, obs.pitch = g_yaw, g_pitch, head_yaw, head_pitch
    fa._classify_zone(obs)
    return obs.gaze_zone + (f":{obs.gaze_off_direction}" if obs.gaze_off_direction else "")


class Bench:
    """Один процесс сайдкара; сессий на нём может быть несколько."""

    def __init__(self) -> None:
        self.sc = sidecar_main.ProctorSidecar(ProctorConfig())
        self.sc.face = Detector()
        self.sc.gaze_calib = GazeCalibration(self.sc.cfg_dict, tempfile.mkdtemp())
        self.sc._cv_thread = threading.current_thread()   # «камера есть»
        self.msgs: list[dict] = []
        self.events: list = []

    async def start(self) -> None:
        self.sc.loop = asyncio.get_running_loop()

        async def broadcast(msg: dict) -> None:
            if msg.get("type") == "calibration":
                self.msgs.append(msg)

        async def emit(evs) -> None:
            self.events.extend(evs or [])

        self.sc._broadcast = broadcast
        self.sc._emit = emit
        self.consumer = asyncio.create_task(self.sc._consume_cv())

    def _last(self, stage: str, since: int, pred) -> dict | None:
        for m in self.msgs[since:]:
            if m.get("stage") == stage and pred(m):
                return m
        return None

    async def calibrate(self, student: Student, fps: float) -> dict:
        sc = self.sc
        stop = threading.Event()
        rng = random.Random(int(fps * 100))

        def camera() -> None:
            while not stop.is_set():
                sc._handle_calibration(None, student.observe(), None, time.time())
                time.sleep(rng.uniform(0.85, 1.15) / fps)

        th = threading.Thread(target=camera, daemon=True)
        th.start()
        mark = len(self.msgs)
        events_mark = len(self.events)

        student.look(0.5, 0.5)
        await sc._cmd_calibrate({"stage": "gaze_center", "point": [0.5, 0.5]})
        t0 = time.time()
        while time.time() - t0 < CENTER_MS / 1000 and \
                self._last("gaze_center", mark, lambda m: m.get("done")) is None:
            await asyncio.sleep(0.016)
        await asyncio.sleep(0.42)

        holds = []
        for p in SHELL_GRID:
            since = len(self.msgs)
            student.look(*p)
            await sc._cmd_calibrate({"stage": "gaze_grid", "point": p})
            t1 = time.time()
            while True:
                t = (time.time() - t1) * 1000
                mine = [m for m in self.msgs[since:] if m.get("stage") == "gaze_grid"
                        and (m.get("result") or {}).get("point") == p
                        and not (m.get("result") or {}).get("degraded")]
                done = any((m.get("result") or {}).get("point_done") for m in mine)
                if t >= POINT_MAX_MS or (done and t >= POINT_MIN_MS) or (not mine and t >= GRID_MS):
                    break
                await asyncio.sleep(0.016)
            holds.append(time.time() - t1)
        since = len(self.msgs)
        await sc._cmd_calibrate({"stage": "gaze_grid", "point": None})
        t1 = time.time()
        while time.time() - t1 < GRID_RESULT_WAIT and \
                self._last("gaze_grid", since, lambda m: m.get("done")) is None:
            await asyncio.sleep(0.016)

        stop.set()
        th.join(timeout=1)
        await asyncio.sleep(0.05)
        final = self._last("gaze_grid", since, lambda m: m.get("done")) or {}
        gc = sc.gaze_calib
        return {
            "holds": holds,
            "grid_done_msgs": sum(1 for m in self.msgs[mark:]
                                  if m.get("stage") == "gaze_grid" and m.get("done")),
            "final": final.get("result") or {},
            "applied": sc.face.applied,
            "map_kind": ((sc.face.applied or {}).get("screen_map") or {}).get("kind"),
            "quality": gc.quality(),
            "targets": [list(g["point"]) for g in gc._grid if g["samples"]],
            "calib_done_events": sum(1 for e in self.events[events_mark:]
                                     if getattr(getattr(e, "kind", None), "value", None)
                                     == "CALIBRATION_DONE"),
        }


failed = 0


def ok(cond: bool, what: str) -> None:
    global failed
    print(("  ок     " if cond else "  ОШИБКА ") + what)
    if not cond:
        failed += 1


def six_point_map() -> tuple[str | None, float, str]:
    """6 точек из 9 (3 точки без кадров): LOO должен считаться, а не быть 0."""
    rng = random.Random(3)
    st = Student(3, (0.008, 0.015))
    gc = GazeCalibration({}, None)
    gc.begin_center()
    for _ in range(45):
        g = st.gaze_of(0.5, 0.5)
        gc.add_center_sample({"gaze_ok": True, "pose_ok": True, "gaze_yaw": g[0] + rng.gauss(0, 0.008),
                              "gaze_pitch": g[1] + rng.gauss(0, 0.015), "yaw": 0, "pitch": -6,
                              "roll": 0, "eye_aspect_ratio": 0.38})
    gc.finish_center()
    for p in SHELL_GRID[:6]:
        gc.begin_point(p, fresh=True)
        for _ in range(12):
            g = st.gaze_of(*p)
            gc.add_grid_sample({"gaze_ok": True, "pose_ok": True,
                                "gaze_yaw": g[0] + rng.gauss(0, 0.008),
                                "gaze_pitch": g[1] + rng.gauss(0, 0.015),
                                "yaw": 0, "pitch": -6, "roll": 0, "eye_aspect_ratio": 0.38}, p)
    gc.finish_grid()
    q = gc.quality()
    return gc._screen_map.get("kind"), q.get("loo_rmse", 0.0), q.get("map_grade")


def fair_map_edges() -> list[tuple[float, list[str], str]]:
    """Карты с map_grade 'fair' (шум на грани) — и что детектор с ними делает."""
    out = []
    for seed in range(60):
        rng = random.Random(seed)
        st = Student(seed, (0.0, 0.0))
        noise = (0.03, 0.04)
        gc = GazeCalibration({}, None)
        gc.begin_center()
        for _ in range(45):
            g = st.gaze_of(0.5, 0.5)
            gc.add_center_sample({"gaze_ok": True, "pose_ok": True, "yaw": 0, "pitch": -6,
                                  "gaze_yaw": g[0] + rng.gauss(0, 0.008),
                                  "gaze_pitch": g[1] + rng.gauss(0, 0.015),
                                  "roll": 0, "eye_aspect_ratio": 0.38})
        gc.finish_center()
        for p in SHELL_GRID:
            gc.begin_point(p, fresh=True)
            for _ in range(15):
                g = st.gaze_of(*p)
                gc.add_grid_sample({"gaze_ok": True, "pose_ok": True, "yaw": 0, "pitch": -6,
                                    "gaze_yaw": g[0] + rng.gauss(0, noise[0]),
                                    "gaze_pitch": g[1] + rng.gauss(0, noise[1]),
                                    "roll": 0, "eye_aspect_ratio": 0.38}, p)
        gc.finish_grid()
        if gc.quality().get("map_grade") != "fair":
            continue
        det = Detector()
        gc.attach(det)
        edges = [zone_of(det.applied, *st.gaze_of(x, y))
                 for x, y in ((0.97, 0.5), (0.03, 0.5), (0.97, 0.05), (0.03, 0.95))]
        out.append((gc.quality()["loo_rmse"], edges, zone_of(det.applied, *st.gaze_of(1.6, 0.5))))
    return out


async def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    benches = [Bench() for _ in range(5)]
    for b in benches:
        await b.start()
    normal_b, noisy_b, slow_b, crawl_b, fast_b = benches
    normal_st = Student(7, (0.008, 0.015))
    normal, noisy, slow, crawl, fast = await asyncio.gather(
        normal_b.calibrate(normal_st, 15),
        noisy_b.calibrate(Student(8, (0.09, 0.11)), 15),
        slow_b.calibrate(Student(9, (0.008, 0.015)), 8),
        crawl_b.calibrate(Student(10, (0.008, 0.015)), 2),
        fast_b.calibrate(Student(13, (0.008, 0.015), reaction=0.45), 30),
    )

    print("=== обычный: 15 к/с, амплитуда и сглаживание как у живого детектора ===")
    q = normal["quality"]
    ok(normal["grid_done_msgs"] == 1, "done:true по сетке ровно один раз — на конце обхода")
    ok(normal["final"].get("all_points_done") is True, "итог помечен all_points_done")
    ok(sorted(map(tuple, normal["targets"])) == sorted(map(tuple, SHELL_GRID)),
       "цели регрессии — ровно точки оболочки")
    ok(max(normal["holds"]) < POINT_MAX_MS / 1000 - 0.2, "точку отпускает point_done, а не потолок: "
       f"{min(normal['holds']):.2f}–{max(normal['holds']):.2f} с")
    ok(normal["map_kind"] == "poly2" and q.get("map_grade") in ("good", "fair"),
       f"карта применена: {normal['map_kind']}, map_grade {q.get('map_grade')}, LOO {q.get('loo_rmse')}")
    ok(normal["calib_done_events"] == 2, "в журнале 2 CALIBRATION_DONE (центр и карта), а не по точке")
    cal = normal["applied"]
    st = normal_st
    checks = [
        ("правый край своего экрана", (0.97, 0.5), "center"),
        ("левый верхний угол", (0.03, 0.05), "center"),
        ("второй монитор справа", (1.4, 0.5), "off_screen:right"),
        ("телефон слева от ноутбука", (-0.4, 0.6), "off_screen:left"),
        ("клавиатура под экраном", (0.5, 1.3), "center"),
    ]
    for what, (x, y), want in checks:
        got = zone_of(cal, *st.gaze_of(x, y))
        ok(got == want, f"{what} -> {got}")
    got = zone_of(cal, *st.gaze_of(1.4, 0.5), head_yaw=12.0)
    ok(got in ("right", "off_screen:right"),
       f"голова на 12° (вне позы обучения карты) и взгляд вправо -> {got}: решают пороги")
    ok(zone_of(cal, *st.gaze_of(0.5, 0.5), head_yaw=12.0) != "off_screen:right",
       "при повороте головы карта не применяется к взгляду в центр")

    print("=== шумный: плохая карта не должна доходить до детектора ===")
    nq = noisy["quality"]
    ok(nq.get("map_grade") == "poor" and noisy["map_kind"] is None,
       f"map_grade {nq.get('map_grade')} (LOO {nq.get('loo_rmse')}), детектор без карты")
    ok(noisy["final"].get("screen_map_applied") is False, "оболочке сказано: карта не применена")

    print("=== «приемлемые» карты: запас границы растёт с ошибкой карты ===")
    fair_maps = fair_map_edges()
    ok(len(fair_maps) >= 5, f"найдено «приемлемых» карт: {len(fair_maps)}")
    false_edges = sum(z != "center" for _, edges, _ in fair_maps for z in edges)
    ok(false_edges == 0, f"края и углы своего экрана -> мимо экрана: {false_edges} из "
       f"{4 * len(fair_maps)}")
    caught = sum(far.startswith("off_screen") or far == "right" for _, _, far in fair_maps)
    ok(caught == len(fair_maps), f"второй монитор справа (x=1.6) пойман: {caught} из {len(fair_maps)}")

    print("=== медленная камера: 8 к/с ===")
    ok(max(slow["holds"]) > 1.6, f"оболочка ждёт кадры дольше 1.5 с: до {max(slow['holds']):.2f} с")
    ok(slow["map_kind"] == "poly2", f"карта всё равно построена ({slow['quality'].get('map_grade')}, "
       f"LOO {slow['quality'].get('loo_rmse')})")

    print("=== быстрая камера, медленные глаза: 30 к/с, реакция 0.45 с ===")
    ok(min(fast["holds"]) >= 0.95, f"точка держится не меньше 0.95 с: от {min(fast['holds']):.2f} с")
    ok(fast["map_kind"] == "poly2" and fast["quality"].get("map_grade") in ("good", "fair"),
       f"карта годная: {fast['quality'].get('map_grade')}, LOO {fast['quality'].get('loo_rmse')}")

    print("=== почти нет кадров: 2 к/с ===")
    ok(crawl["grid_done_msgs"] == 1, "итог сетки всё равно пришёл")
    ok(crawl["map_kind"] is None and crawl["final"].get("screen_map_applied") is False,
       "карты нет, и это сказано")
    ok("не хватило кадров" in str(crawl["quality"].get("message")),
       f"причина названа: {crawl['quality'].get('message')}")

    print("=== вторая сессия в том же процессе сайдкара ===")
    sc = normal_b.sc
    old_map = (normal["applied"] or {}).get("screen_map", {}).get("coef_x")
    sc._reset_calibration()
    ok(sc.face.applied is None, "новая сессия сняла калибровку прошлого студента с детектора")
    second = await normal_b.calibrate(Student(11, (0.008, 0.015), ear=0.22, amp=(0.10, 0.05)), 15)
    ok(sorted(map(tuple, second["targets"])) == sorted(map(tuple, SHELL_GRID)),
       "цели второй сессии — снова точки оболочки, а не DEFAULT_GRID")
    ok(second["map_kind"] == "poly2" and
       (second["applied"] or {}).get("screen_map", {}).get("coef_x") != old_map,
       "у второго студента своя карта")
    ok(second["quality"].get("center_samples", 0) >= 15,
       f"центр второго студента (другой EAR) набрал кадры: {second['quality'].get('center_samples')}")

    print("=== тишина взгляда ограничена бюджетом сессии ===")
    sc = crawl_b.sc
    sc._reset_calibration()
    budget = float(sc.cfg.calibration_gaze_quiet_budget)
    t0 = 1_000_000.0
    for i in range(600):                    # «calibrate» раз в 2 с двадцать минут подряд
        sc._gaze_quiet(t0 + 2.0 * i)
    ok(sc._gaze_quiet_spent <= budget + 1e-6 and sc._gaze_quiet_until <= t0 + budget + 2.0,
       f"тишина за сессию {sc._gaze_quiet_spent:.0f} с из бюджета {budget:.0f} с, "
       f"дальше взгляд оценивается")

    print("=== 6 точек из 9 ===")
    kind, loo, grade = six_point_map()
    ok(kind == "affine" and loo > 0.0, f"6 точек -> {kind}, LOO {loo:.4f} посчитан, map_grade {grade}")

    for b in benches:
        b.consumer.cancel()
    print(f"\nПровалено проверок: {failed}" if failed else "\nВсе проверки пройдены")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
