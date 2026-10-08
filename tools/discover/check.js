'use strict';
/**
 * Проверка слоя правил инструмента разведки (tools/discover/classify.js).
 *
 * Зачем этот файл: совет инструмента читает человек и по нему правит белый
 * список экзамена. Ошибка «посоветовал счётчик как нужный» стоит открытого
 * источника на весь экзамен, а ошибка «назвал поставщика входа безобидным
 * CDN» стоит открытой почты студента.
 *
 * Главное, что закреплено здесь после ревью, — ПРАВИЛО «НЕ ОПОЗНАЛ — НЕ
 * ВКЛЮЧИЛ». Первая версия проверок утверждала обратное: строка
 *
 *     ok(classifySource('moodle.com').include === true, 'незнакомый хост в
 *        черновик попадает, но с пометкой «решает человек»')
 *
 * фиксировала как правильное поведение то, из-за чего в белый список уезжали
 * pastebin.com, dropbox.com и cdn.jsdelivr.net. Теперь проверки стоят
 * наоборот, и вернуть прежнее поведение молча нельзя.
 *
 * Запуск: node tools/discover/check.js   (чистый node, electron не нужен)
 */

const path = require('path');
const {
  siteBase, isZone, classifySource, foldObservations, buildDraft, displayUrl,
  buildCoverage, compareWithPrevious, mergeWithPrevious, isPlaceholder,
  DRAFT_SENTINEL,
} = require(path.join(__dirname, 'classify.js'));

let failed = 0;
function ok(cond, what) {
  if (cond) {
    console.log(`  ok   ${what}`);
  } else {
    failed += 1;
    console.log(`  FAIL ${what}`);
  }
}

const AITU = 'lms.astanait.edu.kz';
const kind = (host, examHost) => classifySource(host, { examHost: examHost || AITU }).kind;
const included = (host, examHost) => classifySource(host,
  { examHost: examHost || AITU }).include === true;

console.log('слой правил разведки источников');

// --- 1. «Свой домен вуза» ---------------------------------------------------
// Главный риск этой функции — вернуть зону. Тогда «своим доменом» LMS станет
// edu.kz, и инструмент начнёт считать нужным любой вуз страны.
ok(siteBase('lms.astanait.edu.kz') === 'astanait.edu.kz',
  'свой домен lms.astanait.edu.kz — astanait.edu.kz, а не edu.kz');
ok(siteBase('moodle.ksu.edu.kz') === 'ksu.edu.kz', 'свой домен ksu.edu.kz');
ok(siteBase('moodle.example.com') === 'example.com', 'свой домен example.com');
ok(siteBase('lms.vuz.kz') === 'vuz.kz', 'двухсоставное имя остаётся как есть');
ok(!isZone('astanait.edu.kz'), 'astanait.edu.kz зоной не является');
ok(isZone('edu.kz') && isZone('edu') && isZone('kz') && isZone('ac.uz'),
  'edu.kz, edu, kz, ac.uz — зоны или классы организаций');

// --- 2. Отказы, повторяющие ядро -------------------------------------------
ok(kind('edu.kz') === 'zone', 'зона целиком — отказ');
ok(kind('ya.ru') === 'search' && kind('bing.com') === 'search', 'поисковик — отказ');
ok(kind('www.google.kz') === 'search', 'google на любом ccTLD — поисковик');
ok(kind('chatgpt.com') === 'ai' && kind('claude.ai') === 'ai', 'ИИ-ассистент — отказ');
ok(kind('127.0.0.1') === 'local' && kind('localhost') === 'local',
  'петля в профиль LMS не предлагается');
for (const bad of ['', '*', 'not a host', '..', '-x.kz']) {
  ok(['invalid', 'zone'].indexOf(kind(bad)) !== -1, `мусор «${bad}» — отказ`);
}

// --- 3. ГЛАВНОЕ: не опознал — НЕ включил -----------------------------------
// Разворот правила. Пометка «решает человек» ничего не меняла, потому что
// запись уже лежала в белом списке: разница между «не знаю» и «можно» была
// стёрта в сторону «можно».
ok(kind('moodle.com') === 'unknown', 'незнакомый хост не выдаётся за нужный');
ok(included('moodle.com') === false,
  'НЕЗНАКОМЫЙ ХОСТ В ЧЕРНОВИК НЕ ПОПАДАЕТ: не опознал — не включил');
for (const host of ['ps1-dev.oes.kz', 'api-free.deepl.com', 'js.intercomcdn.com',
  'code.tidio.co', 'va.tawk.to', 'widget-mediator.zopim.com']) {
  ok(included(host) === false, `${host} в черновик не попадает`);
}

// --- 4. Источники с ПРОИЗВОЛЬНЫМ содержимым --------------------------------
// Одиночный хост, отдающий что угодно. Перечисление «поимённо, а не *.домен»
// здесь не помогает вообще: имя одно, содержимое любое. Проверено curl:
// cdn.jsdelivr.net/gh/<юзер>/<репо>@<ветка>/<файл> отдаёт любой файл любого
// публичного репозитория GitHub.
for (const host of ['cdn.jsdelivr.net', 'unpkg.com', 'storage.googleapis.com',
  's3.amazonaws.com', 'cdn.discordapp.com', 'raw.githubusercontent.com',
  'pastebin.com', 'dropbox.com', 'onedrive.live.com', 'astanait-my.sharepoint.com',
  'notion.so', 'cloudfront.net', 'polyfill.io', 'vercel.app', 'ngrok-free.app']) {
  ok(kind(host) === 'open_content', `${host} — произвольное содержимое`);
  ok(included(host) === false, `${host} В ЧЕРНОВИК НЕ ПОПАДАЕТ`);
}
const jsd = classifySource('cdn.jsdelivr.net', { examHost: AITU });
ok(/репозитор/i.test(jsd.opens) && /GitHub/.test(jsd.opens),
  'про jsDelivr сказано прямо: любой файл любого репозитория GitHub');
ok(/НЕ отклонит|не отклонит/.test(jsd.advice),
  'сказано, что ядро такую запись НЕ отклонит: сверка с ядром тут не защита');

// --- 5. Живой чат с человеком — НЕ «аналитика» -----------------------------
// Прежняя версия относила intercom.io и crisp.chat к счётчикам, с текстом
// «на оценку он не влияет». Это окно переписки с живым оператором: в него
// отправляют условие задачи. И один виджет не может иметь два вердикта —
// widget.intercom.io и js.intercomcdn.com теперь в одном классе.
for (const host of ['static.zdassets.com', 'ekr.zdassets.com', 'embed.tawk.to',
  'va.tawk.to', 'widget.intercom.io', 'js.intercomcdn.com', 'crisp.chat',
  'static.olark.com', 'code.tidio.co', 'cdn.chatra.io', 'widget-mediator.zopim.com',
  'livechatinc.com', 'jivosite.com']) {
  ok(kind(host) === 'live_chat', `${host} — ЖИВОЙ ЧАТ, а не счётчик`);
  ok(included(host) === false, `${host} в черновик не попадает`);
}
ok(/ЖИВОЙ ЧАТ/.test(classifySource('static.zdassets.com', { examHost: AITU }).title),
  'в заголовке вердикта прямо сказано «живой чат с человеком»');
ok(kind('static.zdassets.com') !== 'cdn_guess',
  'имя «static» НЕ перебивает опознание чата: порядок проверок держится');

// --- 6. Сайты с ответами, большие сайты, переводчики, видео ----------------
for (const host of ['chegg.com', 'coursehero.com', 'studocu.com', 'brainly.ru',
  'symbolab.com', 'wolframalpha.com', 'quizlet.com']) {
  ok(kind(host) === 'homework' && !included(host),
    `${host} — сайт с ответами, в черновик не попадает`);
}
for (const host of ['www.youtube.com', 'zoom.us', 'teams.microsoft.com',
  'web.whatsapp.com', 'stackoverflow.com', 'github.com', 'reddit.com']) {
  ok(kind(host) === 'wide_site' && !included(host),
    `${host} — большой сайт с поиском и общением, в черновик не попадает`);
}
// Документы Google ядро относит к поисковикам целиком (весь google.com), и
// спорить с ядром инструмент не должен: класс отказа тут строже нашего.
ok(kind('docs.google.com') === 'search' && !included('docs.google.com'),
  'docs.google.com ядро считает поисковиком — инструмент не спорит с ядром');
ok(kind('www.youtube.com') !== 'cdn_fixed',
  'youtube.com больше не лежит в списке CDN: это полный сайт с поиском');
ok(kind('zoom.us') !== 'cdn_fixed', 'zoom.us больше не «доставка статики»');
for (const host of ['translate.googleapis.com', 'api-free.deepl.com']) {
  ok(kind(host) === 'translate' && !included(host),
    `${host} — переводчик, в черновик не попадает`);
}
ok(kind('player.vimeo.com') === 'video' && !included('player.vimeo.com'),
  'плеер видео по умолчанию не включается');

// --- 7. Единый вход опознаётся РАНЬШЕ «своего домена» и CDN ----------------
ok(kind('login.microsoftonline.com') === 'sso', 'microsoftonline — единый вход');
ok(kind('idp.astanait.edu.kz') === 'sso',
  'свой idp вуза — это ВХОД, а не просто свой домен');
ok(kind('keycloak.vuz.edu.kz', 'lms.vuz.edu.kz') === 'sso', 'keycloak — единый вход');
ok(kind('aadcdn.msauth.net') === 'sso',
  'статика поставщика входа тоже вход, хотя имя выглядит как CDN');
// Найдено ревью: прежние подсказки держались на словах idp/sso/saml, и мимо
// проходило всё, что вузы называют по-человечески. login.astanait.edu.kz
// уезжал в «свой домен вуза — вероятно нужен», то есть прямо в белый список.
ok(kind('login.astanait.edu.kz') === 'sso',
  'login.<домен вуза> — это ВХОД, а не «свой домен вуза»');
ok(included('login.astanait.edu.kz') === false,
  'login.<домен вуза> в черновик НЕ попадает');
for (const host of ['auth.kaznu.kz', 'passport.satbayev.edu.kz', 'id.narxoz.kz',
  'account.kbtu.kz', 'signin.enu.kz', 'sso.astanait.edu.kz']) {
  ok(kind(host) === 'sso', `${host} опознан как вход, а не «не опознано»`);
}
const ms = classifySource('login.microsoftonline.com', { examHost: AITU });
ok(/OUTLOOK/.test(ms.advice) && /TEAMS/.test(ms.advice) && /ONEDRIVE/.test(ms.advice),
  'про Microsoft сказано прямо: Outlook, Teams, OneDrive');
const goo = classifySource('accounts.google.com', { examHost: AITU });
ok(goo.kind === 'search',
  'accounts.google.com ядро считает поисковиком — и инструмент не спорит с ядром');

// --- 8. Что ВСЁ-ТАКИ попадает в черновик -----------------------------------
ok(kind(AITU) === 'exam_host' && included(AITU), 'сам адрес экзамена — нужен');
ok(kind('moodle.astanait.edu.kz') === 'own_domain' && included('moodle.astanait.edu.kz'),
  'сосед по домену вуза — вероятно нужен');
ok(kind('fonts.googleapis.com') === 'cdn_fixed' && included('fonts.googleapis.com'),
  'шрифты Google — CDN с неизменяемым содержимым, попадает');
ok(kind('cdnjs.cloudflare.com') === 'cdn_fixed' && included('cdnjs.cloudflare.com'),
  'cdnjs — попадает: свой файл туда не положить');
ok(kind('code.jquery.com') === 'cdn_fixed', 'раздача jQuery под своим именем — попадает');
// Намёк по имени больше не включает ничего: так назывался static.zdassets.com.
ok(kind('static.example-cdn.net') === 'cdn_guess',
  'незнакомый static.* — только ДОГАДКА по имени');
ok(included('static.example-cdn.net') === false,
  'ДОГАДКА ПО ИМЕНИ В ЧЕРНОВИК НЕ ПОПАДАЕТ: содержимое неизвестно');
ok(kind('static.vuz.kz', 'lms.vuz.kz') === 'own_domain',
  'static.vuz.kz при LMS на lms.vuz.kz — всё-таки свой домен, и это точнее CDN');
ok(kind('mc.yandex.ru') === 'search',
  'счётчик Метрики ядро относит к поисковику — совет инструмента совпадает с ядром');
ok(kind('www.googletagmanager.com') === 'analytics', 'диспетчер тегов — аналитика');
ok(kind('clarity.ms') === 'analytics' && kind('hotjar.com') === 'analytics',
  'записывающие счётчики — аналитика');
ok(kind('browser.events.data.microsoft.com') === 'analytics',
  'сборщик телеметрии Microsoft (OneCollector) — аналитика');
ok(kind('copilot.microsoft.com') === 'ai',
  'при этом copilot.microsoft.com остаётся ИИ-ассистентом');
ok(!included('www.googletagmanager.com'), 'аналитика в черновик не включается');

// --- 9. Фазы: до отметки и после -------------------------------------------
const OBS = [
  { url: `https://${AITU}/login/index.php`, resourceType: 'mainFrame', page: '', phase: 'auth' },
  { url: `https://${AITU}/theme/font.woff2`, resourceType: 'font',
    page: `https://${AITU}/login/index.php`, phase: 'auth' },
  { url: 'https://login.microsoftonline.com/common/oauth2/authorize',
    resourceType: 'mainFrame', page: `https://${AITU}/login/index.php`, phase: 'auth' },
  { url: 'https://fonts.googleapis.com/css?family=X', resourceType: 'stylesheet',
    page: `https://${AITU}/mod/quiz/view.php`, phase: 'exam' },
  { url: 'https://cdn.jsdelivr.net/npm/x/y.js', resourceType: 'script',
    page: `https://${AITU}/mod/quiz/view.php`, phase: 'exam' },
  { url: 'https://mc.yandex.ru/watch/1', resourceType: 'image',
    page: `https://${AITU}/mod/quiz/view.php`, phase: 'exam' },
  { url: 'https://www.google.com/search?q=answer', resourceType: 'mainFrame',
    page: `https://${AITU}/mod/quiz/view.php`, phase: 'exam' },
  { url: 'file:///Users/stud/shpora.html', resourceType: 'mainFrame', page: '', phase: 'exam' },
];
const folded = foldObservations(OBS, { examUrl: `https://${AITU}/login/index.php` });

ok(folded.examHost === AITU, 'хост экзамена определён по адресу');
ok(folded.skipped === 1, 'адрес без сетевого хоста (file:) в источники не попал');
const built = buildDraft(folded, {
  examUrl: `https://${AITU}/login/index.php`,
  coverage: { seconds: 45, requests: OBS.length, pages: [`https://${AITU}/login/index.php`],
    phaseMarked: true, offeredHosts: ['aitu.oes.kz', 'moodle.com'],
    scriptHosts: ['ajax.googleapis.com', 'aitu.oes.kz'], scriptsScanned: 2,
    scriptScanNote: 'прочитано тел скриптов: 2 из 2 зафиксированных' },
});
ok(built.draft.allowed_origins.indexOf(AITU) !== -1, 'хост LMS в черновике');
ok(built.draft.allowed_origins.indexOf('fonts.googleapis.com') !== -1,
  'CDN с неизменяемым содержимым в черновике');
for (const bad of ['login.microsoftonline.com', 'mc.yandex.ru', 'www.google.com',
  'cdn.jsdelivr.net']) {
  ok(built.draft.allowed_origins.indexOf(bad) === -1, `${bad} в черновик НЕ попал`);
}
ok(built.draft.discovery.auth_origins.indexOf(AITU) !== -1,
  'источник до отметки — в auth_origins');
ok(built.draft.discovery.exam_origins.indexOf('fonts.googleapis.com') !== -1,
  'источник после отметки — в exam_origins');
ok(built.draft.discovery.auth_origins.indexOf('fonts.googleapis.com') === -1,
  'источник, которого до входа не было, в окно входа не записан');
ok(built.draft.discovery.two_phase_not_supported_yet === true,
  'про то, что ядро пока однофазное, сказано прямо в файле');

// --- 10. Кандидаты: не отказ, а решение человека ---------------------------
const candidates = built.draft.discovery.candidates;
const byOrigin = (o) => candidates.find((c) => c.origin === o) || {};
ok(candidates.length > 0, 'раздел кандидатов в файле есть');
ok(byOrigin('cdn.jsdelivr.net').paste_line === '    "cdn.jsdelivr.net",',
  'у кандидата есть готовая строка для вставки руками');
ok(/репозитор/i.test(byOrigin('cdn.jsdelivr.net').opens || ''),
  'у кандидата написано, ЧТО через него открывается');
ok(byOrigin('www.google.com').paste_line === '',
  'у поисковика строки для вставки нет: ядро её всё равно выбросит');
ok(byOrigin('login.microsoftonline.com').class === 'sso',
  'единый вход попал в кандидаты с классом sso');

// --- 11. ЧЕГО РАЗВЕДКА НЕ ВИДЕЛА -------------------------------------------
// Главный ответ на вопрос «как инструмент предупреждает о неполноте»: он
// обязан СЧИТАТЬ И ПЕЧАТАТЬ то, чего не видел, и делать это в ФАЙЛЕ, потому
// что применяется файл, а не печатный разбор.
const cov = built.draft.discovery.coverage;
ok(/ТОЛЬКО ТО, ЧТО ВЫ ПРОШЛИ РУКАМИ/.test(cov.warning),
  'предупреждение о неполноте лежит в самом файле черновика');
ok(cov.seconds_observed === 45 && cov.requests_seen === OBS.length,
  'длительность прогона и число запросов записаны числом');
ok(cov.pages_visited_count === 1, 'число открытых страниц записано');
ok(cov.phase_marked === true, 'отметка фазы записана в файл, а не только в печать');
ok(cov.student_path_not_walked.some((s) => /ВСТРОЕННОГО КАДРА/.test(s)),
  'отсутствие встроенных кадров названо прямо: путь студента не пройден');
ok(cov.student_path_not_walked.some((s) => /ПОСТОЯННОГО СОЕДИНЕНИЯ/.test(s)),
  'отсутствие websocket названо прямо');
ok(cov.student_path_not_walked.some((s) => /ВСЕГО ОДНА страница/.test(s)),
  'одна открытая страница — отдельный признак неполноты');
// Живой случай с LMS заказчика: aitu.oes.kz виден в ссылках и в тексте
// спящего скрипта, но ни одним запросом не запрашивается.
ok(cov.offered_but_not_visited.some((x) => x.host === 'aitu.oes.kz'),
  'адрес, который страница предлагает ссылкой, попал в «не видели»');
ok(cov.mentioned_in_scripts_never_requested.some((x) => x.host === 'ajax.googleapis.com'),
  'адрес из ТЕКСТА скрипта попал в «не видели»: так выглядит спящий скрипт');
ok(built.draft.allowed_origins.indexOf('aitu.oes.kz') === -1
   && built.draft.allowed_origins.indexOf('ajax.googleapis.com') === -1,
  'НИ ОДИН из «не видели» в белый список не попал: упоминание — не запрос');
const covNoPhase = buildCoverage(folded, { phaseMarked: false, pages: [] });
ok(covNoPhase.student_path_not_walked.some((s) => /отметку «вход завершён» НЕ СТАВИЛИ/.test(s)),
  'не поставленная отметка фазы — признак неполноты В ФАЙЛЕ');

// --- 12. Черновик не выдаёт себя за применённый профиль --------------------
ok(built.draft.notes.indexOf(DRAFT_SENTINEL) === 0,
  `метка-страж «${DRAFT_SENTINEL}» стоит В НАЧАЛЕ notes — а notes входит в канон и в хеш`);
ok(/ТОЛЬКО ТО, ЧТО ПРОШЛИ\s+РУКАМИ/.test(built.draft.notes),
  'в notes профиля сказано про неполноту: это поле доезжает до отчёта');
ok(built.draft.allow_search === false, 'поиск в черновике закрыт');
ok(isPlaceholder(built.draft.issued_by) && isPlaceholder(built.draft.issued_at)
   && isPlaceholder(built.draft.institution) && isPlaceholder(built.draft.exam_id),
  'issued_by, issued_at, institution и exam_id остаются заглушками: ядро их ловит');
ok(built.draft.allowed_origins.every((o) => o.indexOf('*') === -1),
  'в черновике нет ни одной записи «*.домен»: только поимённо');
const whyForLms = built.draft.discovery.why[AITU] || [];
ok(whyForLms.length > 0 && /нужен потому что/.test(whyForLms[0]),
  'у каждой записи есть строка «нужен потому что»');

// --- 13. НАКОПЛЕНИЕ: второй прогон не уничтожает первый --------------------
// Прежняя версия писала файл безусловно, и второй прогон затирал первый
// вместе с правками человека. При этом инструмент сам советовал пройти путь
// студента ещё раз — то есть велел сделать то, что затирало результат.
const previous = {
  institution: 'Astana IT University',
  exam_id: 'SE-2026-01',
  issued_by: 'Иванова А.Б., старший проктор',
  issued_at: '2026-10-16T09:00:00+05:00',
  notes: 'Проверено глазами 16.10.2026.',
  allow_search: false,
  allowed_origins: [AITU, 'video.astanait.edu.kz'],
  discovery: {
    // Инструмент в прошлый раз предлагал ещё и forum.astanait.edu.kz —
    // в allowed_origins его нет, значит человек вычеркнул.
    included_by_tool: [AITU, 'video.astanait.edu.kz', 'forum.astanait.edu.kz'],
    removed_by_human: [],
    coverage: { requests_seen: 43, origins_seen: 6, pages_visited_count: 4,
      seen_origin_keys: [AITU, 'ps1-dev.oes.kz', 'login.microsoftonline.com'] },
  },
};
const second = buildDraft(folded, {
  examUrl: `https://${AITU}/login/index.php`,
  coverage: { seconds: 50, requests: OBS.length, pages: [`https://${AITU}/login/index.php`],
    phaseMarked: true },
  previous,
});
const merge = second.draft.discovery.merge;
ok(merge.had_previous === true, 'прошлый черновик прочитан, а не проигнорирован');
ok(second.draft.allowed_origins.indexOf('video.astanait.edu.kz') !== -1,
  'запись из прошлого прогона СОХРАНЕНА: прогоны накапливаются');
ok(second.draft.allowed_origins.indexOf('forum.astanait.edu.kz') === -1,
  'ВЫЧЕРКНУТОЕ ЧЕЛОВЕКОМ НЕ ВЕРНУЛОСЬ');
ok(second.draft.discovery.removed_by_human.indexOf('forum.astanait.edu.kz') !== -1,
  'вычеркнутое запомнено, чтобы не вернулось и в следующих прогонах');
ok(second.draft.issued_by === 'Иванова А.Б., старший проктор'
   && second.draft.issued_at === '2026-10-16T09:00:00+05:00',
  'ФИО проктора и дата выдачи НЕ затёрты заглушками');
ok(second.draft.institution === 'Astana IT University'
   && second.draft.exam_id === 'SE-2026-01',
  'название вуза и код экзамена сохранены');
ok(second.draft.notes === 'Проверено глазами 16.10.2026.',
  'заметки человека сохранены как есть: он убрал метку-страж осознанно');
ok(merge.added_now.indexOf('fonts.googleapis.com') !== -1,
  'новое, найденное этим прогоном, добавлено');
// Пустой прогон не должен обнулять накопленное — именно так и ломалось:
// «ПРОГОН 1: 10 записей, ПРОГОН 2: []».
const empty = buildDraft(foldObservations([], {}), { previous });
ok(empty.draft.allowed_origins.length === 2,
  'ПУСТОЙ ПРОГОН НЕ ОБНУЛЯЕТ накопленный список');

// --- 14. «Этот прогон беднее предыдущего» ----------------------------------
// Воспроизведённое ревью непостоянство: три прогона одного адреса дали 43/26
// запросов и 6/2 источника, а черновики по форме были неотличимы.
const poorer = second.draft.discovery.previous_run;
ok(poorer.had_previous === true, 'сравнение с прошлым прогоном выполнено');
ok(poorer.poorer === true, 'прогон, собравший меньше, НАЗВАН БЕДНЕЕ прямо в файле');
ok(/БЕДНЕЕ/.test(poorer.verdict), 'вердикт сравнения написан словами');
ok((poorer.lost_since_previous || []).indexOf('ps1-dev.oes.kz') !== -1,
  'сказано, какой источник был виден в прошлом прогоне, а в этом нет');
const richer = compareWithPrevious(
  { requests_seen: 100, origins_seen: 20, pages_visited_count: 9,
    seen_origin_keys: previous.discovery.coverage.seen_origin_keys.slice() },
  previous,
);
ok(richer.poorer === false, 'прогон богаче предыдущего беднее не называется');

// --- 15. Токены сеанса в вывод не попадают ----------------------------------
const shown = displayUrl(`https://${AITU}/login/index.php?sesskey=SECRET123&token=T#frag`);
ok(shown.indexOf('SECRET123') === -1 && shown.indexOf('token') === -1
   && shown.indexOf('#') === -1,
  'запрос и фрагмент из адреса отрезаются: sesskey и токены в файл не уезжают');
ok(shown === `https://${AITU}/login/index.php`, 'схема, хост и путь при этом остаются');
const long = displayUrl(`https://${AITU}/${'a'.repeat(300)}`);
ok(long.length < 140, 'бесконечный путь обрезается');

// --- 16. Ничего не падает на пустом и на мусоре ----------------------------
const emptyFold = foldObservations([], {});
ok(emptyFold.sources.length === 0, 'пустой список наблюдений разбирается');
const emptyDraft = buildDraft(emptyFold, {});
ok(emptyDraft.draft.allowed_origins.length === 0, 'пустой черновик — пустой список');
ok(emptyDraft.draft.discovery.coverage.student_path_not_walked.length > 0,
  'у пустого прогона признаков «путь не пройден» много, и они напечатаны');
ok(foldObservations(null, null).sources.length === 0, 'null вместо наблюдений не роняет');
ok(buildDraft(null, null).draft.institution.indexOf('<') === 0, 'null вместо картины не роняет');
ok(classifySource(null, null).include === false, 'null вместо хоста не роняет');
ok(compareWithPrevious({ requests_seen: 0, origins_seen: 0, pages_visited_count: 0 },
  null).had_previous === false, 'отсутствие прошлого файла не роняет сравнение');
ok(mergeWithPrevious({ allowed_origins: [], discovery: {} }, null).had_previous === false,
  'отсутствие прошлого файла не роняет слияние');
ok(mergeWithPrevious({ allowed_origins: [], discovery: {} },
  { allowed_origins: 'мусор', discovery: 42 }).had_previous === true,
  'мусор вместо прошлого файла не роняет слияние');

console.log(failed
  ? `\nПРОВАЛЕНО проверок: ${failed}`
  : '\nвсе проверки слоя правил разведки пройдены');
process.exit(failed ? 1 : 0);
