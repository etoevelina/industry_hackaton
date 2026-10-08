# Контракт модулей — обязателен к соблюдению

Источник истины по событиям: `sidecar/protocol.py`. Новых строковых литералов для
типов событий не вводить — только `EventKind`.

## Транспорт

WebSocket-сервер поднимает **сайдкар** на `ws://127.0.0.1:8787`, Electron подключается
как клиент и переподключается с бэкоффом. JSON, один объект на сообщение, конверт:
`{"v":1,"type":<MsgType>,"ts":<float>, ...payload}`.

Размер сообщения shell -> sidecar: не больше `ws_max_message` (1 МиБ, `config.py`) для
всех типов, кроме `screen_evidence`. Больше — ядро сообщение не обрабатывает: пишет отказ в
лог и отвечает `error` с `code:"too_large"`, соединение остаётся. Транспортный потолок кадра
выше — `WS_TRANSPORT_MAX_BYTES` (`protocol.py`, ~4 МиБ + 64 КиБ): ради снимка окна экзамена
до 3 МБ в base64. Сообщение больше потолка библиотека не отклоняет, а рвёт соединение (1009).

### sidecar -> shell
| type | payload |
|---|---|
| `hello` | `{capabilities:{vision:bool,gaze:bool,identity:bool,audio:bool,env:bool}, version:str}` |
| `status` | `{fps:float, face_present:bool, face_count:int, gaze:{yaw:float,pitch:float,zone:str}, phone:bool, identity_ok:bool, audio_ok:bool, risk:float, state:str}` |
| `event` | `{event:<ProctorEvent.to_dict()>, screen:bool}` — `screen:true` у каждого неслужебного события, пока сессия пишется: оболочка снимает окно экзамена и отвечает `screen_evidence` (см. «Доказательства к инциденту») |
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
| `screen_evidence` | `{event_id:str, mime:"image/jpeg", data_b64:str, width:int, height:int, captured_at:float, source:"exam_view"\|"main_window"}` — или без `data_b64` и с `error:str`, если снять окно не удалось |

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

## Доказательства к инциденту: кадр камеры и снимок окна экзамена

### Кадр камеры (сайдкар)

К КАЖДОМУ событию, кроме служебных, ядро прикладывает кадр камеры **до** записи в
хеш-цепочку: путь к кадру — часть подписанного тела события. Служебные виды —
`SERVICE_EVENT_KINDS` (`sidecar/protocol.py`, каноническая копия `SERVICE_KINDS` отчёта):
`SESSION_STARTED`, `SESSION_ENDED`, `CALIBRATION_DONE`, `SHELL_CONFIG`,
`EXAM_PROFILE_APPLIED`, `EXAM_PROFILE_ABSENT`, `EXAM_PROFILE_SEARCH_ALLOWED` (и `EXAM_PROFILE`
из журналов прежних версий).

- События CV-потока получают кадр своего момента; остальные (оболочка, окружение, связки,
  решения проктора) — последний кадр камеры (`_emit` -> `_ensure_frame`).
- `evidence.frame_path` — кадр, `evidence.extra.frame_ts` — время кадра (epoch, с). У событий
  оболочки оно не совпадает с `ts` события, поэтому отчёт показывает именно его.
- На каждом сохранённом кадре размываются ВСЕ лица, кроме основного: «прочие» лица FaceMesh
  (основное — самое крупное) и лица, найденные каскадом Хаара OpenCV
  (`haarcascade_frontalface_default.xml` из `cv2.data.haarcascades`, `storage/evidence.py`,
  `plan_fallback_blur`). Каскад проходит каждый кадр, уходящий на диск: FaceMesh видит не
  больше `max_num_faces` лиц (2), пропускает мелкие, а без mediapipe не видит никого.
  Основным у каскада считается лицо, совпавшее с рамкой основного лица FaceMesh (наибольший
  IoU, не меньше 0.15, или центр внутри рамки); без FaceMesh — самое крупное.
  - `extra.blurred_faces` — сколько лиц размыто (лицо, найденное обоими детекторами,
    считается один раз); нет поля — размывать было некого;
  - `extra.blur` — `"haar"`: кадр прошёл каскад; `"unavailable"`: каскада нет (нет opencv с
    `cv2.data`, файл каскада не читается) — кадр всё равно записан, но лица, которых не
    увидел FaceMesh (а без mediapipe — все), НЕ размыты. Отчёт обязан это показать.
  - Подпись на кадре — латиницей (`VIS_101 PHONE_IN_FRAME`): `cv2.putText` кириллицу не
    рисует.
- Кадра нет -> `event.detail.evidence_skipped` (ставит только ядро; из `shell_event` это
  поле снимается на границе сокета):

| код | когда |
|---|---|
| `service` | служебный вид, кадр не нужен |
| `no_camera` | камеры нет: `--headless`, `--mock` (события из сценария), нет opencv — или устройство в этом процессе не отдало ни одного кадра (не подключено, занято, доступ не выдан) |
| `stale_frame` | камера кадры отдавала, но последний старше 2 с (`EVIDENCE_FRAME_MAX_AGE`) — камера пропала |
| `write_failed` | файл кадра не записался (в том числе `cv2.imwrite` вернул `False` для кадра или кропа) |
| `not_recording` | сессия не пишется (событие до `session_start` или после `session_end`) |
| `disabled` | кадр выключен конфигурацией: `save_evidence:false` или severity ниже `evidence_frame_min_severity`. При настройках по умолчанию не возникает |

- Правило кадра — `evidence_frame_min_severity` (по умолчанию `"info"`, то есть все
  неслужебные). Правило клипа — отдельное: severity не ниже `evidence_min_severity` (по
  умолчанию `"medium"`), никогда для `SECOND_FACE`, и клипа нет — с причиной в
  `extra.clip_skipped` (`CLIP_SKIP_*` в `protocol.py`):

| `clip_skipped` | когда |
|---|---|
| `multiple_faces` | в кадре больше одного лица — по FaceMesh или по каскаду на кадре события; с запасом: второе лицо в последние `evidence_clip_seconds` тоже закрывает клип (10 с «до» берутся из буфера) |
| `faces_unknown` | FaceMesh недоступен (нет mediapipe, канал выключен флагом): «одно лицо» не доказано |

  Клип размывать нечем, поэтому он пишется только при заведомо одном лице.

- **Снятие клипа (`clip_cancelled`).** Клип регистрируется в момент события: путь уже стоит
  в `evidence.clip_path`, а треть `evidence_clip_seconds` «после» добирается из следующих
  кадров. Если, пока «после» не набрано, FaceMesh (или каскад на сохраняемом кадре) увидел
  два лица и больше, рекордер снимает ВСЕ незакрытые клипы (`EvidenceRecorder.cancel_pending`):
  файл не создаётся вовсе, кадр со вторым лицом в них не попадает. Клипы, чьё окно закрылось
  раньше, не трогаются. На каждый снятый клип ядро пишет в хеш-цепочку запись
  `control`/`clip_cancelled`:

```
{event_id,                       # id события, чей clip_path теперь без файла
 kind,                           # вид события
 reason:"multiple_faces_after",  # посторонний вошёл в кадр после события
 at}                             # когда снят (epoch, с)
```

  Связь с событием — только по `event_id`. Обычно запись стоит в цепочке после события; у
  события оболочки, клип которого сняли, пока оно писалось, может оказаться и раньше.
  Отчёт и проверка пакета обязаны показывать такой клип как снятый («посторонний в кадре»),
  а не как потерянный файл; `scripts/verify_report.py` считает эти записи отдельно от
  записей о решениях.

### Снимок окна экзамена (оболочка -> сайдкар)

Снимается ТОЛЬКО представление экзамена: `webContents` LMS-представления, если экзамен идёт в
нём, иначе `webContents` главного окна — `webContents.capturePage()`. Рабочий стол, другие
окна и `desktopCapturer` — никогда. Снимок показывает вопрос и ответ студента в этот момент.

На `event` с `screen:true` оболочка снимает окно, уменьшает до ширины не больше 1280,
кодирует JPEG качества 70 и отвечает:

```
{"v":1,"type":"screen_evidence","ts":<epoch>,"event_id":"<id события>","mime":"image/jpeg",
 "data_b64":"<base64>","width":W,"height":H,"captured_at":<epoch>,"source":"exam_view"|"main_window"}
```

Снять не удалось — то же без `data_b64` и с `"error":"<короткая причина>"`. `capturePage()`,
не ответивший за 4 с (`SCREEN_EVIDENCE.captureTimeoutMs` в `shell/state.js`), — тоже неудача:
`"error":"capturePage не ответил за 4 с"`; зависший снимок снимается с полёта, следующее
событие снимает заново, а его поздний результат не отправляется и не переиспользуется. События в
пределах 1.5 с от предыдущего снимка могут получить те же байты, но каждое — своим
сообщением со своим `event_id`.

Ядро принимает снимок только пока сессия пишется, только к событию, которое само разослало
с `screen:true` в этой сессии не раньше 60 с назад (`SCREEN_EVIDENCE_MAX_AGE`), не больше
одного раза на `event_id`, не больше 3 МБ после декодирования и только JPEG (первые байты
`FF D8 FF`). С резервным журналом (JSONL вместо SQLite: записей `control` он не умеет) снимок
отклоняется до записи файла — файл без sha256 в цепочке был бы непроверяемым. Отклонённое
пишется в лог и в журнал не попадает. Принятое — файл
`evidence/<ms>_<seq>_<kind>_screen.jpg` (`session.evidence_file(f"{kind}_screen")`) и запись
`control`/`evidence_screen` в хеш-цепочке:

```
{event_id, kind, path,          # path — относительно каталога сессии
 sha256, width, height, bytes,  # sha256 записанных байт
 captured_at, received_at, source}
```

Для сообщения с `error` — `{event_id, kind, error, received_at}` (текст ошибки — одна
строка, до 200 символов). Если ядро не смогло записать уже принятый снимок, запись та же, с
`error` от ядра.

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
