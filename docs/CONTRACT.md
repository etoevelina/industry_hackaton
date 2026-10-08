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
| `risk` | `{score:float, level:str, breakdown:[{kind,contribution,count}], action:<VerdictAction>, requested_action:<VerdictAction>, review_required:bool}` |
| `calibration` | `{stage:str, progress:float, done:bool, result:dict}` |
| `verdict` | `{action:<VerdictAction>, reason:str, score:float, requested_action:<VerdictAction>, review_required:bool, review:<Review>\|absent, decided_by:str\|absent}` |
| `error` | `{code:str, message:str, fatal:bool}` |

### shell -> sidecar
| type | payload |
|---|---|
| `session_start` | `{student_id:str, exam_id:str, student_name:str}` |
| `session_end` | `{reason:str}` |
| `calibrate` | `{stage:"gaze_center"\|"gaze_grid"\|"identity"\|"voice", point:[x,y]\|null}` |
| `telemetry` | `{kind:"keystroke"\|"paste"\|"answer_submit"\|"question_shown", question_id:str, ...}` |
| `shell_event` | `{kind:<EventKind>, detail:dict}` — оболочка шлёт уже готовый EventKind |
| `command` | `{name:"snapshot"\|"reset_risk"\|"export_report"\|"proctor_lock"\|"proctor_release", actor:str, reason:str}` |

### Калибровка — детальнее
- `gaze_center`, `identity`, `voice`: одно `calibrate` на этап, итог — `calibration` с `done:true`.
- `gaze_grid`: точки обходит **оболочка**. `calibrate {point:[x,y]}` — «сейчас показана эта
  точка» (координата становится целью регрессии как есть; первый показ после другой стадии
  начинает сетку заново, повторный показ точки сбрасывает её прошлые кадры). Ответы по точке
  идут с `done:false` и `result.point` — прогресс, затем `result.point_done:true`, когда точка
  набрала кадры. Оболочка держит точку до `point_done`, но 0.9–3 с (без ответов — 1.5 с).
  CALIBRATION_DONE по точке не пишется. После обхода оболочка шлёт
  `calibrate {stage:"gaze_grid", point:null}` — «обход закончен, строй карту по собранному».
  Только на него приходит `done:true` с `result.all_points_done:true`,
  `result.screen_map_applied:bool` и `result.quality` (`grade`, `map_grade`, `loo_rmse`, ...).
- Если оболочка ушла с `gaze_center`/`identity` по своему таймеру до `done`, сайдкар
  закрывает стадию тем, что успело собраться, и шлёт её `done:true` позже с
  `result.superseded:true`. Итог стадии оболочка берёт только из `done`: промежуточный
  прогресс несёт оценку прошлой попытки.
- Качество лежит в `result.quality`, а не в корне `result`: `grade` — общая оценка,
  `map_grade` — оценка карты по LOO. Карта с `map_grade:"poor"` (или не построенная) детектору
  не отдаётся (`screen_map_applied:false`): плохая карта искажает «мимо экрана», детектор
  остаётся на персональных порогах. Карта решает только горизонталь; вверх/вниз — пороги.
- Пока идёт калибровка взгляда (и 2 с после последнего её шага), взгляд и поворот головы не
  становятся инцидентами; лицо, второе лицо и живость проверяются как обычно. Тишина ограничена
  бюджетом 90 с на сессию: `calibrate`, присланный посреди экзамена, не глушит взгляд дольше, а
  переход в калибровку остаётся в `state_history` (`meta.json` пакета).
- Точка сетки считается набранной (`point_done`) не раньше 0.5 с паузы на саккаду + 0.6 с сбора.
- `session_start` сбрасывает калибровку: база и карта прошлого студента не переходят к новому.

### Телеметрия ввода (для fusion) — детальнее
- `keystroke`: `{question_id, ts, interval_ms:int, key_class:"char"\|"nav"\|"ctrl"}` (сами символы НЕ передаются — приватность)
- `paste`: `{question_id, ts, length:int, source:"clipboard"}`
- `answer_submit`: `{question_id, ts, length:int, time_to_answer_ms:int, typing_stats:{mean_ms,std_ms,chars}}`
- `question_shown`: `{question_id, ts, difficulty:int}`

## Эскалация и решение человека — обязательно к реализации в оболочке

Позиционирование продукта — «Integrity Score вместо надзора, решение принимает человек».
Держится оно не формулировкой, а этими полями. Оболочка, которая их игнорирует,
опровергает позиционирование прямо на экране студента.

### Потолок автоматики

`AUTO_ACTION_CAP = PAUSE` (`sidecar/protocol.py`). Автоматика НИКОГДА не выносит `lock`
сама: `cap_auto_action()` опускает любое автоматическое `LOCK` до `PAUSE`. Исключение одно
и объявленное — флаг `--auto-lock`; тогда `policy_info().auto_action_cap == "lock"`, и
оболочка обязана говорить про автоматическую блокировку честно.

Отсюда следствие для шкалы в HUD: подпись уровня 90+ — **«решение проктора»**, а не
«блокировка». Называть автоматическим действием то, чего автоматика не делает, нельзя.

### Поля вердикта

| поле | смысл |
|---|---|
| `action` | что делать СЕЙЧАС, уже ограничено политикой: `none`\|`warn`\|`pause`\|`lock` |
| `requested_action` | чего просила автоматика ДО ограничения. `requested_action="lock"` при `action="pause"` означает «дошли до порога блокировки, но решает человек» |
| `review_required` | `true` — приостановка ждёт решения ЧЕЛОВЕКА |
| `review` | описание запроса, см. ниже. Присутствует, пока запрос открыт |
| `decided_by` | имя человека, принявшего решение. Присутствует только в вердикте, вызванном `proctor_lock`/`proctor_release` |

`<Review>` (payload `review`, он же `control`/`lock_review_requested` в хеш-цепочке):

```
{pending:bool, ts:float, score:float, threshold:float,
 session_code:str, session_code_display:str,
 top:[str],              # топ-3 вклада в риск, по-русски
 explanation:str}        # ГОТОВЫЙ текст для экрана студента
```

`explanation` берётся как есть и не переписывается в оболочке: одна формулировка в журнале,
в отчёте и на экране — то же требование, из которого выросла вся политика.

### Что оболочка ОБЯЗАНА делать

1. **`review_required=true` -> студент не снимает паузу сам.** Ни при каком спаде риска.
   Сайдкар сессию из `PAUSED` не выпускает (`_apply_action`), и если оболочка выпустит
   экзамен сама, получится худший из возможных разрывов: таймер идёт, ответы принимаются,
   а журнал считает экзамен приостановленным в ожидании человека.
2. **Пауза приостанавливает ВВОД, а не только таймер.** Оверлей перехватывает мышь, но не
   клавиатуру. Нужны `disabled` на полях и кнопках, `inert`/`aria-hidden` на содержимом
   экзамена и запрет навигации по билету. Пауза, которая останавливает часы и оставляет
   поле ответа рабочим, для списывающего строго выгоднее отсутствия паузы.
3. **Показать `explanation` и `session_code_display`.** Студент должен знать, что решение
   принимает человек и по какому коду его искать.
4. **Текст порога — тот, который сработал.** При `review_required` это порог блокировки,
   а не паузы.
5. **Экран блокировки называет инициатора.** При политике по умолчанию до него доходят
   только через `proctor_lock`, то есть через решение человека; `decided_by` для этого и
   присылается.

### Команды решения

```
{"v":1,"type":"command","ts":<float>,"name":"proctor_lock","actor":"<кто>","reason":"<почему>"}
{"v":1,"type":"command","ts":<float>,"name":"proctor_release","actor":"<кто>","reason":"<почему>"}
```

`actor` **обязателен** для обеих: без него сайдкар отвечает `error` с кодом
`actor_required`. Запись в журнале без имени человека не имеет смысла — ради неё всё и
делалось. Оба решения оставляют запись `control`/`proctor_decision` в хеш-цепочке.

`proctor_release` дополнительно обнуляет накопленный риск (отдельной записью
`control`/`risk_reset`): иначе экзамен продолжится и через секунду упрётся в тот же порог,
и человека позовут опять по тем же наблюдениям.

### Аутентификация канала

Включена **по умолчанию**. Токен предъявляется заголовком `X-Proctor-Token` (принимаются
также `Authorization: Bearer …` и, как запасной вариант, `?token=…` в адресе — только потому
что адрес петлевой). Источники токена, в порядке приоритета:

1. переменная окружения `PROCTOR_WS_TOKEN`;
2. файл `<temp>/qih-proctor-<порт>.token` (права 0600, JSON `{token, host, port, header}`).

Токен живёт процесс сайдкара: после перезапуска он новый, поэтому клиент перечитывает его
перед каждой попыткой реконнекта. Без токена рукопожатие отклоняется с **403** — симптом
выглядит как «сайдкар не запущен», поэтому 403 логируется отдельным понятным сообщением.

`single_client` тоже включён по умолчанию: второе соединение получает 403 «канал уже занят
другим клиентом». Отдельный CLI проктора рядом с оболочкой требует `--allow-multi-client`.

## Приватность журнала: метки, приходящие извне

`question_id` — это **номер в билете, а не содержание ответа**. Банк вопросов — внешние
данные, и метка вида `q3/<первая строка ответа>` приходит не от злого умысла, а от
небрежного экспорта.

`safe_label()` (`sidecar/protocol.py`) — единственное правило для таких меток: не длиннее
`LABEL_MAX_LEN = 16`, без пробелов, только буквы (латиница, кириллица, казахские), цифры и
`-_.`. Всё остальное заменяется устойчивым суррогатом `qid-<8 hex>`: он одинаков для
одинакового входа, поэтому дедупликация в `engine/fusion.py`, потолок наблюдения в
`engine/risk.py` и группировка в отчёте продолжают работать, а содержание в журнал не
попадает. Обрезка по длине задачу НЕ решает: обрезанный ответ — это всё ещё ответ.

`sanitize_telemetry()` (`sidecar/main.py`) — единственные ворота потока телеметрии, и стоят
они на стороне **сайдкара**. Санитизация в оболочке есть и дублирует правило, но сайдкар ей
не верит: доверять содержанию сообщения из сети он не вправе, кем бы ни был клиент. Поля,
не прошедшие белый список, не видит НИКТО ниже этих ворот — ни движок правил, ни журнал, ни
лог. Недокументированных полей в телеметрии не бывает: поле, которого нет в таблице выше,
отбрасывается (так ушёл `origin` у `paste`, уносивший URL с текстом ответа в хеш-цепочку).

## Правила оценки: профиль весов в хеш-цепочке

Профиль весов объявляется записью `control`/`weight_profile` **при старте сессии, до первого
события** — то есть правила фиксируются ДО экзамена, а не выбираются после него.

```
{profile:str, label:str, description:str, base:"closed-book",
 builtin:bool,            # имя из DEFAULT_WEIGHT_PROFILES
 overridden:bool,         # встроенное имя переопределено конфигурацией вуза
 weights:{KIND:float},    # таблица ЦЕЛИКОМ
 changed:[KIND],          # отклонения от protocol.py
 available_profiles:[str]}
```

Таблица пишется целиком не для полноты, а по необходимости: экзаменатору не нужен конфиг
вуза, чтобы повторить счёт по объявленным правилам — он берёт их из подписанного журнала.
`build_report()` и `recompute_integrity()` читают профиль ОТТУДА, а конфиг остаётся запасным
источником только для журналов прежних версий (и отчёт об этом прямо говорит).

Зачем так. Политика блокировки в цепочку писалась, а правила оценки — нет: профиль читался
из конфигурации в момент сборки отчёта. Из одной целой цепочки собирались два по-разному
подписанных отчёта с разными integrity, и ни один верификатор не мог сказать, какие правила
вуз объявил заранее. Хуже: `weight_profiles()` разрешает переопределить встроенное имя,
поэтому `closed-book` с `keep: []` обнулял все 38 весов, давал integrity 100 под доверенным
именем и настоящим описанием, и отличить его от эталонного по отчёту было нельзя.

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
