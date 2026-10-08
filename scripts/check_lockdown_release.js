'use strict';
/**
 * Проверка захвата и освобождения глобальных сочетаний (shell/lockdown.js).
 * Запускается под Electron, потому что globalShortcut есть только там:
 *
 *   npx electron scripts/check_lockdown_release.js
 *
 * Главное, что здесь проверяется, — требование безопасности из инцидента 07.10:
 *  1. release() снимает ТОЛЬКО то, что Lockdown занял сам;
 *  2. аварийный выход прокторa переживает release() — раньше его снимал
 *     globalShortcut.unregisterAll(), то есть единственный способ выйти из
 *     экзамена отключал сам себя;
 *  3. после release() не остаётся ни одного перехваченного сочетания;
 *  4. таймер очистки системного буфера обмена останавливается.
 *
 * Блокировки держатся доли секунды, буфер обмена НЕ чистится: интервал
 * очистки выставлен заведомо больше времени прогона.
 */

const { app, globalShortcut } = require('electron');
const { Lockdown } = require('../shell/lockdown');

const ADMIN_EXIT = 'CommandOrControl+Alt+Shift+Q';

let failed = 0;
function ok(cond, what) {
  if (cond) console.log(`  ok   ${what}`);
  else { failed += 1; console.log(`  FAIL ${what}`); }
}
function held(acc) {
  try { return globalShortcut.isRegistered(acc); } catch (e) { return false; }
}

app.disableHardwareAcceleration();

app.whenReady().then(() => {
  console.log(`захват и освобождение глобальных сочетаний (${process.platform})`);

  const lockdown = new Lockdown({
    reserved: [ADMIN_EXIT],
    // буфер обмена пользователя не трогаем: до первого тика прогон закончится
    clipboardIntervalMs: 10 * 60 * 1000,
    displayIntervalMs: 10 * 60 * 1000,
    logger: () => {},
  });

  // --- 0. Чистый старт ----------------------------------------------------
  ok(!held(ADMIN_EXIT), 'до начала аварийный выход не занят');

  // --- 1. Аварийный выход регистрируется ПЕРВЫМ ---------------------------
  const adminOk = globalShortcut.register(ADMIN_EXIT, () => {});
  ok(adminOk && held(ADMIN_EXIT), 'аварийный выход зарегистрирован первым');

  // --- 2. engage() ---------------------------------------------------------
  lockdown.engage();
  const taken = lockdown.registeredAccelerators();
  ok(taken.length > 0, `engage() занял сочетания: ${taken.length}`);
  ok(taken.indexOf(ADMIN_EXIT) === -1,
    'Lockdown НЕ взял себе аварийный выход (он в reserved)');
  ok(held(ADMIN_EXIT), 'после engage() аварийный выход на месте');
  ok(taken.every(held), 'система подтверждает: занятые сочетания действительно наши');
  ok(lockdown.engaged === true, 'lockdown.engaged === true');

  // --- 3. release(): точечное снятие --------------------------------------
  const res = lockdown.release('проверка');
  ok(res.freed.length === taken.length,
    `release() освободил ровно столько, сколько занял: ${res.freed.length} из ${taken.length}`);
  ok(res.failed.length === 0, 'ни одно снятие не завершилось ошибкой');
  const stuck = taken.filter(held);
  ok(stuck.length === 0,
    `после release() не осталось перехваченных сочетаний${stuck.length ? `: ${stuck.join(', ')}` : ''}`);

  // ГЛАВНОЕ: аварийный выход переживает release()
  ok(held(ADMIN_EXIT),
    'аварийный выход ПЕРЕЖИЛ release() — выйти из экзамена по-прежнему можно');
  ok(lockdown.engaged === false, 'lockdown.engaged === false');
  ok(lockdown.registeredAccelerators().length === 0, 'учёт занятых сочетаний пуст');

  // --- 4. Таймеры остановлены ---------------------------------------------
  ok(lockdown._clipboardTimer === null,
    'таймер очистки системного буфера обмена остановлен');

  // --- 5. reregister() молчит, когда блокировки сняты ----------------------
  lockdown.reregister();
  ok(lockdown.registeredAccelerators().length === 0,
    'reregister() вне экзамена ничего не захватывает (возврат фокуса на экране согласия безопасен)');

  // --- 6. Повторный цикл и dispose() --------------------------------------
  lockdown.engage();
  const taken2 = lockdown.registeredAccelerators();
  ok(taken2.length === taken.length, 'повторный engage() занял то же множество');
  lockdown.dispose('проверка');
  ok(taken2.filter(held).length === 0, 'после dispose() сочетания освобождены');
  ok(held(ADMIN_EXIT), 'аварийный выход пережил и dispose()');
  ok(lockdown._displayTimer === null, 'dispose() остановил и опрос мониторов');

  // --- 7. Двойной release() безвреден -------------------------------------
  const again = lockdown.release('повторно');
  ok(again.freed.length === 0 && again.failed.length === 0,
    'повторный release() ничего не ломает');
  ok(held(ADMIN_EXIT), 'аварийный выход на месте и после повторного release()');

  // уборка за собой
  try { globalShortcut.unregister(ADMIN_EXIT); } catch (e) { /* ok */ }
  try { globalShortcut.unregisterAll(); } catch (e) { /* ok */ }
  ok(!held(ADMIN_EXIT), 'проверка убрала за собой: аварийный выход отпущен');

  console.log(failed
    ? `\nПРОВАЛЕНО проверок: ${failed}`
    : '\nвсе проверки захвата и освобождения пройдены');
  app.exit(failed ? 1 : 0);
});
