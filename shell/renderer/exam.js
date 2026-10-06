/* ===========================================================================
 * exam.js — мок-тест на 6 вопросов: 3 с выбором варианта, 3 с развёрнутым
 * текстовым ответом. На развёрнутых ответах работает телеметрия набора
 * (telemetry.js) — именно она питает fusion-движок.
 *
 * Таймер умеет останавливаться: на verdict "pause" время не идёт.
 * =========================================================================== */

(function () {
  'use strict';

  var TOTAL_SECONDS = 20 * 60;   // длительность мок-теста

  /** Банк вопросов. difficulty 1..3 уходит в телеметрию question_shown. */
  var QUESTIONS = [
    {
      id: 'Q1',
      type: 'choice',
      difficulty: 1,
      text: 'Какой показатель описывает долю времени, в течение которого оборудование реально выпускает годную продукцию на номинальной скорости?',
      options: [
        'MTBF — среднее время между отказами',
        'OEE — общая эффективность оборудования',
        'MTTR — среднее время восстановления',
        'SPC — статистическое управление процессом'
      ],
      correct: 1
    },
    {
      id: 'Q2',
      type: 'open',
      difficulty: 2,
      text: 'Датчик вибрации на конвейерном редукторе раз в сутки выдаёт короткий выброс амплитуды, но смена об этом не сообщает. Опишите, как вы отличите реальный зарождающийся дефект от помехи измерения, и какие данные для этого потребуются.',
      placeholder: 'Ответ в свободной форме: 4–8 предложений…'
    },
    {
      id: 'Q3',
      type: 'choice',
      difficulty: 2,
      text: 'Контроллер опрашивает 400 тегов с периодом 100 мс, канал связи до SCADA держит не более 1500 значений в секунду. Что корректнее сделать в первую очередь?',
      options: [
        'Увеличить период опроса всех тегов до 1 секунды',
        'Передавать значения по изменению с зоной нечувствительности',
        'Поставить второй контроллер и разделить теги пополам',
        'Отключить часть тегов из передачи в SCADA'
      ],
      correct: 1
    },
    {
      id: 'Q4',
      type: 'open',
      difficulty: 3,
      text: 'Цех просит «предсказывать поломки насосов». Исторических отказов за два года — девять штук, разметка аварий ведётся вручную в журнале мастера. Сформулируйте, какую задачу вы на самом деле будете решать и как измерите пользу от решения.',
      placeholder: 'Ответ в свободной форме: постановка задачи, данные, метрика…'
    },
    {
      id: 'Q5',
      type: 'choice',
      difficulty: 3,
      text: 'Модель предсказания брака на обучении даёт ROC-AUC 0.94, на проде — около 0.62. Доля брака в выборке 1.5 %. Что наиболее вероятно?',
      options: [
        'Модель недоучена, нужно больше эпох',
        'В обучающие признаки попали данные, недоступные в момент предсказания',
        'ROC-AUC не применим к бинарной классификации',
        'Нужно просто понизить порог отсечения'
      ],
      correct: 1
    },
    {
      id: 'Q6',
      type: 'open',
      difficulty: 3,
      text: 'Вы внедряете систему контроля качества на основе машинного зрения. Опишите, как организуете разбор ложных срабатываний, чтобы операторы не начали игнорировать систему через месяц работы.',
      placeholder: 'Ответ в свободной форме: процесс, роли, обратная связь…'
    }
  ];

  var TYPE_TAG = { choice: 'выбор варианта', open: 'развёрнутый ответ' };
  var DIFF_TAG = { 1: 'базовый', 2: 'средний', 3: 'сложный' };

  function el(id) { return document.getElementById(id); }
  function pad2(n) { return n < 10 ? '0' + n : String(n); }
  function mmss(sec) {
    sec = Math.max(0, Math.round(sec));
    return pad2(Math.floor(sec / 60)) + ':' + pad2(sec % 60);
  }
  function esc(s) {
    return String(s === undefined || s === null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function Exam() {
    this.dom = {};
    this.index = 0;
    this.answers = {};       // id -> {value:string} | {choice:number}
    this.submitted = {};     // id -> true, чтобы не дублировать answer_submit
    this.remaining = TOTAL_SECONDS;
    this.paused = true;
    this.running = false;
    this.startedAt = 0;
    this.pausedMs = 0;
    this._pauseStartedAt = 0;
    this._timer = null;
    this.telemetry = null;
    this.onFinish = null;
    this.onQuestionShown = null;
  }

  Exam.prototype.init = function (opts) {
    opts = opts || {};
    var self = this;
    this.telemetry = opts.telemetry || (window.Proctor && window.Proctor.telemetry) || null;
    this.onFinish = opts.onFinish || null;
    this.onQuestionShown = opts.onQuestionShown || null;

    this.dom = {
      name: el('exam-name'),
      pos: el('exam-pos'),
      palette: el('exam-palette'),
      timer: el('exam-timer'),
      timerVal: el('exam-timerval'),
      body: el('exam-body'),
      prev: el('btn-prev'),
      next: el('btn-next'),
      finish: el('btn-finish'),
      saveState: el('exam-savestate')
    };

    if (this.dom.prev) this.dom.prev.addEventListener('click', function () { self.go(self.index - 1); });
    if (this.dom.next) this.dom.next.addEventListener('click', function () { self.go(self.index + 1); });
    if (this.dom.finish) this.dom.finish.addEventListener('click', function () { self.finish('student'); });
  };

  Exam.prototype.questionCount = function () { return QUESTIONS.length; };

  /** Запуск экзамена: отрисовать первый вопрос и пустить таймер. */
  Exam.prototype.start = function (examTitle) {
    this.index = 0;
    this.answers = {};
    this.submitted = {};
    this.remaining = TOTAL_SECONDS;
    this.paused = false;
    this.running = true;
    this.startedAt = Date.now();
    this.pausedMs = 0;
    if (this.dom.name && examTitle) this.dom.name.textContent = examTitle;
    this._renderPalette();
    this._render();
    this._startTimer();
  };

  Exam.prototype._startTimer = function () {
    var self = this;
    if (this._timer) clearInterval(this._timer);
    var last = Date.now();
    this._timer = setInterval(function () {
      var now = Date.now();
      var dt = (now - last) / 1000;
      last = now;
      if (self.paused || !self.running) return;
      self.remaining -= dt;
      if (self.remaining <= 0) {
        self.remaining = 0;
        self._renderTimer();
        self.finish('timeout');
        return;
      }
      self._renderTimer();
    }, 250);
    this._renderTimer();
  };

  Exam.prototype._renderTimer = function () {
    if (this.dom.timerVal) this.dom.timerVal.textContent = mmss(this.remaining);
    if (this.dom.timer) {
      this.dom.timer.classList.toggle('is-low', this.remaining <= 300 && this.remaining > 60);
      this.dom.timer.classList.toggle('is-crit', this.remaining <= 60);
    }
  };

  /** Пауза по вердикту прокторинга: время не идёт, ввод недоступен (оверлей сверху). */
  Exam.prototype.pause = function () {
    if (this.paused || !this.running) return;
    this.paused = true;
    this._pauseStartedAt = Date.now();
    if (this.dom.saveState) this.dom.saveState.textContent = 'тест приостановлен, время не идёт';
  };

  Exam.prototype.resume = function () {
    if (!this.paused || !this.running) return;
    this.paused = false;
    if (this._pauseStartedAt) this.pausedMs += Date.now() - this._pauseStartedAt;
    this._pauseStartedAt = 0;
    if (this.dom.saveState) this.dom.saveState.textContent = 'ответы сохраняются локально';
    var ta = this.dom.body ? this.dom.body.querySelector('textarea') : null;
    if (ta) { try { ta.focus(); } catch (e) {} }
  };

  Exam.prototype.isPaused = function () { return this.paused; };

  // --- навигация ---

  Exam.prototype.go = function (nextIndex) {
    if (!this.running) return;
    if (nextIndex < 0) return;
    if (nextIndex >= QUESTIONS.length) { this.finish('student'); return; }
    this._leaveCurrent();
    this.index = nextIndex;
    this._render();
    this._renderPalette();
  };

  /** Фиксация ответа на уходящем вопросе — отсюда уходит answer_submit. */
  Exam.prototype._leaveCurrent = function () {
    var q = QUESTIONS[this.index];
    if (!q) return;
    this._captureAnswer(q);
    this._submitAnswer(q);
  };

  Exam.prototype._captureAnswer = function (q) {
    if (q.type !== 'open') return;
    var ta = this.dom.body ? this.dom.body.querySelector('textarea') : null;
    if (ta) this.answers[q.id] = { value: ta.value };
  };

  Exam.prototype._submitAnswer = function (q) {
    if (!this.telemetry) return;
    var a = this.answers[q.id];
    var len = 0;
    if (a && typeof a.value === 'string') len = a.value.length;
    this.telemetry.answerSubmit(q.id, len);
    this.submitted[q.id] = true;
  };

  Exam.prototype._renderPalette = function () {
    if (!this.dom.palette) return;
    var html = '';
    for (var i = 0; i < QUESTIONS.length; i++) {
      var q = QUESTIONS[i];
      var a = this.answers[q.id];
      var answered = !!(a && ((typeof a.choice === 'number') || (typeof a.value === 'string' && a.value.trim().length > 0)));
      var cls = 'qbtn';
      if (q.type === 'open') cls += ' is-open';
      if (answered) cls += ' is-answered';
      if (i === this.index) cls += ' is-current';
      html += '<button type="button" class="' + cls + '" data-idx="' + i + '" ' +
              'aria-label="Вопрос ' + (i + 1) + '">' + (i + 1) +
              '<span class="qbtn__kind"></span></button>';
    }
    this.dom.palette.innerHTML = html;
    var self = this;
    var btns = this.dom.palette.querySelectorAll('.qbtn');
    for (var j = 0; j < btns.length; j++) {
      btns[j].addEventListener('click', function (ev) {
        var idx = parseInt(ev.currentTarget.getAttribute('data-idx'), 10);
        if (!isNaN(idx) && idx !== self.index) self.go(idx);
      });
    }
  };

  Exam.prototype._render = function () {
    var q = QUESTIONS[this.index];
    if (!q || !this.dom.body) return;

    if (this.dom.pos) {
      this.dom.pos.textContent = 'вопрос ' + (this.index + 1) + ' из ' + QUESTIONS.length;
    }
    if (this.dom.prev) this.dom.prev.disabled = this.index === 0;
    if (this.dom.next) this.dom.next.textContent = this.index === QUESTIONS.length - 1 ? 'К завершению' : 'Далее';
    if (this.dom.saveState) this.dom.saveState.textContent = 'ответы сохраняются локально';

    var head =
      '<div class="qcard__top">' +
        '<span class="tag' + (q.type === 'open' ? ' tag--open' : '') + '">' + esc(TYPE_TAG[q.type]) + '</span>' +
        '<span class="tag tag--diff">' + esc(DIFF_TAG[q.difficulty] || 'средний') + '</span>' +
        '<span class="tag">вопрос ' + (this.index + 1) + '</span>' +
      '</div>' +
      '<div class="qcard__text">' + esc(q.text) + '</div>';

    var card = document.createElement('div');
    card.className = 'qcard';

    if (q.type === 'choice') {
      var saved = this.answers[q.id];
      var opts = '<div class="opts">';
      for (var i = 0; i < q.options.length; i++) {
        var checked = saved && saved.choice === i;
        opts += '<label class="opt' + (checked ? ' is-checked' : '') + '" data-opt="' + i + '">' +
                  '<input type="radio" name="' + esc(q.id) + '" value="' + i + '"' + (checked ? ' checked' : '') + ' />' +
                  '<span class="opt__radio"></span>' +
                  '<span class="opt__label">' + esc(q.options[i]) + '</span>' +
                '</label>';
      }
      opts += '</div>';
      card.innerHTML = head + opts +
        '<div class="qcard__foot"><span></span><span>вариант можно изменить до перехода к следующему вопросу</span></div>';
      this.dom.body.innerHTML = '';
      this.dom.body.appendChild(card);

      var self = this;
      var labels = card.querySelectorAll('.opt');
      for (var k = 0; k < labels.length; k++) {
        labels[k].addEventListener('change', function (ev) {
          var idx = parseInt(ev.currentTarget.getAttribute('data-opt'), 10);
          self.answers[q.id] = { choice: idx };
          var all = card.querySelectorAll('.opt');
          for (var m = 0; m < all.length; m++) all[m].classList.toggle('is-checked', m === idx);
          self._renderPalette();
        });
      }
    } else {
      card.innerHTML = head +
        '<textarea class="answer" id="answer-' + esc(q.id) + '" spellcheck="false" ' +
        'placeholder="' + esc(q.placeholder || 'Ваш ответ…') + '"></textarea>' +
        '<div class="qcard__foot">' +
          '<span class="qcard__privacy">' +
            '<svg viewBox="0 0 24 24" aria-hidden="true">' +
              '<rect x="4" y="10" width="16" height="10" rx="2.5"></rect>' +
              '<path d="M8 10V7.5a4 4 0 0 1 8 0V10"></path>' +
            '</svg>' +
            'фиксируются только интервалы между нажатиями, не символы' +
          '</span>' +
          '<span id="type-hint">0 символов</span>' +
        '</div>';
      this.dom.body.innerHTML = '';
      this.dom.body.appendChild(card);

      var ta = card.querySelector('textarea');
      var savedOpen = this.answers[q.id];
      // значение подставляется ДО привязки телеметрии, иначе восстановление
      // ответа выглядело бы как программная вставка
      if (savedOpen && typeof savedOpen.value === 'string') ta.value = savedOpen.value;

      var hint = card.querySelector('#type-hint');
      var selfRef = this;
      ta.addEventListener('input', function () {
        selfRef.answers[q.id] = { value: ta.value };
        if (hint) hint.textContent = ta.value.length + ' символов';
        selfRef._renderPalette();
      });
      if (hint) hint.textContent = ta.value.length + ' символов';

      if (this.telemetry) {
        this.telemetry.attach(ta, function () { return q.id; });
      }
      try { ta.focus(); } catch (e) {}
    }

    // question_shown уходит ПОСЛЕ отрисовки — время до ответа считается от показа
    if (this.telemetry) this.telemetry.questionShown(q.id, q.difficulty);
    if (this.onQuestionShown) {
      try { this.onQuestionShown(q, this.index); } catch (e) {}
    }
  };

  /** Завершение: 'student' | 'timeout' | 'lock'. */
  Exam.prototype.finish = function (reason) {
    if (!this.running) return;
    var q = QUESTIONS[this.index];
    if (q) { this._captureAnswer(q); this._submitAnswer(q); }

    // добираем всё, что было отвечено, но не зафиксировано (на всякий случай)
    for (var i = 0; i < QUESTIONS.length; i++) {
      var qq = QUESTIONS[i];
      if (!this.submitted[qq.id] && this.answers[qq.id]) this._submitAnswer(qq);
    }

    this.running = false;
    this.paused = true;
    if (this._timer) { clearInterval(this._timer); this._timer = null; }
    if (this.telemetry) this.telemetry.detachAll();

    var answered = 0;
    for (var j = 0; j < QUESTIONS.length; j++) {
      var a = this.answers[QUESTIONS[j].id];
      if (a && ((typeof a.choice === 'number') || (typeof a.value === 'string' && a.value.trim()))) answered++;
    }

    var res = {
      reason: reason || 'student',
      answered: answered,
      total: QUESTIONS.length,
      elapsed_ms: Date.now() - this.startedAt - this.pausedMs,
      paused_ms: this.pausedMs,
      remaining_s: Math.round(this.remaining)
    };
    if (this.onFinish) { try { this.onFinish(res); } catch (e) {} }
    return res;
  };

  Exam.prototype.questions = function () { return QUESTIONS; };

  window.Proctor = window.Proctor || {};
  window.Proctor.exam = new Exam();
})();
