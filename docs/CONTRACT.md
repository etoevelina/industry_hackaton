# Контракт модулей — обязателен к соблюдению

Источник истины по событиям: `sidecar/protocol.py`. Новых строковых литералов для
типов событий не вводить — только `EventKind`.

## Транспорт

WebSocket-сервер поднимает **сайдкар** на `ws://127.0.0.1:8787`, Electron подключается
как клиент и переподключается с бэкоффом. JSON, один объект на сообщение, конверт:
`{"v":1,"type":<MsgType>,"ts":<float>, ...payload}`.

### sidecar -> shell
| type | payload |
|---|---|
| `hello` | `{capabilities:{vision:bool,gaze:bool,identity:bool,audio:bool,env:bool}, version:str}` |
| `status` | `{fps:float, face_present:bool, face_count:int, gaze:{yaw:float,pitch:float,zone:str}, phone:bool, identity_ok:bool, audio_ok:bool, risk:float, state:str}` |
| `event` | `{event:<ProctorEvent.to_dict()>}` |
| `risk` | `{score:float, level:str, breakdown:[{kind,contribution,count}], action:<VerdictAction>}` |
| `calibration` | `{stage:str, progress:float, done:bool, result:dict}` |
| `verdict` | `{action:<VerdictAction>, reason:str, score:float}` |
| `error` | `{code:str, message:str, fatal:bool}` |

### shell -> sidecar
| type | payload |
|---|---|
| `session_start` | `{student_id:str, exam_id:str, student_name:str}` |
| `session_end` | `{reason:str}` |
| `calibrate` | `{stage:"gaze_center"\|"gaze_grid"\|"identity"\|"voice", point:[x,y]\|null}` |
| `telemetry` | `{kind:"keystroke"\|"paste"\|"answer_submit"\|"question_shown", question_id:str, ...}` |
| `shell_event` | `{kind:<EventKind>, detail:dict}` — оболочка шлёт уже готовый EventKind |
| `command` | `{name:"snapshot"\|"reset_risk"\|"export_report"}` |

### Телеметрия ввода (для fusion) — детальнее
- `keystroke`: `{question_id, ts, interval_ms:int, key_class:"char"\|"nav"\|"ctrl"}` (сами символы НЕ передаются — приватность)
- `paste`: `{question_id, ts, length:int, source:"clipboard"}`
- `answer_submit`: `{question_id, ts, length:int, time_to_answer_ms:int, typing_stats:{mean_ms,std_ms,chars}}`
- `question_shown`: `{question_id, ts, difficulty:int}`

## Интерфейсы Python-модулей

Каждый модуль — самодостаточный класс без импортов из соседних детекторов.
Общий стиль: конструктор принимает `config: dict`, метод обработки принимает кадр/данные
и возвращает **структуру наблюдений**, НЕ события. События порождает только Event Engine.

```python
# sidecar/detectors/objects.py
class ObjectDetector:
    def __init__(self, config: dict) -> None: ...
    def available(self) -> bool: ...
    def detect(self, frame_bgr) -> list[Detection]:
        """Detection: {label:str, conf:float, bbox:(x,y,w,h), area_ratio:float}"""

# sidecar/detectors/face_mesh.py
class FaceAnalyzer:
    def __init__(self, config: dict) -> None: ...
    def analyze(self, frame_bgr) -> FaceObservation:
        """FaceObservation: face_count, yaw, pitch, roll, gaze_yaw, gaze_pitch,
        gaze_zone, eye_aspect_ratio, mouth_open_ratio, landmarks, face_bbox, blink"""
    def calibrate_center(self, samples) -> dict: ...

# sidecar/detectors/identity.py
class IdentityVerifier:
    def __init__(self, config: dict) -> None: ...
    def enroll(self, frame_bgr, face_bbox) -> bool: ...
    def verify(self, frame_bgr, face_bbox) -> IdentityObservation:
        """IdentityObservation: match:bool, similarity:float, enrolled:bool"""

# sidecar/detectors/audio.py
class AudioMonitor:
    def start(self) -> None: ...
    def poll(self) -> AudioObservation:
        """AudioObservation: speech:bool, is_owner:bool|None, rms:float, confidence:float"""
    def enroll_owner(self, seconds: float) -> bool: ...
    def stop(self) -> None: ...

# sidecar/env_checks.py
def run_all_checks(config: dict) -> list[EnvFinding]:
    """EnvFinding: {kind:EventKind, detail:dict, severity:Severity}
    Проверки: виртуальная камера, remote-софт, VM, запись экрана, чёрный список процессов."""

# sidecar/engine/events.py
class EventEngine:
    """Единственное место, где наблюдения превращаются в ProctorEvent.
    Отвечает за окно подтверждения, гистерезис и cooldown."""
    def __init__(self, config: dict) -> None: ...
    def push_observation(self, name: str, active: bool, ts: float, **detail) -> list[ProctorEvent]: ...
    def push_external(self, kind: EventKind, detail: dict) -> list[ProctorEvent]: ...

# sidecar/engine/risk.py
class RiskScorer:
    def add(self, event: ProctorEvent) -> None: ...
    def decay(self, now: float) -> None: ...
    @property
    def score(self) -> float: ...
    def breakdown(self) -> list[dict]: ...
    def action(self) -> VerdictAction: ...

# sidecar/engine/fusion.py
class FusionEngine:
    """Коррелирует CV-события с телеметрией ввода. Ядро отличия продукта."""
    def note_event(self, event: ProctorEvent) -> None: ...
    def note_telemetry(self, msg: dict) -> list[ProctorEvent]: ...

# sidecar/storage/db.py
class EvidenceStore:
    """SQLite + hash-chain: каждая запись хранит hash(prev_hash + payload)."""
    def open_session(self, meta: dict) -> str: ...
    def append(self, event: ProctorEvent) -> str: ...
    def verify_chain(self) -> tuple[bool, int]: ...
    def close_session(self, summary: dict) -> None: ...

# sidecar/storage/report.py
def build_report(session_dir: str, db_path: str, out_html: str) -> str: ...
def sign_report(path: str, key_path: str) -> str: ...
def verify_report(path: str, sig_path: str, pub_key_path: str) -> bool: ...
```

## Правила, обязательные для всех

1. **Ни один детектор не падает фатально.** Нет модели/камеры/библиотеки — `available()`
   возвращает False, сайдкар работает дальше в урезанном режиме и сообщает это в `hello`.
   Демо не должно умирать от отсутствия одного пакета.
2. **Ленивый импорт тяжёлых зависимостей** (mediapipe, onnxruntime, insightface, torch)
   — внутри `__init__`/метода, в try/except, не на верхнем уровне модуля.
3. Все пользовательские строки (`message`) — по-русски, пригодны для вставки в отчёт.
4. Никаких сетевых вызовов наружу. Всё локально. Это требование кейса.
5. Конфиг — один файл `sidecar/config.py` (dataclass + загрузка из `config.json`).
6. Python 3.12, macOS arm64 основная платформа разработки; Windows-специфичный код
   обязан быть под `if sys.platform == "win32"` и не ломать импорт на macOS.
