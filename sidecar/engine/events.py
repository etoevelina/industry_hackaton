"""
Движок событий: превращает шумный поток покадровых наблюдений в редкие
обоснованные инциденты.

Зачем нужен отдельный слой. Детекторы отдают наблюдение на КАЖДОМ кадре: при
15 fps взгляд, ушедший вниз на четыре секунды, — это шестьдесят «нарушений».
Отчёт из такого потока нечитаем, а HUD мигает. Поэтому между наблюдениями и
`ProctorEvent` стоят три механизма:

1. **Окно подтверждения** (`confirm_sec`). Инцидент рождается только если
   условие держалось непрерывно не меньше своего окна. Мгновенный промах
   детектора (блик, поворот головы на секунду) событием не становится.

2. **Гистерезис** (`release_sec != confirm_sec`). Условие считается снятым
   только после `release_sec` непрерывного отсутствия. Без этого на границе
   порога (взгляд дрожит вокруг 20°) наблюдение щёлкает active/inactive каждые
   два кадра, окно подтверждения обнуляется, и либо не рождается ничего, либо
   рождается очередь дублей. Release специально короче confirm: отпускать надо
   быстрее, чем подтверждать, иначе система залипнет на уже снятом нарушении.

3. **Cooldown на тип** (`cooldown_sec`). Повторный инцидент того же вида не
   выпускается раньше cooldown. При этом ПРОДОЛЖАЮЩЕЕСЯ условие не плодит
   события вообще: пока оно держится, движок обновляет `duration` и `message`
   уже выпущенного инцидента (объект тот же, его видят и хранилище, и fusion).
   Новый инцидент того же вида возможен только после honest-release и только
   если с предыдущего прошло не меньше cooldown.

Составное правило `speech_without_lip_motion` собирается из двух независимых
каналов: `audio.speech` (VAD) и `face.mouth_open_ratio` (FaceMesh). Речь при
закрытом рте и при наличии лица в кадре — это подсказка через наушник; условие
проходит обычный конвейер подтверждения с confirm 1.5 с.

Конфиг правил — модульный словарь `RULES`, его можно крутить без правки логики.
Значения из `sidecar/config.py` (`confirm_windows`, `cooldowns`,
`release_window`) переопределяют табличные, если заданы.

Приватность: движок не хранит кадры и ничего не пишет на диск, только числа из
`detail`.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

_SIDECAR = Path(__file__).resolve().parent.parent
if str(_SIDECAR) not in sys.path:
    sys.path.insert(0, str(_SIDECAR))

from protocol import (  # noqa: E402  (путь настраиваем выше)
    RISK_WEIGHTS,
    Channel,
    EventKind,
    ProctorEvent,
    Severity,
)

#: Защита от сравнения float'ов «на грани»: 1.7999999 >= 1.8 должно быть True.
_EPS = 1e-6


def severity_for_weight(weight: float) -> Severity:
    """Severity по весу события из RISK_WEIGHTS — таблица не дублируется."""
    if weight >= 55:
        return Severity.CRITICAL
    if weight >= 35:
        return Severity.HIGH
    if weight >= 20:
        return Severity.MEDIUM
    if weight >= 8:
        return Severity.LOW
    return Severity.INFO


@dataclass(frozen=True)
class Rule:
    """Правило подтверждения для одного наблюдаемого условия.

    confirm_sec   — сколько условие должно держаться, чтобы стать инцидентом;
    release_sec   — сколько оно должно отсутствовать, чтобы считаться снятым;
    cooldown_sec  — минимальная пауза между повторными инцидентами этого вида;
    message_template — русский текст для отчёта, подстановки см. _message_vars().
    """
    kind: EventKind
    severity: Severity
    channel: Channel
    confirm_sec: float
    release_sec: float
    cooldown_sec: float
    message_template: str


def _rule(kind: EventKind, channel: Channel, confirm: float, release: float,
          cooldown: float, template: str, severity: Severity | None = None) -> Rule:
    return Rule(
        kind=kind,
        severity=severity or severity_for_weight(RISK_WEIGHTS.get(kind, 10.0)),
        channel=channel,
        confirm_sec=confirm,
        release_sec=release,
        cooldown_sec=cooldown,
        message_template=template,
    )


# ===========================================================================
# Реестр правил. Ключ — имя наблюдения в нижнем регистре; EventKind.value
# нормализуется к нему же, поэтому push_observation принимает и 'GAZE_DOWN',
# и 'gaze_down'.
# ===========================================================================
RULES: dict[str, Rule] = {
    # --- объекты в кадре -------------------------------------------------
    "phone_in_frame": _rule(
        EventKind.PHONE_IN_FRAME, Channel.VISION, 0.8, 0.6, 12.0,
        "Телефон в кадре{held_na}{conf_area}"),
    "phone_raised": _rule(
        EventKind.PHONE_RAISED, Channel.VISION, 0.6, 0.6, 12.0,
        "Телефон поднят к лицу{held_for}{conf_area}"),
    # Текст перечисляет только то, что действительно проверено эвристикой
    # detectors/objects.py::_eval_aimed (поднят, вертикальный бокс, приближение,
    # выдержка). Направление взгляда она не использует, и утверждать его в
    # доказательном отчёте нельзя — подробности всегда лежат в detail.explain.
    "phone_aimed_at_screen": _rule(
        EventKind.PHONE_AIMED_AT_SCREEN, Channel.VISION, 0.6, 0.6, 12.0,
        "Телефон направлен на экран{held_na}: поднят, держится вертикально "
        "и приближается к камере{conf_area}"),
    "forbidden_object": _rule(
        EventKind.FORBIDDEN_OBJECT, Channel.VISION, 2.0, 1.5, 30.0,
        "В кадре посторонний предмет: {label_ru}{held_dash}{conf_area}"),

    # --- лицо и присутствие ----------------------------------------------
    "no_face": _rule(
        EventKind.NO_FACE, Channel.VISION, 2.5, 1.0, 15.0,
        "Лицо не видно в кадре{held}"),
    "second_face": _rule(
        EventKind.SECOND_FACE, Channel.VISION, 1.0, 1.5, 20.0,
        "В кадре {faces} лица одновременно{held_dash}"),
    "identity_mismatch": _rule(
        EventKind.IDENTITY_MISMATCH, Channel.IDENTITY, 2.0, 2.0, 20.0,
        "Лицо за компьютером не совпадает с зарегистрированным"
        "{sim_note}{streak_note}{held_par}"),
    "liveness_fail": _rule(
        EventKind.LIVENESS_FAIL, Channel.IDENTITY, 3.0, 2.0, 60.0,
        "Признаки неживого изображения: лицо в кадре, но морганий нет"
        "{no_blink_note} (подозрение на фото или зацикленное видео)"),

    # --- взгляд и голова --------------------------------------------------
    "gaze_down": _rule(
        EventKind.GAZE_DOWN, Channel.GAZE, 1.8, 0.8, 10.0,
        "Взгляд отведён вниз{held_na}{dev_note}"),
    "gaze_side": _rule(
        EventKind.GAZE_SIDE, Channel.GAZE, 1.8, 0.8, 10.0,
        "Взгляд отведён {zone_ru}{held_na}{dev_note}"),
    "gaze_off_screen": _rule(
        EventKind.GAZE_OFF_SCREEN, Channel.GAZE, 2.0, 0.8, 10.0,
        "Взгляд вне экрана{held_na}: точка взгляда не попадает в карту "
        "монитора{dev_note}"),
    "head_turned": _rule(
        EventKind.HEAD_TURNED, Channel.GAZE, 2.0, 1.0, 15.0,
        "Голова отвернута от экрана{held_na}{dev_note}{angles_note}"),

    # --- аудио -------------------------------------------------------------
    "voice_other": _rule(
        EventKind.VOICE_OTHER, Channel.AUDIO, 1.2, 1.5, 15.0,
        "Слышен посторонний голос{held_na}{rms_conf}"),
    "speech_without_lip_motion": _rule(
        EventKind.SPEECH_WITHOUT_LIP_MOTION, Channel.AUDIO, 1.5, 1.0, 20.0,
        "Речь слышна{held_na}, при этом губы не двигаются{mouth_note} — "
        "вероятна подсказка через наушник"),

    # --- окружение ---------------------------------------------------------
    "virtual_camera": _rule(
        EventKind.VIRTUAL_CAMERA, Channel.ENVIRONMENT, 0.0, 1.0, 120.0,
        "Изображение приходит с виртуальной камеры{name_par}"),
    "remote_access_software": _rule(
        EventKind.REMOTE_ACCESS_SOFTWARE, Channel.ENVIRONMENT, 0.0, 1.0, 120.0,
        "Запущено ПО удалённого доступа{name_colon}"),
    "virtual_machine": _rule(
        EventKind.VIRTUAL_MACHINE, Channel.ENVIRONMENT, 0.0, 1.0, 300.0,
        "Экзамен проходит в виртуальной машине{name_par}"),
    "screen_recording": _rule(
        EventKind.SCREEN_RECORDING, Channel.ENVIRONMENT, 0.0, 1.0, 60.0,
        "Идёт запись или трансляция экрана{name_colon}"),
    "multiple_displays": _rule(
        EventKind.MULTIPLE_DISPLAYS, Channel.ENVIRONMENT, 0.0, 1.0, 120.0,
        "Подключено несколько мониторов{displays_note}"),
    "blacklisted_process": _rule(
        EventKind.BLACKLISTED_PROCESS, Channel.ENVIRONMENT, 0.0, 1.0, 60.0,
        "Запущен запрещённый процесс{name_colon}"),

    # --- оболочка ----------------------------------------------------------
    "window_blur": _rule(
        EventKind.WINDOW_BLUR, Channel.SHELL, 0.0, 0.5, 5.0,
        "Окно экзамена потеряло фокус — студент переключился в другое приложение"),
    "fullscreen_exit": _rule(
        EventKind.FULLSCREEN_EXIT, Channel.SHELL, 0.0, 0.5, 5.0,
        "Выход из полноэкранного режима"),
    "shortcut_blocked": _rule(
        EventKind.SHORTCUT_BLOCKED, Channel.SHELL, 0.0, 0.5, 10.0,
        "Заблокировано сочетание клавиш{combo_note}"),
    "clipboard_paste": _rule(
        EventKind.CLIPBOARD_PASTE, Channel.SHELL, 0.0, 0.5, 5.0,
        "Вставка из буфера обмена{length_note}"),
    "devtools_attempt": _rule(
        EventKind.DEVTOOLS_ATTEMPT, Channel.SHELL, 0.0, 0.5, 15.0,
        "Попытка открыть инструменты разработчика"),

    # --- поведение ввода (приходят от fusion как внешние) ------------------
    "paste_burst": _rule(
        EventKind.PASTE_BURST, Channel.FUSION, 0.0, 0.5, 15.0,
        "Вставлен крупный блок текста{length_note}"),
    "typing_anomaly": _rule(
        EventKind.TYPING_ANOMALY, Channel.FUSION, 0.0, 0.5, 30.0,
        "Ритм набора не похож на базовый профиль студента"),

    # --- fusion -------------------------------------------------------------
    "fusion_gaze_then_answer": _rule(
        EventKind.FUSION_GAZE_THEN_ANSWER, Channel.FUSION, 0.0, 0.5, 20.0,
        "Взгляд уходил с экрана, сразу после этого — готовый ответ"),
    "fusion_blur_then_answer": _rule(
        EventKind.FUSION_BLUR_THEN_ANSWER, Channel.FUSION, 0.0, 0.5, 20.0,
        "Переключение из окна экзамена, сразу после возврата — готовый ответ"),
    "fusion_phone_then_answer": _rule(
        EventKind.FUSION_PHONE_THEN_ANSWER, Channel.FUSION, 0.0, 0.5, 20.0,
        "Телефон в кадре, сразу после этого — готовый ответ"),

    # --- служебное ----------------------------------------------------------
    "sensor_lost": _rule(
        EventKind.SENSOR_LOST, Channel.SYSTEM, 3.0, 1.5, 30.0,
        "Пропал источник данных{sensor_note}{held_na}"),
    "session_started": _rule(
        EventKind.SESSION_STARTED, Channel.SYSTEM, 0.0, 0.5, 0.0,
        "Сессия начата"),
    "session_ended": _rule(
        EventKind.SESSION_ENDED, Channel.SYSTEM, 0.0, 0.5, 0.0,
        "Сессия завершена"),
    "calibration_done": _rule(
        EventKind.CALIBRATION_DONE, Channel.SYSTEM, 0.0, 0.5, 0.0,
        "Калибровка завершена{stage_note}"),
}

#: Синонимы имён наблюдений: разные модули называют одно и то же по-разному.
ALIASES: dict[str, str] = {
    "speech_no_lips": "speech_without_lip_motion",
    "speech_without_lips": "speech_without_lip_motion",
    "no_lips": "speech_without_lip_motion",
    "gaze_away": "gaze_side",          # EventKind.GAZE_AWAY не существует
    "gaze_aside": "gaze_side",
    "head_away": "head_turned",
    "face_lost": "no_face",
    "face_absent": "no_face",
    "second_person": "second_face",
    "multiple_faces": "second_face",
    "phone": "phone_in_frame",
    "other_voice": "voice_other",
    "foreign_voice": "voice_other",
    "camera_lost": "sensor_lost",
    "paste": "clipboard_paste",
}

#: Входы составного правила: имя наблюдения -> слот внутреннего состояния.
COMPOSITE_INPUTS: dict[str, str] = {
    "audio.speech": "speech",
    "audio_speech": "speech",
    "face.mouth_open_ratio": "mouth",
    "face_mouth_open_ratio": "mouth",
    "mouth_open_ratio": "mouth",
    "face.mouth": "mouth",
}

#: Во сколько раз превышен порог, если детектор не отдал нормированную девиацию:
#: (ключи detail, из которых берём max|·|; ключ конфига с лимитом; дефолт лимита).
DEV_FALLBACK: dict[str, tuple[tuple[str, ...], str, float]] = {
    "gaze_down": (("gaze_pitch",), "gaze_pitch_down_limit", 18.0),
    "gaze_side": (("gaze_yaw",), "gaze_yaw_limit", 20.0),
    "gaze_off_screen": (("gaze_yaw", "gaze_pitch"), "gaze_off_screen_limit", 32.0),
    "head_turned": (("head_yaw", "head_pitch"), "head_yaw_limit", 25.0),
}

#: Нормированные девиации, если детектор их отдаёт (|dev| > 1 — за персональным порогом).
DEV_KEYS = ("dev", "deviation", "gaze_dev_v", "gaze_dev_h",
            "head_dev_yaw", "head_dev_pitch")

#: Зоны взгляда -> по-русски, для подстановки в message.
ZONE_RU: dict[str, str] = {
    "left": "влево", "right": "вправо", "down": "вниз", "bottom": "вниз",
    "up": "вверх", "top": "вверх", "center": "в центр экрана",
    "side": "в сторону", "off": "мимо экрана", "off_screen": "мимо экрана",
    "away": "мимо экрана", "outside": "мимо экрана", "unknown": "в сторону",
}

#: Метки YOLO -> по-русски.
LABEL_RU: dict[str, str] = {
    "cell phone": "телефон", "mobile phone": "телефон", "phone": "телефон",
    "book": "книга", "laptop": "ноутбук", "tv": "монитор или телевизор",
    "monitor": "второй монитор", "tvmonitor": "монитор", "remote": "пульт",
    "keyboard": "посторонняя клавиатура", "person": "посторонний человек",
}


class _SafeVars(dict):
    """Подстановка без падений: отсутствующий ключ шаблона даёт пустую строку."""

    def __missing__(self, key: str) -> str:  # pragma: no cover - тривиально
        return ""


@dataclass
class _State:
    """Состояние одного наблюдаемого условия."""
    name: str
    since: float | None = None          # когда условие стало активным
    last_active: float = 0.0            # последний активный ts
    last_seen: float = 0.0              # монотонность входного времени
    fired: bool = False                 # инцидент по этому проходу уже выпущен
    last_fire: float = 0.0              # когда выпускали в последний раз
    open_event: ProctorEvent | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    fired_total: int = 0
    peak_duration: float = 0.0
    held_total: float = 0.0             # суммарное время под условием, сек


class EventEngine:
    """Единственное место, где наблюдения превращаются в ProctorEvent.

    Использование:
        eng = EventEngine(config)
        events = eng.push_observation("GAZE_DOWN", True, ts, gaze_pitch=-43.0)
        events = eng.push_external(EventKind.WINDOW_BLUR, {"source": "shell"})

    Все методы возвращают список готовых `ProctorEvent` (обычно пустой).
    Потокобезопасность не обеспечивается специально: вызывающий (main.py)
    держит собственный lock вокруг движка.
    """

    def __init__(self, config: dict | None = None) -> None:
        cfg = config or {}
        self.cfg = cfg
        self._confirm_cfg = dict(cfg.get("confirm_windows") or {})
        self._cooldown_cfg = dict(cfg.get("cooldowns") or {})
        self._release_cfg = float(cfg.get("release_window", 0.0) or 0.0)
        self._identity_threshold = float(cfg.get("identity_threshold", 0.35) or 0.35)
        self._mouth_threshold = float(cfg.get("mouth_open_speech", 0.22) or 0.22)
        self._states: dict[str, _State] = {}
        self._external_fire: dict[str, float] = {}
        #: Срезы закрытых инцидентов — отчёт берёт отсюда итоговые длительности.
        self._closed: list[dict[str, Any]] = []
        #: Имена наблюдений, для которых нет правила — видно в stats(), не падаем.
        self._unknown: dict[str, int] = {}
        # состояние составного правила
        self._speech = False
        self._speech_detail: dict[str, Any] = {}
        self._mouth_ratio = 0.0
        self._face_present = True
        self._fired_total = 0

    # ------------------------------------------------------------------ сброс
    def reset(self) -> None:
        """Полный сброс между сессиями."""
        self._states.clear()
        self._external_fire.clear()
        self._closed.clear()
        self._unknown.clear()
        self._speech = False
        self._speech_detail = {}
        self._mouth_ratio = 0.0
        self._face_present = True
        self._fired_total = 0

    def available(self) -> bool:
        """Движок чистый Python, зависимостей нет — доступен всегда."""
        return True

    # ------------------------------------------------------------- параметры
    @staticmethod
    def normalize(name: Any) -> str:
        """Имя наблюдения -> канонический ключ правила."""
        if isinstance(name, EventKind):
            raw = name.value
        else:
            raw = str(getattr(name, "value", name) or "")
        key = raw.strip().lower().replace(" ", "_").replace("-", "_")
        return ALIASES.get(key, key)

    def rule_for(self, name: Any) -> Rule | None:
        return RULES.get(self.normalize(name))

    def confirm_for(self, name: Any) -> float:
        """Окно подтверждения: конфиг (по EventKind.value) важнее таблицы."""
        key = self.normalize(name)
        rule = RULES.get(key)
        if rule is None:
            return float(self.cfg.get("confirm_window", 1.5) or 1.5)
        value = self._confirm_cfg.get(rule.kind.value, self._confirm_cfg.get(key))
        return float(value) if value is not None else rule.confirm_sec

    def cooldown_for(self, name: Any) -> float:
        key = self.normalize(name)
        rule = RULES.get(key)
        if rule is None:
            return float(self.cfg.get("cooldown_sec", 10.0) or 10.0)
        value = self._cooldown_cfg.get(rule.kind.value, self._cooldown_cfg.get(key))
        return float(value) if value is not None else rule.cooldown_sec

    def release_for(self, name: Any) -> float:
        """Окно отпускания. Конфиг задаёт один общий `release_window`."""
        key = self.normalize(name)
        rule = RULES.get(key)
        if rule is None:
            return self._release_cfg or 1.0
        return self._release_cfg or rule.release_sec

    # --------------------------------------------------------- основной вход
    def push_observation(self, name: str, active: bool, ts: float, /,
                         **detail: Any) -> list[ProctorEvent]:
        """Принять покадровое наблюдение. Вернуть рождённые инциденты (обычно []).

        `name` — имя условия: EventKind.value ('GAZE_DOWN'), каноническое имя
        ('gaze_down'), синоним из ALIASES или вход составного правила
        ('audio.speech', 'face.mouth_open_ratio').

        Первые три параметра — positional-only: detail приходит как **kwargs, и
        ключ с именем `name`/`active`/`ts` иначе ломал бы вызов целиком.
        """
        key = self.normalize(name)
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            ts = time.time()

        # вход составного правила: сам по себе события не даёт
        if key in COMPOSITE_INPUTS:
            return self._note_composite(COMPOSITE_INPUTS[key], bool(active), ts, detail)

        # побочный учёт присутствия лица — нужен составному правилу
        if key == "no_face":
            self._face_present = not bool(active)

        rule = RULES.get(key)
        if rule is None:
            self._unknown[str(name)] = self._unknown.get(str(name), 0) + 1
            return []
        return self._advance(key, rule, bool(active), ts, detail)

    def push_external(self, kind: EventKind, detail: dict | None = None) -> list[ProctorEvent]:
        """Готовое событие от оболочки, env-проверок или fusion.

        Окно подтверждения не применяется (условие уже установлено источником),
        но cooldown по виду нужен: оболочка умеет щёлкать фокусом десять раз в
        секунду, и отчёт не должен состоять из тридцати WINDOW_BLUR.
        """
        detail = dict(detail or {})
        key = self.normalize(kind)
        rule = RULES.get(key)
        real_kind = rule.kind if rule is not None else _as_kind(kind)
        if real_kind is None:
            self._unknown[str(kind)] = self._unknown.get(str(kind), 0) + 1
            return []

        ts = _as_float(detail.get("ts"), time.time())
        cooldown = self.cooldown_for(real_kind)
        last = self._external_fire.get(real_kind.value, 0.0)
        if cooldown > 0 and last and (ts - last) + _EPS < cooldown:
            return []
        self._external_fire[real_kind.value] = ts

        severity = _as_severity(detail.pop("severity", None))
        duration = _as_float(detail.get("duration"), 0.0)
        detail.pop("ts", None)
        event = self._build(rule, real_kind, key, detail, duration, ts,
                            severity=severity, external=True)
        self._fired_total += 1
        return [event]

    # ---------------------------------------------------- составное правило
    def _note_composite(self, slot: str, active: bool, ts: float,
                        detail: dict[str, Any]) -> list[ProctorEvent]:
        """Собрать `speech_without_lip_motion` из речи и раскрытия рта.

        Речь даёт `active`, рот — числовое значение (`value`/`mouth_open_ratio`/
        `ratio`); если числа нет, `active` трактуется как «рот открыт».
        Условие активно, когда есть речь, рот закрыт и лицо в кадре: речь без
        лица — это уже NO_FACE, смешивать не надо.
        """
        if slot == "speech":
            self._speech = bool(active)
            self._speech_detail = {k: v for k, v in detail.items() if k != "ts"}
        else:
            value = detail.get("value", detail.get("mouth_open_ratio", detail.get("ratio")))
            if value is None:
                self._mouth_ratio = 1.0 if active else 0.0
            else:
                self._mouth_ratio = _as_float(value, 0.0)
            if "mouth_open_speech" in detail:
                self._mouth_threshold = _as_float(detail["mouth_open_speech"],
                                                  self._mouth_threshold)

        mouth_closed = self._mouth_ratio < self._mouth_threshold
        condition = bool(self._speech and mouth_closed and self._face_present)
        merged: dict[str, Any] = dict(self._speech_detail)
        merged.update({
            "mouth_open_ratio": round(self._mouth_ratio, 3),
            "mouth_threshold": round(self._mouth_threshold, 3),
            "speech": self._speech,
            "face_present": self._face_present,
        })
        rule = RULES["speech_without_lip_motion"]
        return self._advance("speech_without_lip_motion", rule, condition, ts, merged)

    # -------------------------------------------------- конечный автомат
    def _advance(self, key: str, rule: Rule, active: bool, ts: float,
                 detail: dict[str, Any]) -> list[ProctorEvent]:
        st = self._states.get(key)
        if st is None:
            st = self._states[key] = _State(name=key)
        # время может прийти назад (разные потоки, разные часы) — не даём откатиться
        ts = max(ts, st.last_seen)
        st.last_seen = ts

        if active:
            if detail:
                st.detail.update({k: v for k, v in detail.items() if k != "ts"})
            if st.since is None:
                st.since = ts
            prev_active = st.last_active or ts
            st.held_total += max(0.0, min(ts - prev_active, 1.0))
            st.last_active = ts
            held = ts - st.since
            st.peak_duration = max(st.peak_duration, held)

            if st.fired:
                # условие продолжается: обновляем уже выпущенный инцидент
                self._refresh(rule, key, st, held)
                return []

            if held + _EPS < self.confirm_for(key):
                return []
            cooldown = self.cooldown_for(key)
            if cooldown > 0 and st.last_fire and (ts - st.last_fire) + _EPS < cooldown:
                # инцидент заслужен, но слишком рано — выпустим, когда cooldown истечёт
                return []

            event = self._build(rule, rule.kind, key, dict(st.detail), held, ts)
            st.fired = True
            st.last_fire = ts
            st.open_event = event
            st.fired_total += 1
            self._fired_total += 1
            return [event]

        # условие не наблюдается
        if st.since is None and not st.fired:
            return []
        gap = ts - (st.last_active or ts)
        if gap + _EPS >= self.release_for(key):
            self._close(st)
        return []

    def _refresh(self, rule: Rule, key: str, st: _State, held: float) -> None:
        """Продлить уже выпущенный инцидент: duration и текст, тот же объект."""
        ev = st.open_event
        if ev is None:
            return
        ev.duration = round(held, 2)
        ev.detail["duration_total"] = round(held, 2)
        ev.message = self._message(rule, key, ev.detail, held)

    def _close(self, st: _State) -> None:
        """Снять условие по гистерезису и зафиксировать итог для отчёта."""
        if st.open_event is not None:
            self._closed.append({
                "kind": st.open_event.kind.value,
                "id": st.open_event.id,
                "ts": st.open_event.ts,
                "duration": st.open_event.duration,
                "message": st.open_event.message,
            })
            if len(self._closed) > 500:
                del self._closed[:-500]
        st.since = None
        st.fired = False
        st.open_event = None
        st.detail.clear()

    # ------------------------------------------------------- сборка события
    def _build(self, rule: Rule | None, kind: EventKind, key: str,
               detail: dict[str, Any], duration: float, ts: float,
               severity: Severity | None = None,
               external: bool = False) -> ProctorEvent:
        weight = RISK_WEIGHTS.get(kind, 10.0)
        channel = rule.channel if rule is not None else Channel.SYSTEM
        sev = severity or (rule.severity if rule is not None else severity_for_weight(weight))
        confidence = self._confidence(key, detail)
        detail = dict(detail)
        detail.setdefault("confirm_sec", round(self.confirm_for(key), 2))
        if external:
            detail.setdefault("confirmed_by", "external")
        # источник мог прислать готовый русский текст (fusion, env-проверки) —
        # он подробнее шаблона, поэтому имеет приоритет
        ready = detail.get("message")
        message = (str(ready) if isinstance(ready, str) and ready.strip()
                   else self._message(rule, key, detail, duration))
        return ProctorEvent(
            kind=kind,
            severity=sev,
            channel=channel,
            confidence=confidence,
            ts=ts,
            duration=round(float(duration), 2),
            message=message,
            detail=detail,
        )

    def _confidence(self, key: str, detail: dict[str, Any]) -> float:
        """Уверенность инцидента.

        Приоритет — число от детектора (`conf`/`confidence`). Если его нет, но
        известно, во сколько раз превышен персональный порог, уверенность
        растёт от 0.55 на самой границе до 1.0 при двукратном превышении:
        пограничный случай не должен весить столько же, сколько очевидный.
        """
        for field_name in ("conf", "confidence", "score"):
            if field_name in detail:
                return _clamp(_as_float(detail[field_name], 1.0), 0.05, 1.0)
        ratio = self._dev_ratio(key, detail)
        if ratio is not None:
            return _clamp(0.55 + 0.45 * (ratio - 1.0), 0.55, 1.0)
        return 1.0

    def _dev_ratio(self, key: str, detail: dict[str, Any]) -> float | None:
        """Во сколько раз превышен порог. None — посчитать не из чего."""
        best: float | None = None
        for dev_key in DEV_KEYS:
            if dev_key in detail:
                value = abs(_as_float(detail[dev_key], 0.0))
                best = value if best is None else max(best, value)
        if best is not None and best > 0:
            return best
        spec = DEV_FALLBACK.get(key)
        if spec is None:
            return None
        keys, cfg_key, default_limit = spec
        limit = abs(_as_float(self.cfg.get(cfg_key), default_limit)) or default_limit
        raw: float | None = None
        for k in keys:
            if k in detail:
                value = abs(_as_float(detail[k], 0.0))
                raw = value if raw is None else max(raw, value)
        if raw is None or limit <= 0:
            return None
        return raw / limit

    def _message(self, rule: Rule | None, key: str, detail: dict[str, Any],
                 duration: float) -> str:
        if rule is None:
            return key.upper()
        try:
            return rule.message_template.format_map(
                self._message_vars(key, detail, duration))
        except Exception:  # шаблон кривой — отчёт всё равно должен собраться
            return rule.message_template

    def _message_vars(self, key: str, detail: dict[str, Any],
                      duration: float) -> _SafeVars:
        """Значения для подстановки в message_template.

        Все значения — уже строки: шаблоны пишутся без форматных спецификаторов,
        поэтому отсутствующий ключ безопасно превращается в пустую строку.

        Необязательные уточнения собираются как ЦЕЛЫЕ фразы вместе со своими
        знаками препинания (`{conf_area}` -> « (уверенность 82%, площадь кадра
        2.1%)»). Иначе внешнее событие без длительности и уверенности — а
        именно такие шлёт оболочка и env-проверки — дало бы в отчёте «на 0.0 с»
        и пустые скобки.
        """
        out = _SafeVars()
        for name, value in detail.items():
            out[name] = _fmt(value)

        dur = max(duration, 0.0)
        has_dur = dur >= 0.05
        out["duration"] = f"{dur:.1f}"
        out["duration_int"] = str(int(round(dur)))
        out["held_na"] = f" на {dur:.1f} с" if has_dur else ""
        out["held"] = f" {dur:.1f} с" if has_dur else ""
        out["held_dash"] = f" — {dur:.1f} с" if has_dur else ""
        out["held_par"] = f" ({dur:.1f} с)" if has_dur else ""
        out["held_for"] = f" и держится {dur:.1f} с" if has_dur else ""

        conf = self._confidence(key, detail)
        out["conf_pct"] = str(int(round(conf * 100)))

        ratio = self._dev_ratio(key, detail)
        if ratio is not None and ratio >= 1.05:
            out["dev_note"] = f" (базовое отклонение превышено в {ratio:.1f} раза)"
            out["dev_ratio"] = f"{ratio:.1f}"
        else:
            out["dev_note"] = ""
            out["dev_ratio"] = "" if ratio is None else f"{ratio:.1f}"

        zone = str(detail.get("zone", detail.get("gaze_zone", "")) or "").lower()
        out["zone_ru"] = "вниз" if key == "gaze_down" else ZONE_RU.get(zone, "в сторону")

        label = str(detail.get("label", "") or "").lower()
        out["label_ru"] = LABEL_RU.get(label, label or "неопознанный предмет")

        # уверенность детектора и площадь — только если их реально прислали
        pieces: list[str] = []
        for field_name in ("conf", "confidence", "score"):
            if field_name in detail:
                pieces.append(f"уверенность {int(round(_as_float(detail[field_name], 0.0) * 100))}%")
                break
        if "area_ratio" in detail:
            area = _as_float(detail["area_ratio"], 0.0) * 100
            out["area_pct"] = f"{area:.1f}"
            pieces.append(f"площадь кадра {area:.1f}%")
        out["conf_area"] = f" ({', '.join(pieces)})" if pieces else ""

        out["threshold_pct"] = str(int(round(self._identity_threshold * 100)))
        if "similarity" in detail:
            sim = int(round(_as_float(detail["similarity"], 0.0) * 100))
            out["similarity_pct"] = str(sim)
            out["sim_note"] = (f": близость {sim}% при пороге "
                               f"{int(round(self._identity_threshold * 100))}%")
        if "streak" in detail:
            streak = int(_as_float(detail["streak"], 0.0))
            if streak > 0:
                word = _plural(streak, ("проверка", "проверки", "проверок"))
                out["streak_note"] = f", {streak} {word} подряд"
        # SECOND_FACE по смыслу — минимум два лица. Если источник события не
        # передал число (например, push_external из оболочки с пустым detail),
        # шаблон без этой подстановки даёт «В кадре  лица одновременно» —
        # дыру в тексте, который уйдёт в отчёт. Подставляем осмысленный минимум.
        faces_n = int(_as_float(detail.get("faces"), 0.0))
        out["faces"] = str(faces_n if faces_n >= 2 else 2)
        if "no_blink_sec" in detail:
            out["no_blink_note"] = f" {_as_float(detail['no_blink_sec'], 0.0):.1f} с"

        angles = []
        if "head_yaw" in detail:
            angles.append(f"поворот {_as_float(detail['head_yaw'], 0.0):.0f}°")
        if "head_pitch" in detail:
            angles.append(f"наклон {_as_float(detail['head_pitch'], 0.0):.0f}°")
        out["angles_note"] = (", " + ", ".join(angles)) if angles else ""

        audio = []
        if "rms" in detail:
            audio.append(f"громкость {_as_float(detail['rms'], 0.0):.3f}")
        if "conf" in detail or "confidence" in detail:
            audio.append(f"уверенность {out['conf_pct']}%")
        out["rms_conf"] = f" ({', '.join(audio)})" if audio else ""

        out["mouth_threshold"] = f"{self._mouth_threshold:.2f}"
        if "mouth_open_ratio" in detail:
            out["mouth_note"] = (
                f" (раскрытие рта {_as_float(detail['mouth_open_ratio'], 0.0):.2f} "
                f"при пороге {self._mouth_threshold:.2f})")

        if "length" in detail:
            length = int(_as_float(detail["length"], 0.0))
            out["length_note"] = (f", {length} "
                                  f"{_plural(length, ('символ', 'символа', 'символов'))}")
        if "displays" in detail:
            out["displays_note"] = f" ({_fmt(detail['displays'])})"
        if detail.get("combo"):
            out["combo_note"] = f" {_fmt(detail['combo'])}"
        if detail.get("stage"):
            out["stage_note"] = f" ({_fmt(detail['stage'])})"
        if detail.get("sensor"):
            out["sensor_note"] = f" ({_fmt(detail['sensor'])})"

        # для env-проверок имя процесса/устройства лежит под разными ключами
        for candidate in ("name", "process", "device", "software", "app", "reason"):
            value = detail.get(candidate)
            if value:
                out["name_or_detail"] = _fmt(value)
                out["name_par"] = f" ({_fmt(value)})"
                out["name_colon"] = f": {_fmt(value)}"
                break
        else:
            out["name_or_detail"] = "подробности в отчёте"
        return out

    # ------------------------------------------------------------- телеметрия
    def open_incidents(self) -> list[dict[str, Any]]:
        """Инциденты, условие которых держится прямо сейчас."""
        rows = []
        for st in self._states.values():
            if st.open_event is None:
                continue
            rows.append({
                "kind": st.open_event.kind.value,
                "id": st.open_event.id,
                "ts": st.open_event.ts,
                "duration": st.open_event.duration,
                "message": st.open_event.message,
            })
        return rows

    def closed_incidents(self) -> list[dict[str, Any]]:
        return list(self._closed)

    def stats(self) -> dict[str, Any]:
        """Диагностика движка: сколько чего выпущено и что не опознано."""
        return {
            "fired_total": self._fired_total,
            "by_name": {name: st.fired_total for name, st in self._states.items()
                        if st.fired_total},
            "held_sec": {name: round(st.held_total, 1) for name, st in self._states.items()
                         if st.held_total >= 0.5},
            "open": self.open_incidents(),
            "unknown_names": dict(self._unknown),
            "mouth_threshold": self._mouth_threshold,
        }


# ===========================================================================
# мелкие помощники
# ===========================================================================
def _as_kind(value: Any) -> EventKind | None:
    if isinstance(value, EventKind):
        return value
    try:
        return EventKind(str(getattr(value, "value", value)).strip().upper())
    except (ValueError, TypeError):
        return None


def _as_severity(value: Any) -> Severity | None:
    if isinstance(value, Severity):
        return value
    if value is None:
        return None
    try:
        return Severity(str(value).strip().lower())
    except (ValueError, TypeError):
        return None


def _as_float(value: Any, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


def _plural(n: int, forms: tuple[str, str, str]) -> str:
    """Русское согласование числительного: 1 проверка, 3 проверки, 5 проверок."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def _fmt(value: Any) -> str:
    """Человекочитаемое представление значения detail для message."""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, float):
        return f"{value:.0f}" if value.is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")
    if isinstance(value, (list, tuple)):
        return ", ".join(_fmt(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}={_fmt(v)}" for k, v in value.items())
    return str(value)


def rule_names() -> Iterable[str]:
    """Имена всех известных наблюдений — удобно для тестов и отладки."""
    return RULES.keys()


__all__ = ["EventEngine", "Rule", "RULES", "ALIASES", "COMPOSITE_INPUTS",
           "severity_for_weight", "rule_names"]
