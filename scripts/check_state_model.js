'use strict';
/**
 * Проверка модели состояний оболочки (shell/state.js).
 *
 * Зачем этот файл существует: 07.10 блокировки включались на старте приложения,
 * до экрана согласия и до предполётной проверки, и залочивали машину целиком.
 * Правило «блокировки только в exam и paused» теперь выражено таблицей, и эта
 * проверка не даёт тихо вернуть прежнее поведение — например дописав exam
 * в достижимые из consent состояния или добавив consent в LOCKED_STATES.
 *
 * Запуск: node scripts/check_state_model.js   (чистый node, electron не нужен)
 */

const {
  SHELL_STATES, LOCKED_STATES, isLockedState, canTransition, allowedFrom,
  lockPermissions,
} = require('../shell/state');

let failed = 0;
function ok(cond, what) {
  if (cond) {
    console.log(`  ok   ${what}`);
  } else {
    failed += 1;
    console.log(`  FAIL ${what}`);
  }
}

console.log('модель состояний оболочки');

// --- 1. Блокировки только там, где идёт экзамен ----------------------------
ok(LOCKED_STATES.length === 2, 'состояний под блокировкой ровно два');
ok(isLockedState('exam'), 'exam — под блокировкой');
ok(isLockedState('paused'), 'paused — под блокировкой (пауза внутри экзамена)');
for (const s of ['idle', 'consent', 'preflight', 'calibration', 'finished']) {
  ok(!isLockedState(s), `${s} — БЕЗ блокировок, машина у пользователя`);
}
ok(!isLockedState(''), 'пустое имя не считается состоянием под блокировкой');
ok(!isLockedState(undefined), 'undefined не считается состоянием под блокировкой');

// --- 2. Старт не может оказаться под блокировкой ---------------------------
// Именно это и произошло в инциденте: приложение стартовало залоченным.
ok(!isLockedState('idle'), 'начальное состояние idle свободно');
ok(!isLockedState('consent'), 'первый экран (согласие) свободен');

// --- 3. В экзамен нельзя попасть иначе как через калибровку ----------------
const intoExam = SHELL_STATES.filter((s) => canTransition(s, 'exam').ok && s !== 'exam');
ok(intoExam.length === 2 && intoExam.indexOf('calibration') !== -1
   && intoExam.indexOf('paused') !== -1,
`в exam ведут только calibration и paused (сейчас: ${intoExam.join(', ')})`);
ok(!canTransition('consent', 'exam').ok, 'consent -> exam отвергается');
ok(!canTransition('idle', 'exam').ok, 'idle -> exam отвергается');
ok(!canTransition('preflight', 'exam').ok, 'preflight -> exam отвергается');
ok(!canTransition('finished', 'exam').ok, 'finished -> exam отвергается');

// --- 4. Выход из экзамена всегда возможен ----------------------------------
for (const s of LOCKED_STATES) {
  ok(canTransition(s, 'finished').ok, `${s} -> finished разрешён (машина освободится)`);
}

// --- 5. Мусор отвергается -------------------------------------------------
for (const bad of ['EXAM', 'ex am', 'lockdown', '', null, undefined, 0, {}, []]) {
  const v = canTransition('consent', bad);
  ok(!v.ok && v.reason === 'unknown_state',
    `мусорное состояние ${JSON.stringify(bad)} отвергнуто как unknown_state`);
}
ok(canTransition('нет такого', 'exam').reason === 'unknown_current_state',
  'неизвестное текущее состояние отвергается, а не трактуется как «можно»');

// --- 6. Таблица согласована -----------------------------------------------
ok(canTransition('exam', 'exam').ok && canTransition('exam', 'exam').reason === 'unchanged',
  'повторное сообщение того же состояния не считается переходом');
for (const from of SHELL_STATES) {
  for (const to of allowedFrom(from)) {
    ok(SHELL_STATES.indexOf(to) !== -1, `переход ${from} -> ${to} ведёт в известное состояние`);
  }
}
ok(SHELL_STATES.every((s) => Object.prototype.hasOwnProperty.call(
  require('../shell/state').STATE_TRANSITIONS, s,
)), 'у каждого состояния описаны переходы (иначе оболочка «залипнет»)');

// --- 7. Из каждого состояния достижим выход --------------------------------
for (const from of SHELL_STATES) {
  if (from === 'finished') continue;
  const seen = {};
  const queue = [from];
  let reaches = false;
  while (queue.length) {
    const cur = queue.shift();
    if (seen[cur]) continue;
    seen[cur] = true;
    if (cur === 'finished') { reaches = true; break; }
    for (const nxt of allowedFrom(cur)) queue.push(nxt);
  }
  ok(reaches, `из ${from} достижимо finished (машину всегда можно освободить)`);
}

// --- 8. Смысл флагов командной строки --------------------------------------
const plain = lockPermissions({});
ok(plain.shortcuts === true && plain.window === true,
  'без флагов на время экзамена разрешено всё: и сочетания, и захват окна');

const noLock = lockPermissions({ noLockdown: true });
ok(noLock.shortcuts === false, '--no-lockdown: сочетания НЕ перехватываются никогда');
ok(noLock.window === false, '--no-lockdown: окно НЕ захватывает экран никогда');

const noKiosk = lockPermissions({ noKiosk: true });
ok(noKiosk.window === false,
  '--no-kiosk: захват окна запрещён (окно двигается, сворачивается, закрывается)');
ok(noKiosk.shortcuts === true,
  '--no-kiosk сам по себе не отменяет перехват сочетаний — для этого есть --no-lockdown');

const both = lockPermissions({ noKiosk: true, noLockdown: true });
ok(both.shortcuts === false && both.window === false,
  '--no-kiosk --no-lockdown: блокировок нет вообще');

ok(lockPermissions(null).shortcuts === true, 'отсутствие флагов не роняет разбор');

console.log(failed ? `\nПРОВАЛЕНО проверок: ${failed}` : '\nвсе проверки модели состояний пройдены');
process.exit(failed ? 1 : 0);
