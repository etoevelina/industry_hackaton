/* ===========================================================================
 * hud.js — панель прокторинга (тёмная среда наблюдения) рядом со светлым
 * листом теста. Визуальный язык NEON/PROCTOR, см. memory/decisions.md Р-13.
 *
 * Что здесь есть:
 *   1. Кольцевой индикатор risk-score на инлайновом SVG. Цвет по порогам
 *      30 / 60 / 90 из sidecar/protocol.py, в центре число И СЛОВО уровня
 *      («норма» / «внимание» / «пауза» / «блокировка») — цвет никогда не
 *      единственный носитель смысла (требование a11y из гайда).
 *   2. Индикаторы каналов: точка-фигура + название канала + состояние
 *      словами. Недоступный канал НЕ скрывается, а показывается серым с
 *      подписью «канал недоступен»: честная деградация видна и студенту, и жюри.
 *      Последним в том же списке стоит профиль экзамена — правила, которые
 *      задал проктор. Это не датчик, но студент обязан видеть и то, под
 *      какими правилами он сдаёт, а не только то, что за ним смотрят.
 *   3. Лента инцидентов по паттерну .event из гайда: тональная точка, факт,
 *      пояснение приглушённым, время моноширинным справа. Длина ленты
 *      ограничена, новые элементы добавляются без перерисовки всего списка.
 *   4. Эскалация по вердикту: warn — неблокирующий .alert, pause — оверлей с
 *      условием возврата, lock — экран блокировки с номером сессии и кодом
 *      инцидента.
 *   5. Профиль экзамена и попытки выйти за его пределы. Разбор профиля живёт
 *      здесь (этот файл подключается раньше app.js) и отдаётся наружу как
 *      window.Proctor.profile. Заблокированный переход показывается сразу
 *      по событию — отдельным .alert с доменом и прямым указанием, что
 *      попытка записана в журнал вместе с полным адресом.
 *
 * Тон формулировок — прямое правило гайда (раздел Alerts):
 *   наблюдаемый факт -> контекст -> понятное действие.
 * Обвинительных слов нет ни в одной строке этого файла. Система сообщает
 * наблюдение, решение принимает человек.
 *
 * Устойчивость к шторму: входящие события не пишут в DOM напрямую, а копятся
 * в очереди и выливаются одним проходом в requestAnimationFrame. 50 событий
 * за 2 секунды дают максимум 120 кадровых обновлений, а не 50 реflow подряд.
 *
 * Все значения цвета и размера — из shell/renderer/tokens.css. Хардкода нет.
 * =========================================================================== */

(function () {
  'use strict';

  /* ------------------------------------------------------------------ пороги */

  // Дубликат RISK_WARN / RISK_PAUSE / RISK_LOCK из sidecar/protocol.py.
  // Менять только синхронно с источником истины.
  var RISK_WARN = 30.0;
  var RISK_PAUSE = 60.0;
  var RISK_LOCK = 90.0;

  /**
   * Уровни риска. shape — фигура индикатора: смысл дублируется формой,
   * чтобы уровень читался и при цветовой слепоте, и на монохромном проекторе.
   */
  /*
   * Подпись верхнего уровня — «решение проктора», а не «блокировка». Шкала
   * стоит в одном кадре с политикой, и называть автоматическим действием то,
   * чего автоматика не делает, нельзя: её потолок — приостановка
   * (AUTO_ACTION_CAP в sidecar/protocol.py), а блокировку подтверждает
   * человек. Прежняя подпись противоречила политике прямо на экране.
   */
  var LEVELS = [
    { min: RISK_LOCK,  key: 'lock',  cls: 'lvl-lock',  text: 'решение проктора', tone: 'var(--risk-lock)',  shape: 'octa' },
    { min: RISK_PAUSE, key: 'pause', cls: 'lvl-pause', text: 'пауза',      tone: 'var(--risk-pause)', shape: 'square' },
    { min: RISK_WARN,  key: 'warn',  cls: 'lvl-warn',  text: 'внимание',   tone: 'var(--risk-warn)',  shape: 'triangle' },
    { min: -1,         key: 'ok',    cls: 'lvl-ok',    text: 'норма',      tone: 'var(--risk-ok)',    shape: 'circle' }
  ];

  var SEV_RANK = { info: 0, low: 1, medium: 2, high: 3, critical: 4 };

  var SEV_TONE = {
    info: 'var(--sev-info)',
    low: 'var(--sev-low)',
    medium: 'var(--sev-medium)',
    high: 'var(--sev-high)',
    critical: 'var(--sev-critical)'
  };

  /** Важность словами — дубль цветовой точки в ленте. */
  var SEV_TEXT = {
    info: 'информация',
    low: 'низкая',
    medium: 'средняя',
    high: 'высокая',
    critical: 'критическая'
  };

  /* ------------------------------------------------- словарь формулировок */

  /** Наблюдаемый факт по EventKind. Запасной вариант, если message пуст. */
  var EVENT_TEXT = {
    PHONE_IN_FRAME: 'В кадре зафиксирован телефон',
    PHONE_RAISED: 'Телефон поднят к уровню лица',
    PHONE_AIMED_AT_SCREEN: 'Телефон направлен на экран',
    FORBIDDEN_OBJECT: 'В кадре посторонний предмет',

    NO_FACE: 'Лицо не видно в кадре',
    SECOND_FACE: 'В кадре второй человек',
    IDENTITY_MISMATCH: 'Лицо не совпадает с эталоном калибровки',
    LIVENESS_FAIL: 'Признаки живого лица не подтверждены',

    GAZE_DOWN: 'Взгляд направлен вниз',
    GAZE_SIDE: 'Взгляд уведён в сторону',
    GAZE_OFF_SCREEN: 'Точка взгляда вне карты экрана',
    HEAD_TURNED: 'Голова отвёрнута от экрана',

    VOICE_OTHER: 'В записи посторонний голос',
    SPEECH_WITHOUT_LIP_MOTION: 'Речь без движения губ',

    AUDIO_DEVICE_CONNECTED: 'Подключено аудио-устройство',
    VIRTUAL_CAMERA: 'Обнаружена виртуальная камера',
    REMOTE_ACCESS_SOFTWARE: 'Запущено ПО удалённого доступа',
    VIRTUAL_MACHINE: 'Признаки виртуальной машины',
    SCREEN_RECORDING: 'Идёт запись экрана',
    MULTIPLE_DISPLAYS: 'Подключён второй монитор',
    BLACKLISTED_PROCESS: 'Запущена программа из стоп-листа',

    WINDOW_BLUR: 'Окно экзамена потеряло фокус',
    FULLSCREEN_EXIT: 'Выход из полноэкранного режима',
    SHORTCUT_BLOCKED: 'Горячая клавиша заблокирована',
    CLIPBOARD_PASTE: 'Вставка из буфера обмена',
    DEVTOOLS_ATTEMPT: 'Обращение к инструментам разработчика',

    PASTE_BURST: 'Вставлен большой блок текста',
    TYPING_ANOMALY: 'Ритм набора отличается от вашей базовой линии',

    FUSION_GAZE_THEN_ANSWER: 'Отвод взгляда, затем быстрый ответ',
    FUSION_BLUR_THEN_ANSWER: 'Переключение окна, затем быстрый ответ',
    FUSION_PHONE_THEN_ANSWER: 'Телефон в кадре, затем быстрый ответ',

    SESSION_STARTED: 'Сессия начата',
    SESSION_ENDED: 'Сессия завершена',
    CALIBRATION_DONE: 'Калибровка завершена',
    SENSOR_LOST: 'Потерян датчик: камера или микрофон',

    /*
     * Профиль экзамена. Для перехода и подзапроса это ЗАПАСНЫЕ строки: когда
     * в detail есть адрес, формулировку собирают navFact/subresFact с хостом,
     * и она точнее. Подзапрос описан как действие СТРАНИЦЫ, а не человека:
     * вес 2 в protocol.py поставлен ровно потому, что шрифт с чужого CDN —
     * это устройство сайта, и обвинять за него нельзя.
     */
    NAVIGATION_OFF_PROFILE: 'Переход за пределы правил этого экзамена не разрешён',
    SUBRESOURCE_OFF_PROFILE: 'Страница обратилась к источнику вне правил экзамена',
    EXAM_PROFILE_APPLIED: 'Применён профиль экзамена',
    EXAM_PROFILE_ABSENT: 'Правила экзамена проктором не задавались',
    EXAM_PROFILE_SEARCH_ALLOWED: 'Правилами экзамена разрешены поисковые системы'
  };

  /** Короткие подписи — список вклада в risk-score и подсветка канала. */
  var EVENT_SHORT = {
    PHONE_IN_FRAME: 'телефон в кадре',
    PHONE_RAISED: 'телефон поднят',
    PHONE_AIMED_AT_SCREEN: 'телефон на экран',
    FORBIDDEN_OBJECT: 'посторонний предмет',
    NO_FACE: 'лица не видно',
    SECOND_FACE: 'второй человек',
    IDENTITY_MISMATCH: 'лицо не совпало',
    LIVENESS_FAIL: 'живость не подтверждена',
    GAZE_DOWN: 'взгляд вниз',
    GAZE_SIDE: 'взгляд в сторону',
    GAZE_OFF_SCREEN: 'взгляд вне экрана',
    HEAD_TURNED: 'поворот головы',
    VOICE_OTHER: 'посторонний голос',
    SPEECH_WITHOUT_LIP_MOTION: 'речь без движения губ',
    AUDIO_DEVICE_CONNECTED: 'аудио-устройство',
    VIRTUAL_CAMERA: 'виртуальная камера',
    REMOTE_ACCESS_SOFTWARE: 'удалённый доступ',
    VIRTUAL_MACHINE: 'виртуальная машина',
    SCREEN_RECORDING: 'запись экрана',
    MULTIPLE_DISPLAYS: 'второй монитор',
    BLACKLISTED_PROCESS: 'программа из стоп-листа',
    WINDOW_BLUR: 'потеря фокуса',
    FULLSCREEN_EXIT: 'выход из полноэкрана',
    SHORTCUT_BLOCKED: 'горячая клавиша',
    CLIPBOARD_PASTE: 'вставка',
    DEVTOOLS_ATTEMPT: 'инструменты разработчика',
    PASTE_BURST: 'вставка блока',
    TYPING_ANOMALY: 'ритм набора',
    // Связка записана словом, а не стрелкой: U+2192 нет ни в одном из семи
    // вшитых woff2 и ни в одном объявленном unicode-range (см. fonts.css),
    // а font-src 'self' не даёт подтянуть шрифт со стрелкой. Эти подписи уходят
    // в .chan__state, в .hud__contrib и через text.short в .bar__name отчёта —
    // то есть в самое видное место на демо: вес FUSION_PHONE_THEN_ANSWER
    // в protocol.py максимальный одиночный, эта полоса в отчёте будет верхней.
    FUSION_GAZE_THEN_ANSWER: 'взгляд, затем ответ',
    FUSION_BLUR_THEN_ANSWER: 'окно, затем ответ',
    FUSION_PHONE_THEN_ANSWER: 'телефон, затем ответ',
    SENSOR_LOST: 'потерян датчик',

    NAVIGATION_OFF_PROFILE: 'переход вне правил',
    SUBRESOURCE_OFF_PROFILE: 'запрос страницы вне правил',
    EXAM_PROFILE_APPLIED: 'профиль применён',
    EXAM_PROFILE_ABSENT: 'правила не заданы',
    EXAM_PROFILE_SEARCH_ALLOWED: 'поиск разрешён правилами'
  };

  /**
   * Код инцидента и понятное действие.
   *
   * Код нужен на экране блокировки и в .alert: студент называет его организатору,
   * организатор находит запись в журнале. Формат — префикс канала плюс номер,
   * моноширинным (правило 5 дизайн-системы: идентификаторы — моно).
   *
   * РЕЕСТР КОДОВ ОДИН, И ОН НЕ ЗДЕСЬ. Канонический — `EVENT_CODES` в
   * `sidecar/engine/events.py`: именно он уходит в журнал, в хеш-цепочку и в
   * отчёт, и именно по нему комиссия поднимает эпизод. Сайдкар кладёт код в
   * `detail.code` каждого события, поэтому HUD берёт его ОТТУДА (см. codeFor),
   * а таблица ниже — только запасной вариант для события без detail
   * (воспроизведение старой записи, чужой источник, мок без кода).
   *
   * Значения в таблице обязаны совпадать с EVENT_CODES ВИД В ВИД. Раньше это
   * была вторая, независимая таблица: HUD показывал студенту OBJ_201 там, где
   * в журнале лежит VIS_101 — расхождение по всем 32 видам. Апелляция по
   * названному с экрана коду не находила в журнале ничего, то есть единственный
   * заявленный механизм оспаривания (LIMITATIONS §5) не работал. Новый вид
   * события заводится СНАЧАЛА в EVENT_CODES, и тем же номером здесь.
   *
   * act — ТРЕТЬЯ часть формулы гайда: что человеку сделать прямо сейчас.
   * Ни одно действие не содержит обвинения: система описывает наблюдение.
   */
  var ADVICE = {
    PHONE_IN_FRAME: { code: 'VIS_101', act: 'Уберите телефон за пределы рабочего места.' },
    PHONE_RAISED: { code: 'VIS_102', act: 'Опустите телефон и уберите его со стола.' },
    PHONE_AIMED_AT_SCREEN: { code: 'VIS_103', act: 'Уберите телефон. Фрагмент записи сохранён для разбора экзаменатором.' },
    FORBIDDEN_OBJECT: { code: 'VIS_110', act: 'Освободите рабочее место от посторонних предметов.' },

    NO_FACE: { code: 'VIS_120', act: 'Сядьте так, чтобы лицо полностью попадало в кадр.' },
    SECOND_FACE: { code: 'VIS_121', act: 'В кадре должны быть только вы — попросите остальных отойти.' },
    LIVENESS_FAIL: { code: 'IDN_302', act: 'Посмотрите в камеру и моргните: нужна повторная сверка.' },

    IDENTITY_MISMATCH: { code: 'IDN_301', act: 'Посмотрите прямо в камеру при обычном освещении для повторной сверки.' },

    GAZE_DOWN: { code: 'GAZE_201', act: 'Вернитесь взглядом к экрану.' },
    GAZE_SIDE: { code: 'GAZE_202', act: 'Вернитесь взглядом к экрану.' },
    GAZE_OFF_SCREEN: { code: 'GAZE_203', act: 'Работайте, глядя на этот экран.' },
    HEAD_TURNED: { code: 'GAZE_210', act: 'Разверните голову к экрану.' },

    VOICE_OTHER: { code: 'AUD_401', act: 'Если рядом разговаривают, попросите тишины.' },
    SPEECH_WITHOUT_LIP_MOTION: { code: 'AUD_402', act: 'Снимите наушники и гарнитуру.' },
    AUDIO_DEVICE_CONNECTED: { code: 'ENV_504', act: 'Отключите наушники или гарнитуру от компьютера.' },

    VIRTUAL_CAMERA: { code: 'ENV_501', act: 'Закройте программу виртуальной камеры и повторите проверку.' },
    REMOTE_ACCESS_SOFTWARE: { code: 'ENV_502', act: 'Закройте программу удалённого доступа.' },
    VIRTUAL_MACHINE: { code: 'ENV_503', act: 'Экзамен выполняется на физическом компьютере — обратитесь к организатору.' },
    SCREEN_RECORDING: { code: 'ENV_505', act: 'Остановите запись экрана.' },
    MULTIPLE_DISPLAYS: { code: 'ENV_506', act: 'Отключите второй монитор физически.' },
    BLACKLISTED_PROCESS: { code: 'ENV_507', act: 'Закройте эту программу и повторите проверку.' },

    WINDOW_BLUR: { code: 'SHL_601', act: 'Оставайтесь в окне экзамена до конца работы.' },
    FULLSCREEN_EXIT: { code: 'SHL_602', act: 'Вернитесь в полноэкранный режим.' },
    SHORTCUT_BLOCKED: { code: 'SHL_603', act: 'Горячие клавиши на время экзамена отключены — пользуйтесь кнопками на экране.' },
    CLIPBOARD_PASTE: { code: 'SHL_604', act: 'Набирайте ответ в поле ввода.' },
    DEVTOOLS_ATTEMPT: { code: 'SHL_605', act: 'Инструменты разработчика на время экзамена недоступны.' },

    PASTE_BURST: { code: 'FUS_701', act: 'Набирайте ответ своими словами.' },
    TYPING_ANOMALY: { code: 'FUS_702', act: 'Продолжайте работу в обычном темпе.' },

    FUSION_GAZE_THEN_ANSWER: { code: 'FUS_710', act: 'Эпизод отмечен для разбора экзаменатором. Продолжайте работу.' },
    FUSION_BLUR_THEN_ANSWER: { code: 'FUS_711', act: 'Эпизод отмечен для разбора экзаменатором. Продолжайте работу.' },
    FUSION_PHONE_THEN_ANSWER: { code: 'FUS_712', act: 'Эпизод отмечен для разбора экзаменатором. Продолжайте работу.' },

    SENSOR_LOST: { code: 'SYS_910', act: 'Проверьте подключение камеры и микрофона.' },

    // Режим запуска оболочки — не инцидент студента, а условия наблюдения.
    // Код обязан быть и здесь: без него запись уходила бы на экран как
    // OBS_000, то есть под кодом, которого в журнале нет вообще.
    SHELL_CONFIG: { code: 'SHL_600', act: 'Условия запуска зафиксированы. Продолжайте работу.' }
  };

  /** Какому индикатору принадлежит инцидент. */
  var KIND_CHANNEL = {
    PHONE_IN_FRAME: 'phone', PHONE_RAISED: 'phone', PHONE_AIMED_AT_SCREEN: 'phone',
    FORBIDDEN_OBJECT: 'phone',
    NO_FACE: 'face', SECOND_FACE: 'face', LIVENESS_FAIL: 'face',
    IDENTITY_MISMATCH: 'identity',
    GAZE_DOWN: 'gaze', GAZE_SIDE: 'gaze', GAZE_OFF_SCREEN: 'gaze', HEAD_TURNED: 'gaze',
    VOICE_OTHER: 'audio', SPEECH_WITHOUT_LIP_MOTION: 'audio', AUDIO_DEVICE_CONNECTED: 'audio',
    VIRTUAL_CAMERA: 'env', REMOTE_ACCESS_SOFTWARE: 'env', VIRTUAL_MACHINE: 'env',
    SCREEN_RECORDING: 'env', MULTIPLE_DISPLAYS: 'env', BLACKLISTED_PROCESS: 'env',
    WINDOW_BLUR: 'env', FULLSCREEN_EXIT: 'env', SHORTCUT_BLOCKED: 'env',
    CLIPBOARD_PASTE: 'env', DEVTOOLS_ATTEMPT: 'env',
    SENSOR_LOST: 'env'
  };

  /** Инциденты окружения сами не «рассасываются» — держим подсветку канала. */
  var STICKY_KINDS = {
    VIRTUAL_CAMERA: 1, REMOTE_ACCESS_SOFTWARE: 1, VIRTUAL_MACHINE: 1,
    SCREEN_RECORDING: 1, MULTIPLE_DISPLAYS: 1, BLACKLISTED_PROCESS: 1,
    AUDIO_DEVICE_CONNECTED: 1
  };

  /** Служебные события инцидентами не считаются. */
  var SERVICE_KINDS = { SESSION_STARTED: 1, SESSION_ENDED: 1, CALIBRATION_DONE: 1 };

  /* ------------------------------------------- попытка выйти за правила ----
   * Переход за пределы белого списка — САМОЕ ЦЕННОЕ наблюдение профиля:
   * не запрет, а доказательство попытки. Поэтому его надо узнавать в потоке
   * и показывать отдельно от «нажата заблокированная горячая клавиша».
   *
   * ТРИ РАЗНЫЕ ВЕЩИ, которые нельзя смешивать (и протокол их не смешивает,
   * см. EventKind и RISK_WEIGHTS в sidecar/protocol.py):
   *
   *   1. NAVIGATION_OFF_PROFILE (вес 20) — переход, который ИНИЦИИРОВАЛ
   *      человек. Это и есть улика, о ней студенту говорим сразу.
   *   2. SUBRESOURCE_OFF_PROFILE (вес 2) — подзапрос, который сделала САМА
   *      СТРАНИЦА: шрифт с CDN, счётчик, картинка. Это почти всегда техника
   *      чужой страницы, поэтому ни сообщения, ни формулировки «вы перешли»
   *      здесь быть не может — иначе обвинение приходит из устройства сайта,
   *      а не из действия человека.
   *   3. EXAM_PROFILE_APPLIED / _ABSENT / _SEARCH_ALLOWED (вес 0) — УСЛОВИЯ
   *      экзамена. Из них мы берём сами правила и показываем их в панели, но
   *      в число инцидентов не записываем.
   *
   * Запасные имена в NAV_KINDS и разбор SHORTCUT_BLOCKED оставлены сознательно:
   * оболочка отбивает навигацию ещё и в hardenWebContents, где событие уходит
   * как SHORTCUT_BLOCKED с detail {combo:'NAVIGATION', reason:'navigation_denied',
   * url}. Эта ветка работает и без профиля, и терять её нельзя.
   *
   * НОВЫХ КЛЮЧЕЙ В ADVICE ЗДЕСЬ НЕТ СОЗНАТЕЛЬНО. `make test` сверяет таблицу
   * ADVICE с каноническим EVENT_CODES (sidecar/engine/events.py) вид в вид и
   * падает на ключе, которого в реестре нет, — а кодов для новых видов
   * профиля в реестре пока нет. Код для показа берётся из detail.code через
   * codeFor(); запасной SHL_603 есть в реестре. Когда EVENT_CODES пополнится,
   * ADVICE пополнит тот же коммит — не раньше.
   */
  var NAV_KINDS = {
    NAVIGATION_OFF_PROFILE: 1,
    NAVIGATION_BLOCKED: 1, NAVIGATION_DENIED: 1, BLOCKED_NAVIGATION: 1,
    URL_BLOCKED: 1, URL_NOT_ALLOWED: 1, OUT_OF_SCOPE_NAVIGATION: 1,
    PROFILE_VIOLATION: 1, EXAM_PROFILE_VIOLATION: 1
  };

  /** Подзапрос страницы вне списка: действие сайта, а не человека. */
  var SUBRES_KINDS = { SUBRESOURCE_OFF_PROFILE: 1, SUBRESOURCE_BLOCKED: 1 };

  /** Состояние правил: условия экзамена, вес 0, инцидентом не считается. */
  var PROFILE_STATE_KINDS = {
    EXAM_PROFILE_APPLIED: 1, EXAM_PROFILE_ABSENT: 1, EXAM_PROFILE_SEARCH_ALLOWED: 1
  };

  var NAV_COMBOS = { NAVIGATION: 1, WINDOW_OPEN: 1, REDIRECT: 1, NEW_WINDOW: 1 };

  var NAV_REASON_RE = /navigat|window_open|new_window|redirect|allowlist|not_allowed|out_of_scope|profile/i;

  /**
   * Каналы наблюдения. cap — ключ из hello.capabilities (docs/CONTRACT.md).
   * Порядок: от самого наглядного к служебному.
   *
   * Последний — «Профиль экзамена»: cap у него НЕТ, потому что это не датчик
   * сайдкара, а правила, которые задал проктор (какой адрес открыт и какие
   * источники разрешены). Он стоит в том же списке и по тем же правилам
   * дизайн-системы — точка, фигура и подпись СЛОВОМ, — потому что студент
   * обязан видеть не только то, что за ним смотрят, но и то, под какими
   * правилами он сдаёт. Профиль занимает всю ширину строки (.chan--profile):
   * в нём живёт хеш, а он не влезает в половину панели.
   */
  var CHANNELS = [
    { id: 'face',     name: 'Лицо',      cap: 'vision' },
    { id: 'gaze',     name: 'Взгляд',    cap: 'gaze' },
    { id: 'phone',    name: 'Телефон',   cap: 'vision' },
    { id: 'identity', name: 'Личность',  cap: 'identity' },
    { id: 'env',      name: 'Окружение', cap: 'env' },
    { id: 'audio',    name: 'Звук',      cap: 'audio' },
    { id: 'profile',  name: 'Профиль экзамена', cap: null }
  ];

  var ZONE_TEXT = {
    center: 'в центре экрана', screen: 'на экране', up: 'вверх', down: 'вниз',
    left: 'влево', right: 'вправо', side: 'в сторону',
    off: 'вне экрана', off_screen: 'вне экрана', unknown: 'нет данных'
  };

  var STATE_TEXT = {
    idle: 'сессия не начата',
    calibrating: 'калибровка',
    running: 'экзамен идёт',
    paused: 'пауза',
    locked: 'сессия закрыта'
  };

  /** Состояние канала -> фигура и тон. Цвет дублируется формой и подписью. */
  var CHAN_STATE = {
    ok:   { shape: 'circle',   tone: 'var(--risk-ok)' },
    warn: { shape: 'triangle', tone: 'var(--risk-warn)' },
    bad:  { shape: 'diamond',  tone: 'var(--risk-lock)' },
    off:  { shape: 'off',      tone: 'var(--gray-500)' }
  };

  /* ------------------------------------------------------------- ограничения */

  var MAX_FEED = 24;        // узлов в ленте: два часа экзамена не растят DOM
  var MAX_EVENTS = 800;     // инцидентов в памяти для отчёта
  var MAX_ALERTS = 3;       // одновременно видимых .alert
  var ALERT_MS = 9000;      // сколько держится неблокирующее сообщение
  var ALERT_GAP_MS = 2500;  // не чаще одного нового сообщения
  var ALERT_DEDUP_MS = 12000; // один и тот же вид инцидента не повторяем
  var HIGHLIGHT_MS = 6000;  // сколько держится подсветка канала инцидентом

  var RING_R = 42;                        // радиус кольца в разметке viewBox 0 0 100 100
  var RING_LEN = 2 * Math.PI * RING_R;    // длина окружности для stroke-dashoffset

  /* ----------------------------------------------- запасные правила вёрстки */

  /**
   * Запасные правила для узлов, которые создаёт ЭТОТ файл: лента по паттерну
   * .event, неблокирующие .alert, фигуры-индикаторы, бейдж уровня.
   *
   * Селекторы завёрнуты в :where() — нулевая специфичность, любое правило из
   * styles.css их перебивает. Значения — переменные tokens.css; второй аргумент
   * var() задан только для размеров (страховка, если tokens.css не подключён).
   * Цвета без запасных значений: хардкод палитры запрещён.
   */
  var FALLBACK_CSS = [
    // фигуры: второй, нецветовой носитель состояния
    ':where(.chan__shape){display:inline-flex;align-items:center;margin-left:auto;flex:none;}',
    ':where(.chan__shape) svg,:where(.badge) svg{display:block;width:12px;height:12px;}',
    // бейдж уровня риска и статусов
    ':where(.badge){display:inline-flex;gap:var(--space-2,8px);align-items:center;' +
      'padding:4px var(--space-2,8px);border:1px solid var(--border-hair-dark);' +
      'border-radius:99px;font-family:var(--font-mono,monospace);font-size:var(--fs-label,13px);}',
    // лента инцидентов: точка / факт и пояснение / время справа
    ':where(.feed)>:where(.event){display:grid;' +
      'grid-template-columns:8px minmax(0,1fr) auto;gap:var(--space-3,12px);' +
      'align-items:start;padding:var(--space-3,12px) 0;' +
      'border-top:1px solid var(--border-hair-dark);font-size:var(--fs-small,14px);' +
      'line-height:var(--lh-body,1.55);}',
    ':where(.event)>i{width:8px;height:8px;margin-top:0.45em;border-radius:50%;background:var(--tone);}',
    ':where(.event__body) b{font-weight:600;}',
    ':where(.event__time){white-space:nowrap;font-size:var(--fs-label,13px);}',
    // неблокирующие сообщения
    // Полоса верхней панели, а не угол: во всех углах окна стоят органы
    // управления (главная кнопка, галочка согласия), и карточка их накрывала —
    // живой запуск 08.10. Указатель сообщение не принимает вовсе, кроме «✕»:
    // неблокирующее сообщение не должно блокировать нажатия.
    ':where(.alerts),:where(.toasts){position:fixed;top:var(--space-2,8px);' +
      'right:var(--space-6,24px);' +
      'z-index:40;display:flex;flex-direction:column;gap:var(--space-3,12px);' +
      'max-width:min(440px,calc(100vw - 48px));pointer-events:none;}',
    ':where(.alerts)>*,:where(.toasts)>*{pointer-events:none;}',
    ':where(.alerts) :where(.alert__close),:where(.toasts) :where(.alert__close)' +
      '{pointer-events:auto;}',
    ':where(.hud__alerts){flex:none;display:flex;flex-direction:column;' +
      'gap:var(--space-2,8px);padding:var(--space-3,12px) var(--space-4,16px) 0;}',
    ':where(.hud__alerts):empty{display:none;}',
    ':where(.alert--toast){display:grid;grid-template-columns:5px minmax(0,1fr) auto auto;' +
      'gap:var(--space-3,12px);align-items:start;padding:var(--space-4,16px);' +
      'border:1px solid var(--border-hair-dark);border-radius:var(--radius-sm,8px);' +
      'color:var(--text-on-dark);background:var(--bg-panel);' +
      'transition:opacity var(--transition,0.16s ease);}',
    ':where(.alert--toast) :where(.alert-bar){width:5px;min-height:44px;align-self:stretch;' +
      'border-radius:var(--radius-xs,4px);background:var(--tone);}',
    ':where(.alert--toast) strong{display:block;margin-bottom:2px;font-size:var(--fs-small,14px);}',
    ':where(.alert--toast) p{margin:0;font-size:var(--fs-small,14px);color:var(--text-muted-on-dark);}',
    ':where(.alert--toast) code{font-size:var(--fs-label,13px);color:var(--text-muted-on-dark);}',
    ':where(.alert--toast).is-out{opacity:0;}',
    // зона нажатия кнопки закрытия — не меньше 44x44 (правило 6)
    ':where(.alert__close){min-width:var(--tap-min,44px);min-height:var(--tap-min,44px);' +
      'display:grid;place-items:center;border:0;border-radius:var(--radius-xs,4px);' +
      'color:inherit;background:transparent;}',
    // состояние защищённого режима: студент обязан видеть, что с его машиной
    ':where(.hud__guard){display:grid;gap:var(--space-2,8px);' +
      'padding:var(--space-3,12px) 0;border-bottom:1px solid var(--border-hair-dark);}',
    ':where(.hud__guardrow){display:flex;gap:var(--space-2,8px);align-items:center;}',
    ':where(.hud__guardmark){width:10px;height:10px;flex:none;border-radius:2px;' +
      'border:1px solid currentColor;}',
    ':where(.hud__guard).is-on :where(.hud__guardmark){background:currentColor;}',
    ':where(.hud__guardname){font-size:var(--fs-small,14px);font-weight:600;}',
    ':where(.hud__guardwhy){margin:0;font-size:var(--fs-label,13px);' +
      'line-height:var(--lh-body,1.55);color:var(--text-muted-on-dark);}',
    ':where(.hud__guardkeys){margin:0;font-family:var(--font-mono,monospace);' +
      'font-size:var(--fs-label,13px);color:var(--text-muted-on-dark);' +
      'overflow-wrap:anywhere;}',
    // профиль экзамена: своя строка во всю ширину — в ней живёт хеш
    ':where(.chan--profile){grid-column:1 / -1;}',
    ':where(.chan__id){font-family:var(--font-mono,monospace);overflow-wrap:anywhere;}',
    // полный адрес попытки перехода в журнале панели
    ':where(.event__url){display:block;margin-top:2px;font-family:var(--font-mono,monospace);' +
      'font-size:var(--fs-label,13px);color:var(--text-muted-on-dark);overflow-wrap:anywhere;}'
  ].join('\n');

  function installFallbackStyles() {
    if (document.getElementById('ds-fallback-hud')) return;
    try {
      var style = document.createElement('style');
      style.id = 'ds-fallback-hud';
      style.textContent = FALLBACK_CSS;
      document.head.appendChild(style);
    } catch (e) { /* без запасных правил панель всё равно читается */ }
  }

  /* --------------------------------------------------------------- утилиты */

  function el(id) { return document.getElementById(id); }

  function reducedMotion() {
    try {
      return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    } catch (e) { return false; }
  }

  function escapeHtml(s) {
    return String(s === undefined || s === null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function pad2(n) { return n < 10 ? '0' + n : String(n); }

  /**
   * Нормализация счётчика повторов из breakdown. Контракт обещает число, но
   * проверка типа была только у contribution: при рассинхроне версий или кривом
   * JSON в подпись уезжало «×undefined» либо чужая разметка.
   */
  function normCount(v) {
    var n = typeof v === 'number' ? v : parseInt(v, 10);
    if (!isFinite(n) || n < 1) return 1;
    return Math.round(n);
  }

  function hhmmss(tsSec) {
    var d = (typeof tsSec === 'number' && isFinite(tsSec)) ? new Date(tsSec * 1000) : new Date();
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  /**
   * Инлайновая фигура-индикатор. Внешних ресурсов нет (CSP режет сеть),
   * эмодзи не используются — только геометрия. Цвет берётся из --tone.
   */
  function glyph(shape) {
    var inner;
    if (shape === 'triangle') inner = '<path d="M6 1.1 11.3 10.9H0.7Z"/>';
    else if (shape === 'square') inner = '<rect x="1.7" y="1.7" width="8.6" height="8.6" rx="1.2"/>';
    else if (shape === 'diamond') inner = '<path d="M6 0.7 11.3 6 6 11.3 0.7 6Z"/>';
    else if (shape === 'octa') inner = '<path d="M4.1 0.9h3.8L11.1 4.1v3.8L7.9 11.1H4.1L0.9 7.9V4.1Z"/>';
    else if (shape === 'off') {
      inner = '<circle cx="6" cy="6" r="4.2" style="fill:none;stroke:var(--tone,currentColor)" stroke-width="1.5"/>' +
              '<path d="M3.1 8.9 8.9 3.1" style="fill:none;stroke:var(--tone,currentColor)" stroke-width="1.5"/>';
    } else inner = '<circle cx="6" cy="6" r="4.4"/>';
    return '<svg class="glyph" viewBox="0 0 12 12" width="12" height="12" aria-hidden="true" ' +
           'focusable="false" style="fill:var(--tone,currentColor)">' + inner + '</svg>';
  }

  function levelOf(score) {
    var s = (typeof score === 'number' && isFinite(score)) ? score : 0;
    for (var i = 0; i < LEVELS.length; i++) {
      if (s >= LEVELS[i].min) return LEVELS[i];
    }
    return LEVELS[LEVELS.length - 1];
  }

  function colorFor(score) { return levelOf(score).tone; }

  function severityOf(ev) {
    var sev = ev && ev.severity;
    return SEV_TONE[sev] ? sev : 'medium';
  }

  function eventText(ev) {
    /*
     * Попытка перехода разбирается ДО ev.message, и это не произвол.
     * Оболочка отбивает навигацию видом SHORTCUT_BLOCKED (shell/main.js), а
     * сайдкар подписывает такой вид общей строкой «Горячая клавиша
     * заблокирована». Для перехода на сторонний адрес это не просто неточно:
     * студент и комиссия читали бы в журнале про клавиатуру там, где в
     * действительности зафиксирована попытка уйти с экзамена. Распознаватель
     * срабатывает только когда в detail есть навигационный признак, поэтому
     * обычное событие о клавише своей формулировки не теряет.
     */
    if (isNavBlock(ev)) return navFact(ev);
    if (isSubresBlock(ev)) return subresFact(ev);
    if (ev && typeof ev.message === 'string' && ev.message.trim()) return ev.message.trim();
    return (ev && EVENT_TEXT[ev.kind]) || (ev && ev.kind) || 'Наблюдение';
  }

  function shortFor(ev) {
    if (isProfileScope(ev)) {
      var host = navTarget(ev).host;
      if (isSubresBlock(ev)) return host ? 'запрос страницы: ' + host : 'запрос вне правил';
      return host ? 'переход: ' + host : 'переход не разрешён';
    }
    var s = EVENT_SHORT[ev.kind];
    if (s) return s;
    var t = eventText(ev);
    return t.length > 28 ? t.slice(0, 27) + '…' : t;
  }

  function adviceFor(kind) {
    return ADVICE[kind] || { code: 'OBS_000', act: 'Продолжайте работу. Эпизод отмечен для разбора экзаменатором.' };
  }

  /**
   * Код инцидента ДЛЯ ПОКАЗА СТУДЕНТУ — ровно тот, что лёг в журнал.
   *
   * Источник истины один: `detail.code`, который сайдкар проставляет каждому
   * событию из канонического EVENT_CODES (`engine/events.py`) и который уходит
   * в хеш-цепочку и в отчёт. Студент называет этот код организатору, организатор
   * ищет его в журнале — значит показать можно только его, иначе апелляция
   * ссылается на код, которого в записи нет.
   *
   * Таблица ADVICE остаётся запасным вариантом для события без detail:
   * воспроизведение старой записи, чужой источник, мок. Её значения совпадают
   * с EVENT_CODES, так что разойтись эти две ветки уже не могут.
   *
   * Формат проверяется: detail приходит по сокету, и подставить в код разметку
   * или строку на пол-экрана источник не должен. Не подошло под формат —
   * откатываемся на таблицу, а не показываем что прислали.
   */
  var CODE_RE = /^[A-Z][A-Z0-9]{1,7}_[0-9]{3}$/;

  function codeFor(ev) {
    var fallback = adviceFor(ev && ev.kind).code;
    var raw = ev && ev.detail && ev.detail.code;
    if (typeof raw !== 'string') return fallback;
    raw = raw.trim();
    return CODE_RE.test(raw) ? raw : fallback;
  }

  /**
   * Контекст инцидента — ВТОРАЯ часть формулы гайда.
   * Только измеренные величины: длительность, уверенность, наличие записи.
   * Ничего, что сайдкар не присылал, здесь не появляется.
   */
  function contextFor(ev) {
    var parts = [];
    if (ev && typeof ev.duration === 'number' && ev.duration >= 0.1) {
      parts.push('длительность ' + ev.duration.toFixed(1) + ' с');
    }
    if (ev && typeof ev.confidence === 'number' && ev.confidence > 0 && ev.confidence < 1) {
      parts.push('уверенность ' + Math.round(ev.confidence * 100) + '%');
    }
    if (ev && ev.evidence && (ev.evidence.clip_path || ev.evidence.frame_path)) {
      parts.push('фрагмент записи сохранён');
    }
    if (!parts.length) return '';
    var s = parts.join(' · ');
    return s.charAt(0).toUpperCase() + s.slice(1) + '.';
  }

  /* =========================================================================
   * ПРОФИЛЬ ЭКЗАМЕНА: правила, под которыми человек сдаёт.
   *
   * Что это. Проктор задаёт КОНКРЕТНЫЕ источники на КОНКРЕТНЫЙ экзамен: какой
   * адрес открыт и какие адреса разрешены вдобавок. Ни фильтр по домену, ни
   * класс доменов целиком списком не годятся: .edu с 2001 года выдаётся только
   * вузам США, казахстанские вузы живут на .edu.kz (ksu.edu.kz), а под .edu.kz
   * целиком попадают форумы, файлообменники и Moodle других вузов с теми же
   * курсами. Поисковые системы в списке по умолчанию отсутствуют: выдача
   * показывает готовый ответ прямо на странице, переходить никуда не нужно.
   *
   * ЧТО ЭТО НЕ ТАКОЕ. Профиль — улика, а не защита. Нигде в интерфейсе не
   * утверждается, что его нельзя обойти: ценность в том, что каждая попытка
   * выхода за список попадает в журнал с ПОЛНЫМ адресом, а хеш действовавшего
   * профиля идёт в подписанную цепочку сессии. Урок Safe Exam Browser ровно
   * про это: подпись у .seb была, а Config Key в Moodle включается отдельно и
   * по умолчанию выключен — проверять подпись было некому. Отказ был
   * организационным, не техническим.
   *
   * ОТКУДА БЕРЁМ ДАННЫЕ. Поля в window.proctor добавляет оболочка, и писались
   * они параллельно с этим файлом. Поэтому: читаем через опциональный доступ,
   * принимаем несколько написаний каждого поля (camelCase и snake_case,
   * объект и функция, значение и Promise) и обязаны работать, когда профиля
   * нет вообще. Пустой профиль = прежнее поведение: локальный мок-тест.
   * ========================================================================= */

  /** Первое непустое строковое значение из перечисленных ключей. */
  function pickStr(src, keys) {
    for (var i = 0; i < keys.length; i++) {
      var v = src[keys[i]];
      if (typeof v === 'string' && v.trim()) return v.trim();
    }
    return '';
  }

  /** Первое настоящее булево значение; null — «не сказано». */
  function pickBool(src, keys) {
    for (var i = 0; i < keys.length; i++) {
      var v = src[keys[i]];
      if (typeof v === 'boolean') return v;
    }
    return null;
  }

  /**
   * Хост из адреса или из записи белого списка. Запись может быть и голым
   * доменом (`moodle.ksu.edu.kz`), и шаблоном (`*.ksu.edu.kz`), и полным
   * адресом с путём — URL() такое съедает не всегда, поэтому есть запасной
   * разбор строкой.
   */
  function hostOf(value) {
    var s = String(value === undefined || value === null ? '' : value).trim();
    if (!s) return '';
    /*
     * У локальной схемы хоста нет, и выдумывать его нельзя. У file:///app/mock
     * запасной разбор строкой вытаскивал «app» — первый сегмент ПУТИ, — и на
     * экране появлялся домен, которого не существует. На демо это ровно тот
     * случай, который мы показываем: профиль пуст, открыт локальный мок-тест.
     */
    if (/^(file|about|data|blob):/i.test(s)) return '';
    if (/^[a-z][a-z0-9+.-]*:\/\//i.test(s)) {
      try {
        var u = new URL(s);
        if (u.host) return u.host;
      } catch (e) { /* разберём строкой ниже */ }
    }
    var m = /^([*a-z0-9.:_-]+)/i.exec(s.replace(/^[a-z][a-z0-9+.-]*:\/*/i, ''));
    return m ? m[1].replace(/[.:]+$/, '') : '';
  }

  /** Локальный ли адрес: его открывают через loadFile, сети при этом нет. */
  function isLocalUrl(url) {
    if (!url) return true;
    return /^(file|about|data|blob):/i.test(url);
  }

  /**
   * Хеш профиля в читаемом виде. Хеш — идентификатор, поэтому на экране он
   * всегда моноширинным (правило 5 дизайн-системы), а здесь только режется:
   * 64 шестнадцатеричных знака в строку индикатора не влезают и не нужны —
   * сверяют их по подписанному отчёту, а не глазами с экрана.
   */
  function shortHash(hash, len) {
    var s = String(hash || '').replace(/^sha(?:-?256)?[:=]/i, '').trim();
    if (!s) return '';
    var n = len || 12;
    return s.length > n ? s.slice(0, n) : s;
  }

  /** Есть ли в объекте хоть одно поле, по которому его можно счесть профилем. */
  function looksLikeProfile(o) {
    if (!o || typeof o !== 'object' || Array.isArray(o)) return false;
    var keys = ['applied', 'hash', 'profile_hash', 'profileHash', 'coreHash',
                'url', 'entry', 'entry_url', 'entryUrl', 'exam_url', 'examUrl',
                'examOrigin', 'origins', 'allowed_origins', 'allowedOrigins',
                'sources', 'allow', 'allowed', 'allowlist', 'allow_list',
                'allowed_sources', 'allowedSources', 'domains', 'hosts'];
    for (var i = 0; i < keys.length; i++) {
      if (Object.prototype.hasOwnProperty.call(o, keys[i])) return true;
    }
    return false;
  }

  /**
   * Белый список -> [{host, note, full, wildcard}]. Принимаем массив строк,
   * массив объектов и карту. Ведущее имя поля — `origins`: так его зовёт
   * оболочка (shell/state.js, normalizeExamProfile), и записи приходят в виде
   * `moodle.ksu.edu.kz` либо `moodle.ksu.edu.kz/*` — со звёздочкой, когда
   * разрешены и поддомены. Исходную строку сохраняем целиком: `/*` меняет
   * СМЫСЛ записи, и обрезать его до хоста нельзя.
   */
  function normalizeSources(src) {
    var keys = ['origins', 'allowed_origins', 'allowedOrigins',
                'sources', 'allowed_sources', 'allowedSources', 'allowlist',
                'allow_list', 'allowed', 'allow', 'domains', 'hosts', 'whitelist'];
    var raw = null;
    for (var i = 0; i < keys.length && raw === null; i++) {
      var v = src[keys[i]];
      if (Array.isArray(v) && v.length) raw = v;
      else if (v && typeof v === 'object' && !Array.isArray(v)) raw = v;
    }
    var out = [];
    var seen = {};
    var push = function (host, note, full) {
      var h = hostOf(host);
      if (!h || seen[h]) return;
      seen[h] = 1;
      var raw = String(full || host || '').trim();
      out.push({
        host: h,
        note: String(note || '').trim(),
        full: raw,
        // «и поддомены» — слово вместо звёздочки: /*  и  *.host это одно и то же
        wildcard: /\/\*\s*$/.test(raw) || /^\*\./.test(raw)
      });
    };

    if (Array.isArray(raw)) {
      for (var j = 0; j < raw.length && out.length < 24; j++) {
        var item = raw[j];
        if (typeof item === 'string') push(item, '', item);
        else if (item && typeof item === 'object') {
          var h = pickStr(item, ['host', 'domain', 'origin', 'url', 'pattern', 'match', 'name']);
          push(h, pickStr(item, ['note', 'title', 'label', 'comment', 'why', 'purpose']), h);
        }
      }
    } else if (raw && typeof raw === 'object') {
      var names = Object.keys(raw);
      for (var k = 0; k < names.length && out.length < 24; k++) {
        var val = raw[names[k]];
        push(names[k], typeof val === 'string' ? val : '', names[k]);
      }
    }
    return out;
  }

  /** Отклонённые записи белого списка -> [{raw, text}]. */
  function normalizeRejected(src) {
    var raw = src.rejected || src.rejected_origins || src.rejectedOrigins;
    if (!Array.isArray(raw)) return [];
    var out = [];
    for (var i = 0; i < raw.length && out.length < 16; i++) {
      var r = raw[i];
      if (typeof r === 'string') { out.push({ raw: r, text: '' }); continue; }
      if (!r || typeof r !== 'object') continue;
      var what = pickStr(r, ['raw', 'origin', 'host', 'value', 'entry']);
      if (!what) continue;
      out.push({ raw: what, text: pickStr(r, ['text', 'reason_text', 'message', 'reason']) });
    }
    return out;
  }

  /** Пустой профиль: правила не задавались. Прежнее поведение, локальный тест. */
  function emptyProfile() {
    return {
      present: false,
      hash: '', hashShort: '',
      url: '', host: '', local: true,
      sources: [],
      rejected: [],         // записи проктора, которые оболочка не приняла
      allowSearch: false,   // по умолчанию поисковики ЗАПРЕЩЕНЫ
      searchKnown: false,   // профиль об этом ничего не сказал
      allowSearchBy: '',    // чем разрешены: «профиль» или «--allow-search»
      broad: false,         // в списке был класс доменов, а не источник
      /*
       * Встал ли фильтр источников. ТРЁХЗНАЧНОЕ: null — страницу экзамена ещё
       * не открывали. Без этого признака кольцо канала показывало «поисковики
       * запрещены» и хеш профиля по сессии, в которой фильтра не существовало:
       * профиль без адреса экзамена оболочка применяет (present=true), но
       * BrowserView не создаёт и onBeforeRequest не ставит. Студент должен
       * видеть, что правила к его обращениям НЕ применяются, — иначе экран и
       * подписанный отчёт говорили бы разное об одной сессии.
       */
      filterInstalled: null,
      rulesWithoutUrl: false,   // правила есть, адреса экзамена нет
      preset: '', title: '', source: ''
    };
  }

  /**
   * Привести что угодно к профилю. Любая форма конверта, любое написание полей,
   * любой мусор на входе — на выходе всегда один и тот же объект.
   */
  function normalizeProfile(raw) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return emptyProfile();

    var src = raw;
    var inner = raw.examProfile || raw.exam_profile || raw.profile || raw.examRules
      || raw.exam_rules || raw.rules;
    if (looksLikeProfile(inner)) src = inner;
    else if (!looksLikeProfile(src)) return emptyProfile();

    var p = emptyProfile();
    p.hash = pickStr(src, ['hash', 'profile_hash', 'profileHash', 'coreHash',
                           'core_hash', 'sha256', 'digest', 'fingerprint']);
    p.url = pickStr(src, ['examUrl', 'exam_url', 'url', 'entry', 'entry_url', 'entryUrl',
                          'start_url', 'startUrl', 'lms_url', 'lmsUrl', 'target']);
    p.title = pickStr(src, ['label', 'title', 'name', 'presetLabel', 'preset_label',
                            'institution', 'exam', 'exam_title', 'examTitle']);
    p.preset = pickStr(src, ['preset', 'institution', 'university', 'org', 'domain']);
    p.source = pickStr(src, ['source', 'path', 'file', 'profile_path', 'profilePath', 'config']);
    p.sources = normalizeSources(src);
    p.rejected = normalizeRejected(src);
    p.broad = pickBool(src, ['broad', 'too_broad', 'tooBroad']) === true;
    p.filterInstalled = pickBool(src, ['filterInstalled', 'filter_installed']);
    p.rulesWithoutUrl = pickBool(src, ['rulesWithoutUrl', 'rules_without_url']) === true;

    var search = pickBool(src, ['allowSearch', 'allow_search', 'searchAllowed',
                                'search_allowed', 'search', 'searchEngines', 'search_engines']);
    p.searchKnown = search !== null;
    p.allowSearch = search === true;
    p.allowSearchBy = pickStr(src, ['allowSearchBy', 'allow_search_by']);

    // hashShort оболочка считает сама (examProfileInfo), и резать уже
    // сокращённый хеш второй раз незачем.
    p.hashShort = pickStr(src, ['hashShort', 'hash_short']) || shortHash(p.hash, 12);
    p.host = hostOf(p.url) || hostOf(pickStr(src, ['examOrigin', 'exam_origin', 'origin']));
    p.local = isLocalUrl(p.url);

    /*
     * Применён ли профиль, оболочка говорит ПРЯМО полем `applied`
     * (shell/state.js: normalizeExamProfile). Это ведущий признак, и он важнее
     * любой нашей догадки: профиль может быть прочитан, но отвергнут целиком
     * (битый адрес, слишком широкий список) — тогда applied=false, и писать
     * студенту «действует профиль» было бы неправдой. Догадка по содержанию
     * остаётся только для источников, которые про applied не сказали.
     */
    var applied = pickBool(src, ['applied', 'present', 'enabled', 'active']);
    p.present = (applied === null)
      ? !!(p.hash || p.url || p.sources.length)
      : (applied === true);
    if (src.empty === true) p.present = false;
    return p;
  }

  /** Подпись профиля: по ней видно, менялось ли что-то, без сравнения объектов. */
  function profileSig(p) {
    var hosts = [];
    for (var i = 0; i < p.sources.length; i++) hosts.push(p.sources[i].full || p.sources[i].host);
    // Состояние фильтра входит в подпись: без него смена «фильтр встал» ->
    // «фильтр не встал» не перерисовала бы канал, и студент продолжал бы
    // видеть прежнюю строку.
    return [p.present, p.hash, p.url, p.allowSearch, p.searchKnown,
            p.allowSearchBy, p.filterInstalled, p.rulesWithoutUrl,
            p.rejected.length, hosts.join(',')].join('|');
  }

  /* ---------------------------------------- распознавание попытки перехода */

  /** Это попытка уйти за пределы правил экзамена, а не нажатая горячая клавиша? */
  function isNavBlock(ev) {
    if (!ev || typeof ev !== 'object') return false;
    var kind = String(ev.kind || '');
    if (NAV_KINDS[kind]) return true;
    if (kind !== 'SHORTCUT_BLOCKED') return false;
    var d = ev.detail;
    if (!d || typeof d !== 'object') return false;
    if (NAV_COMBOS[String(d.combo || '').toUpperCase()]) return true;
    return NAV_REASON_RE.test(String(d.reason || ''));
  }

  /** Подзапрос страницы вне списка. Сообщения по нему НЕ показываем. */
  function isSubresBlock(ev) {
    return !!(ev && SUBRES_KINDS[String(ev.kind || '')]);
  }

  /** Относится ли событие к каналу «Профиль экзамена». */
  function isProfileScope(ev) {
    return isNavBlock(ev) || isSubresBlock(ev);
  }

  /**
   * Куда пытались уйти. Полный адрес нужен в журнале (это и есть
   * доказательство попытки), хост — в коротком сообщении на экране.
   */
  function navTarget(ev) {
    var d = (ev && ev.detail && typeof ev.detail === 'object') ? ev.detail : {};
    var url = pickStr(d, ['url', 'target', 'href', 'location', 'to']) ||
              pickStr(ev || {}, ['url', 'target']);
    return { url: url, host: hostOf(url) };
  }

  /** Факт попытки — первая часть формулы сообщения. Без обвинений. */
  function navFact(ev) {
    var host = navTarget(ev).host;
    return host
      ? 'Переход на ' + host + ' не разрешён правилами этого экзамена'
      : 'Переход за пределы правил этого экзамена не разрешён';
  }

  /**
   * Факт подзапроса. Подлежащее здесь — СТРАНИЦА, и это не стилистика:
   * шрифт, картинку или счётчик со стороннего адреса запрашивает сайт, а не
   * человек за столом. Вес 2 в protocol.py поставлен из того же соображения.
   */
  function subresFact(ev) {
    var host = navTarget(ev).host;
    return host
      ? 'Страница запросила ' + host + ' — источник вне правил экзамена'
      : 'Страница обратилась к источнику вне правил экзамена';
  }

  /** Создать элемент с классом и набором атрибутов. */
  function mk(tag, cls, attrs) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (attrs) {
      for (var k in attrs) {
        if (Object.prototype.hasOwnProperty.call(attrs, k)) node.setAttribute(k, attrs[k]);
      }
    }
    return node;
  }

  /**
   * Найти элемент по id или создать его в parent.
   *
   * Разметку index.html параллельно переписывает другой агент: модуль обязан
   * работать и с его вариантом, и без него. Поэтому ничего не предполагаем —
   * используем, что есть, и досоздаём, чего нет.
   */
  function ensure(id, tag, cls, parent, attrs) {
    var node = el(id);
    if (node) return node;
    if (!parent) return null;
    node = mk(tag, cls, attrs);
    node.id = id;
    parent.appendChild(node);
    return node;
  }

  // =========================================================================
  // HUD
  // =========================================================================

  function Hud() {
    this.dom = {};
    this.caps = { vision: true, gaze: true, identity: true, audio: true, env: true };
    this.mode = null;            // 'classroom' | 'remote' | null (неизвестно)
    this.score = 0;
    this.lastStatus = null;
    this.lastBreakdown = [];
    this._riskFromRiskMsg = false;  // пришло risk — поле risk из status больше не ведущее
    this.events = [];
    this._dropped = 0;
    this.highlights = {};
    this.verdict = 'none';
    this.sessionId = '—';
    this.incidentCode = null;
    //: Приостановка ждёт решения человека (`review_required` вердикта).
    //: Пока true, студент не может снять паузу сам ни при каком риске.
    this.reviewRequired = false;
    //: Описание запроса решения (`review` вердикта): объяснение, код сессии.
    this.review = null;
    //: Кто принял решение — ставится из `decided_by`. Экран блокировки обязан
    //: называть инициатора: до него доходят только решением человека.
    this.decidedBy = '';
    this.protection = null;      // состояние защищённого режима из оболочки
    this._guardSig = null;       // подпись показанного состояния, против лишних записей в DOM
    //: Профиль экзамена: какой адрес открыт и какие источники разрешил проктор.
    //: Пустой — значит правила не задавались и открыт только локальный тест.
    this.profile = emptyProfile();
    this._profileSig = profileSig(this.profile);
    this._profileSubs = [];      // кому сообщить о смене правил (экраны в app.js)

    this.onPause = null;
    this.onResume = null;
    this.onLock = null;
    this.onReport = null;

    this._queue = [];
    this._rafPending = false;
    this._rafId = 0;
    this._riskDirty = true;
    this._chanDirty = true;
    this._tick = null;
    this._chanCache = {};
    this._alerts = [];
    this._lastAlertAt = 0;
    this._lastAlertRank = -1;   // severity последнего ПОКАЗАННОГО сообщения
    this._alertSeen = {};
    this._pendingAlert = null;  // сообщение, отложенное троттлингом ALERT_GAP_MS
    this._alertRetry = null;    // таймер повторной попытки показа
    this._reduced = reducedMotion();
    this._mq = null;            // подписка на prefers-reduced-motion
    this._inited = false;
    this._focusBeforeOverlay = null;
  }

  /* ------------------------------------------------------------------ init */

  Hud.prototype.init = function (opts) {
    opts = opts || {};
    var self = this;

    /*
     * Идемпотентность. Второй init() раньше записывал новый setInterval поверх
     * прежнего дескриптора (остановить старый было уже нечем) и навешивал второй
     * click-слушатель на #hud-toggle — после чего classList.toggle срабатывал
     * дважды за клик и кнопка сворачивания панели переставала работать при
     * внешне исправном интерфейсе. Повторный запуск демо без перезагрузки окна
     * теперь безопасен: обработчики и колбэки просто обновляются.
     */
    this.onPause = opts.onPause || null;
    this.onResume = opts.onResume || null;
    this.onLock = opts.onLock || null;
    this.onReport = opts.onReport || null;
    if (this._inited) return;
    this._inited = true;

    installFallbackStyles();
    this._mount();

    if (this.dom.toggle) {
      this._onToggleClick = function () {
        var collapsed = self.dom.root.classList.toggle('is-collapsed');
        self.dom.toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        self.dom.toggle.setAttribute('aria-label', collapsed ? 'Развернуть панель наблюдения' : 'Свернуть панель наблюдения');
      };
      this.dom.toggle.addEventListener('click', this._onToggleClick);
    }
    if (this.dom.resumeBtn) {
      this._onResumeClick = function () { self.clearPause(true); };
      this.dom.resumeBtn.addEventListener('click', this._onResumeClick);
    }
    if (this.dom.lockBtn) {
      this._onLockReportClick = function () {
        if (self.onReport) { try { self.onReport(); } catch (e) {} }
      };
      this.dom.lockBtn.addEventListener('click', this._onLockReportClick);
    }

    this._paintChannels();
    this.setRisk(0, [], 'none');

    /*
     * prefers-reduced-motion читается не один раз в конструкторе, а живёт:
     * оператор может включить системную настройку уже после запуска оболочки,
     * и тогда CSS перестраивается сразу (медиа-запросы в tokens.css и styles.css),
     * а js оставался бы в прежнем режиме — класс feed__new на узлах ленты и
     * снятие тоста через 300 мс вместо немедленного. Правило 6 требует уважать
     * настройку, а не её значение на момент загрузки.
     */
    this._watchReducedMotion();

    // Раз в секунду гасим истёкшие подсветки каналов: шесть узлов, дешёво.
    this._tick = setInterval(function () { self._chanDirty = true; self._schedule(); }, 1000);

    // hello — источник истины по каналам и режиму развёртывания. app.js тоже
    // его слушает, но мы подписываемся сами: так HUD узнаёт exam_mode, который
    // app.js до нас не доносит (он передаёт только карту каналов).
    this._listenHello();
    this._listenProfile();
  };

  /**
   * Правила экзамена. Подписываемся на все известные написания канала и
   * вдобавок спрашиваем текущее значение: renderer мог загрузиться позже
   * первого push и пропустить его. Ничего из этого может не существовать —
   * тогда остаётся пустой профиль, и интерфейс работает как раньше.
   */
  Hud.prototype._listenProfile = function () {
    var self = this;
    var api = null;
    try { api = window.proctor || null; } catch (e) { api = null; }
    if (!api) return;

    var subs = ['onExamProfile', 'onProfile', 'onExamRules'];
    for (var i = 0; i < subs.length; i++) {
      if (typeof api[subs[i]] === 'function') {
        try { api[subs[i]](function (p) { self.setExamProfile(p); }); } catch (e) {}
      }
    }
    // универсальная подписка по короткому имени канала (preload.on)
    if (typeof api.on === 'function') {
      var names = ['examProfile', 'profile', 'examRules'];
      for (var j = 0; j < names.length; j++) {
        try { api.on(names[j], function (p) { self.setExamProfile(p); }); } catch (e) {}
      }
    }

    // текущее значение: поле, функция или функция, вернувшая Promise
    var getters = ['examProfile', 'profile', 'examRules', 'getExamProfile', 'getProfile'];
    for (var k = 0; k < getters.length; k++) {
      var raw = api?.[getters[k]];
      if (raw === undefined || raw === null) continue;
      if (typeof raw === 'function') {
        try {
          Promise.resolve(raw.call(api)).then(function (val) { self.setExamProfile(val); },
            function () { /* метода нет или он отказал — остаёмся без правил */ });
        } catch (e) { /* вызов не удался — не страшно */ }
      } else if (typeof raw === 'object') {
        this.setExamProfile(raw);
      }
    }
  };

  /**
   * Применить правила экзамена. Принимает и сам профиль, и конверт, в котором
   * он лежит (shell-status, capabilities, hello) — разбирается normalizeProfile.
   * Возвращает true, если правила действительно изменились.
   */
  Hud.prototype.setExamProfile = function (raw) {
    var next = normalizeProfile(raw);
    // Конверт без профиля не должен стирать уже известные правила: shell-status
    // приходит раз в секунду, и половина его веток профиля не содержит вовсе.
    if (!next.present && this.profile.present) return false;
    var sig = profileSig(next);
    if (sig === this._profileSig) return false;
    this._profileSig = sig;
    this.profile = next;
    this._chanDirty = true;
    this._schedule();
    for (var i = 0; i < this._profileSubs.length; i++) {
      try { this._profileSubs[i](next); } catch (e) { /* подписчик не рвёт поток */ }
    }
    return true;
  };

  /** Текущие правила. Всегда объект: пустой профиль — тоже ответ. */
  Hud.prototype.examProfile = function () { return this.profile; };

  /** Подписка на смену правил: экраны согласия, проверки и отчёта. */
  Hud.prototype.onProfileChange = function (cb) {
    if (typeof cb === 'function') this._profileSubs.push(cb);
  };

  Hud.prototype._watchReducedMotion = function () {
    var self = this;
    try {
      if (!window.matchMedia) return;
      var mq = window.matchMedia('(prefers-reduced-motion: reduce)');
      this._mq = mq;
      this._onMotionChange = function (e) { self._reduced = !!(e && e.matches); };
      if (typeof mq.addEventListener === 'function') mq.addEventListener('change', this._onMotionChange);
      else if (typeof mq.addListener === 'function') mq.addListener(this._onMotionChange); // старый API
      this._reduced = !!mq.matches;
    } catch (e) { /* без подписки остаёмся на значении из конструктора */ }
  };

  /**
   * Остановить панель: гасит секундный интервал, отложенные показы и снимает
   * навешенные слушатели. Нужен, чтобы перезапуск оболочки «на живую» между
   * прогонами демо не оставлял за собой работающие таймеры.
   */
  Hud.prototype.destroy = function () {
    if (this._tick) { clearInterval(this._tick); this._tick = null; }
    if (this._alertRetry) { clearTimeout(this._alertRetry); this._alertRetry = null; }
    this._pendingAlert = null;
    if (this._rafId && window.cancelAnimationFrame) {
      try { window.cancelAnimationFrame(this._rafId); } catch (e) {}
    }
    this._rafId = 0;
    this._rafPending = false;

    if (this.dom.toggle && this._onToggleClick) {
      this.dom.toggle.removeEventListener('click', this._onToggleClick);
    }
    if (this.dom.resumeBtn && this._onResumeClick) {
      this.dom.resumeBtn.removeEventListener('click', this._onResumeClick);
    }
    if (this.dom.lockBtn && this._onLockReportClick) {
      this.dom.lockBtn.removeEventListener('click', this._onLockReportClick);
    }
    if (this._mq && this._onMotionChange) {
      try {
        if (typeof this._mq.removeEventListener === 'function') this._mq.removeEventListener('change', this._onMotionChange);
        else if (typeof this._mq.removeListener === 'function') this._mq.removeListener(this._onMotionChange);
      } catch (e) {}
    }
    this._mq = null;
    this._inited = false;
  };

  Hud.prototype._listenHello = function () {
    var self = this;
    var api = null;
    try { api = window.proctor || null; } catch (e) { api = null; }
    if (!api) return;
    try {
      if (typeof api.onHello === 'function') {
        api.onHello(function (h) { self.applyHello(h); });
      }
      if (typeof api.capabilities === 'function') {
        Promise.resolve(api.capabilities()).then(function (raw) { self.applyHello(raw); },
          function () { /* моста нет — работаем как есть */ });
      }
    } catch (e) { /* деградация без моста допустима */ }
  };

  /** Разобрать hello целиком: каналы + режим развёртывания (Р-10). */
  Hud.prototype.applyHello = function (h) {
    if (!h || typeof h !== 'object') return;
    var mode = h.exam_mode || h.mode || (h.config && h.config.exam_mode) || null;
    if (mode === 'classroom' || mode === 'remote') this.mode = mode;
    var caps = (h.capabilities && typeof h.capabilities === 'object') ? h.capabilities : null;
    if (caps) this.setCapabilities(caps);
    else { this._chanDirty = true; this._schedule(); }
  };

  /**
   * Собрать разметку панели. Используем узлы index.html, если они есть,
   * и достраиваем недостающие: модуль самодостаточен.
   */
  Hud.prototype._mount = function () {
    var root = el('hud');
    if (!root) {
      root = mk('aside', 'hud monitor', { 'aria-label': 'Панель наблюдения' });
      root.id = 'hud';
      root.hidden = true;
      document.body.appendChild(root);
    }
    var body = el('hud-body');
    if (!body) {
      body = mk('div', 'hud__body');
      body.id = 'hud-body';
      root.appendChild(body);
    }

    this.dom.root = root;
    this.dom.body = body;
    this.dom.toggle = el('hud-toggle');

    this._mountGuard(body);
    this._mountRisk(body);
    this._mountChannels(body);
    this._mountFeed(body);
    this._mountAlerts();
    this._mountPause();
    this._mountLock();
  };

  /*
   * Разметку берём из index.html, если она есть, и достраиваем сами, если её нет.
   * Обе ветки обязаны давать ОДИН результат. Класс wordmark у латинской метки
   * «risk-score» раньше терялся в самодостроенной ветке: index.html объявляет
   * узел как "hud__riskcap caps wordmark", а js строил "hud__riskcap caps" —
   * метка рисовалась Onest вместо Space Grotesk, с другим трекингом и другой
   * формой g и r. Бейджу уровня wordmark, наоборот, не нужен: «норма» —
   * кириллица, которой в Space Grotesk нет (Р-14).
   */
  /**
   * Состояние защищённого режима — первым блоком панели.
   *
   * Зачем: блокировки отбирают у студента системные сочетания и чистят буфер
   * обмена его машины. Он обязан видеть, когда это началось и когда кончилось,
   * иначе «у меня перестал работать Cmd+W» выглядит как поломка компьютера.
   * Статус несут и цвет, и слово, и заполненность квадрата — цвет не
   * единственный носитель смысла (Р-13).
   */
  Hud.prototype._mountGuard = function (body) {
    var box = el('hud-guard');
    if (!box) {
      box = mk('section', 'hud__guard', { 'aria-label': 'Защищённый режим' });
      box.id = 'hud-guard';
      box.innerHTML =
        '<div class="hud__guardrow">' +
          '<span class="hud__guardmark" id="hud-guard-mark" aria-hidden="true"></span>' +
          '<span class="hud__guardname" id="hud-guard-name">защищённый режим: проверяем…</span>' +
        '</div>' +
        '<p class="hud__guardwhy" id="hud-guard-why"></p>' +
        '<p class="hud__guardkeys" id="hud-guard-keys" hidden></p>';
      // первым ребёнком: статус режима важнее risk-score
      if (body.firstChild) body.insertBefore(box, body.firstChild);
      else body.appendChild(box);
    }
    // aria-live: смена режима объявляется, но не перебивает чтение вопроса
    box.setAttribute('role', 'status');
    box.setAttribute('aria-live', 'polite');
    this.dom.guard = box;
    this.dom.guardMark = el('hud-guard-mark');
    this.dom.guardName = el('hud-guard-name');
    this.dom.guardWhy = el('hud-guard-why');
    this.dom.guardKeys = el('hud-guard-keys');
  };

  /**
   * Применить состояние защищённого режима. Вызывается оболочкой через
   * app.js при каждом изменении: {active, examState, reason, accelerators,
   * systemWide, windowLocked, disabledByFlag, adminExit}.
   */
  Hud.prototype.setProtection = function (info) {
    if (!info || typeof info !== 'object') return;
    var prev = this.protection;
    // Состояние приходит и событием, и в составе shell-status раз в секунду.
    // Писать в DOM одно и то же каждую секунду не надо.
    var sig = [
      info.active === true, info.windowLocked === true, info.examState || '',
      info.disabledByFlag === true, (info.accelerators || []).join(',')
    ].join('|');
    if (this._guardSig === sig) return;
    this._guardSig = sig;
    this.protection = info;
    var on = info.active === true;

    if (!this.dom.guard) return;
    this.dom.guard.classList.toggle('is-on', on);
    this.dom.guard.setAttribute('data-state', on ? 'on' : 'off');
    if (this.dom.guardMark) {
      this.dom.guardMark.style.color = on ? 'var(--acid, currentColor)' : 'var(--text-muted-on-dark)';
    }
    if (this.dom.guardName) {
      this.dom.guardName.textContent = on
        ? 'Защищённый режим ВКЛЮЧЁН'
        : 'Защищённый режим снят';
    }

    var why = [];
    if (on) {
      if (info.systemWide) {
        why.push('Перечисленные ниже сочетания клавиш перехвачены на уровне всей '
          + 'системы и не работают сейчас ни в одной программе.');
      } else if ((info.accelerators || []).length) {
        why.push('Перечисленные ниже сочетания клавиш перехвачены, пока идёт тест.');
      }
      if (info.clipboardCleared) why.push('Буфер обмена очищается автоматически.');
      if (info.windowLocked) why.push('Окно теста держится поверх других окон и не закрывается.');
      why.push('Всё перечисленное снимается, как только тест завершён.');
    } else if (info.disabledByFlag) {
      why.push('Приложение запущено с флагом --no-lockdown: блокировки не включатся. '
        + 'Это режим разработки, не экзаменационный.');
    } else {
      why.push('Сочетания клавиш, буфер обмена и окна в вашем распоряжении. '
        + 'Блокировки включатся только на время самого теста.');
    }
    if (this.dom.guardWhy) this.dom.guardWhy.textContent = why.join(' ');

    var keys = Array.isArray(info.accelerators) ? info.accelerators : [];
    if (this.dom.guardKeys) {
      if (on && keys.length) {
        this.dom.guardKeys.hidden = false;
        this.dom.guardKeys.textContent = keys.join('  ·  ');
      } else {
        this.dom.guardKeys.hidden = true;
        this.dom.guardKeys.textContent = '';
      }
    }

    // Переход режима — отдельное сообщение: его легко пропустить в панели.
    if (prev && prev.active !== on) {
      this.alert({
        tone: on ? 'var(--sev-warn)' : 'var(--sev-ok)',
        title: on ? 'Защищённый режим включён' : 'Защищённый режим снят',
        text: on
          ? ('Часть системных сочетаний клавиш и буфер обмена заблокированы до конца теста. '
             + 'Аварийный выход: ' + (info.adminExit || '—'))
          : 'Блокировки сняты, машина снова полностью в вашем распоряжении.',
        code: on ? 'GUARD_ON' : 'GUARD_OFF',
        rank: SEV_RANK.info
      });
    }
  };

  Hud.prototype._mountRisk = function (body) {
    var ring = el('hud-ring');
    if (!ring) {
      var sec = mk('section', 'hud__risk', { 'aria-label': 'Оценка риска' });
      sec.innerHTML =
        '<div class="ring" id="hud-ring-wrap">' +
          '<svg viewBox="0 0 100 100" aria-hidden="true" focusable="false">' +
            '<circle class="ring__track" cx="50" cy="50" r="' + RING_R + '"></circle>' +
            '<circle class="ring__value" id="hud-ring" cx="50" cy="50" r="' + RING_R + '"></circle>' +
          '</svg>' +
          '<div class="ring__center">' +
            '<div class="ring__num mono" id="hud-score">0</div>' +
            '<div class="ring__label" id="hud-level">норма</div>' +
          '</div>' +
        '</div>' +
        '<div class="hud__riskside">' +
          '<div class="hud__riskcap caps wordmark">risk-score</div>' +
          '<span class="badge" id="hud-levelbadge" style="--tone:var(--risk-ok)">норма</span>' +
          '<ul class="hud__contrib" id="hud-contrib"><li class="hud__contribempty muted">отклонений не зафиксировано</li></ul>' +
          '<div class="hud__fps mono" id="hud-fps">поток: —</div>' +
        '</div>';
      body.appendChild(sec);
    }

    this.dom.ringWrap = el('hud-ring-wrap');
    this.dom.ring = el('hud-ring');
    this.dom.score = el('hud-score');
    this.dom.level = el('hud-level');
    this.dom.contrib = el('hud-contrib');
    this.dom.fps = el('hud-fps');

    // Бейдж уровня — третий носитель смысла рядом с цветом дуги и числом:
    // слово + фигура. Если в разметке его нет, добавляем сами.
    if (!el('hud-levelbadge')) {
      var cap = document.querySelector('.hud__riskcap');
      if (cap && cap.parentNode) {
        var badge = mk('span', 'badge');
        badge.id = 'hud-levelbadge';
        badge.style.setProperty('--tone', 'var(--risk-ok)');
        badge.innerHTML = glyph('circle') + '<span>норма</span>';
        cap.parentNode.insertBefore(badge, cap.nextSibling);
      }
    }
    this.dom.levelBadge = el('hud-levelbadge');

    // Экран читает не только глазами: число и слово уровня доступны ассистивно.
    if (this.dom.ringWrap) {
      this.dom.ringWrap.setAttribute('role', 'img');
      this.dom.ringWrap.setAttribute('aria-live', 'off');
    }
    this._decorateRing();
  };

  /**
   * Довести кольцо до состояния, не зависящего от styles.css:
   * длина пунктира, начало дуги сверху, засечки на порогах 30/60/90.
   *
   * Засечки — вторая, нецветовая подсказка: по ним видно, какой порог дуга уже
   * прошла, даже если цвет не читается.
   */
  Hud.prototype._decorateRing = function () {
    var ring = this.dom.ring;
    if (!ring) return;

    var r = parseFloat(ring.getAttribute('r')) || RING_R;
    var len = 2 * Math.PI * r;
    this._ringLen = len;

    // Длину пунктира задаём сами: без неё stroke-dashoffset ничего не двигает,
    // а значение должно совпадать с фактическим радиусом из разметки.
    ring.style.strokeDasharray = String(len.toFixed(2));
    ring.style.strokeDashoffset = String(len.toFixed(2));
    ring.setAttribute('stroke-linecap', 'round');

    var svg = ring.ownerSVGElement || ring.parentNode;

    /*
     * Дуга SVG-окружности начинается на 3 часах, а расти должна с 12.
     * Поворот может уже делать styles.css правилом `.ring svg { rotate(-90deg) }`.
     * Если он есть — своего НЕ добавляем, иначе получится -180° и кольцо
     * начнёт заполняться снизу. Если его нет — поворачиваем сами.
     */
    var externallyRotated = false;
    try {
      var cs = (svg && typeof window.getComputedStyle === 'function')
        ? window.getComputedStyle(svg) : null;
      if (cs && cs.transform && cs.transform !== 'none') externallyRotated = true;
    } catch (e) { /* нет getComputedStyle — считаем, что поворота нет */ }

    if (!externallyRotated) {
      ring.style.transform = 'rotate(-90deg)';
      ring.style.transformOrigin = '50% 50%';
    }

    if (!svg || svg.getAttribute === undefined || el('hud-ring-ticks')) return;

    var NS = 'http://www.w3.org/2000/svg';
    var g = document.createElementNS(NS, 'g');
    g.setAttribute('id', 'hud-ring-ticks');
    g.setAttribute('aria-hidden', 'true');
    // Засечки живут в той же системе координат, что и дуга: угол считаем от
    // 3 часов, а компенсирующий поворот накладываем ровно там, где и на дугу.
    if (!externallyRotated) {
      g.style.transform = 'rotate(-90deg)';
      g.style.transformOrigin = '50% 50%';
    }
    var marks = [RISK_WARN, RISK_PAUSE, RISK_LOCK];
    for (var i = 0; i < marks.length; i++) {
      var a = (marks[i] / 100) * 2 * Math.PI;
      var line = document.createElementNS(NS, 'line');
      line.setAttribute('x1', (50 + Math.cos(a) * (r - 6)).toFixed(2));
      line.setAttribute('y1', (50 + Math.sin(a) * (r - 6)).toFixed(2));
      line.setAttribute('x2', (50 + Math.cos(a) * (r + 6)).toFixed(2));
      line.setAttribute('y2', (50 + Math.sin(a) * (r + 6)).toFixed(2));
      line.setAttribute('class', 'ring__tick');
      line.setAttribute('stroke-width', '1.4');
      line.style.stroke = 'var(--border-hair-dark)';
      g.appendChild(line);
    }
    try { svg.insertBefore(g, ring); } catch (e) { svg.appendChild(g); }
  };

  Hud.prototype._mountChannels = function (body) {
    var list = el('hud-chans');
    if (!list) {
      var sec = mk('section', 'hud__section');
      sec.innerHTML = '<div class="hud__sectitle caps">Каналы наблюдения</div>' +
                      '<ul class="chans" id="hud-chans"></ul>';
      body.appendChild(sec);
      list = el('hud-chans');
    }
    this.dom.chans = list;

    // Точка (.chan__dot) остаётся пустой: её красит CSS по --tone и классу s-*.
    // Фигура (.chan__shape) — второй, нецветовой носитель состояния; стоит в
    // конце строки и ничего не ломает, даже если CSS про неё не знает.
    var html = '';
    for (var i = 0; i < CHANNELS.length; i++) {
      var c = CHANNELS[i];
      html += '<li class="chan chan--' + c.id + ' s-off" id="chan-' + c.id + '" style="--tone:var(--gray-500)">' +
                '<span class="chan__dot" aria-hidden="true"></span>' +
                '<span class="chan__text">' +
                  '<span class="chan__name">' + c.name + '</span>' +
                  '<span class="chan__state" id="chanstate-' + c.id + '">нет данных</span>' +
                '</span>' +
                '<span class="chan__shape" id="chandot-' + c.id + '" aria-hidden="true">' +
                  glyph('off') +
                '</span>' +
              '</li>';
    }
    list.innerHTML = html;
  };

  Hud.prototype._mountFeed = function (body) {
    var feed = el('hud-feed');
    if (!feed) {
      var sec = mk('section', 'hud__section event-feed');
      sec.innerHTML = '<div class="hud__sectitle caps">Последние наблюдения ' +
                        '<span class="mono" id="hud-feedcount">00</span>' +
                      '</div>' +
                      '<ul class="feed" id="hud-feed"></ul>';
      body.appendChild(sec);
      feed = el('hud-feed');
    }
    // Счётчик всех зафиксированных наблюдений за сессию: лента показывает
    // последние MAX_FEED, а число должно оставаться честным.
    if (!el('hud-feedcount')) {
      var sec2 = feed.parentNode;
      var title = sec2 ? sec2.querySelector('.hud__sectitle') : null;
      if (title) {
        var cnt = mk('span', 'mono');
        cnt.id = 'hud-feedcount';
        cnt.textContent = '00';
        title.appendChild(document.createTextNode(' '));
        title.appendChild(cnt);
      }
    }

    this.dom.feed = feed;
    this.dom.feedCount = el('hud-feedcount');

    // role=log, но без автоозвучки: при шторме событий это превратилось бы в
    // непрерывное бормотание скринридера. О важном сообщает .alert.
    feed.setAttribute('role', 'log');
    feed.setAttribute('aria-live', 'off');
    if (!feed.firstChild) {
      feed.innerHTML = '<li class="feed__empty muted">Пока ничего не зафиксировано.</li>';
    }
  };

  Hud.prototype._mountAlerts = function () {
    var box = el('alerts') || el('toasts');
    if (!box) {
      box = mk('div', 'alerts', { 'aria-live': 'polite', 'aria-atomic': 'false' });
      box.id = 'alerts';
      document.body.appendChild(box);
    } else {
      box.setAttribute('aria-live', 'polite');
    }
    this.dom.alerts = box;
    // Контейнер внутри самой панели: пока панель на экране, сообщения идут
    // туда (см. _alertHost). Может отсутствовать в старой разметке — тогда
    // всё работает по-прежнему, через плавающий контейнер.
    this.dom.hudAlerts = el('hud-alerts') || null;
  };

  /**
   * Куда положить сообщение.
   *
   * Панель наблюдения на экране — внутрь панели: во время экзамена это
   * единственная площадь окна, не накрытая нативным слоем страницы теста, и
   * там сообщение стоит в потоке, то есть не закрывает ни одной кнопки.
   * Панели нет (согласие, предполётная, отчёт) — плавающий контейнер.
   */
  Hud.prototype._alertHost = function () {
    var panel = this.dom.hudAlerts;
    var hud = this.dom.root || el('hud');
    if (panel && hud && !hud.hidden) return panel;
    return this.dom.alerts;
  };

  /**
   * Блок «подпись + значение» по образцу .overlay__session из разметки.
   * Используется для номера сессии, кода инцидента и времени: значения —
   * идентификаторы, поэтому моноширинным (правило 5 дизайн-системы).
   */
  function sessionBlock(cap, id) {
    var box = mk('div', 'overlay__session');
    box.innerHTML = '<span class="overlay__sesscap caps">' + cap + '</span>' +
                    '<span class="overlay__sessval mono" id="' + id + '">—</span>';
    return box;
  }

  /** Поставить id на существующий узел или создать его после anchor. */
  function adopt(scope, selector, id, factory, anchor) {
    var node = el(id);
    if (node) return node;
    node = scope ? scope.querySelector(selector) : null;
    if (node) { node.id = id; return node; }
    if (!factory || !anchor || !anchor.parentNode) return null;
    node = factory();
    anchor.parentNode.insertBefore(node, anchor.nextSibling);
    return node;
  }

  /**
   * Оверлей паузы. Разметку пишет index.html (другой агент) — мы её НЕ
   * перетираем, а дополняем: подписываем узлы, которыми управляем, и
   * досоздаём отсутствующие. Список условий возврата заполняется динамически,
   * по тому, что реально зафиксировано.
   */
  Hud.prototype._mountPause = function () {
    var ov = el('overlay-pause');
    if (!ov) {
      ov = mk('div', 'overlay overlay--pause');
      ov.id = 'overlay-pause';
      ov.hidden = true;
      ov.innerHTML =
        '<div class="overlay__card monitor">' +
          '<div class="overlay__badge overlay__badge--pause">' +
            '<span class="overlay__badgemark" aria-hidden="true"></span>пауза' +
          '</div>' +
          '<h2 class="overlay__title">Тест приостановлен</h2>' +
          '<p class="overlay__text" id="pause-reason">Наблюдение вышло за пределы нормы.</p>' +
          '<ul class="overlay__cond" id="pause-cond"></ul>' +
          '<p class="overlay__note">Таймер экзамена остановлен, ответы сохранены. ' +
            'Продолжить можно, когда наблюдение вернётся в норму.</p>' +
          '<button class="btn btn--primary btn--lg" id="btn-resume" type="button" disabled>Продолжить тест</button>' +
          '<p class="overlay__wait" id="pause-wait">Ожидание нормализации…</p>' +
          /*
           * Решение проктора. Приостановку с review_required снимает только
           * человек — так и задумано. Но до 08.10 вызвать это решение было
           * НЕЧЕМ: путь proctorRelease в мосте есть и доходит до цепочки
           * событием PROCTOR_DECISION, а показать его в интерфейсе забыли.
           * В одиночном прогоне без проктора единственным выходом оставалось
           * «Перейти к отчёту», то есть сдать работу — и это ровно то, на что
           * пожаловалась владелец продукта.
           *
           * Блок скрыт и открывается сочетанием, которое знает проктор, а не
           * студент. Имя обязательно: решение попадает в подписанную цепочку
           * с указанием, КТО его принял, иначе оно ничего не значит.
           */
          '<div class="overlay__proctor" id="pause-proctor" hidden>' +
            '<p class="overlay__note">Решение проктора. Снятие будет записано ' +
              'в журнал с вашим именем и причиной.</p>' +
            '<label class="overlay__field">Кто снимает' +
              '<input type="text" id="pause-actor" autocomplete="off" ' +
                'placeholder="фамилия и должность">' +
            '</label>' +
            '<label class="overlay__field">Причина' +
              '<input type="text" id="pause-why" autocomplete="off" ' +
                'placeholder="например: проверено лично, нарушения нет">' +
            '</label>' +
            '<button class="btn btn--secondary" id="btn-proctor-release" type="button">' +
              'Снять приостановку</button>' +
            '<p class="overlay__wait" id="pause-proctor-note"></p>' +
          '</div>' +
        '</div>';
      document.body.appendChild(ov);
    }

    var card = ov.querySelector('.overlay__card') || ov;
    var title = ov.querySelector('.overlay__title');
    if (title && !title.id) title.id = 'pause-title';

    ov.setAttribute('role', 'alertdialog');
    ov.setAttribute('aria-modal', 'true');
    if (title) ov.setAttribute('aria-labelledby', title.id);
    ov.setAttribute('aria-describedby', 'pause-reason');

    var reason = el('pause-reason');
    var cond = adopt(ov, '.overlay__cond', 'pause-cond', function () {
      return mk('ul', 'overlay__cond');
    }, reason);

    var wait = el('pause-wait');
    if (wait) wait.setAttribute('role', 'status');

    // Блок решения проктора. Досоздаём тем же способом, что и список условий:
    // разметку index.html не перетираем. Сайдкар присылает для него и готовое
    // объяснение, и код сессии (`review.explanation`, `review.session_code_display`),
    // а до этой правки оболочка не показывала ни того, ни другого — студент
    // видел паузу и не знал, что её снимает человек и по какому коду.
    var review = adopt(ov, '.overlay__review', 'pause-review', function () {
      var node = mk('p', 'overlay__review');
      node.hidden = true;
      return node;
    }, cond);
    if (review) review.setAttribute('role', 'status');

    this.dom.pause = ov;
    this.dom.pauseCard = card;
    this.dom.pauseReason = reason;
    this.dom.pauseCond = cond;
    this.dom.pauseWait = wait;
    this.dom.pauseReview = review;
    this.dom.pauseNote = ov.querySelector('.overlay__note');
    this.dom.resumeBtn = el('btn-resume');
    this.dom.proctorBox = el('pause-proctor');
    this.dom.proctorActor = el('pause-actor');
    this.dom.proctorWhy = el('pause-why');
    this.dom.proctorBtn = el('btn-proctor-release');
    this.dom.proctorNote = el('pause-proctor-note');
    this._bindProctorRelease();
  };

  /**
   * Сочетание проктора, открывающее блок решения на оверлее паузы.
   *
   * Почему сочетание, а не кнопка на виду: приостановку, которая ждёт решения
   * человека, не должен снимать студент. Видимая кнопка превратила бы решение
   * проктора в обычный выход из блокировки. Сочетание знает тот, кто ведёт
   * экзамен, — ровно как с аварийным выходом Cmd+Alt+Shift+Q.
   *
   * Снятие уходит через api.proctorRelease и попадает в подписанную цепочку
   * событием PROCTOR_DECISION с именем и причиной: решение без автора ничего
   * не доказывает и разбору не помогает.
   */
  Hud.prototype._bindProctorRelease = function () {
    var self = this;
    if (!this._onProctorKey) {
      this._onProctorKey = function (e) {
        var combo = (e.metaKey || e.ctrlKey) && e.altKey && e.shiftKey &&
                    String(e.key || '').toLowerCase() === 'r';
        if (!combo) return;
        if (!self.dom.pause || self.dom.pause.hidden) return;
        if (!self.dom.proctorBox) return;
        e.preventDefault();
        self.dom.proctorBox.hidden = false;
        try { self.dom.proctorActor.focus(); } catch (err) {}
      };
      document.addEventListener('keydown', this._onProctorKey, true);
    }
    if (this.dom.proctorBtn && !this._onProctorRelease) {
      this._onProctorRelease = function () {
        var actor = String((self.dom.proctorActor || {}).value || '').trim();
        var why = String((self.dom.proctorWhy || {}).value || '').trim();
        if (!actor) {
          if (self.dom.proctorNote) {
            self.dom.proctorNote.textContent =
              'Укажите, кто снимает приостановку: решение записывается с именем.';
          }
          try { self.dom.proctorActor.focus(); } catch (err) {}
          return;
        }
        if (self.dom.proctorNote) {
          self.dom.proctorNote.textContent = 'Решение отправлено в журнал…';
        }
        var api = (typeof window !== 'undefined' && window.proctor) || null;
        if (!api || typeof api.proctorRelease !== 'function') {
          if (self.dom.proctorNote) {
            self.dom.proctorNote.textContent =
              'Ядро недоступно — решение не записано. Приостановка остаётся.';
          }
          return;
        }
        Promise.resolve(api.proctorRelease(actor, why || 'без указания причины'))
          .then(function () {
            if (self.dom.proctorNote) {
              self.dom.proctorNote.textContent =
                'Решение записано. Приостановка снимается по ответу ядра.';
            }
          })
          .catch(function () {
            if (self.dom.proctorNote) {
              self.dom.proctorNote.textContent =
                'Ядро не приняло решение. Приостановка остаётся.';
            }
          });
      };
      this.dom.proctorBtn.addEventListener('click', this._onProctorRelease);
    }
  };

  /**
   * Показать на оверлее паузы, что решение принимает человек.
   *
   * Текст берётся из `review.explanation` сайдкара, а не пишется здесь: одна
   * формулировка в журнале, в отчёте и на экране — это то же требование, из
   * которого выросла вся правка политики. Своего текста оболочка добавляет
   * ровно столько, чтобы объяснить, что именно сейчас произойдёт.
   */
  Hud.prototype._renderPauseReview = function () {
    var node = this.dom.pauseReview;
    if (node) {
      if (this.reviewRequired) {
        var r = this.review || {};
        var code = r.session_code_display || this.sessionId;
        node.textContent = (r.explanation ||
          ('Экзамен приостановлен автоматически: накопленные наблюдения достигли ' +
           'порога, при котором требуется решение проктора. Это не решение о ' +
           'нарушении. Сообщите проктору код сессии ' + code + ': он посмотрит ' +
           'запись и либо продолжит экзамен, либо закроет его.'));
        node.hidden = false;
      } else {
        node.textContent = '';
        node.hidden = true;
      }
    }
    // Статичная подпись оверлея обещает возврат «когда наблюдение вернётся в
    // норму». При ожидании решения это неправда: нормализация паузу не снимет.
    if (this.dom.pauseNote) {
      this.dom.pauseNote.textContent = this.reviewRequired
        ? ('Таймер экзамена остановлен, ответы сохранены. Снять приостановку может ' +
           'только проктор — нормализация наблюдения её не снимает.')
        : ('Таймер экзамена остановлен, ответы сохранены. Продолжить можно, когда ' +
           'наблюдение вернётся в норму.');
    }
  };

  /**
   * Экран блокировки. Обязателен номер сессии И код инцидента: с этими двумя
   * строками студент идёт к организатору, а организатор находит запись в журнале.
   */
  Hud.prototype._mountLock = function () {
    var ov = el('overlay-lock');
    if (!ov) {
      ov = mk('div', 'overlay overlay--lock');
      ov.id = 'overlay-lock';
      ov.hidden = true;
      ov.innerHTML =
        '<div class="overlay__card overlay__card--lock monitor">' +
          '<div class="overlay__badge overlay__badge--lock">' +
            '<span class="overlay__badgemark" aria-hidden="true"></span>сессия закрыта' +
          '</div>' +
          '<h2 class="overlay__title">Экзамен остановлен системой</h2>' +
          '<p class="overlay__text" id="lock-reason">Накопленная оценка риска достигла порога ' +
            RISK_LOCK + '.</p>' +
          '<div class="overlay__session">' +
            '<span class="overlay__sesscap caps">номер сессии</span>' +
            '<span class="overlay__sessval mono" id="lock-session">—</span>' +
          '</div>' +
          '<p class="overlay__note">Доказательная база сохранена локально и сцеплена хеш-цепочкой. ' +
            'Решение по работе принимает экзаменатор — обратитесь к организатору.</p>' +
          '<button class="btn btn--primary btn--lg" id="btn-lock-report" type="button">Перейти к отчёту</button>' +
        '</div>';
      document.body.appendChild(ov);
    }

    var card = ov.querySelector('.overlay__card') || ov;
    var title = ov.querySelector('.overlay__title');
    if (title && !title.id) title.id = 'lock-title';

    ov.setAttribute('role', 'alertdialog');
    ov.setAttribute('aria-modal', 'true');
    if (title) ov.setAttribute('aria-labelledby', title.id);
    ov.setAttribute('aria-describedby', 'lock-reason');

    // Номер сессии в разметке уже есть; код инцидента и время добавляем рядом
    // тем же паттерном, чтобы не изобретать вторую визуальную форму.
    var sessVal = el('lock-session');
    var sessBox = null;
    if (sessVal) {
      sessBox = (typeof sessVal.closest === 'function') ? sessVal.closest('.overlay__session') : null;
      if (!sessBox) sessBox = sessVal.parentNode;
    }
    if (sessBox && !el('lock-code')) {
      sessBox.parentNode.insertBefore(sessionBlock('код инцидента', 'lock-code'), sessBox.nextSibling);
    }
    var codeBox = el('lock-code');
    var codeWrap = codeBox && typeof codeBox.closest === 'function'
      ? codeBox.closest('.overlay__session') : null;
    if ((codeWrap || sessBox) && !el('lock-time')) {
      var anchor = codeWrap || sessBox;
      anchor.parentNode.insertBefore(sessionBlock('время', 'lock-time'), anchor.nextSibling);
    }

    this.dom.lock = ov;
    this.dom.lockCard = card;
    this.dom.lockReason = el('lock-reason');
    this.dom.lockSession = sessVal;
    this.dom.lockCode = el('lock-code');
    this.dom.lockTime = el('lock-time');
    this.dom.lockBtn = el('btn-lock-report');
  };

  /* ------------------------------------------------------- внешние настройки */

  Hud.prototype.setCapabilities = function (caps) {
    if (caps && typeof caps === 'object') {
      var src = (caps.capabilities && typeof caps.capabilities === 'object') ? caps.capabilities : caps;
      this.caps = {
        vision: src.vision !== false,
        gaze: src.gaze !== false,
        identity: src.identity !== false,
        audio: src.audio !== false,
        env: src.env !== false
      };
      var mode = src.exam_mode || caps.exam_mode || caps.mode || null;
      if (mode === 'classroom' || mode === 'remote') this.mode = mode;
    }
    this._chanDirty = true;
    this._schedule();
  };

  Hud.prototype.setSessionId = function (id) {
    this.sessionId = id || '—';
    if (this.dom.lockSession) this.dom.lockSession.textContent = this.sessionId;
  };

  Hud.prototype.show = function () {
    if (this.dom.root) this.dom.root.hidden = false;
    this._rehomeAlerts();
  };
  Hud.prototype.hide = function () {
    if (this.dom.root) this.dom.root.hidden = true;
    this._rehomeAlerts();
  };

  /**
   * Переселить ЖИВЫЕ сообщения в контейнер, который сейчас подходит.
   *
   * Сообщение, поднятое за секунду до перехода на экзамен, осталось бы в
   * плавающем контейнере — то есть частично под нативным слоем страницы
   * теста, и студент дочитать бы его не смог. Срок жизни у сообщения свой
   * (ALERT_MS), и обрывать его из-за смены экрана неправильно: оно про то,
   * что уже случилось. Поэтому не гасим, а переносим.
   */
  Hud.prototype._rehomeAlerts = function () {
    var host = this._alertHost();
    if (!host) return;
    for (var i = 0; i < this._alerts.length; i++) {
      var node = this._alerts[i];
      if (node && node.parentNode !== host) {
        try { host.appendChild(node); } catch (e) { /* узел уже убран */ }
      }
    }
  };

  /* --------------------------------------------- батчинг обновлений в rAF */

  /**
   * Любое входящее сообщение только помечает, что экран устарел.
   * Писать в DOM имеет право только _flush, и только один раз на кадр.
   */
  Hud.prototype._schedule = function () {
    if (this._rafPending) return;
    this._rafPending = true;
    var self = this;
    var run = function () { self._rafPending = false; self._flush(); };
    if (typeof requestAnimationFrame === 'function') this._rafId = requestAnimationFrame(run);
    else this._rafId = setTimeout(run, 16);
  };

  Hud.prototype._flush = function () {
    if (this._riskDirty) { this._riskDirty = false; this._paintRisk(); }
    if (this._chanDirty) { this._chanDirty = false; this._paintChannels(); }
    if (this._queue.length) this._paintFeed();
  };

  /* ------------------------------------------------------------- каналы */

  /** Базовое состояние канала по последнему status и карте возможностей. */
  Hud.prototype._baseState = function (id) {
    var s = this.lastStatus;
    var caps = this.caps;

    // Камера не отдаёт кадров — причина важнее симптома. Без этого и «лицо не
    // видно», и «нет разрешения», и «указан несуществующий индекс» выглядели
    // одинаково, и человек не знал, что чинить.
    var camProblem = null;
    if (s && s.camera && !s.camera.opened) {
      camProblem = s.camera.error
        ? String(s.camera.error)
        : 'камера ' + s.camera.index + ' не открыта';
    }

    if (id === 'face') {
      if (!caps.vision) return ['off', 'канал недоступен'];
      if (!s) return ['off', 'нет данных'];
      if (camProblem) return ['bad', camProblem];
      if (s.face_count > 1) return ['bad', 'в кадре лиц: ' + s.face_count];
      if (!s.face_present) return ['bad', 'лицо не видно'];
      return ['ok', 'в кадре'];
    }
    if (id === 'gaze') {
      if (!caps.gaze) return ['off', 'канал недоступен'];
      if (camProblem) return ['bad', camProblem];
      if (!s || !s.gaze) return ['off', 'нет данных'];
      var z = s.gaze.zone || 'unknown';
      var t = ZONE_TEXT[z] || String(z);
      if (z === 'center' || z === 'screen') return ['ok', t];
      if (z === 'unknown') return ['off', t];
      if (z === 'off' || z === 'off_screen') return ['bad', t];
      return ['warn', t];
    }
    if (id === 'phone') {
      if (!caps.vision) return ['off', 'канал недоступен'];
      if (!s) return ['off', 'нет данных'];
      return s.phone ? ['bad', 'виден в кадре'] : ['ok', 'не обнаружен'];
    }
    if (id === 'identity') {
      if (!caps.identity) return ['off', 'канал недоступен'];
      if (!s) return ['off', 'нет данных'];
      return s.identity_ok ? ['ok', 'совпадает с эталоном'] : ['warn', 'сверка не подтверждена'];
    }
    if (id === 'env') {
      if (!caps.env) return ['off', 'канал недоступен'];
      return ['ok', 'проверки пройдены'];
    }
    if (id === 'audio') {
      // Р-10: в режиме аудитории анализ звука выключен намеренно, а не сломан.
      // Пишем это прямо: скрытый канал выглядел бы как дефект.
      if (!caps.audio) {
        return ['off', this.mode === 'classroom'
          ? 'канал недоступен: режим аудитории'
          : 'канал недоступен'];
      }
      if (!s) return ['off', 'нет данных'];
      // audio_ok из status означает «микрофон опрашивается без ошибок»,
      // а НЕ «посторонних звуков нет». Про посторонний голос говорят
      // инциденты VOICE_OTHER / SPEECH_WITHOUT_LIP_MOTION через highlights.
      return s.audio_ok ? ['ok', 'микрофон слушает'] : ['warn', 'сигнала с микрофона нет'];
    }
    if (id === 'profile') {
      /*
       * Правила экзамена. Это не датчик: состояние не зависит ни от камеры,
       * ни от каналов сайдкара, и «нет данных» здесь означало бы неправду —
       * данные есть всегда, их два возможных значения.
       *
       * Пустой профиль — НЕ ошибка и не деградация: это штатный режим
       * локального мок-теста, поэтому s-off (пустое кольцо), а не s-bad.
       * Разрешённые поисковики, наоборот, s-warn: это ослабление правил,
       * студент и комиссия должны видеть его сразу, а не искать в отчёте.
       */
      var p = this.profile;
      if (!p || !p.present) return ['off', 'не задан: открыт только локальный тест'];
      // Хеш — идентификатор, поэтому уходит третьим элементом: его рисует
      // .mono (правило 5 дизайн-системы), а не общий шрифт подписи.
      var tail = p.hashShort ? 'хеш ' + p.hashShort : '';
      /*
       * ПРАВИЛА ЕСТЬ, А ФИЛЬТРА НЕТ. Говорим это прежде, чем про поисковики:
       * «поисковики запрещены» по сессии без фильтра — утверждение о правиле,
       * которое ни к чему не применялось, и экран расходился бы с отчётом.
       * s-bad, а не s-off: пустой профиль — штатный режим, а объявленные
       * правила, которые не действуют, — отказ.
       */
      if (p.filterInstalled === false) {
        return ['bad', 'фильтр источников не встал: правила не применяются', tail];
      }
      if (p.rulesWithoutUrl) {
        return ['bad', 'в правилах нет адреса экзамена: открыт локальный тест', tail];
      }
      if (p.allowSearch) return ['warn', 'поисковики разрешены', tail];
      return ['ok', 'поисковики запрещены', tail];
    }
    return ['off', 'нет данных'];
  };

  Hud.prototype._paintChannels = function () {
    if (!this.dom.chans) return;
    var now = Date.now();

    for (var i = 0; i < CHANNELS.length; i++) {
      var id = CHANNELS[i].id;
      var node = el('chan-' + id);
      var label = el('chanstate-' + id);
      var dot = el('chandot-' + id);
      if (!node || !label) continue;

      var hl = this.highlights[id];
      if (hl && !hl.sticky && hl.until <= now) { delete this.highlights[id]; hl = null; }

      // Третий элемент базового состояния — необязательный идентификатор
      // (сейчас это хеш профиля). Он рисуется моноширинным, остальная подпись
      // остаётся обычным шрифтом: правило 5 дизайн-системы.
      var st, text, mono;
      if (hl) { st = hl.state; text = hl.text; mono = ''; }
      else { var b = this._baseState(id); st = b[0]; text = b[1]; mono = b[2] || ''; }

      // Пишем в DOM только при реальном изменении: шторм событий не должен
      // устраивать перекладку узлов каждый кадр.
      var cache = this._chanCache[id];
      if (cache && cache.st === st && cache.text === text && cache.mono === mono) continue;
      this._chanCache[id] = { st: st, text: text, mono: mono };

      var vis = CHAN_STATE[st] || CHAN_STATE.off;
      // chan--<id> обязателен и здесь: className переписывается целиком, и без
      // него профиль терял бы свою строку во всю ширину при первой же перекраске.
      node.className = 'chan chan--' + id + ' s-' + st;
      node.style.setProperty('--tone', vis.tone);
      if (mono) {
        label.innerHTML = escapeHtml(text) +
          ' <span class="chan__id mono">' + escapeHtml(mono) + '</span>';
      } else {
        label.textContent = text;
      }
      if (dot) dot.innerHTML = glyph(vis.shape);
      node.setAttribute('aria-label', CHANNELS[i].name + ': ' + text + (mono ? ', ' + mono : ''));
    }
  };

  Hud.prototype.applyStatus = function (st) {
    if (!st || typeof st !== 'object') return;
    this.lastStatus = st;
    if (this.dom.fps) {
      var fps = (typeof st.fps === 'number' && isFinite(st.fps)) ? st.fps.toFixed(1) : '—';
      var state = st.state ? ' · ' + (STATE_TEXT[st.state] || st.state) : '';
      this.dom.fps.textContent = 'поток ' + fps + ' к/с' + state;
    }
    if (typeof st.risk === 'number' && !this._riskFromRiskMsg) this.setRisk(st.risk, null, null);
    this._chanDirty = true;
    this._schedule();
  };

  /* --------------------------------------------------------------- риск */

  Hud.prototype.setRisk = function (score, breakdown, action) {
    if (typeof score === 'number' && isFinite(score)) {
      this.score = Math.max(0, Math.min(100, score));
      this._riskDirty = true;
    }
    if (breakdown) this._renderContrib(breakdown);
    if (action) this.verdict = action;
    this._schedule();
  };

  Hud.prototype.applyRisk = function (msg) {
    if (!msg) return;
    this._riskFromRiskMsg = true;
    this.lastBreakdown = msg.breakdown || [];
    this.setRisk(typeof msg.score === 'number' ? msg.score : this.score, msg.breakdown, msg.action);
  };

  Hud.prototype._paintRisk = function () {
    var lv = levelOf(this.score);
    var len = this._ringLen || RING_LEN;

    if (this.dom.ring) {
      this.dom.ring.style.strokeDashoffset = String((len * (1 - this.score / 100)).toFixed(2));
      this.dom.ring.style.stroke = lv.tone;
    }
    if (this.dom.score) this.dom.score.textContent = String(Math.round(this.score));
    if (this.dom.level) this.dom.level.textContent = lv.text;
    if (this.dom.ringWrap) {
      this.dom.ringWrap.className = 'ring ' + lv.cls;
      this.dom.ringWrap.style.setProperty('--tone', lv.tone);
      this.dom.ringWrap.setAttribute('aria-label',
        'Оценка риска ' + Math.round(this.score) + ' из 100, уровень: ' + lv.text);
    }
    // Бейдж повторяет уровень словом и фигурой: цвет не единственный носитель.
    if (this.dom.levelBadge) {
      this.dom.levelBadge.style.setProperty('--tone', lv.tone);
      this.dom.levelBadge.innerHTML = glyph(lv.shape) + '<span>' + lv.text + '</span>';
    }
  };

  Hud.prototype._renderContrib = function (breakdown) {
    if (!this.dom.contrib) return;
    var items = (breakdown || []).slice().filter(function (b) {
      return b && typeof b.contribution === 'number' && b.contribution > 0;
    });
    items.sort(function (a, b) { return b.contribution - a.contribution; });
    items = items.slice(0, 3);

    if (!items.length) {
      this.dom.contrib.innerHTML = '<li class="hud__contribempty muted">отклонений не зафиксировано</li>';
      return;
    }
    var html = '';
    for (var i = 0; i < items.length; i++) {
      var b = items[i];
      var name = EVENT_SHORT[b.kind] || EVENT_TEXT[b.kind] || b.kind || 'наблюдение';
      // count из сообщения risk приводим к числу: нечисловое значение дало бы
      // подпись «×undefined» / «×NaN», а строка с разметкой — мусор в ленте.
      var n = normCount(b.count);
      var cnt = n > 1 ? ' ×' + n : '';
      html += '<li><span class="c__n">' + escapeHtml(name + cnt) + '</span>' +
              '<span class="c__v mono">+' + Math.round(b.contribution) + '</span></li>';
    }
    this.dom.contrib.innerHTML = html;
  };

  /* ------------------------------------------------------------- события */

  Hud.prototype.applyEvent = function (ev) {
    if (!ev || typeof ev !== 'object' || !ev.kind) return;

    /*
     * Состояние профиля — это УСЛОВИЯ экзамена, а не поведение человека:
     * RISK_WEIGHTS в sidecar/protocol.py даёт этим видам вес 0 ровно по той же
     * причине, по какой его имеет SHELL_CONFIG. Правила отсюда забираем и в
     * ленте показываем, но в число инцидентов НЕ пишем: «инцидентов: 3», где
     * два из трёх — «профиль применён» и «поиск разрешён правилами», читается
     * с экрана отчёта как три нарушения, которых не было.
     */
    if (PROFILE_STATE_KINDS[ev.kind]) {
      this.setExamProfile(ev.detail);
      this._queue.push(ev);
      this._schedule();
      return;
    }

    if (SERVICE_KINDS[ev.kind]) return;

    this.events.push(ev);
    if (this.events.length > MAX_EVENTS) {
      this.events.shift();
      this._dropped++;
    }

    // Выход за правила принадлежит профилю, а не каналу «Окружение»: по виду
    // события это может быть SHORTCUT_BLOCKED, по смыслу — правила экзамена.
    var chan = isProfileScope(ev) ? 'profile' : KIND_CHANNEL[ev.kind];
    if (chan) {
      var sev = severityOf(ev);
      this.highlights[chan] = {
        state: (SEV_RANK[sev] || 0) >= 2 ? 'bad' : 'warn',
        text: shortFor(ev),
        sticky: !!STICKY_KINDS[ev.kind],
        until: Date.now() + HIGHLIGHT_MS
      };
      this._chanDirty = true;
    }

    this._queue.push(ev);
    this._schedule();

    /*
     * Блокировка перехода — единственный вид, о котором студента извещаем по
     * самому событию, не дожидаясь вердикта: он только что нажал ссылку и
     * имеет право сразу узнать, почему ничего не открылось.
     *
     * Подзапросы (isSubresBlock) сюда НЕ попадают намеренно. Одна страница
     * Moodle со шрифтом и счётчиком с чужого CDN дала бы веер сообщений о том,
     * чего человек не делал. В ленте и в журнале они остаются, на экран не
     * лезут.
     */
    if (isNavBlock(ev)) this._navAlert(ev);
  };

  /**
   * Переход не разрешён: короткое неблокирующее сообщение по паттерну .alert.
   * Формула та же, что у остальных сообщений, — факт, контекст, действие, —
   * и без обвинений: система описывает наблюдение, решает человек.
   *
   * Про журнал сказано прямо. Это не угроза и не способ надавить: студент
   * обязан знать, что попытка записана, — иначе «Integrity Score вместо
   * надзора» превращается в скрытое наблюдение, от которого мы и уходим.
   * Дедупликация по хосту, а не по виду события: десять нажатий на одну и ту
   * же ссылку — одно сообщение, а переход на другой адрес виден отдельно.
   */
  Hud.prototype._navAlert = function (ev) {
    var target = navTarget(ev);
    var key = 'NAV:' + (target.host || '—');
    var now = Date.now();
    if (this._alertSeen[key] && now - this._alertSeen[key] < ALERT_DEDUP_MS) return;

    // Пауза и блокировка сообщением не перебиваются — правило из applyVerdict.
    var busy = (this.dom.pause && !this.dom.pause.hidden) ||
               (this.dom.lock && !this.dom.lock.hidden);
    if (busy) return;

    var sev = severityOf(ev);
    var ctx = this.profile.present
      ? 'Открыты только источники из профиля этого экзамена.'
      : 'Правила экзамена проктором не задавались: открыт только локальный тест.';
    var spec = {
      tone: SEV_TONE[sev],
      title: navFact(ev),
      text: ctx + ' Вернитесь к странице теста. ' +
            'Попытка зафиксирована в журнале вместе с полным адресом.',
      code: codeFor(ev),
      rank: SEV_RANK[sev] || SEV_RANK.medium
    };

    var node = this.alert(spec);
    if (node) { this._alertSeen[key] = Date.now(); return; }
    this._defer(key, spec);   // отбито интервалом — покажем, как только можно
  };

  /**
   * Один узел ленты по паттерну .event из гайда:
   * тональная точка -> факт + пояснение приглушённым -> время моноширинным справа.
   *
   * Порядок узлов (точка, тело, время) и класс .event — из гайда; раскладка
   * ожидается как grid 8px / 1fr / auto. Классы feed__* продублированы, чтобы
   * цвет точки и приглушённость пояснения работали и со старыми правилами.
   */
  Hud.prototype._feedNode = function (ev) {
    var sev = severityOf(ev);
    var li = mk('li', 'event' + (this._reduced ? '' : ' feed__new'));
    li.style.setProperty('--tone', SEV_TONE[sev]);

    var ctx = contextFor(ev);
    // Важность словом — обязательный дубль цветовой точки (a11y-правило гайда).
    var detail = 'Важность: ' + SEV_TEXT[sev] + (ctx ? ' · ' + ctx.replace(/\.$/, '') : '');

    /*
     * Адрес попытки — ПОЛНЫЙ, со схемой, путём и запросом, и именно в журнале.
     * В сообщении на экране стоит хост: он короткий и его видно. А в журнале
     * ценность ровно в полноте: «пробовал открыть moodle другого вуза» и
     * «пробовал открыть конкретный вопрос конкретного курса» — разные факты,
     * и разбирать их будет человек. Адрес — идентификатор, поэтому .mono;
     * перенос по любому знаку, иначе длинная ссылка растянет панель.
     */
    var navUrl = isProfileScope(ev) ? navTarget(ev).url : '';

    li.innerHTML =
      '<i class="event__dot feed__sev sev-' + escapeHtml(sev) + '" aria-hidden="true"></i>' +
      '<span class="event__body feed__msg">' +
        '<b>' + escapeHtml(eventText(ev)) + '</b><br>' +
        '<span class="muted">' + escapeHtml(detail) + '</span>' +
        (navUrl ? '<br><span class="event__url mono">' + escapeHtml(navUrl) + '</span>' : '') +
      '</span>' +
      '<time class="event__time feed__time mono" datetime="' + escapeHtml(new Date(
        (typeof ev.ts === 'number' ? ev.ts * 1000 : Date.now())).toISOString()) + '">' +
        hhmmss(ev.ts) +
      '</time>';
    return li;
  };

  /**
   * Вылить очередь одним проходом.
   *
   * Если за кадр пришло 50 событий, в DOM уедут только последние MAX_FEED:
   * остальные всё равно были бы вытеснены на следующей строке, а в отчёте они
   * сохранены полностью (this.events).
   */
  Hud.prototype._paintFeed = function () {
    var feed = this.dom.feed;
    var queue = this._queue;
    this._queue = [];
    if (!feed || !queue.length) return;

    var empty = feed.querySelector('.feed__empty');
    if (empty && empty.parentNode) empty.parentNode.removeChild(empty);

    var batch = queue.length > MAX_FEED ? queue.slice(queue.length - MAX_FEED) : queue;
    var frag = document.createDocumentFragment();
    for (var i = batch.length - 1; i >= 0; i--) frag.appendChild(this._feedNode(batch[i]));
    feed.insertBefore(frag, feed.firstChild);

    while (feed.children.length > MAX_FEED) feed.removeChild(feed.lastChild);

    if (this.dom.feedCount) {
      var total = this.events.length + this._dropped;
      this.dom.feedCount.textContent = total < 100 ? pad2(total) : String(total);
    }
  };

  /* ------------------------------------------- эскалация: warn/pause/lock */

  Hud.prototype.applyVerdict = function (v) {
    if (!v) return;
    var action = v.action || 'none';
    var reason = v.reason || '';
    this.verdict = action;

    // Ждёт ли приостановка решения ЧЕЛОВЕКА. Сайдкар присылает это в каждом
    // вердикте (`review_required`) и описание запроса в `review`; до этой
    // правки оболочка оба поля игнорировала, и политика «решение принимает
    // проктор» существовала только в Python.
    if (typeof v.review_required === 'boolean') this.reviewRequired = v.review_required;
    if (v.review && typeof v.review === 'object') this.review = v.review;
    if (v.decided_by) this.decidedBy = String(v.decided_by);
    if (!this.reviewRequired) this.review = null;

    if (action === 'lock') { this.showLock(reason); return; }
    if (action === 'pause') { this.showPause(reason); return; }

    // Пауза важнее мягкого сообщения: оверлей тостом не перебиваем.
    if (this.dom.pause && !this.dom.pause.hidden) {
      // Спад риска НЕ снимает приостановку, которая ждёт проктора. Раньше
      // здесь стоял безусловный _allowResume(true): риск затухал ниже порога,
      // кнопка «Продолжить тест» разблокировалась, студент снимал паузу сам —
      // и экзамен шёл дальше, пока сайдкар считал сессию приостановленной в
      // ожидании человека. Ровно то расхождение документа и кода, на котором
      // ловят на защите, только в обратную сторону.
      if (this.reviewRequired) {
        this._allowResume(false);
        this._renderPauseReview();
      } else {
        this._allowResume(true);
      }
      return;
    }
    if (action === 'warn') this._warn(reason);
  };

  /** Последний значимый инцидент — основа формулировки «факт». */
  Hud.prototype._lastSignificant = function (windowMs) {
    var cutoff = (Date.now() - (windowMs || 30000)) / 1000;
    for (var i = this.events.length - 1; i >= 0; i--) {
      var ev = this.events[i];
      if (typeof ev.ts === 'number' && ev.ts < cutoff) break;
      if ((SEV_RANK[severityOf(ev)] || 0) >= 1) return ev;
    }
    return this.events.length ? this.events[this.events.length - 1] : null;
  };

  /** verdict=warn — неблокирующее сообщение по паттерну .alert из гайда. */
  Hud.prototype._warn = function (reason) {
    var ev = this._lastSignificant(30000);
    if (!ev) {
      this.alert({
        tone: 'var(--risk-warn)',
        title: 'Наблюдение отметило отклонение',
        text: (reason ? reason + ' ' : '') +
              'Оценка риска ' + Math.round(this.score) + ' из 100. ' +
              'Продолжайте работу, глядя на экран.',
        code: 'OBS_000',
        rank: SEV_RANK.medium
      });
      return;
    }
    var now = Date.now();
    if (this._alertSeen[ev.kind] && now - this._alertSeen[ev.kind] < ALERT_DEDUP_MS) return;

    var adv = adviceFor(ev.kind);
    var sev = severityOf(ev);
    var ctx = contextFor(ev);
    var spec = {
      tone: SEV_TONE[sev],
      title: eventText(ev),
      text: (ctx ? ctx + ' ' : '') + adv.act,
      code: codeFor(ev),
      rank: SEV_RANK[sev] || 0
    };

    /*
     * ВАЖНО: пометка «этот вид уже показан» ставится ТОЛЬКО после того, как узел
     * действительно создан. Раньше _alertSeen[ev.kind] ставился до вызова alert(),
     * а alert() умеет вернуть null по троттлингу ALERT_GAP_MS — непоказанное
     * сообщение считалось показанным и глушило тот же вид инцидента на весь
     * ALERT_DEDUP_MS. На серии вердиктов это давало окно тишины 12 с при уже
     * покрасневшем кольце риска. Отказ не теряем, а откладываем.
     */
    var node = this.alert(spec);
    if (node) { this._alertSeen[ev.kind] = Date.now(); return; }
    this._defer(ev.kind, spec);
  };

  /**
   * Отложить сообщение, отбитое интервалом ALERT_GAP_MS, и показать его, как
   * только интервал истечёт. Слот один: более важное сообщение вытесняет
   * менее важное, равное по важности — обновляет формулировку на свежую.
   */
  Hud.prototype._defer = function (kind, spec) {
    var pend = this._pendingAlert;
    if (pend && (pend.spec.rank || 0) > (spec.rank || 0)) return;

    this._pendingAlert = { kind: kind, spec: spec, tries: 0 };
    this._scheduleRetry();
  };

  Hud.prototype._scheduleRetry = function () {
    var self = this;
    if (this._alertRetry) return;
    var wait = ALERT_GAP_MS - (Date.now() - this._lastAlertAt);
    if (wait < 0) wait = 0;
    this._alertRetry = setTimeout(function () {
      self._alertRetry = null;
      self._flushPending();
    }, wait + 40);
  };

  Hud.prototype._flushPending = function () {
    var pend = this._pendingAlert;
    if (!pend) return;
    // Пока сообщение ждало, мог подняться блокирующий оверлей. Правило то же,
    // что в applyVerdict: пауза и блокировка тостом не перебиваются.
    var blocked = (this.dom.pause && !this.dom.pause.hidden) || (this.dom.lock && !this.dom.lock.hidden);
    if (blocked) { this._pendingAlert = null; return; }
    // Пока сообщение ждало, тот же вид мог быть показан по другому пути.
    var seen = this._alertSeen[pend.kind];
    if (seen && Date.now() - seen < ALERT_DEDUP_MS) { this._pendingAlert = null; return; }

    var node = this.alert(pend.spec);
    if (node) {
      this._alertSeen[pend.kind] = Date.now();
      this._pendingAlert = null;
      return;
    }
    pend.tries++;
    if (pend.tries >= 4) { this._pendingAlert = null; return; }  // не крутим вечно
    this._scheduleRetry();
  };

  /**
   * Неблокирующее сообщение: факт -> контекст -> действие, код справа.
   * Не модалка, не отбирает фокус, закрывается само или кнопкой.
   */
  Hud.prototype.alert = function (spec) {
    var host = this._alertHost();
    if (!host || !spec) return null;
    var now = Date.now();
    var rank = typeof spec.rank === 'number' ? spec.rank : SEV_RANK.info;
    /*
     * Не спамим — но и не глушим важное. Внутри интервала ALERT_GAP_MS проходит
     * только сообщение строго важнее последнего показанного: иначе системный тост
     * об ошибке ядра (info) мог бы задержать первое предупреждение о телефоне.
     */
    if (now - this._lastAlertAt < ALERT_GAP_MS && rank <= this._lastAlertRank) return null;
    this._lastAlertAt = now;
    this._lastAlertRank = rank;

    var node = mk('div', 'alert alert--toast', { role: 'status' });
    node.style.setProperty('--tone', spec.tone || 'var(--sev-info)');
    node.innerHTML =
      '<span class="alert-bar" aria-hidden="true"></span>' +
      '<div><strong>' + escapeHtml(spec.title || 'Наблюдение') + '</strong>' +
      '<p>' + escapeHtml(spec.text || '') + '</p></div>' +
      '<code class="mono">' + escapeHtml(spec.code || 'OBS_000') + '</code>' +
      '<button class="alert__close" type="button" aria-label="Скрыть сообщение">' +
        '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">' +
          '<path d="M3 3 13 13 M13 3 3 13" style="stroke:currentColor;fill:none" stroke-width="1.8"/>' +
        '</svg>' +
      '</button>';

    var self = this;
    var close = function () { self._removeAlert(node); };
    var btn = node.querySelector('.alert__close');
    if (btn) btn.addEventListener('click', close);

    host.appendChild(node);
    this._alerts.push(node);
    while (this._alerts.length > MAX_ALERTS) this._removeAlert(this._alerts[0]);

    setTimeout(close, ALERT_MS);
    return node;
  };

  Hud.prototype._removeAlert = function (node) {
    if (!node) return;
    var idx = this._alerts.indexOf(node);
    if (idx >= 0) this._alerts.splice(idx, 1);
    if (!node.parentNode) return;
    if (this._reduced) { node.parentNode.removeChild(node); return; }
    node.classList.add('is-out');
    setTimeout(function () {
      if (node.parentNode) node.parentNode.removeChild(node);
    }, 300);
  };

  /** Совместимость с app.js: мягкое сообщение об ошибке ядра. */
  Hud.prototype.toast = function (title, sub) {
    return this.alert({
      tone: 'var(--sev-info)',
      title: title || 'Наблюдение',
      text: sub || '',
      code: 'SYS_100',
      rank: SEV_RANK.info
    });
  };

  /* ------------------------------------------------------------ пауза */

  /** Условия возврата: выводим из того, что реально зафиксировано. */
  Hud.prototype._conditions = function () {
    var out = [];
    var seen = {};
    var cutoff = (Date.now() - 60000) / 1000;
    for (var i = this.events.length - 1; i >= 0 && out.length < 3; i--) {
      var ev = this.events[i];
      if (typeof ev.ts === 'number' && ev.ts < cutoff) break;
      if ((SEV_RANK[severityOf(ev)] || 0) < 1) continue;
      if (seen[ev.kind]) continue;
      seen[ev.kind] = 1;
      out.push(adviceFor(ev.kind).act);
    }
    if (!out.length) {
      out = [
        'Сядьте так, чтобы лицо полностью попадало в кадр.',
        'Смотрите на экран, уберите телефон и посторонние предметы.',
        'В кадре должны быть только вы.'
      ];
    }
    return out;
  };

  Hud.prototype.showPause = function (reason) {
    if (!this.dom.pause) return;
    var ev = this._lastSignificant(60000);

    if (this.dom.pauseReason) {
      var fact = ev ? eventText(ev) + '. ' : '';
      var ctx = ev ? contextFor(ev) : '';
      // Порог в тексте — тот, который СРАБОТАЛ. При ожидании решения проктора
      // это порог блокировки, а не паузы: автоматика дошла до него и на этом
      // остановилась. Прежний текст писал «порог паузы — 60» под риском 95,
      // то есть называл студенту не ту причину, по которой он стоит.
      var threshold = this.reviewRequired ? RISK_LOCK : RISK_PAUSE;
      var thresholdWord = this.reviewRequired
        ? 'порог, при котором требуется решение проктора, — '
        : 'порог паузы — ';
      this.dom.pauseReason.textContent =
        fact + (ctx ? ctx + ' ' : '') +
        'Оценка риска ' + Math.round(this.score) + ' из 100, ' + thresholdWord +
        threshold + '. ' + (reason ? reason : '');
    }
    if (this.dom.pauseCode) {
      this.dom.pauseCode.textContent = ev ? codeFor(ev) : 'PAU_600';
    }
    if (this.dom.pauseCond) {
      var conds = this._conditions();
      var html = '';
      for (var i = 0; i < conds.length; i++) html += '<li>' + escapeHtml(conds[i]) + '</li>';
      this.dom.pauseCond.innerHTML = html;
    }
    this._renderPauseReview();

    this._allowResume(false);
    if (this.dom.pause.hidden) {
      this._focusBeforeOverlay = document.activeElement;
      this.dom.pause.hidden = false;
      this._focusOverlay(this.dom.pauseCard);
      if (this.onPause) { try { this.onPause(); } catch (e) {} }
    }
  };

  Hud.prototype._allowResume = function (ok) {
    if (this.dom.resumeBtn) this.dom.resumeBtn.disabled = !ok;
    if (this.dom.pauseWait) {
      var text;
      if (this.reviewRequired) {
        // Эту приостановку снимает человек. Говорить студенту «ожидание
        // нормализации» было бы неправдой: сколько бы ни нормализовалось
        // наблюдение, кнопка не разблокируется — и он обязан это знать.
        var code = (this.review && this.review.session_code_display) || this.sessionId;
        text = 'Продолжение возможно только по решению проктора. Сообщите ему код '
             + 'сессии ' + code + '. Ожидание нормализации наблюдения паузу не снимет.';
      } else {
        text = ok
          ? 'Наблюдение в норме — можно продолжать.'
          : 'Ожидание нормализации: условия выше ещё не выполнены.';
      }
      this.dom.pauseWait.textContent = text;
    }
    if (ok && this.dom.resumeBtn && this.dom.pause && !this.dom.pause.hidden) {
      try { this.dom.resumeBtn.focus(); } catch (e) {}
    }
  };

  /** Снять паузу. byUser=true — студент нажал «Продолжить тест». */
  Hud.prototype.clearPause = function (byUser) {
    if (!this.dom.pause || this.dom.pause.hidden) return;
    if (byUser && this.dom.resumeBtn && this.dom.resumeBtn.disabled) return;
    // Последний предохранитель. Кнопка на этой ветке уже заблокирована, но
    // снятие паузы вызывается и из кода (горячая клавиша, таймер, повторный
    // вердикт), а приостановку, которая ждёт решения человека, не снимает
    // ничто, кроме этого решения — то есть вердикта с review_required=false.
    if (byUser && this.reviewRequired) return;
    this.dom.pause.hidden = true;
    this._restoreFocus();
    if (this.onResume) { try { this.onResume(); } catch (e) {} }
  };

  /* --------------------------------------------------------- блокировка */

  Hud.prototype.showLock = function (reason) {
    this.clearPause(false);
    if (!this.dom.lock) return;

    var ev = this._lastSignificant(120000);
    this.incidentCode = ev ? codeFor(ev) : 'LCK_900';

    if (this.dom.lockReason) {
      var fact = ev ? eventText(ev) + '. ' : '';
      var ctx = ev ? contextFor(ev) : '';
      // Авторство решения. При политике по умолчанию до этого экрана доходят
      // ТОЛЬКО через решение человека (proctor_lock), поэтому прежний текст
      // «накопленная оценка риска достигла порога блокировки» был неправдой
      // по построению: он приписывал автоматике решение, которого она не
      // принимает. Автоматический текст остаётся для сессий с --auto-lock.
      var who;
      if (this.decidedBy) {
        who = 'Экзамен закрыт решением проктора (' + this.decidedBy + ') '
            + 'при оценке риска ' + Math.round(this.score) + ' из 100. '
            + 'Решение принял человек, а не система.';
      } else if (this.reviewRequired) {
        who = 'Экзамен закрыт по запросу решения проктора при оценке риска '
            + Math.round(this.score) + ' из 100.';
      } else {
        who = 'Накопленная оценка риска ' + Math.round(this.score)
            + ' из 100 достигла порога блокировки ' + RISK_LOCK
            + ', и сессия закрыта автоматически (режим --auto-lock).';
      }
      this.dom.lockReason.textContent =
        fact + (ctx ? ctx + ' ' : '') + who + ' ' + (reason ? reason : '');
    }
    if (this.dom.lockSession) this.dom.lockSession.textContent = this.sessionId;
    if (this.dom.lockCode) this.dom.lockCode.textContent = this.incidentCode;
    if (this.dom.lockTime) this.dom.lockTime.textContent = hhmmss();

    if (this.dom.lock.hidden) {
      this._focusBeforeOverlay = document.activeElement;
      this.dom.lock.hidden = false;
      this._focusOverlay(this.dom.lockBtn);
      if (this.onLock) { try { this.onLock(); } catch (e) {} }
    }
  };

  Hud.prototype.hideLock = function () {
    if (this.dom.lock) this.dom.lock.hidden = true;
    this._restoreFocus();
  };

  Hud.prototype._focusOverlay = function (node) {
    if (!node) return;
    try {
      if (node.tabIndex < 0 && !/^(BUTTON|A|INPUT|SELECT|TEXTAREA)$/.test(node.tagName)) {
        node.setAttribute('tabindex', '-1');
      }
      node.focus();
    } catch (e) { /* фокус не критичен */ }
  };

  Hud.prototype._restoreFocus = function () {
    var node = this._focusBeforeOverlay;
    this._focusBeforeOverlay = null;
    if (node && typeof node.focus === 'function') {
      try { node.focus(); } catch (e) {}
    }
  };

  /* ----------------------------------------------------------- сводка */

  /** Сводка для экрана отчёта (app.js). */
  Hud.prototype.summary = function () {
    var high = 0;
    for (var i = 0; i < this.events.length; i++) {
      if ((SEV_RANK[severityOf(this.events[i])] || 0) >= 3) high++;
    }
    return {
      score: this.score,
      level: levelOf(this.score),
      total: this.events.length + this._dropped,
      high: high,
      dropped: this._dropped,
      events: this.events.slice(),
      breakdown: this.lastBreakdown || [],
      incidentCode: this.incidentCode
    };
  };

  /* -------------------------------------------------------------- экспорт */

  window.Proctor = window.Proctor || {};
  window.Proctor.hud = new Hud();

  // Общий словарь формулировок: им пользуется экран отчёта в app.js.
  window.Proctor.text = {
    event: EVENT_TEXT,
    short: EVENT_SHORT,
    advice: ADVICE,
    zone: ZONE_TEXT,
    state: STATE_TEXT,
    channels: CHANNELS,
    channelOf: KIND_CHANNEL,
    serviceKinds: SERVICE_KINDS,
    eventText: eventText,
    shortFor: shortFor,
    contextFor: contextFor,
    adviceFor: adviceFor,
    codeFor: codeFor,
    levelOf: levelOf,
    colorFor: colorFor,
    levels: LEVELS,
    hhmmss: hhmmss,
    escapeHtml: escapeHtml,
    glyph: glyph,
    sevRank: SEV_RANK,
    sevTone: SEV_TONE,
    sevText: SEV_TEXT,
    thresholds: { warn: RISK_WARN, pause: RISK_PAUSE, lock: RISK_LOCK },
    ringLen: RING_LEN,
    // распознавание попытки выйти за правила: нужно и экрану отчёта в app.js
    isNavBlock: isNavBlock,
    isSubresBlock: isSubresBlock,
    isProfileScope: isProfileScope,
    navTarget: navTarget,
    navFact: navFact,
    subresFact: subresFact
  };

  /*
   * Профиль экзамена — один источник на весь renderer.
   *
   * Разбор лежит здесь, а не в app.js, по порядку загрузки: hud.js
   * подключается вторым, app.js последним (см. index.html), поэтому экраны
   * согласия, проверки и отчёта берут готовые правила отсюда, а не держат
   * второй, неизбежно разъезжающийся разбор того же window.proctor.
   */
  window.Proctor.profile = {
    /** Текущие правила. Всегда объект; пустой профиль — тоже ответ. */
    current: function () { return window.Proctor.hud.examProfile(); },
    /** Подписка на смену правил: экран перерисуется, когда профиль дойдёт. */
    onChange: function (cb) { window.Proctor.hud.onProfileChange(cb); },
    /** Применить конверт, в котором может лежать профиль (shell-status и т.п.). */
    apply: function (raw) { return window.Proctor.hud.setExamProfile(raw); },
    /*
     * Только разбор и чистые помощники. ФОРМУЛИРОВОК здесь нет сознательно:
     * тексты для студента живут в одном месте — в app.js, который их и
     * показывает. Вторая таблица фраз рядом с разбором неизбежно разъехалась
     * бы с первой, как однажды разъехались коды инцидентов.
     */
    normalize: normalizeProfile,
    empty: emptyProfile,
    hostOf: hostOf,
    isLocalUrl: isLocalUrl,
    shortHash: shortHash
  };
})();
