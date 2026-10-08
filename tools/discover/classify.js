'use strict';
/**
 * РАЗВЕДКА ИСТОЧНИКОВ LMS: слой правил. Без Electron, без ввода-вывода.
 *
 * Зачем этот файл отдельно от tools/discover/main.js: ровно по той же причине,
 * по которой shell/state.js отделён от shell/main.js. Решение «что это за
 * источник и попадёт ли он в черновик» обязано гоняться на чистом node, без
 * окна и без сети, — иначе проверить его можно только руками на живой LMS.
 *
 * ЧТО ЭТОТ ФАЙЛ НЕ ДЕЛАЕТ. Он не придумывает вторых правил белого списка.
 * Разбор записи, сверка хоста, опознание поисковика и ИИ-ассистента, класс
 * «зона целиком» — всё берётся из shell/state.js, то есть из того самого
 * слоя, который применяется на экзамене. Здесь добавлено только одно, чего в
 * ядре нет и быть не должно: СОВЕТ ЧЕЛОВЕКУ.
 *
 * ГЛАВНОЕ ПРАВИЛО ЭТОГО ФАЙЛА, И ОНО ОБРАТНОЕ ПРЕЖНЕМУ:
 *
 *      НЕ ОПОЗНАЛ — НЕ ВКЛЮЧИЛ.
 *
 * Первая версия работала наоборот: незнакомый хост попадал в белый список с
 * пометкой «решает человек». Ревью показало, чем это кончается: в черновик
 * уезжали pastebin.com, dropbox.com, notion.so, web.whatsapp.com,
 * raw.githubusercontent.com и cdn.jsdelivr.net — последний по схеме
 * `/gh/<пользователь>/<репозиторий>@<ветка>/<файл>` отдаёт ЛЮБОЙ файл из
 * ЛЮБОГО публичного репозитория GitHub. Студент за минуту делает репозиторий
 * со шпаргалкой и читает её с хоста, который инструмент назвал «вероятно
 * нужен». Пометка не спасала: запись уже лежала в `allowed_origins`.
 *
 * Сверка с ядром от этого тоже не защищает, и это надо сказать прямо: ядро
 * знает три класса отказа (`search`, `assistant`, `too_broad`), всё остальное
 * для него `ok`. «Ядро примет» означает «запись подействует», а не «через неё
 * нельзя списать».
 *
 * Поэтому в `allowed_origins` черновика попадают только три вещи: сам адрес
 * экзамена, соседи по домену вуза и ИМЕНОВАННЫЙ список CDN с неизменяемым
 * содержимым. Всё прочее, что страница запрашивала, уходит в раздел
 * «КАНДИДАТЫ» — с объяснением, что именно откроется, и с готовой строкой для
 * вставки руками. Цена ошибки поменялась местами: вместо «открыли лишний
 * источник на весь экзамен» получается «у студента не загрузился виджет, и
 * проктор добавил строку, прочитав, что она значит».
 */

const path = require('path');

const {
  TOO_BROAD_SUFFIXES,
  parseOriginSpec,
  originRejectText,
  searchSourceMatch,
  aiSourceMatch,
  isLoopbackHost,
  hostUnderDomain,
  examResourceClass,
} = require(path.join(__dirname, '..', '..', 'shell', 'state.js'));

/** Метка-страж в `notes` черновика. Её ищет ядро (sidecar/config.py). */
const DRAFT_SENTINEL = 'ЧЕРНОВИК-НЕ-УТВЕРЖДЁН';

/** Первые строки любого вывода инструмента. Не мелким шрифтом и не в конце. */
const INCOMPLETE_WARNING = [
  'СОБРАНО ТОЛЬКО ТО, ЧТО ВЫ ПРОШЛИ РУКАМИ. Это не полный список источников',
  'вашей LMS, а список запросов тех страниц, которые открылись за этот прогон.',
  'Чего вы не открыли — того здесь нет, и инструмент об этом знать не может.',
  'ПРИМЕНЯТЬ НЕЛЬЗЯ, ПОКА СПИСОК НЕ ПРОЧИТАЛ ЧЕЛОВЕК и не заполнил поля',
  'issued_by, issued_at, institution, exam_id. Пока в файле стоят заглушки',
  `<...> или метка ${DRAFT_SENTINEL}, сайдкар НЕ СЧИТАЕТ экзамен готовым`,
  'и пишет это в шапку отчёта.',
];

/*
 * Метки второго уровня, которые в любой стране означают «класс организаций»:
 * edu.kz, ac.uk, com.tr. Зеркало PROFILE_REGISTRY_LABELS из sidecar/config.py.
 * Нужно ровно для одного — найти «свой домен вуза» в имени хоста LMS:
 * у lms.astanait.edu.kz свой домен это astanait.edu.kz, а не edu.kz.
 */
const REGISTRY_LABELS = Object.freeze([
  'edu', 'ac', 'com', 'co', 'org', 'net', 'gov', 'mil', 'int', 'or', 'ne', 'go',
]);

/** Зеркало `_suffix_like()` из sidecar/config.py: это зона/класс, а не источник. */
function registryLike(host) {
  const labels = String(host || '').split('.').filter(Boolean);
  if (labels.length <= 1) return true;
  if (labels.length === 2 && REGISTRY_LABELS.indexOf(labels[0]) !== -1
      && labels[1].length <= 3) {
    return true;
  }
  return false;
}

/** Зона это или класс организаций — по обоим правилам сразу. */
function isZone(host) {
  const h = String(host || '').toLowerCase();
  return TOO_BROAD_SUFFIXES.indexOf(h) !== -1 || registryLike(h);
}

/**
 * «Свой домен» вуза по адресу LMS: самый короткий суффикс имени, который уже
 * НЕ является зоной. Для lms.astanait.edu.kz это astanait.edu.kz.
 *
 * Для чего: отличить соседний хост того же вуза (moodle., files., video.) от
 * постороннего. Это НЕ повод писать в черновик `*.astanait.edu.kz` — черновик
 * перечисляет найденные хосты поимённо, см. buildDraft().
 */
function siteBase(host) {
  const labels = String(host || '').toLowerCase().split('.').filter(Boolean);
  if (!labels.length) return '';
  for (let i = labels.length - 1; i >= 0; i -= 1) {
    const candidate = labels.slice(i).join('.');
    if (!isZone(candidate)) return candidate;
  }
  return labels.join('.');
}

/*
 * ПОСТАВЩИКИ ЕДИНОГО ВХОДА. Этот список — самое ценное, что инструмент говорит
 * человеку, и единственное место, где он обязан быть громким.
 *
 * Разрешив login.microsoftonline.com, вуз открывает не «вход», а весь аккаунт:
 * по тому же адресу идёт выдача токена к Outlook, Teams и OneDrive, и со
 * страницы экзамена они доступны тем же живым сеансом. Это не домысел про
 * будущее, а то, как работает единый вход: один поставщик — один аккаунт.
 *
 * Поэтому запись SSO в черновик НЕ попадает. Она печатается отдельным
 * разделом с прямым текстом, и решение принимает человек: либо вход
 * выполняется ДО старта экзамена и в белый список не входит вовсе (так
 * правильно), либо вуз сознательно соглашается открыть почту и файлы.
 */
const SSO_PROVIDERS = Object.freeze([
  {
    match: ['login.microsoftonline.com', 'microsoftonline.com', 'login.microsoft.com',
      'login.live.com', 'login.windows.net', 'sts.windows.net', 'msauth.net',
      'msftauth.net', 'aadcdn.msauth.net', 'office.com', 'office365.com'],
    name: 'Microsoft (Azure AD / Entra ID)',
    opens: 'Outlook (почта), Teams (чат) и OneDrive (файлы) — это один и тот же '
      + 'аккаунт и один и тот же живой сеанс',
  },
  {
    match: ['accounts.google.com', 'accounts.youtube.com', 'oauth2.googleapis.com'],
    name: 'Google',
    opens: 'Gmail (почта), Google Drive (файлы) и Google Docs — это один и тот же '
      + 'аккаунт и один и тот же живой сеанс',
  },
  {
    match: ['okta.com', 'oktapreview.com', 'okta-emea.com'],
    name: 'Okta',
    opens: 'всё, что вуз подключил к Okta: почту, диск, корпоративный чат',
  },
  {
    match: ['auth0.com'],
    name: 'Auth0',
    opens: 'всё, что подключено к этому арендатору Auth0',
  },
  {
    match: ['onelogin.com'],
    name: 'OneLogin',
    opens: 'всё, что вуз подключил к OneLogin',
  },
]);

/*
 * Опознание по ИМЕНИ хоста, когда поставщик свой, а не покупной: Keycloak и
 * Shibboleth вуз ставит у себя, и хост выглядит как idp.vuz.edu.kz.
 *
 * Метки `login`, `signin`, `account`, `passport`, `id` добавлены ПО ИТОГАМ
 * РЕВЬЮ. Прежний список держался на словах `idp`, `sso`, `saml` — и мимо
 * проходило всё, что вузы называют по-человечески: login.astanait.edu.kz
 * уезжал в «свой домен вуза — вероятно нужен», а auth.kaznu.kz,
 * passport.satbayev.edu.kz, id.narxoz.kz, account.kbtu.kz и signin.enu.kz —
 * в «не опознано», то есть (в прежней версии) прямо в белый список. Разделение
 * фаз «окно входа отдельно от экзамена» держится именно на этом опознании,
 * поэтому приоритет у него выше, чем у «своего домена».
 */
const SSO_NAME_HINTS = Object.freeze([
  'keycloak', 'shibboleth', 'simplesaml', 'adfs', 'openam', 'gluu',
  'idp', 'sso', 'oauth', 'oauth2', 'oidc', 'openid', 'saml', 'cas',
  'login', 'logon', 'signin', 'signon', 'auth', 'authn', 'authz',
  'account', 'accounts', 'passport', 'id', 'ids', 'identity', 'token', 'ldap',
]);

/** Счётчики и аналитика: страница без них работает, оценка от них не зависит. */
const ANALYTICS_HOSTS = Object.freeze([
  'google-analytics.com', 'analytics.google.com', 'googletagmanager.com',
  'doubleclick.net', 'googlesyndication.com', 'googleadservices.com',
  'mc.yandex.ru', 'metrika.yandex.ru', 'yandexmetrica.com', 'top-fwz1.mail.ru',
  'matomo.cloud', 'piwik.pro', 'hotjar.com', 'hotjar.io', 'clarity.ms',
  'facebook.net', 'facebook.com', 'connect.facebook.net', 'fbcdn.net',
  'sentry.io', 'bugsnag.com', 'newrelic.com', 'nr-data.net',
  // Сборщик телеметрии Microsoft (OneCollector). Попал в этот список ПО ИТОГАМ
  // живого прогона на LMS заказчика: страница входа Microsoft стучит туда
  // запросом из скрипта. Имя записано как `events.data.microsoft.com`, а не
  // `microsoft.com` — иначе под аналитику уехал бы весь Microsoft, включая
  // copilot.microsoft.com, у которого в ядре свой класс.
  'events.data.microsoft.com', 'self.events.data.microsoft.com',
  'amplitude.com', 'mixpanel.com', 'segment.com', 'segment.io',
  'vk.com', 'vk.ru', 'tiktok.com', 'criteo.com', 'adsrvr.org',
]);

/*
 * ЖИВОЙ ЧАТ С ЧЕЛОВЕКОМ. Отдельный класс, и появился он по итогам ревью.
 *
 * Прежняя версия относила intercom.io и crisp.chat к АНАЛИТИКЕ, с текстом
 * «страница работает и без него, на оценку он не влияет». Про назначение это
 * неправда: это окно переписки с живым оператором, и на оценку он влияет
 * напрямую — в него можно отправить условие задачи и получить ответ. Админ, у
 * которого без виджета поехала вёрстка, по формулировке «безобидный счётчик»
 * впишет его обратно и не поймёт, что открыл канал связи.
 *
 * Второе, что нашло ревью: один виджет опознавался двумя способами.
 * widget.intercom.io считался аналитикой, а js.intercomcdn.com — «не
 * опознано», то есть попадал в белый список. Поэтому здесь перечислены ВСЕ
 * известные хосты каждого поставщика, включая те, что выглядят как CDN.
 */
const LIVE_CHAT_HOSTS = Object.freeze([
  'zdassets.com', 'zendesk.com', 'zopim.com', 'zdusercontent.com',
  'tawk.to', 'intercom.io', 'intercom.com', 'intercomcdn.com', 'intercomassets.com',
  'crisp.chat', 'crisp.im', 'olark.com', 'tidio.co', 'tidiochat.com',
  'chatra.io', 'chatra.com', 'livechatinc.com', 'livechat.com',
  'jivosite.com', 'jivo.ru', 'jivochat.com', 'drift.com', 'driftt.com',
  'freshchat.com', 'freshworks.com', 'smartsupp.com', 'chaport.com',
  'verbox.ru', 'talk-me.ru', 'callibri.ru', 'carrotquest.io', 'webim.ru',
  'helpcrunch.com', 'userlike.com', 'purechat.com', 'liveperson.net',
]);

/*
 * ИСТОЧНИКИ С ПРОИЗВОЛЬНЫМ СОДЕРЖИМЫМ. Главная находка ревью.
 *
 * Это ОДИНОЧНЫЕ хосты, которые отдают всё, что на них положили, — и положить
 * может кто угодно, в том числе студент за минуту до экзамена. Перечисление
 * «поимённо, а не записью *.домен» здесь не помогает вообще: имя одно,
 * содержимое любое.
 *
 * Проверено curl:
 *   cdn.jsdelivr.net/gh/torvalds/linux@master/README  -> отдаёт файл из
 *   любого публичного репозитория GitHub по схеме /gh/<юзер>/<репо>@<ветка>/
 *   unpkg.com/<любой-опубликованный-npm-пакет>/<файл> -> отдаёт
 *
 * Поэтому класс не включается в черновик, а у каждой записи печатается, что
 * именно через неё открывается. Если без такого источника страница ломается
 * (Bootstrap и MathJax вузы правда тянут с jsDelivr), правильный ход — не
 * вписать хост, а положить файлы на свой домен вуза. Вторым по правильности
 * идёт осознанная строка руками.
 */
const OPEN_CONTENT_HOSTS = Object.freeze([
  {
    match: ['jsdelivr.net', 'cdn.jsdelivr.net', 'fastly.jsdelivr.net'],
    what: 'ЛЮБОЙ файл из ЛЮБОГО публичного репозитория GitHub (схема '
      + '/gh/<юзер>/<репо>@<ветка>/<файл>) и любой пакет npm',
  },
  {
    match: ['unpkg.com'],
    what: 'любой файл из любого пакета npm; пакет публикуется за минуту',
  },
  {
    match: ['storage.googleapis.com', 'firebasestorage.googleapis.com',
      'appspot.com'],
    what: 'любое публичное ведро Google Cloud Storage',
  },
  {
    match: ['s3.amazonaws.com', 'amazonaws.com', 's3.eu-central-1.amazonaws.com'],
    what: 'любое публичное ведро Amazon S3',
  },
  {
    match: ['cloudfront.net', 'akamaihd.net', 'akamaized.net', 'fastly.net',
      'b-cdn.net', 'bunnycdn.com', 'cdn77.org', 'kxcdn.com'],
    what: 'раздача любого сайта, который завёл себе эту раздачу: имя общее, '
      + 'содержимое — чужое и произвольное',
  },
  {
    match: ['cdn.discordapp.com', 'discordapp.net', 'media.discordapp.net'],
    what: 'любой файл, который кто угодно загрузил в Discord',
  },
  {
    match: ['raw.githubusercontent.com', 'githubusercontent.com',
      'gist.githubusercontent.com', 'github.io', 'gitcdn.link', 'statically.io',
      'rawcdn.githack.com', 'raw.githack.com'],
    what: 'любой файл из любого репозитория GitHub, включая созданный сегодня',
  },
  {
    match: ['pastebin.com', 'paste.ee', 'hastebin.com', 'dpaste.org',
      'ghostbin.com', 'controlc.com', 'rentry.co', 'telegra.ph', 'justpaste.it'],
    what: 'произвольный текст, опубликованный кем угодно, — то есть шпаргалка',
  },
  {
    match: ['dropbox.com', 'dropboxusercontent.com', 'onedrive.live.com',
      'sharepoint.com', '1drv.ms', 'mega.nz', 'mega.io', 'wetransfer.com',
      'file.io', 'transfer.sh', 'catbox.moe', 'anonfiles.com', 'disk.yandex.ru',
      'cloud.mail.ru', 'files.fm', 'gofile.io', 'pixeldrain.com'],
    what: 'файлообменник: чужие файлы по ссылке и загрузка своих',
  },
  {
    match: ['notion.so', 'notion.site', 'coda.io', 'airtable.com',
      'evernote.com', 'obsidian.md', 'hackmd.io', 'etherpad.org'],
    what: 'заметки и документы, которые можно подготовить заранее и открыть '
      + 'на экзамене',
  },
  {
    match: ['codepen.io', 'jsfiddle.net', 'codesandbox.io', 'stackblitz.com',
      'replit.com', 'repl.it', 'glitch.me', 'netlify.app', 'vercel.app',
      'pages.dev', 'surge.sh', 'herokuapp.com', 'ngrok.io', 'ngrok-free.app',
      'trycloudflare.com', 'loca.lt'],
    what: 'площадка, где любой человек поднимает свою страницу за минуту — '
      + 'в том числе страницу со шпаргалкой или с чужим чатом внутри',
  },
  {
    match: ['polyfill.io'],
    what: 'скрипт, который сервер выбирает сам по вашему браузеру. В 2024 году '
      + 'через этот хост раздавали постороннюю вставку всем, кто его подключил',
  },
]);

/*
 * САЙТЫ С ОТВЕТАМИ НА ЗАДАНИЯ. Разрешать их на экзамене — то же, что
 * разрешить поисковик, только адреснее: там лежат решения типовых задач и
 * живые люди, готовые решить за деньги.
 */
const HOMEWORK_HOSTS = Object.freeze([
  'chegg.com', 'coursehero.com', 'studocu.com', 'studylib.net', 'studfile.net',
  'brainly.com', 'brainly.ru', 'znanija.com', 'gdz.ru', 'reshebnik.com',
  'symbolab.com', 'wolframalpha.com', 'mathway.com', 'photomath.com',
  'gauthmath.com', 'quizlet.com', 'slader.com', 'numerade.com', 'bartleby.com',
  'sdamzavas.net', 'studmed.ru', 'allbest.ru', 'cyberleninka.ru', 'scribd.com',
]);

/*
 * БОЛЬШИЕ САЙТЫ С ПОИСКОМ И ОБЩЕНИЕМ. В прежней версии youtube.com и zoom.us
 * лежали прямо в списке CDN, то есть предлагались как «вероятно нужны»:
 * youtube.com — полный сайт с поиском и комментариями (для встраивания
 * существует youtube-nocookie.com), zoom.us — веб-клиент с чатом и показом
 * экрана.
 */
const WIDE_SITE_HOSTS = Object.freeze([
  { match: ['youtube.com', 'www.youtube.com', 'm.youtube.com'],
    what: 'весь YouTube: поиск, комментарии и рекомендации. Для встроенных '
      + 'видео есть youtube-nocookie.com, и он тоже отдельное решение' },
  { match: ['zoom.us', 'zoom.com'],
    what: 'веб-клиент Zoom: звонок, чат и показ экрана' },
  { match: ['teams.microsoft.com', 'teams.live.com'],
    what: 'Teams: переписка и звонки' },
  { match: ['meet.google.com', 'webex.com', 'whereby.com'],
    what: 'видеозвонок с кем угодно' },
  { match: ['discord.com', 'discordapp.com'],
    what: 'Discord: чат, голос и показ экрана' },
  { match: ['whatsapp.com', 'web.whatsapp.com', 'messenger.com', 'viber.com',
    'signal.org', 'max.ru'],
    what: 'мессенджер в браузере' },
  { match: ['stackoverflow.com', 'stackexchange.com', 'superuser.com',
    'serverfault.com', 'askubuntu.com'],
    what: 'вопросы и ответы: готовые решения и возможность спросить' },
  { match: ['github.com', 'gitlab.com', 'bitbucket.org'],
    what: 'хранилище кода с поиском по всему публичному коду' },
  { match: ['reddit.com', 'quora.com', 'medium.com', 'habr.com', 'dzen.ru'],
    what: 'площадка с поиском, обсуждениями и возможностью задать вопрос' },
  { match: ['docs.google.com', 'drive.google.com', 'sheets.google.com',
    'keep.google.com'],
    what: 'документы и диск: свои заметки и совместная правка с другим человеком' },
]);

/** Переводчики: произвольный текст уходит на сторонний сервер и возвращается. */
const TRANSLATE_HOSTS = Object.freeze([
  'translate.googleapis.com', 'translate.google.com', 'translate-pa.googleapis.com',
  'deepl.com', 'api-free.deepl.com', 'api.deepl.com', 'libretranslate.com',
  'translate.yandex.net', 'reverso.net', 'linguee.com', 'promt.one',
]);

/** Видеоплееры и каталоги видео. Нужны только если в курсе правда есть видео. */
const VIDEO_HOSTS = Object.freeze([
  'player.vimeo.com', 'vimeo.com', 'vimeocdn.com', 'youtube-nocookie.com',
  'ytimg.com', 'wistia.com', 'wistia.net', 'kinescope.io', 'rutube.ru',
  'bigbluebutton.org', 'jitsi.net', 'brightcove.net', 'jwplayer.com',
  'vk.video', 'dailymotion.com',
]);

/*
 * CDN С НЕИЗМЕНЯЕМЫМ СОДЕРЖИМЫМ — ЕДИНСТВЕННЫЙ сторонний класс, который
 * попадает в черновик.
 *
 * Критерий ровно один: на этот хост нельзя положить свой файл. Сюда входят
 * шрифты Google, cdnjs (куда библиотека попадает через разбор заявки), и
 * раздачи конкретных библиотек под своим именем (code.jquery.com,
 * bootstrapcdn, fontawesome, datatables). Сюда НЕ входят jsDelivr и unpkg:
 * первый отдаёт любой репозиторий GitHub, второй — любой пакет npm, и оба
 * переехали в OPEN_CONTENT_HOSTS.
 */
const CDN_FIXED_HOSTS = Object.freeze([
  'fonts.googleapis.com', 'fonts.gstatic.com', 'gstatic.com',
  'cdnjs.cloudflare.com', 'ajax.googleapis.com',
  'maxcdn.bootstrapcdn.com', 'stackpath.bootstrapcdn.com', 'bootstrapcdn.com',
  'code.jquery.com', 'cdn.datatables.net',
  'use.fontawesome.com', 'kit.fontawesome.com', 'cdn.fontawesome.com',
  'fonts.bunny.net', 'cdn.mathjax.org',
]);

/**
 * Имя хоста НАМЕКАЕТ на доставку статики: cdn.vuz.kz, static.lms.kz.
 *
 * Прежде такое совпадение давало вердикт «вероятно нужен» и запись в белый
 * список. Ревью показало цену: static.zdassets.com — это загрузчик ЖИВОГО
 * ЧАТА Zendesk, и он получал «вероятно нужен» за то, что его НАЗВАЛИ
 * `static`. Теперь намёк по имени не включает ничего: он только даёт человеку
 * подсказку в разделе кандидатов.
 */
const CDN_NAME_HINTS = Object.freeze([
  'cdn', 'static', 'assets', 'media', 'img', 'images', 'files', 'storage',
  's3', 'fonts', 'video', 'stream', 'player',
]);

function matchAnyDomain(host, list) {
  for (const d of list) {
    if (hostUnderDomain(host, d)) return d;
  }
  return '';
}

/** Найти запись вида {match:[...], what:'...'} по хосту. */
function matchAnyGroup(host, groups) {
  for (const group of groups) {
    const hit = matchAnyDomain(host, group.match);
    if (hit) return { matched: hit, what: group.what, name: group.name || '' };
  }
  return null;
}

function hasLabelHint(host, hints) {
  const labels = String(host || '').toLowerCase().split('.').filter(Boolean);
  for (const label of labels) {
    for (const hint of hints) {
      if (label === hint || label.startsWith(`${hint}-`) || label.endsWith(`-${hint}`)) {
        return hint;
      }
    }
  }
  return '';
}

function ssoProviderFor(host) {
  for (const provider of SSO_PROVIDERS) {
    const hit = matchAnyDomain(host, provider.match);
    if (hit) return { name: provider.name, opens: provider.opens, matched: hit };
  }
  const hint = hasLabelHint(host, SSO_NAME_HINTS);
  if (hint) {
    return {
      name: `похоже на свой поставщик входа вуза (по имени хоста: «${hint}»)`,
      opens: 'всё, что вуз подключил к этому входу. Проверьте глазами, что именно',
      matched: hint,
    };
  }
  return null;
}

/*
 * Классы совета.
 *
 * `include: true` стоит ровно у трёх классов — адрес экзамена, свой домен вуза
 * и CDN с неизменяемым содержимым. Всё остальное `false`. Это и есть разворот
 * правила «не опознал → включил» в «не опознал → НЕ включил»: разница между
 * «не знаю» и «можно» восстановлена, и восстановлена в сторону «не знаю».
 */
const VERDICTS = Object.freeze({
  exam_host: {
    include: true, rank: 0,
    title: 'сам адрес экзамена — нужен',
    advice: 'без него страница экзамена не откроется; ядро добавляет этот хост '
      + 'в действующий список само',
  },
  own_domain: {
    include: true, rank: 1,
    title: 'свой домен вуза — вероятно нужен',
    advice: 'тот же домен, что у адреса экзамена. В черновик попадает ПОИМЁННО, '
      + 'а не записью «*.домен»: под класс поддоменов попадают форум, '
      + 'файлообменник и библиотека вуза. Проверьте глазами: под своим доменом '
      + 'вуз мог поднять и облако для файлов, и почту',
  },
  cdn_fixed: {
    include: true, rank: 2,
    title: 'CDN с неизменяемым содержимым — вероятно нужен',
    advice: 'отдаёт только библиотеки и шрифты, свой файл на него положить '
      + 'нельзя. Без него у студента не загрузится скрипт или шрифт, и это '
      + 'будет выглядеть как сломанный экзамен',
  },
  sso: {
    include: false, rank: 3,
    title: 'ПОСТАВЩИК ЕДИНОГО ВХОДА — в черновик не включён',
    advice: '',   // собирается отдельно, текст зависит от поставщика
  },
  open_content: {
    include: false, rank: 4,
    title: 'ИСТОЧНИК С ПРОИЗВОЛЬНЫМ СОДЕРЖИМЫМ — в черновик НЕ включён',
    advice: '',   // собирается отдельно, текст зависит от хоста
  },
  homework: {
    include: false, rank: 5,
    title: 'САЙТ С ОТВЕТАМИ НА ЗАДАНИЯ — в черновик НЕ включён',
    advice: 'там лежат решения типовых задач, и там же сидят люди, готовые '
      + 'решить за деньги. Ядро такой хост НЕ отклонит — он не поисковик и не '
      + 'ИИ-ассистент, — так что единственный барьер здесь ваш',
  },
  live_chat: {
    include: false, rank: 6,
    title: 'ЖИВОЙ ЧАТ С ЧЕЛОВЕКОМ — в черновик НЕ включён',
    advice: 'это НЕ счётчик: виджет открывает окно переписки с живым '
      + 'оператором, и в него можно отправить условие задачи. Если без него '
      + 'едет вёрстка — лучше выключить виджет в самой LMS на время экзамена, '
      + 'чем открыть канал связи',
  },
  wide_site: {
    include: false, rank: 7,
    title: 'БОЛЬШОЙ САЙТ С ПОИСКОМ И ОБЩЕНИЕМ — в черновик НЕ включён',
    advice: '',   // собирается отдельно
  },
  translate: {
    include: false, rank: 8,
    title: 'ПЕРЕВОДЧИК — в черновик НЕ включён',
    advice: 'переводит произвольный текст по запросу: условие задачи уходит '
      + 'на сторонний сервер, а ответ возвращается на страницу. Если экзамен '
      + 'на иностранном языке и перевод разрешён правилами — впишите руками и '
      + 'назовите это в правилах экзамена вслух',
  },
  video: {
    include: false, rank: 9,
    title: 'видео или плеер — в черновик НЕ включён',
    advice: 'нужен только если в самом задании есть видео. Плеер обычно тянет '
      + 'за собой каталог и поиск по нему, поэтому по умолчанию не включаем',
  },
  analytics: {
    include: false, rank: 10,
    title: 'аналитика или счётчик — НЕ нужен',
    advice: 'страница работает и без него, на оценку он не влияет. '
      + 'Не включайте: каждая лишняя запись белого списка — это открытый '
      + 'источник на всё время экзамена',
  },
  cdn_guess: {
    include: false, rank: 11,
    title: 'имя намекает на статику, но СОДЕРЖИМОЕ НЕИЗВЕСТНО — не включён',
    advice: 'хост назвали cdn/static/files, и это всё, что про него известно. '
      + 'Так назывался static.zdassets.com — загрузчик живого чата Zendesk. '
      + 'Откройте адрес из строки «нужен потому что» в обычном браузере и '
      + 'посмотрите, что это, прежде чем вписывать',
  },
  unknown: {
    include: false, rank: 12,
    title: 'назначение НЕ ОПОЗНАНО — в черновик НЕ включён',
    advice: 'инструмент не знает, что это, и поэтому не предлагает. Откройте '
      + 'адрес из строки «нужен потому что» в обычном браузере, посмотрите, '
      + 'что отдаёт хост, и решите сами. Если без него ломается страница — '
      + 'впишите строку руками',
  },
  search: {
    include: false, rank: 13,
    title: 'ПОИСКОВИК — отказ, как в ядре',
    advice: 'ядро выбросит эту запись из белого списка (класс search): выдача '
      + 'показывает готовый ответ прямо на странице, переходить никуда не нужно',
  },
  ai: {
    include: false, rank: 14,
    title: 'ИИ-ассистент или мессенджер — отказ, как в ядре',
    advice: 'ядро выбросит эту запись из белого списка (класс assistant): '
      + 'отвечает на вопрос задания напрямую',
  },
  zone: {
    include: false, rank: 15,
    title: 'зона или класс организаций — отказ, как в ядре',
    advice: 'ядро выбросит эту запись (класс too_broad): под зоной лежат форумы, '
      + 'файлообменники и Moodle других вузов с теми же курсами',
  },
  local: {
    include: false, rank: 16,
    title: 'локальный адрес — в черновик профиля LMS не включён',
    advice: 'петля или .localhost: это стенд разработчика, а не источник вуза',
  },
  invalid: {
    include: false, rank: 17,
    title: 'запись не разбирается — отказ',
    advice: '',
  },
});

/**
 * Что это за источник и что с ним делать.
 *
 * Порядок проверок — это и есть содержание функции, он не случаен:
 *  1) мусор и зона — отказ ядра, обсуждать нечего;
 *  2) ПОИСКОВИК и ИИ раньше «своего домена»: вуз может поставить у себя
 *     зеркало или встроенный поиск, и прятать это за «свой домен» нельзя;
 *  3) SSO раньше «своего домена»: login.vuz.edu.kz — это вход, и опасен он
 *     именно как вход, а не как чужой хост;
 *  4) ПРОИЗВОЛЬНОЕ СОДЕРЖИМОЕ, сайты с ответами, живой чат, большие сайты и
 *     переводчики — раньше CDN и раньше намёка по имени: static.zdassets.com
 *     обязан опознаться как чат, а не как статика;
 *  5) свой домен, аналитика, CDN с неизменяемым содержимым;
 *  6) остальное — «не опознано», и в черновик оно НЕ идёт.
 *
 * @param {string} host имя хоста из запроса
 * @param {{examHost?:string, port?:string}} [opts]
 */
function classifySource(host, opts) {
  const o = opts || {};
  const h = String(host || '').trim().toLowerCase();
  const parsed = parseOriginSpec(o.port ? `${h}:${o.port}` : h);
  if (!parsed.ok) {
    const kind = parsed.reason === 'too_broad' ? 'zone' : 'invalid';
    return Object.assign({}, VERDICTS[kind], {
      kind,
      host: h,
      why: originRejectText(parsed.reason),
      advice: VERDICTS[kind].advice || originRejectText(parsed.reason),
    });
  }

  if (isLoopbackHost(h)) {
    return Object.assign({}, VERDICTS.local, { kind: 'local', host: h, why: '' });
  }

  const search = searchSourceMatch(h);
  if (search) {
    return Object.assign({}, VERDICTS.search, {
      kind: 'search', host: h, why: `опознан как «${search}»`,
    });
  }
  const ai = aiSourceMatch(h);
  if (ai) {
    return Object.assign({}, VERDICTS.ai, {
      kind: 'ai', host: h, why: `опознан как «${ai}»`,
    });
  }

  const sso = ssoProviderFor(h);
  if (sso) {
    return Object.assign({}, VERDICTS.sso, {
      kind: 'sso',
      host: h,
      provider: sso.name,
      why: `опознан как ${sso.name}`,
      advice: `РАЗРЕШИВ ЭТОТ ИСТОЧНИК, ВЫ ОТКРЫВАЕТЕ ${sso.opens.toUpperCase()}. `
        + 'Единый вход — это один аккаунт: страница экзамена сможет дойти до '
        + 'тех же данных тем же сеансом. Правильный путь другой: пусть студент '
        + 'входит в LMS ДО старта экзамена, и тогда вход в белый список не '
        + 'попадает вовсе. Если вуз всё же согласен — вписывайте запись руками '
        + 'и назовите это в правилах экзамена вслух',
    });
  }

  const open = matchAnyGroup(h, OPEN_CONTENT_HOSTS);
  if (open) {
    return Object.assign({}, VERDICTS.open_content, {
      kind: 'open_content',
      host: h,
      why: `опознан как «${open.matched}»`,
      opens: open.what,
      advice: `ЧЕРЕЗ ЭТОТ ХОСТ ОТКРЫВАЕТСЯ: ${open.what}. Имя у хоста одно, а `
        + 'содержимое любое, поэтому перечисление «поимённо, а не *.домен» '
        + 'здесь не помогает: студент за минуту публикует шпаргалку и читает '
        + 'её с того же адреса. Ядро такую запись НЕ отклонит — для него это '
        + 'обычный хост. Если страница без него ломается, правильный ход — '
        + 'положить эти файлы на свой домен вуза; вписать строку руками — '
        + 'второй по правильности',
    });
  }

  const homework = matchAnyDomain(h, HOMEWORK_HOSTS);
  if (homework) {
    return Object.assign({}, VERDICTS.homework, {
      kind: 'homework', host: h, why: `опознан как «${homework}»`,
    });
  }

  const chat = matchAnyDomain(h, LIVE_CHAT_HOSTS);
  if (chat) {
    return Object.assign({}, VERDICTS.live_chat, {
      kind: 'live_chat', host: h, why: `опознан как виджет поддержки «${chat}»`,
    });
  }

  const wide = matchAnyGroup(h, WIDE_SITE_HOSTS);
  if (wide) {
    return Object.assign({}, VERDICTS.wide_site, {
      kind: 'wide_site',
      host: h,
      why: `опознан как «${wide.matched}»`,
      opens: wide.what,
      advice: `ЧЕРЕЗ ЭТОТ ХОСТ ОТКРЫВАЕТСЯ: ${wide.what}. Ядро его НЕ отклонит, `
        + 'барьер здесь только ваш',
    });
  }

  const translate = matchAnyDomain(h, TRANSLATE_HOSTS);
  if (translate) {
    return Object.assign({}, VERDICTS.translate, {
      kind: 'translate', host: h, why: `опознан как «${translate}»`,
    });
  }

  const examHost = String(o.examHost || '').trim().toLowerCase();
  if (examHost && h === examHost) {
    return Object.assign({}, VERDICTS.exam_host, { kind: 'exam_host', host: h, why: '' });
  }

  const base = examHost ? siteBase(examHost) : '';
  if (base && hostUnderDomain(h, base)) {
    return Object.assign({}, VERDICTS.own_domain, {
      kind: 'own_domain', host: h, why: `поддомен «${base}» — домена самой LMS`,
    });
  }

  const analytics = matchAnyDomain(h, ANALYTICS_HOSTS);
  if (analytics) {
    return Object.assign({}, VERDICTS.analytics, {
      kind: 'analytics', host: h, why: `опознан как «${analytics}»`,
    });
  }

  const video = matchAnyDomain(h, VIDEO_HOSTS);
  if (video) {
    return Object.assign({}, VERDICTS.video, {
      kind: 'video', host: h, why: `опознан как «${video}»`,
    });
  }

  const cdn = matchAnyDomain(h, CDN_FIXED_HOSTS);
  if (cdn) {
    return Object.assign({}, VERDICTS.cdn_fixed, {
      kind: 'cdn_fixed', host: h, why: `опознан как «${cdn}»: свой файл туда не положить`,
    });
  }
  const cdnHint = hasLabelHint(h, CDN_NAME_HINTS);
  if (cdnHint) {
    return Object.assign({}, VERDICTS.cdn_guess, {
      kind: 'cdn_guess', host: h,
      why: `в имени хоста есть метка «${cdnHint}» — и это ВСЁ, что про него известно`,
    });
  }

  return Object.assign({}, VERDICTS.unknown, { kind: 'unknown', host: h, why: '' });
}

/** Человеческое имя типа ресурса — для строки «нужен потому что». */
const RESOURCE_NAMES = Object.freeze({
  mainFrame: 'саму страницу',
  subFrame: 'встроенный кадр',
  stylesheet: 'таблицу стилей',
  script: 'скрипт',
  image: 'картинку',
  font: 'шрифт',
  object: 'встроенный объект',
  xhr: 'данные запросом из скрипта',
  ping: 'служебный пинг',
  cspReport: 'отчёт о нарушении политики',
  media: 'видео или звук',
  webSocket: 'постоянное соединение (websocket)',
  other: 'ресурс',
});

function resourceName(type) {
  return RESOURCE_NAMES[String(type || '')] || `ресурс (${type || 'тип неизвестен'})`;
}

/**
 * Адрес для ПОКАЗА человеку: схема, хост, порт и путь. Запрос и фрагмент
 * отрезаются всегда.
 *
 * Это не косметика. В строке запроса Moodle носит `sesskey`, а поставщики
 * входа — одноразовые коды и токены. Инструмент печатает свой вывод на экран,
 * сохраняет в файл и показывает его на разборе; утащить туда живой токен
 * сеанса значило бы своими руками сделать из инструмента настройки утечку.
 */
function displayUrl(url) {
  const raw = String(url || '');
  let parsed = null;
  try { parsed = new URL(raw); } catch (err) { parsed = null; }
  if (!parsed) return raw.split('?')[0].slice(0, 200);
  const port = parsed.port ? `:${parsed.port}` : '';
  let p = String(parsed.pathname || '');
  if (p.length > 80) p = `${p.slice(0, 77)}...`;
  return `${parsed.protocol}//${parsed.hostname}${port}${p}`;
}

/** Ключ источника: `host` или `host:port`. Та же форма, что у ядра. */
function originKey(host, port) {
  const h = String(host || '').trim().toLowerCase();
  const p = String(port || '').trim();
  return p && p !== '80' && p !== '443' ? `${h}:${p}` : h;
}

/** Имя хоста из адреса или пустая строка. */
function hostOf(url) {
  try {
    const h = new URL(String(url || '')).hostname.toLowerCase();
    return h.replace(/^\[|\]$/g, '');
  } catch (err) {
    return '';
  }
}

/**
 * Сложить наблюдения в картину по источникам.
 *
 * @param {Array<{url:string, resourceType:string, page:string, phase:string, at:number}>} records
 * @param {{examUrl?:string}} [opts]
 */
function foldObservations(records, opts) {
  const o = opts || {};
  const examHost = hostOf(o.examUrl);

  const sources = new Map();
  let skipped = 0;
  for (const rec of (records || [])) {
    let parsed = null;
    try { parsed = new URL(String(rec && rec.url)); } catch (err) { parsed = null; }
    if (!parsed || !parsed.hostname) { skipped += 1; continue; }
    const host = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, '');
    const key = originKey(host, parsed.port);
    let item = sources.get(key);
    if (!item) {
      item = {
        key,
        host,
        port: String(parsed.port || ''),
        schemes: new Set(),
        resourceTypes: new Set(),
        classes: new Set(),
        phases: new Set(),
        firstPhase: String((rec && rec.phase) || 'auth'),
        requests: 0,
        // Примеры «нужен потому что»: страница -> что она попросила.
        examples: [],
      };
      sources.set(key, item);
    }
    item.requests += 1;
    item.schemes.add(String(parsed.protocol || ''));
    item.resourceTypes.add(String((rec && rec.resourceType) || 'other'));
    item.classes.add(examResourceClass(rec && rec.resourceType));
    item.phases.add(String((rec && rec.phase) || 'auth'));
    const example = {
      page: displayUrl((rec && rec.page) || ''),
      resource: resourceName(rec && rec.resourceType),
      url: displayUrl((rec && rec.url) || ''),
    };
    const already = item.examples.some((e) => e.page === example.page
      && e.resource === example.resource);
    // Трёх примеров человеку хватает, чтобы понять назначение источника;
    // полное число запросов при этом не теряется — оно в `requests`.
    if (!already && item.examples.length < 3) item.examples.push(example);
  }

  const out = [];
  for (const item of sources.values()) {
    const verdict = classifySource(item.host, { examHost, port: item.port });
    out.push(Object.assign({}, item, {
      schemes: Array.from(item.schemes).sort(),
      resourceTypes: Array.from(item.resourceTypes).sort(),
      classes: Array.from(item.classes).sort(),
      phases: Array.from(item.phases).sort(),
      insecure: item.schemes.has('http:'),
      verdict,
    }));
  }
  out.sort((a, b) => (a.verdict.rank - b.verdict.rank)
    || (b.requests - a.requests)
    || (a.key < b.key ? -1 : (a.key > b.key ? 1 : 0)));
  return { sources: out, examHost, skipped };
}

/** Строка «нужен потому что» — одна на пример. */
function whyLines(source) {
  const lines = [];
  for (const e of (source.examples || [])) {
    lines.push(`нужен потому что: ${e.page || 'страница экзамена'} запросила `
      + `${e.resource} — ${e.url}`);
  }
  if (!lines.length) lines.push('нужен потому что: запрос зафиксирован без страницы-источника');
  return lines;
}

/*
 * ЧЕГО РАЗВЕДКА НЕ ВИДЕЛА. Второй по важности раздел после самого списка.
 *
 * Ревью нашло на LMS заказчика живой случай, который объясняет, зачем это
 * нужно. Страница входа тянет собственный скрипт прокторинга вуза
 * (ps1-dev.oes.kz/inject.php), и скрипт СПЯЩИЙ: на странице входа он не делает
 * ничего, а просыпается по `#isExam` или по postMessage. Проснувшись, он
 * уходит на aitu.oes.kz и подключает ajax.googleapis.com. Оба адреса нужны
 * настоящему экзамену и физически невидимы для разведки со страницы входа.
 * Профиль, собранный «только по странице входа», заблокировал бы собственный
 * прокторинг заказчика: сорванный экзамен плюс ложный инцидент в журнале.
 *
 * Отсюда правило: инструмент обязан СЧИТАТЬ И ПЕЧАТАТЬ то, чего не видел.
 * Три вида такого знания, и все три попадают в файл, а не только на экран:
 *   1) адреса, которые страница ПРЕДЛАГАЕТ студенту ссылками и формами, но
 *      по которым никто не ходил (`offered`);
 *   2) адреса, НАЙДЕННЫЕ В ТЕКСТЕ скриптов страницы и ни разу не
 *      запрошенные (`in_scripts`) — ровно случай спящего inject.php;
 *   3) типы ресурсов, которых в прогоне не было вовсе: ни одного встроенного
 *      кадра, ни одного видео, ни одного websocket — значит путь студента не
 *      пройден.
 */

/** Типы ресурсов, отсутствие которых означает «путь студента не пройден». */
const PATH_SIGNALS = Object.freeze([
  { type: 'subFrame',
    missing: 'ни одного ВСТРОЕННОГО КАДРА: встроенные тесты, SCORM-пакеты и '
      + 'видео в задании не проверялись' },
  { type: 'media',
    missing: 'ни одного ВИДЕО И ЗВУКА: если в курсе есть видеолекции или '
      + 'аудирование, их источники в списке отсутствуют' },
  { type: 'webSocket',
    missing: 'ни одного ПОСТОЯННОГО СОЕДИНЕНИЯ (websocket): живые уведомления '
      + 'Moodle, чат курса и сторонний прокторинг на вебсокетах не проверялись' },
  { type: 'xhr',
    missing: 'ни одного ЗАПРОСА ИЗ СКРИПТА: страница почти не работала — '
      + 'сохранение ответа и таймер теста ходят именно так' },
  { type: 'font',
    missing: 'ни одного ШРИФТА: вёрстка страницы, похоже, не догрузилась' },
]);

/**
 * Собрать раздел «чего разведка не видела».
 *
 * @param {object} folded результат foldObservations()
 * @param {object} cov сырые счётчики прогона из main.js
 * @returns {object} готовый раздел `coverage` для файла
 */
function buildCoverage(folded, cov) {
  const c = cov || {};
  const sources = (folded && folded.sources) || [];
  const examHost = (folded && folded.examHost) || '';
  const seenKeys = new Set(sources.map((s) => s.key));
  const seenHosts = new Set(sources.map((s) => s.host));

  const typesSeen = new Set();
  for (const s of sources) for (const t of s.resourceTypes) typesSeen.add(t);

  const missing = [];
  for (const signal of PATH_SIGNALS) {
    if (!typesSeen.has(signal.type)) missing.push(signal.missing);
  }
  const pages = Array.from(new Set((c.pages || []).filter(Boolean))).sort();
  if (pages.length <= 1) {
    missing.push('открыта ВСЕГО ОДНА страница: дальше входа разведка не ходила, '
      + 'а курс, тест и загрузка файла тянут свои источники');
  }
  if (!c.phaseMarked) {
    missing.push('отметку «вход завершён» НЕ СТАВИЛИ: разделить окно входа и '
      + 'окно экзамена нечем, все источники отнесены к фазе входа');
  }

  /** Классифицировать и описать список хостов, которых мы не запрашивали. */
  const describe = (hosts) => Array.from(new Set((hosts || [])
    .map((h) => String(h || '').trim().toLowerCase())
    .filter(Boolean)))
    .filter((h) => !seenHosts.has(h))
    .sort()
    .map((h) => {
      const v = classifySource(h, { examHost });
      return { host: h, class: v.kind, title: v.title };
    });

  const offered = describe(c.offeredHosts);
  const inScripts = describe(c.scriptHosts);

  return {
    warning: INCOMPLETE_WARNING.join(' '),
    approved_by_human: false,
    seconds_observed: Math.max(0, Math.round(Number(c.seconds) || 0)),
    requests_seen: Math.max(0, Number(c.requests) || 0),
    origins_seen: sources.length,
    pages_visited: pages,
    pages_visited_count: pages.length,
    phase_marked: Boolean(c.phaseMarked),
    resource_types_seen: Array.from(typesSeen).sort(),
    // Это главный признак «список неполон»: не ошибка, а отсутствие следов.
    student_path_not_walked: missing,
    // Адреса, которые LMS САМА предлагает студенту ссылками и формами, но по
    // которым разведка не ходила. Они не в белом списке — это список того,
    // куда студент пойдёт, а фильтр его не пустит.
    offered_but_not_visited: offered,
    // Адреса из ТЕКСТА скриптов страницы, ни разу не запрошенные. Спящий
    // скрипт прокторинга выглядит именно так.
    mentioned_in_scripts_never_requested: inScripts,
    // Честная граница этого списка. Он ВЕРХНЯЯ ОЦЕНКА, а не список нужного:
    // в текст скриптов попадают адреса из комментариев и лицензионных шапок
    // (w3.org, gnu.org, ссылки на документацию), и они тоже будут здесь.
    // Вычёркивать «заведомо безобидные» имена нельзя: первым вычеркнутым
    // окажется тот самый адрес, из-за которого раздел и написан.
    mentioned_in_scripts_note: 'упоминание в тексте скрипта НЕ РАВНО запросу: '
      + 'сюда попадают и адреса из комментариев и лицензий. Список нужен не '
      + 'для того, чтобы его вписать, а чтобы увидеть, чего в белом списке '
      + 'ещё нет. Спящий скрипт прокторинга выглядит именно так: на странице '
      + 'входа молчит, на старте экзамена уходит на свой адрес',
    scripts_scanned: Math.max(0, Number(c.scriptsScanned) || 0),
    scripts_scan_note: c.scriptScanNote || '',
    not_seen_summary: `не запрошено, но названо страницей: ${offered.length} адресов; `
      + `найдено в тексте скриптов и не запрошено: ${inScripts.length} адресов; `
      + `признаков «путь студента не пройден»: ${missing.length}`,
    seen_origin_keys: Array.from(seenKeys).sort(),
  };
}

/**
 * Сравнить этот прогон с предыдущим черновиком. Инструмент, который на одном
 * и том же адресе то кричит про единый вход, то молчит, обязан сам сказать,
 * что этот прогон беднее.
 */
function compareWithPrevious(coverage, previous) {
  const out = { had_previous: false, poorer: false, lines: [] };
  const prev = (previous && previous.discovery && previous.discovery.coverage) || null;
  if (!prev) return out;
  out.had_previous = true;
  out.previous_requests = Number(prev.requests_seen) || 0;
  out.previous_origins = Number(prev.origins_seen) || 0;
  out.previous_pages = Number(prev.pages_visited_count) || 0;
  if (coverage.requests_seen < out.previous_requests) {
    out.poorer = true;
    out.lines.push(`запросов в этом прогоне ${coverage.requests_seen}, в прошлом было `
      + `${out.previous_requests}`);
  }
  if (coverage.origins_seen < out.previous_origins) {
    out.poorer = true;
    out.lines.push(`источников в этом прогоне ${coverage.origins_seen}, в прошлом было `
      + `${out.previous_origins}`);
  }
  if (coverage.pages_visited_count < out.previous_pages) {
    out.poorer = true;
    out.lines.push(`страниц открыто ${coverage.pages_visited_count}, в прошлом было `
      + `${out.previous_pages}`);
  }
  const prevSeen = Array.isArray(prev.seen_origin_keys) ? prev.seen_origin_keys : [];
  const nowSeen = new Set(coverage.seen_origin_keys || []);
  const lost = prevSeen.filter((k) => !nowSeen.has(k)).sort();
  if (lost.length) {
    out.poorer = true;
    out.lost_since_previous = lost;
    out.lines.push(`в прошлом прогоне были видны, а сейчас нет: ${lost.join(', ')}`);
  }
  if (out.poorer) {
    out.verdict = 'ЭТОТ ПРОГОН БЕДНЕЕ ПРЕДЫДУЩЕГО. Накопленный список сохранён '
      + 'целиком — выводы делайте по накопленному, а не по этому прогону';
  } else {
    out.verdict = 'этот прогон не беднее предыдущего';
  }
  return out;
}

/** Похоже ли значение на незаполненную заглушку `<...>`. */
function isPlaceholder(value) {
  return /<[^>]*>/.test(String(value || ''));
}

/**
 * НАКОПЛЕНИЕ МЕЖДУ ПРОГОНАМИ.
 *
 * Прежняя версия писала файл безусловно: второй прогон молча уничтожал первый
 * вместе с правками человека. При этом сам инструмент советовал «пройдите
 * путь студента ещё раз» — то есть прямым текстом велел сделать то, что
 * затирало результат. А ещё сохранял сам при закрытии окна, без спроса.
 *
 * Раз двухфазности в ядре нет, а полнота набирается только многими проходами,
 * накопление между прогонами — не удобство, а условие работоспособности.
 *
 * Правила слияния:
 *   * `allowed_origins` = прошлый список + найденное сейчас, МИНУС то, что
 *     человек вычеркнул;
 *   * ВЫЧЕРКНУТОЕ ЧЕЛОВЕКОМ НЕ ВОЗВРАЩАЕТСЯ. Определяется сравнением: если
 *     прошлый прогон положил запись в `discovery.included_by_tool`, а в
 *     `allowed_origins` её уже нет — её убрали руками, и это решение
 *     человека, которое инструмент перебивать не вправе;
 *   * поля человека (`issued_by`, `issued_at`, `institution`, `exam_id`,
 *     `notes`, `allow_search`) сохраняются, если они уже заполнены не
 *     заглушкой.
 *
 * @param {object} draft свежий черновик этого прогона
 * @param {object|null} previous разобранный прошлый файл черновика
 */
function mergeWithPrevious(draft, previous) {
  const merge = {
    had_previous: false,
    added_now: [],
    kept_from_previous: [],
    removed_by_human_kept_out: [],
    human_fields_kept: [],
  };
  if (!previous || typeof previous !== 'object') {
    draft.discovery.merge = merge;
    return merge;
  }
  merge.had_previous = true;
  const prevDiscovery = (previous.discovery && typeof previous.discovery === 'object')
    ? previous.discovery : {};
  const asList = (x) => (Array.isArray(x) ? x.map((v) => String(v || '').trim())
    .filter(Boolean) : []);

  const prevAllowed = asList(previous.allowed_origins);
  const prevOffered = asList(prevDiscovery.included_by_tool);
  const prevRemoved = asList(prevDiscovery.removed_by_human);

  // Вычеркнутое руками: инструмент предлагал, в файле не осталось.
  const prevAllowedSet = new Set(prevAllowed);
  const removed = new Set(prevRemoved);
  for (const origin of prevOffered) {
    if (!prevAllowedSet.has(origin)) removed.add(origin);
  }

  const nowProposed = asList(draft.allowed_origins);
  const union = new Set(prevAllowed);
  for (const origin of nowProposed) {
    if (!union.has(origin)) { union.add(origin); merge.added_now.push(origin); }
  }
  for (const origin of prevAllowed) {
    if (nowProposed.indexOf(origin) === -1) merge.kept_from_previous.push(origin);
  }
  for (const origin of Array.from(removed)) {
    if (union.delete(origin)) merge.removed_by_human_kept_out.push(origin);
  }
  draft.allowed_origins = Array.from(union).sort();
  draft.discovery.included_by_tool = Array.from(
    new Set(prevOffered.concat(nowProposed)),
  ).sort();
  draft.discovery.removed_by_human = Array.from(removed).sort();

  // Поля человека. Заглушку `<...>` перетирать можно и нужно, заполненное —
  // нельзя: иначе второй прогон стирал бы ФИО проктора и дату выдачи.
  for (const field of ['institution', 'exam_id', 'exam_url', 'issued_by',
    'issued_at', 'notes']) {
    const prevValue = String(previous[field] || '').trim();
    if (!prevValue || isPlaceholder(prevValue)) continue;
    const ourValue = String(draft[field] || '').trim();
    if (prevValue === ourValue) continue;
    // `notes` человека сохраняем как есть: если он убрал метку-страж, это его
    // осознанное утверждение «я прочитал». Поля-заглушки при этом проверяет
    // ядро отдельно, так что подсунуть неутверждённый файл всё равно не выйдет.
    draft[field] = prevValue;
    merge.human_fields_kept.push(field);
  }
  if (previous.allow_search === true) {
    draft.allow_search = true;
    merge.human_fields_kept.push('allow_search');
  }
  merge.added_now.sort();
  merge.kept_from_previous.sort();
  merge.removed_by_human_kept_out.sort();
  draft.discovery.merge = merge;
  return merge;
}

/**
 * Собрать ЧЕРНОВИК профиля.
 *
 * Два слова о том, чего здесь нет. Здесь нет подписи, нет `issued_by` и нет
 * `issued_at` с настоящей датой: это утверждения ЧЕЛОВЕКА о том, кто и когда
 * выпустил правила, и подставлять их за него инструмент не имеет права.
 * Остаются заглушки в угловых скобках — ровно как в пресете ядра. И это не
 * просто вежливость: ядро ищет эти заглушки и метку-страж в `notes`, и пока
 * они на месте, экзамен не считается готовым.
 *
 * @param {object} folded результат foldObservations()
 * @param {{examUrl?:string, institution?:string, examId?:string,
 *          coverage?:object, previous?:object}} [opts]
 */
function buildDraft(folded, opts) {
  const o = opts || {};
  const sources = (folded && folded.sources) || [];
  const included = sources.filter((s) => s.verdict.include);
  const excluded = sources.filter((s) => !s.verdict.include);

  const auth = [];
  const exam = [];
  for (const s of included) {
    if (s.phases.indexOf('auth') !== -1) auth.push(s.key);
    if (s.phases.indexOf('exam') !== -1) exam.push(s.key);
  }

  const why = {};
  for (const s of sources) why[s.key] = whyLines(s);

  const notIncluded = excluded.map((s) => ({
    origin: s.key,
    class: s.verdict.kind,
    title: s.verdict.title,
    reason: s.verdict.advice,
    opens: s.verdict.opens || '',
    requests: s.requests,
    why: whyLines(s),
    // Готовая строка для вставки руками — чтобы «добавить осознанно» стоило
    // одно движение, а не разбор JSON. Пустая строка у того, что добавлять
    // нельзя в принципе: ядро такую запись всё равно выбросит.
    paste_line: ['search', 'ai', 'zone', 'invalid'].indexOf(s.verdict.kind) === -1
      ? `    "${s.key}",` : '',
  }));

  const coverage = buildCoverage(folded, o.coverage);

  const draft = {
    // --- поля, которые ЧИТАЕТ ядро (EXAM_PROFILE_RULE_FIELDS) ---
    institution: String(o.institution || '') || '<название вуза>',
    exam_id: String(o.examId || '') || '<код экзамена из расписания>',
    exam_url: String(o.examUrl || ''),
    allowed_origins: included.map((s) => s.key).sort(),
    allow_search: false,
    issued_by: '<ФИО и должность проктора>',
    issued_at: '<дата выдачи, ISO 8601>',
    // `notes` ВХОДИТ в канон и в хеш профиля, в отличие от раздела
    // `discovery` ниже. Поэтому метка-страж живёт именно здесь: ядро её
    // находит, печатает в шапку отчёта причиной «экзамен не готов», и в
    // подписанном документе остаётся след, что профиль был машинным
    // черновиком. Уберёт её человек — значит человек утверждает, что прочитал.
    notes: `${DRAFT_SENTINEL}. Собран инструментом разведки (tools/discover) по `
      + 'фактическим запросам страницы LMS. СОБРАНО ТОЛЬКО ТО, ЧТО ПРОШЛИ '
      + 'РУКАМИ: полным списком источников вуза это не является. Список обязан '
      + 'прочитать человек, вычеркнуть лишнее, при необходимости дописать '
      + 'нужное из раздела «кандидаты» и заполнить поля проктора. Поисковики '
      + 'и ИИ-ассистенты закрыты; каждая попытка выхода за список попадает в '
      + 'журнал с полным адресом.',
    // --- служебный раздел: в канон и в хеш профиля НЕ входит ---
    // Ядро про него скажет «поля не входят в канон и в хеш профиля», и это
    // правда: раздел нужен человеку для разбора, а не фильтру. Поэтому всё,
    // что обязано доехать до подписанного отчёта, лежит ВЫШЕ — в `notes` и в
    // заглушках полей проктора, которые ядро проверяет само.
    discovery: {
      v: 2,
      tool: 'tools/discover',
      rule: 'НЕ ОПОЗНАЛ — НЕ ВКЛЮЧИЛ. В allowed_origins попадают только адрес '
        + 'экзамена, соседи по домену вуза и CDN с неизменяемым содержимым. '
        + 'Всё прочее, что страница запрашивала, лежит в candidates — с '
        + 'объяснением, что именно откроется, и готовой строкой для вставки',
      // Заготовка под двухфазный белый список. Ядро двухфазность пока НЕ
      // умеет: `allowed_origins` выше — это объединение двух списков, то есть
      // окно входа сегодня остаётся открытым весь экзамен. Данные собираем
      // уже сейчас, чтобы правка ядра не требовала повторной разведки.
      two_phase_not_supported_yet: true,
      auth_origins: auth.sort(),
      exam_origins: exam.sort(),
      // Что предложил ИНСТРУМЕНТ. Нужно для накопления: сравнив это поле с
      // `allowed_origins` следующего прогона, видно, что человек вычеркнул.
      included_by_tool: included.map((s) => s.key).sort(),
      removed_by_human: [],
      // Записи, включённые в список и требующие человеческого взгляда: всё,
      // кроме самого адреса экзамена.
      review_required: included
        .filter((s) => s.verdict.kind !== 'exam_host')
        .map((s) => s.key)
        .sort(),
      // ЧТО СТРАНИЦА ЗАПРАШИВАЛА, А МЫ НЕ ВКЛЮЧИЛИ. Это не отказ, а решение
      // человека: если без записи ломается страница, её вписывают руками,
      // прочитав `opens` и `reason`.
      candidates: notIncluded,
      why,
      coverage,
      previous_run: compareWithPrevious(coverage, o.previous),
    },
  };

  mergeWithPrevious(draft, o.previous);
  return { draft, included, excluded, candidates: notIncluded, coverage };
}

module.exports = {
  DRAFT_SENTINEL,
  INCOMPLETE_WARNING,
  REGISTRY_LABELS,
  SSO_PROVIDERS,
  SSO_NAME_HINTS,
  ANALYTICS_HOSTS,
  LIVE_CHAT_HOSTS,
  OPEN_CONTENT_HOSTS,
  HOMEWORK_HOSTS,
  WIDE_SITE_HOSTS,
  TRANSLATE_HOSTS,
  VIDEO_HOSTS,
  CDN_FIXED_HOSTS,
  CDN_NAME_HINTS,
  PATH_SIGNALS,
  VERDICTS,
  registryLike,
  isZone,
  siteBase,
  hostOf,
  ssoProviderFor,
  classifySource,
  resourceName,
  displayUrl,
  originKey,
  isPlaceholder,
  foldObservations,
  whyLines,
  buildCoverage,
  compareWithPrevious,
  mergeWithPrevious,
  buildDraft,
};
