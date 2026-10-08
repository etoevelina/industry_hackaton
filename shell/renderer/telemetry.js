/* ===========================================================================
 * telemetry.js — сбор телеметрии ввода для fusion-движка.
 *
 * ПРИВАТНОСТЬ (жёсткое правило, нарушать нельзя):
 *   наружу уходят ТОЛЬКО межклавишный интервал в миллисекундах и класс клавиши
 *   ("char" | "nav" | "ctrl"). Сами символы, содержимое буфера обмена и текст
 *   ответа никогда не покидают renderer. Для answer_submit передаётся только
 *   длина ответа в символах.
 *
 * Формат сообщений — docs/CONTRACT.md, раздел «Телеметрия ввода»:
 *   keystroke      {question_id, ts, interval_ms, key_class}
 *   paste          {question_id, ts, length, source}
 *   answer_submit  {question_id, ts, length, time_to_answer_ms, typing_stats:{mean_ms,std_ms,chars}}
 *   question_shown {question_id, ts, difficulty}
 *
 * Эвристика «программной вставки» (вставка мимо события paste — например
 * скриптом, drag-and-drop или средствами ОС):
 *   на каждое событие input считается прирост длины поля. Если длина выросла
 *   более чем на PASTE_MIN_CHARS символов, прошло меньше PASTE_MAX_GAP_MS с
 *   предыдущего input, и за это время не было соответствующего числа нажатий
 *   клавиш-символов, прирост признаётся вставкой и отправляется как paste.
 *   Нативное событие paste выставляет окно подавления (SUPPRESS_MS), чтобы один
 *   и тот же факт не был отправлен дважды.
 *
 * Статистика ритма: интервалы длиннее IDLE_CUTOFF_MS считаются паузой на
 *   размышление и не попадают в mean/std — иначе одна пауза в 30 секунд
 *   полностью ломает профиль набора.
 * =========================================================================== */

(function () {
  'use strict';

  var PASTE_MIN_CHARS = 80;    // прирост длины, подозрительный сам по себе
  var PASTE_MAX_GAP_MS = 200;  // за такое время руками столько не набрать
  var SUPPRESS_MS = 350;       // окно подавления после нативного paste
  var IDLE_CUTOFF_MS = 5000;   // интервал сверх этого — пауза, не ритм

  // Клавиши навигации и правки: перемещение каретки и удаление.
  var NAV_KEYS = {
    ArrowUp: 1, ArrowDown: 1, ArrowLeft: 1, ArrowRight: 1,
    Home: 1, End: 1, PageUp: 1, PageDown: 1,
    Backspace: 1, Delete: 1, Tab: 1, Enter: 1
  };

  // Модификаторы и прочая служебка.
  var CTRL_KEYS = {
    Shift: 1, Control: 1, Alt: 1, Meta: 1, CapsLock: 1, Escape: 1,
    Insert: 1, ContextMenu: 1, NumLock: 1, ScrollLock: 1, Pause: 1,
    PrintScreen: 1, Dead: 1
  };

  function nowMs() { return (window.performance && performance.now) ? performance.now() : Date.now(); }
  function tsSec() { return Date.now() / 1000; }

  /**
   * Класс клавиши. Любое нажатие с Ctrl/Cmd/Alt — служебное ("ctrl"),
   * даже если сама клавиша печатная: это горячая клавиша, а не набор текста.
   */
  function classifyKey(ev) {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return 'ctrl';
    var k = ev.key;
    if (typeof k !== 'string') return 'ctrl';
    if (NAV_KEYS[k]) return 'nav';
    if (CTRL_KEYS[k]) return 'ctrl';
    if (k.length === 1 || k === 'Spacebar' || k === ' ') return 'char';
    if (/^F\d{1,2}$/.test(k)) return 'ctrl';
    return 'ctrl';
  }

  /** Накопитель статистики по одному вопросу. */
  function QuestionStats() {
    this.intervals = [];   // только «ритмовые» интервалы (< IDLE_CUTOFF_MS)
    this.charKeys = 0;     // число нажатий клавиш-символов
    this.navKeys = 0;
    this.ctrlKeys = 0;
    this.pastes = 0;
    this.pastedChars = 0;
    this.shownAt = nowMs();
    this.firstKeyAt = null;
    this.lastKeyAt = null;
  }

  QuestionStats.prototype.push = function (intervalMs, cls) {
    if (cls === 'char') this.charKeys++;
    else if (cls === 'nav') this.navKeys++;
    else this.ctrlKeys++;
    var t = nowMs();
    if (this.firstKeyAt === null) this.firstKeyAt = t;
    this.lastKeyAt = t;
    if (intervalMs > 0 && intervalMs < IDLE_CUTOFF_MS) this.intervals.push(intervalMs);
  };

  QuestionStats.prototype.summary = function (chars) {
    var n = this.intervals.length;
    var mean = 0, std = 0;
    if (n > 0) {
      var sum = 0, i;
      for (i = 0; i < n; i++) sum += this.intervals[i];
      mean = sum / n;
      var acc = 0;
      for (i = 0; i < n; i++) { var d = this.intervals[i] - mean; acc += d * d; }
      std = Math.sqrt(acc / n);
    }
    return {
      mean_ms: Math.round(mean * 10) / 10,
      std_ms: Math.round(std * 10) / 10,
      chars: typeof chars === 'number' ? chars : this.charKeys
    };
  };

  /**
   * Telemetry — единая точка отправки. Канал передаётся снаружи (bridge.sendTelemetry),
   * чтобы модуль не зависел от наличия window.proctor и был тестируем.
   */
  function Telemetry() {
    this._send = null;
    this._stats = {};          // question_id -> QuestionStats
    this._bound = [];          // привязанные поля, чтобы снимать слушатели
    this._lastKeyDownAt = null; // для межклавишного интервала (сквозной по полю)
    this._current = null;       // id текущего вопроса
    this._sentCount = 0;
  }

  Telemetry.prototype.init = function (sendFn) {
    this._send = typeof sendFn === 'function' ? sendFn : null;
  };

  Telemetry.prototype.sentCount = function () { return this._sentCount; };

  Telemetry.prototype._emit = function (msg) {
    this._sentCount++;
    if (!this._send) return;
    try { this._send(msg); } catch (e) { /* канал мог отвалиться — телеметрия не роняет UI */ }
  };

  Telemetry.prototype.statsFor = function (questionId) {
    if (!this._stats[questionId]) this._stats[questionId] = new QuestionStats();
    return this._stats[questionId];
  };

  /** Вопрос показан: {kind:"question_shown", question_id, ts, difficulty} */
  Telemetry.prototype.questionShown = function (questionId, difficulty) {
    this._current = questionId;
    var st = this.statsFor(questionId);
    st.shownAt = nowMs();
    this._lastKeyDownAt = null;
    this._emit({
      kind: 'question_shown',
      question_id: questionId,
      ts: tsSec(),
      difficulty: difficulty | 0
    });
  };

  /**
   * Ответ зафиксирован: {kind:"answer_submit", question_id, ts, length,
   *                      time_to_answer_ms, typing_stats:{mean_ms,std_ms,chars}}
   * Передаётся длина ответа, не текст.
   */
  Telemetry.prototype.answerSubmit = function (questionId, textLength) {
    var st = this.statsFor(questionId);
    var len = textLength | 0;
    this._emit({
      kind: 'answer_submit',
      question_id: questionId,
      ts: tsSec(),
      length: len,
      time_to_answer_ms: Math.round(nowMs() - st.shownAt),
      typing_stats: st.summary(len),
      // дополнительные поля — справочные, fusion может их игнорировать
      key_counts: { char: st.charKeys, nav: st.navKeys, ctrl: st.ctrlKeys },
      paste_count: st.pastes,
      pasted_chars: st.pastedChars
    });
  };

  /** Вставка: {kind:"paste", question_id, ts, length, source} */
  Telemetry.prototype.paste = function (questionId, length, origin) {
    var st = this.statsFor(questionId);
    st.pastes++;
    st.pastedChars += length | 0;
    this._emit({
      kind: 'paste',
      question_id: questionId,
      ts: tsSec(),
      length: length | 0,
      source: 'clipboard',
      // чем именно обнаружено: нативное событие или скачок длины поля
      origin: origin || 'paste_event'
    });
  };

  /** Нажатие: {kind:"keystroke", question_id, ts, interval_ms, key_class} */
  Telemetry.prototype._keystroke = function (questionId, intervalMs, cls) {
    this._emit({
      kind: 'keystroke',
      question_id: questionId,
      ts: tsSec(),
      interval_ms: Math.round(intervalMs),
      key_class: cls
    });
  };

  /**
   * Привязать сбор телеметрии к textarea.
   * getQuestionId() — функция, возвращающая id вопроса, к которому поле относится
   * (вопрос может переключаться без пересоздания поля).
   */
  Telemetry.prototype.attach = function (el, getQuestionId) {
    if (!el || el.__telemetryBound) return;
    var self = this;
    var qid = typeof getQuestionId === 'function' ? getQuestionId : function () { return String(getQuestionId); };

    var state = {
      prevLen: el.value ? el.value.length : 0,
      charKeysSinceInput: 0,
      suppressJumpUntil: 0,
      // скользящее окно прироста длины шириной PASTE_MAX_GAP_MS
      burstStart: nowMs(),
      burstChars: 0,
      burstKeys: 0
    };

    function onKeyDown(ev) {
      if (ev.isComposing) return;
      // Автоповтор при удержании клавиши — не ритм набора, а настройка
      // системы. Такие интервалы ровные и короткие, и до этой проверки
      // удержание одной клавиши лило их прямо в базовую линию: держать
      // клавишу четыре секунды в начале сессии опускало профиль студента
      // вдвое, а вместе с ним и порог бурста, то есть разом глушило ОБА
      // клавиатурных канала. Интервал от автоповтора не отражает человека,
      // поэтому в профиль он не идёт.
      if (ev.repeat) return;
      var cls = classifyKey(ev);
      var t = nowMs();
      var interval = self._lastKeyDownAt === null ? 0 : t - self._lastKeyDownAt;
      self._lastKeyDownAt = t;
      if (cls === 'char') state.charKeysSinceInput++;
      var id = qid();
      self.statsFor(id).push(interval, cls);
      self._keystroke(id, interval, cls);
    }

    function onKeyUp(ev) {
      // keyup используется только для корректного завершения удержания:
      // интервал считается по keydown, чтобы совпадать с восприятием ритма.
      if (ev.isComposing) return;
      if (ev.key === 'Shift' || ev.key === 'Control' || ev.key === 'Meta' || ev.key === 'Alt') {
        // отпускание модификатора не ломает отсчёт интервала
        return;
      }
    }

    function onPaste(ev) {
      var len = 0;
      try {
        var cd = ev.clipboardData || window.clipboardData;
        // читается только длина, сам текст не сохраняется и не отправляется
        if (cd) len = (cd.getData('text') || '').length;
      } catch (e) { len = 0; }
      state.suppressJumpUntil = nowMs() + SUPPRESS_MS;
      self.paste(qid(), len, 'paste_event');
      // длина поля обновится в onInput — пересинхронизируем базу позже
    }

    function onDrop(ev) {
      var len = 0;
      try {
        var dt = ev.dataTransfer;
        if (dt) len = (dt.getData('text') || '').length;
      } catch (e) { len = 0; }
      state.suppressJumpUntil = nowMs() + SUPPRESS_MS;
      if (len > 0) self.paste(qid(), len, 'drop');
    }

    function onInput() {
      var t = nowMs();
      var len = el.value ? el.value.length : 0;
      var delta = len - state.prevLen;
      state.prevLen = len;

      // окно старше PASTE_MAX_GAP_MS — начинаем считать прирост заново
      if (t - state.burstStart > PASTE_MAX_GAP_MS) {
        state.burstStart = t;
        state.burstChars = 0;
        state.burstKeys = 0;
      }
      if (delta > 0) state.burstChars += delta;
      state.burstKeys += state.charKeysSinceInput;
      state.charKeysSinceInput = 0;

      // Прирост больше PASTE_MIN_CHARS за окно короче PASTE_MAX_GAP_MS, не
      // объяснимый нажатиями клавиш, — это вставка, а не набор.
      if (state.burstChars >= PASTE_MIN_CHARS &&
          state.burstChars > state.burstKeys + 5 &&
          t > state.suppressJumpUntil) {
        self.paste(qid(), state.burstChars, 'length_jump');
        state.suppressJumpUntil = t + SUPPRESS_MS;
        state.burstStart = t;
        state.burstChars = 0;
        state.burstKeys = 0;
        return;
      }

      // после нативного paste окно подавления закрыто — обнуляем накопитель,
      // чтобы тот же текст не был засчитан второй раз
      if (t <= state.suppressJumpUntil) {
        state.burstStart = t;
        state.burstChars = 0;
        state.burstKeys = 0;
      }
    }

    el.addEventListener('keydown', onKeyDown);
    el.addEventListener('keyup', onKeyUp);
    el.addEventListener('paste', onPaste);
    el.addEventListener('drop', onDrop);
    el.addEventListener('input', onInput);

    el.__telemetryBound = true;
    this._bound.push({
      el: el,
      off: function () {
        el.removeEventListener('keydown', onKeyDown);
        el.removeEventListener('keyup', onKeyUp);
        el.removeEventListener('paste', onPaste);
        el.removeEventListener('drop', onDrop);
        el.removeEventListener('input', onInput);
        el.__telemetryBound = false;
      }
    });
  };

  /** Снять все слушатели (вызывается при завершении экзамена). */
  Telemetry.prototype.detachAll = function () {
    for (var i = 0; i < this._bound.length; i++) {
      try { this._bound[i].off(); } catch (e) { /* поле уже удалено из DOM */ }
    }
    this._bound = [];
  };

  /** Сводка по всей сессии — для экрана отчёта. */
  Telemetry.prototype.sessionSummary = function () {
    var all = [], chars = 0, pastes = 0, pasted = 0, ids = Object.keys(this._stats);
    for (var i = 0; i < ids.length; i++) {
      var st = this._stats[ids[i]];
      all = all.concat(st.intervals);
      chars += st.charKeys;
      pastes += st.pastes;
      pasted += st.pastedChars;
    }
    var mean = 0, std = 0;
    if (all.length) {
      var s = 0, j;
      for (j = 0; j < all.length; j++) s += all[j];
      mean = s / all.length;
      var a = 0;
      for (j = 0; j < all.length; j++) { var d = all[j] - mean; a += d * d; }
      std = Math.sqrt(a / all.length);
    }
    return {
      questions: ids.length,
      keystrokes: all.length,
      chars: chars,
      pastes: pastes,
      pasted_chars: pasted,
      mean_ms: Math.round(mean * 10) / 10,
      std_ms: Math.round(std * 10) / 10,
      messages_sent: this._sentCount
    };
  };

  window.Proctor = window.Proctor || {};
  window.Proctor.telemetry = new Telemetry();
  window.Proctor.classifyKey = classifyKey;   // отдельно — удобно проверять вручную
})();
