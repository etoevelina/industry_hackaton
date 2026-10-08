#!/usr/bin/env node
/* ===========================================================================
 * check_calibration_flow.js — экран калибровки (shell/renderer/calibration.js)
 * против сценариев ответа сайдкара, без Electron и без камеры.
 *
 * DOM — заглушка, время — виртуальное: Date.now, requestAnimationFrame и
 * setTimeout подменены, поэтому 13.5 с обхода сетки проходят мгновенно и
 * детерминированно.
 *
 * Что проверяем (регрессия 08.10: этап сетки заканчивался после ПЕРВОЙ точки,
 * потому что done по точке читался как итог этапа, и карта экрана не
 * строилась ни в одном живом прогоне):
 *   1. обход показывает все 9 точек, даже если сайдкар шлёт done по точке;
 *   2. после обхода уходит calibrate {gaze_grid, point:null}, этап ждёт итог;
 *   3. плохая карта -> пауза, «Повторить этап» и «Продолжить» работают;
 *   4. повтор этапа не наследует прогресс прошлой попытки;
 *   5. без ответа сайдкара этап закрывается по потолку ожидания и честно
 *      помечается в сводке;
 *   6. точка держится, пока сайдкар не набрал её кадры (0.9–3 с), — на
 *      медленной камере карта иначе не строилась бы;
 *   7. итог этапа берётся только из done: прогресс с оценкой прошлой попытки
 *      не ставит ложную паузу; плохой центр не роняет хорошую карту.
 *
 * Запуск: node scripts/check_calibration_flow.js
 * =========================================================================== */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'shell', 'renderer', 'calibration.js'), 'utf8');

// ------------------------------------------------------------ виртуальное время
function makeClock() {
  let now = 1_000_000;
  let seq = 0;
  const timers = new Map();
  return {
    now: () => now,
    setTimeout(fn, ms) { const id = ++seq; timers.set(id, { at: now + (ms || 0), fn }); return id; },
    clearTimeout(id) { timers.delete(id); },
    /** Прокрутить время на ms шагами по 16 мс, исполняя созревшие таймеры. */
    advance(ms, onTick) {
      const end = now + ms;
      while (now < end) {
        now = Math.min(end, now + 16);
        for (const [id, t] of [...timers]) {
          if (t.at <= now && timers.has(id)) { timers.delete(id); t.fn(); }
        }
        if (onTick) onTick();
      }
    }
  };
}

// ------------------------------------------------------------------ заглушка DOM
function makeEl(id) {
  const listeners = {};
  const el = {
    id, hidden: false, disabled: false, textContent: '', innerHTML: '', className: '',
    style: { setProperty() {} }, attrs: {}, children: [], parentNode: null, srcObject: null,
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    click() { (listeners.click || []).forEach((fn) => fn()); },
    insertBefore(node) { node.parentNode = this; this.children.push(node); return node; },
    appendChild(node) { node.parentNode = this; this.children.push(node); return node; },
    get firstChild() { return this.children[0] || null; },
    get nextSibling() { return null; }
  };
  return el;
}

function makeDocument() {
  const byId = {};
  const ids = ['calib-steps', 'calib-title', 'calib-hint', 'calib-viewport', 'calib-face',
    'calib-video', 'calib-count', 'calib-dotfield', 'calib-dot', 'calib-voice',
    'calib-progress', 'calib-counter', 'btn-calib-start', 'btn-calib-retry', 'btn-calib-next'];
  for (const id of ids) byId[id] = makeEl(id);
  byId['btn-calib-start'].textContent = 'Начать калибровку';
  byId['btn-calib-next'].hidden = true;
  const parent = makeEl('wrap');
  parent.appendChild(byId['calib-hint']);
  parent.appendChild(byId['calib-counter']);
  const head = makeEl('stagehead');
  const cross = makeEl('cross');
  const footer = makeEl('footer');
  return {
    byId,
    head: makeEl('docHead'),
    getElementById(id) { return byId[id] || null; },
    querySelector(sel) {
      if (sel === '.calib__stagehead') return head;
      if (sel === '.scan-visual__cross') return cross;
      if (sel === '.calib__footer') return footer;
      return null;
    },
    createElement(tag) {
      const node = makeEl('');
      node.tagName = tag;
      const origSet = node.setAttribute.bind(node);
      node.setAttribute = (k, v) => { origSet(k, v); if (k === 'id') byId[v] = node; };
      return node;
    }
  };
}

// --------------------------------------------------------------- один сценарий
/**
 * sidecar(msg, api) — как отвечает сайдкар на каждое calibrate; api.reply(m)
 * доставляет сообщение calibration через delay мс виртуального времени.
 */
function runScenario(name, opts) {
  const clock = makeClock();
  const document = makeDocument();
  const sent = [];
  const window = {};
  const ctx = {
    window, document, console,
    navigator: {},
    Date: Object.assign(function () {}, { now: clock.now }),
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    requestAnimationFrame: (fn) => clock.setTimeout(fn, 16),
    cancelAnimationFrame: clock.clearTimeout,
    Promise
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(SRC, ctx, { filename: 'calibration.js' });
  const calib = ctx.Proctor.calibration;

  const api = {
    reply(msg, delay) {
      clock.setTimeout(() => calib.applyMessage(msg), delay || 50);
    }
  };
  calib.init({
    sendCalibrate(stage, point) {
      sent.push({ stage, point, t: clock.now() });
      if (opts.sidecar) opts.sidecar({ stage, point }, api, sent);
      return opts.bridge !== false;
    }
  });
  // В классе: голоса нет; личность выключена, чтобы смотреть на взгляд.
  calib.applyHello({ exam_mode: 'classroom',
    capabilities: { vision: true, gaze: true, identity: false, audio: false, env: true } });

  const dom = document.byId;
  const btn = { start: dom['btn-calib-start'], retry: dom['btn-calib-retry'], next: dom['btn-calib-next'] };
  return { clock, calib, sent, dom, btn, name };
}

let failures = 0;
function check(cond, label) {
  if (cond) console.log('  ок     ' + label);
  else { console.log('  ОШИБКА ' + label); failures++; }
}

const gridPoints = (sent) => sent.filter((m) => m.stage === 'gaze_grid' && m.point);
const finalizes = (sent) => sent.filter((m) => m.stage === 'gaze_grid' && !m.point);

/** Сайдкар после исправления: done по точке не шлёт, итог — на point=null. */
function honestSidecar(gridResult) {
  return (msg, api) => {
    if (msg.stage === 'gaze_center') {
      api.reply({ stage: 'gaze_center', progress: 1, done: true,
        result: { ok: true, quality: { grade: 'fair', center_stable: true } } }, 3500);
    } else if (msg.stage === 'gaze_grid' && msg.point) {
      api.reply({ stage: 'gaze_grid', progress: 0.1, done: false,
        result: { ok: true, point: msg.point, point_done: true } }, 900);
    } else if (msg.stage === 'gaze_grid') {
      api.reply({ stage: 'gaze_grid', progress: 1, done: true, result: gridResult }, 120);
    }
  };
}

// ---------------------------------------------------------------------------
console.log('=== 1. хорошая карта: 9 точек, сигнал конца обхода, без паузы ===');
{
  const s = runScenario('good', { sidecar: honestSidecar({
    ok: true, all_points_done: true, screen_map_applied: true,
    quality: { grade: 'good', loo_rmse: 0.036 } }) });
  s.btn.start.click();
  s.clock.advance(30000);
  check(gridPoints(s.sent).length === 9, 'показаны все 9 точек сетки');
  check(finalizes(s.sent).length === 1, 'после обхода ровно один calibrate {gaze_grid, null}');
  const lastPoint = gridPoints(s.sent).pop();
  const fin = finalizes(s.sent)[0];
  check(fin && lastPoint && fin.t - lastPoint.t >= 900, 'сигнал ушёл после того, как девятая точка отстояла');
  check(s.calib.done && !s.btn.next.hidden, 'калибровка завершена, «К экзамену» видна');
  check(!/Не удалось/.test(s.dom['calib-hint'].textContent), 'сводка без замечаний');
}

console.log('=== 2. старый сайдкар: done по каждой точке не обрывает обход ===');
{
  const s = runScenario('legacy', { sidecar: (msg, api) => {
    if (msg.stage === 'gaze_center') {
      api.reply({ stage: 'gaze_center', progress: 1, done: true, result: { ok: true } }, 2500);
    } else if (msg.stage === 'gaze_grid') {
      // ровно то, что было в журнале 08.10: progress 1 и done на первой точке
      api.reply({ stage: 'gaze_grid', progress: 1, done: true,
        result: { ok: true, all_points_done: false, point: msg.point } }, 800);
    }
  } });
  s.btn.start.click();
  s.clock.advance(40000);
  check(gridPoints(s.sent).length === 9, 'показаны все 9 точек, а не одна');
}

console.log('=== 3. плохая карта: пауза, «Повторить этап», затем «Продолжить» ===');
{
  let attempt = 0;
  const s = runScenario('poor', { sidecar: (msg, api) => {
    if (msg.stage === 'gaze_grid' && !msg.point) attempt++;
    honestSidecar({ ok: true, all_points_done: true, screen_map_applied: false,
      quality: { grade: 'poor', map_grade: 'poor', loo_rmse: 0.257 } })(msg, api);
  } });
  s.btn.start.click();
  s.clock.advance(25000);
  const todo = s.dom['calib-todo'] ? s.dom['calib-todo'].textContent : '';
  check(!s.calib.done && s.calib._paused, 'этап встал на паузу, а не проскочил');
  check(/26% ширины экрана/.test(todo), 'причина названа числом: «' + todo.slice(0, 60) + '…»');
  check(!s.btn.start.hidden && s.btn.start.textContent === 'Продолжить', 'кнопка «Продолжить» видна');
  check(!s.btn.retry.disabled, '«Повторить этап» доступна');

  const before = gridPoints(s.sent).length;
  s.btn.retry.click();
  s.clock.advance(800);
  check(s.calib._paused === false && s.btn.start.hidden, 'повтор снял паузу');
  check(s.calib.stageProgress < 0.2, 'повтор начался с нуля, а не с прогресса прошлой попытки');
  s.clock.advance(25000);
  check(gridPoints(s.sent).length - before === 9, 'повтор заново прошёл все 9 точек');
  check(attempt === 2 && s.calib._paused, 'после повтора снова пауза (карта снова плохая)');

  s.btn.start.click();
  s.clock.advance(1000);
  check(s.calib.done, '«Продолжить» довёл калибровку до конца');
  check(/Не удалось: карта экрана/.test(s.dom['calib-hint'].textContent),
    'сводка честно говорит, что карта экрана не удалась');
}

console.log('=== 4. сайдкар молчит после обхода: потолок ожидания, честная отметка ===');
{
  const s = runScenario('silent', { sidecar: (msg, api) => {
    if (msg.stage === 'gaze_center') {
      api.reply({ stage: 'gaze_center', progress: 1, done: true, result: { ok: true } }, 2500);
    }
  } });
  s.btn.start.click();
  s.clock.advance(30000);
  const todo = s.dom['calib-todo'] ? s.dom['calib-todo'].textContent : '';
  check(s.calib._paused && /не ответил вовремя/.test(todo), 'пауза с причиной «не ответил вовремя»');
}

console.log('=== 5. моста нет: калибровка идёт локально и не ждёт ответа ===');
{
  const s = runScenario('nobridge', { bridge: false });
  s.btn.start.click();
  s.clock.advance(25000);
  check(s.calib.done && !s.calib._paused, 'завершилась без паузы и без ожидания');
}

console.log('=== 6. мок-сайдкар: деградация не считается провалом ===');
{
  const s = runScenario('mock', { sidecar: (msg, api) => {
    const grid = msg.stage === 'gaze_grid';
    if (grid && msg.point) {
      api.reply({ stage: 'gaze_grid', progress: 0, done: false, result: { point: msg.point, mock: true } }, 20);
      return;
    }
    api.reply({ stage: msg.stage, progress: 1, done: true,
      result: Object.assign({ ok: true, degraded: true, mock: true },
        grid ? { all_points_done: true, screen_map_applied: false } : {}) }, 1050);
  } });
  s.btn.start.click();
  s.clock.advance(30000);
  check(s.calib.done && !s.calib._paused, 'мок проходит калибровку без паузы');
  check(gridPoints(s.sent).length === 9, 'и в моке показаны все 9 точек');
}

console.log('=== 7. медленная камера: точка ждёт point_done, но не дольше 3 с ===');
{
  const s = runScenario('slow', { sidecar: (msg, api) => {
    if (msg.stage === 'gaze_center') {
      api.reply({ stage: 'gaze_center', progress: 1, done: true, result: { ok: true } }, 5000);
    } else if (msg.stage === 'gaze_grid' && msg.point) {
      api.reply({ stage: 'gaze_grid', progress: 0.05, done: false,
        result: { samples: 5, point: msg.point } }, 700);
      // последняя точка так и не набирает кадры
      const last = msg.point[0] === 0.94 && msg.point[1] === 0.92;
      if (!last) {
        api.reply({ stage: 'gaze_grid', progress: 0.1, done: false,
          result: { ok: true, point: msg.point, point_done: true } }, 2400);
      }
    } else if (msg.stage === 'gaze_grid') {
      api.reply({ stage: 'gaze_grid', progress: 1, done: true, result: {
        ok: true, all_points_done: true, screen_map_applied: true,
        quality: { grade: 'fair', map_grade: 'good', loo_rmse: 0.05 } } }, 100);
    }
  } });
  s.btn.start.click();
  s.clock.advance(45000);
  const pts = gridPoints(s.sent);
  const gaps = pts.slice(1).map((p, i) => p.t - pts[i].t);
  const fin = finalizes(s.sent)[0];
  check(pts.length === 9, 'показаны все 9 точек');
  check(gaps.every((g) => g >= 2400 && g < 2600), 'каждая точка ждала point_done (~2.4 с), а не 1.5 с');
  check(fin && fin.t - pts[8].t >= 3000 && fin.t - pts[8].t < 3100,
    'точка без point_done отпущена по потолку 3 с');
  check(s.calib.done && !s.calib._paused, 'карта применена — без паузы');
}

console.log('=== 8. итог — только из done: устаревшая оценка не ставит паузу ===');
{
  const s = runScenario('stale', { sidecar: (msg, api) => {
    if (msg.stage === 'gaze_center') {
      // прогресс несёт оценку ПРОШЛОЙ попытки; done до таймера 7 с не придёт
      api.reply({ stage: 'gaze_center', progress: 0.4, done: false,
        result: { samples: 20, quality: { grade: 'poor' } } }, 1500);
    } else {
      honestSidecar({ ok: true, all_points_done: true, screen_map_applied: true,
        quality: { grade: 'poor', map_grade: 'good', loo_rmse: 0.04 } })(msg, api);
    }
  } });
  s.btn.start.click();
  s.clock.advance(9000);
  check(!s.calib._paused && s.calib.stageIndex > 1, 'центр закрыт таймером без ложной паузы');
  s.clock.advance(30000);
  check(s.calib.done && !s.calib._paused,
    'общая оценка poor (короткий центр) не ставит паузу, если карта хорошая');
}

console.log(failures ? `\nПровалено проверок: ${failures}` : '\nВсе проверки пройдены');
process.exit(failures ? 1 : 0);
