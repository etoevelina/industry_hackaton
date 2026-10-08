/* ===========================================================================
 * exam.js — демо-тест на 6 простых вопросов: 4 с выбором варианта, 2 с развёрнутым
 * текстовым ответом. На развёрнутых ответах работает телеметрия набора
 * (telemetry.js) — именно она питает fusion-движок.
 *
 * ДИЗАЙН-СИСТЕМА NEON/PROCTOR (Р-13):
 *   экран сдачи — светлый лист (.sheet) для чтения и письма, тёмный HUD живёт
 *   отдельно. Вопрос набран шириной 60–75 знаков, кегль не мельче 16px: два
 *   часа чтения с белого должны оставаться комфортными.
 *   Таймер и счётчик символов — моноширинные (телеметрия и таймкоды).
 *   Состояние вопроса в навигации дублируется формой и подписью, не только
 *   цветом: галочка — отвечен, чёрточка — пропущен, шеврон — текущий, кружок —
 *   не открыт; то же состояние словом уходит в aria-label и в легенду под
 *   палитрой (требование a11y из гайда).
 *
 * КОНТРАКТ ТЕЛЕМЕТРИИ (docs/CONTRACT.md) не менялся. Отсюда уходят ровно
 * четыре вида сообщений, и ровно теми же вызовами telemetry.js:
 *   question_shown — telemetry.questionShown(q.id, q.difficulty) после отрисовки
 *   answer_submit  — telemetry.answerSubmit(q.id, lengthInChars) при уходе с вопроса
 *   keystroke      — telemetry.attach(textarea, () => q.id) (интервалы и класс клавиши)
 *   paste          — тем же attach(); вставка НЕ блокируется, это доказательство
 * Текст ответа наружу не уходит никогда — только длина в символах.
 *
 * Таймер умеет останавливаться: на verdict "pause" время не идёт.
 * =========================================================================== */

(function () {
  'use strict';

  var TOTAL_SECONDS = 10 * 60;   // длительность демо-теста

  var LOW_SECONDS = 300;         // «мало времени» — дублируется подписью
  var CRIT_SECONDS = 60;         // «меньше минуты»

  /** Банк вопросов. difficulty 1..3 уходит в телеметрию question_shown. */
  var QUESTIONS = [
    {
      id: 'Q1',
      type: 'choice',
      difficulty: 1,
      text: 'Какой город — столица Казахстана?',
      options: ['Алматы', 'Астана', 'Шымкент', 'Костанай'],
      correct: 1
    },
    {
      id: 'Q2',
      type: 'choice',
      difficulty: 1,
      text: 'Сколько будет 7 × 8?',
      options: ['54', '56', '58', '64'],
      correct: 1
    },
    {
      id: 'Q3',
      type: 'open',
      difficulty: 1,
      text: 'Напишите два-три предложения о том, как прошло ваше сегодняшнее утро.',
      placeholder: 'Ответ в свободной форме: 2–3 предложения…'
    },
    {
      id: 'Q4',
      type: 'choice',
      difficulty: 1,
      text: 'Какая планета ближе всего к Солнцу?',
      options: ['Венера', 'Марс', 'Меркурий', 'Земля'],
      correct: 2
    },
    {
      id: 'Q5',
      type: 'choice',
      difficulty: 1,
      text: 'В каком городе проходит Qostanai Industry Hackathon?',
      options: ['Астана', 'Костанай', 'Караганда', 'Павлодар'],
      correct: 1
    },
    {
      id: 'Q6',
      type: 'open',
      difficulty: 2,
      text: 'Зачем, по-вашему, на экзамене нужен прокторинг и чем он может мешать честному студенту? Два-три предложения.',
      placeholder: 'Ответ в свободной форме: 2–3 предложения…'
    }
  ];

  var TYPE_TAG = { choice: 'выбор варианта', open: 'развёрнутый ответ' };
  var DIFF_TAG = { 1: 'базовый', 2: 'средний', 3: 'сложный' };

  /** Состояния вопроса в навигации. Подпись обязательна: цвет не носитель смысла. */
  var STATE_TEXT = {
    answered: 'отвечен',
    skipped: 'пропущен',
    untouched: 'не открыт'
  };

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

  /** Склонение: 1 символ / 2 символа / 5 символов. */
  function plural(n, one, few, many) {
    var a = Math.abs(n) % 100, b = a % 10;
    if (a > 10 && a < 20) return many;
    if (b > 1 && b < 5) return few;
    if (b === 1) return one;
    return many;
  }

  /**
   * Инлайновый SVG-маркер. Цвет берётся из currentColor, толщина штриха задана
   * атрибутом: иконка остаётся видимой, даже если тему рисует другой лист стилей,
   * а при наличии правил в styles.css они перебивают презентационные атрибуты.
   */
  function glyph(kind) {
    var open = '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false" fill="none" ' +
               'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">';
    if (kind === 'answered') return open + '<path d="M3.2 8.6 L6.3 11.6 L12.8 4.6"></path></svg>';
    if (kind === 'skipped') return open + '<path d="M3.5 8 H12.5"></path></svg>';
    if (kind === 'current') return open + '<path d="M5.5 3.5 L11 8 L5.5 12.5"></path></svg>';
    if (kind === 'untouched') return open + '<circle cx="8" cy="8" r="3.2"></circle></svg>';
    return open + '</svg>';
  }

  /**
   * Запасные правила вёрстки. Селекторы завёрнуты в :where() — нулевая
   * специфичность, поэтому любое правило из styles.css их перебивает. Нужны
   * ровно для того, чтобы требования читаемости (ширина строки, кегль, зона
   * нажатия 44x44) выполнялись и до того, как лист стилей доедет до новых
   * классов. Все значения — переменные tokens.css; второй аргумент var() —
   * страховка на случай, если tokens.css ещё не подключён в index.html.
   */
  var FALLBACK_CSS = [
    /* светлый лист чтения и письма */
    ':where(.qsheet){display:block;width:100%;max-width:var(--layout-max,1440px);margin:0 auto;padding:var(--space-8,32px) var(--space-6,24px) var(--space-12,48px);}',
    ':where(.qcard){display:flex;flex-direction:column;gap:var(--space-6,24px);}',
    ':where(.qcard__top){display:flex;flex-wrap:wrap;gap:var(--space-2,8px);align-items:center;}',
    /*
     * Текст вопроса — РЕЖИМ ЧТЕНИЯ, а не заголовок: формулировки здесь на
     * 200–300 знаков, их читают, а не просматривают. Поэтому ровно три
     * свойства задаются сильнее запасных правил (селектор .qcard .qcard__text,
     * специфичность 0-2-0): строка 60–75 знаков, кегль не мельче 16px,
     * межстрочное расстояние абзаца. Всё остальное оформление — за styles.css.
     * Измерение (проверено в окне 1440px): полоса .qcard равна 80ch от 16px,
     * то есть 851px. Чтобы строка попала в 60–75 знаков, кегль должен лежать
     * между 17 и 21px — берём 20px как производное от токена --fs-h3 через
     * calc, литералов здесь нет. Выходит около 64 знаков в строке.
     */
    '.qcard .qcard__text{max-width:68ch;font-size:calc(var(--fs-h3,22px) - var(--space-1,4px) / 2);line-height:var(--lh-body,1.55);letter-spacing:normal;}',
    '.opts .opt__label{max-width:68ch;}',
    ':where(.qcard__text,.opts,.qcard__top,.qlegend){-webkit-user-select:none;user-select:none;}',
    ':where(.opt__label){max-width:64ch;font-size:var(--fs-body,16px);line-height:var(--lh-body,1.55);}',
    ':where(.opt){min-height:var(--tap-min,44px);}',
    /* поле развёрнутого ответа: крупное, с видимым счётчиком */
    ':where(.answer__wrap){display:flex;flex-direction:column;gap:var(--space-2,8px);max-width:76ch;}',
    ':where(.answer){display:block;width:100%;min-height:15em;resize:vertical;font-size:var(--fs-body,16px);line-height:var(--lh-body,1.55);-webkit-user-select:text;user-select:text;}',
    ':where(.answer__bar){display:flex;flex-wrap:wrap;gap:var(--space-4,16px);justify-content:space-between;align-items:baseline;font-size:var(--fs-small,14px);}',
    ':where(.answer__count){font-family:var(--font-mono,monospace);font-variant-numeric:tabular-nums;font-size:var(--fs-small,14px);}',
    ':where(.answer__label){font-size:var(--fs-small,14px);}',
    /* навигация по вопросам: зона нажатия и маркер состояния */
    ':where(.qbtn){position:relative;min-width:var(--tap-min,44px);min-height:var(--tap-min,44px);display:inline-flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;font-variant-numeric:tabular-nums;}',
    ':where(.qbtn__num){font-size:var(--fs-small,14px);line-height:1;}',
    ':where(.qbtn__mark){display:grid;place-items:center;height:var(--space-4,16px);}',
    ':where(.qbtn__mark) svg{display:block;width:var(--space-4,16px);height:var(--space-4,16px);}',
    ':where(.qlegend){display:flex;flex-wrap:wrap;gap:var(--space-4,16px);align-items:center;font-size:var(--fs-small,14px);}',
    ':where(.qlegend__item){display:inline-flex;align-items:center;gap:var(--space-1,4px);}',
    ':where(.qlegend__item) svg{display:block;width:var(--space-4,16px);height:var(--space-4,16px);}',
    ':where(.opt__mark){display:grid;place-items:center;}',
    ':where(.opt__mark) svg{display:block;width:var(--space-4,16px);height:var(--space-4,16px);}',
    ':where(.exam__timerval){font-family:var(--font-mono,monospace);font-variant-numeric:tabular-nums;}'
  ].join('\n');

  function installFallbackStyles() {
    if (document.getElementById('ds-fallback-exam')) return;
    try {
      var style = document.createElement('style');
      style.id = 'ds-fallback-exam';
      style.textContent = FALLBACK_CSS;
      document.head.appendChild(style);
    } catch (e) { /* без запасных правил экран всё равно работает */ }
  }

  function Exam() {
    this.dom = {};
    this.index = 0;
    this.answers = {};       // id -> {value:string} | {choice:number}
    this.visited = {};       // id -> true, вопрос был открыт (отличает «пропущен» от «не открыт»)
    this.submitted = {};     // id -> true, чтобы не дублировать answer_submit
    this.remaining = TOTAL_SECONDS;
    this.paused = true;
    this.running = false;
    this.startedAt = 0;
    this.pausedMs = 0;
    this._pauseStartedAt = 0;
    this._timer = null;
    this._paletteBuilt = false;
    this.telemetry = null;
    this.onFinish = null;
    this.onQuestionShown = null;
    // Тест идёт на СТОРОННЕЙ странице (LMS из профиля экзамена), а не здесь.
    this.external = false;
    this.externalHost = '';
    this.onIdleFinish = null;
  }

  Exam.prototype.init = function (opts) {
    opts = opts || {};
    var self = this;
    this.telemetry = opts.telemetry || (window.Proctor && window.Proctor.telemetry) || null;
    this.onFinish = opts.onFinish || null;
    this.onQuestionShown = opts.onQuestionShown || null;
    this.onIdleFinish = opts.onIdleFinish || null;

    installFallbackStyles();

    this.dom = {
      screen: el('screen-exam'),
      name: el('exam-name'),
      pos: el('exam-pos'),
      palette: el('exam-palette'),
      timer: el('exam-timer'),
      timerVal: el('exam-timerval'),
      timerLabel: null,
      body: el('exam-body'),
      prev: el('btn-prev'),
      next: el('btn-next'),
      finish: el('btn-finish'),
      saveState: el('exam-savestate'),
      legend: null
    };
    if (this.dom.timer) this.dom.timerLabel = this.dom.timer.querySelector('.exam__timerlabel');

    // таймкод — моноширинный (правило 5 дизайн-системы)
    if (this.dom.timerVal) {
      this.dom.timerVal.classList.add('mono');
      this.dom.timerVal.setAttribute('aria-live', 'off');
    }
    // лист теста — светлая среда чтения и письма. Класс поверхности стоит на
    // самом экране в разметке; дописываем его только если там его нет.
    if (this.dom.screen && !this.dom.screen.classList.contains('sheet') &&
        !this.dom.screen.classList.contains('monitor')) {
      this.dom.screen.classList.add('sheet');
    }

    this.dom.legend = this._ensureLegend();
    this._buildPalette();
    this._installProtection();

    if (this.dom.prev) this.dom.prev.addEventListener('click', function () { self.go(self.index - 1); });
    if (this.dom.next) this.dom.next.addEventListener('click', function () { self.go(self.index + 1); });
    if (this.dom.finish) this.dom.finish.addEventListener('click', function () { self.finish('student'); });
  };

  Exam.prototype.questionCount = function () { return QUESTIONS.length; };

  /**
   * Запуск экзамена на СТОРОННЕЙ странице: тест открыт в BrowserView (LMS из
   * профиля), локального листа вопросов нет.
   *
   * ПОЧЕМУ ОТДЕЛЬНЫЙ РЕЖИМ, А НЕ «просто не смотреть на мок-тест». Нативный
   * слой страницы LMS лежит ПОВЕРХ нашей вёрстки и накрывает #screen-exam
   * целиком: палитра вопросов, таймер, «Назад/Далее/Завершить» оказываются под
   * чужой страницей — замер живого прохода 08.10 дал долю под слоем 1.00 для
   * каждой из этих кнопок. То есть локальный тест в этом режиме не просто
   * лишний: он неработающий, а его таймер на 20 минут и счёт «отвечено 0 из 6»
   * попадали в отчёт, где означали бы, что студент ничего не ответил.
   *
   * Поэтому: вопросов нет, таймера нет, экран оболочки под страницей пуст, а
   * единственное действие — «Завершить» — живёт в HUD, вне прямоугольника
   * представления (его ставит app.js).
   */
  Exam.prototype.startExternal = function (host) {
    this.external = true;
    this.externalHost = String(host || '');
    this.index = 0;
    this.answers = {};
    this.visited = {};
    this.submitted = {};
    this.remaining = 0;
    this.paused = false;
    this.running = true;
    this.startedAt = Date.now();
    this.pausedMs = 0;
    if (this._timer) { clearInterval(this._timer); this._timer = null; }
    if (this.dom.screen) this.dom.screen.classList.add('exam--external');
    this._renderExternalPlaceholder();
  };

  /**
   * Что стоит на экране оболочки под страницей LMS. Человек этого не увидит,
   * пока представление прикреплено, — но увидит в ту секунду, когда оно
   * снимается (пауза, потеря фокуса, конец экзамена), и пустой белый лист там
   * читался бы как сломанный экран.
   */
  Exam.prototype._renderExternalPlaceholder = function () {
    if (this.dom.name) this.dom.name.textContent = 'Тест открыт на странице учебной системы';
    if (this.dom.pos) this.dom.pos.textContent = this.externalHost || 'страница экзамена';
    if (this.dom.palette) this.dom.palette.innerHTML = '';
    if (this.dom.legend) this.dom.legend.innerHTML = '';
    if (this.dom.saveState) this.dom.saveState.textContent = '';
    if (this.dom.body) {
      this.dom.body.innerHTML =
        '<div class="exam__external">' +
          '<p>Тест идёт на странице <b>' + esc(this.externalHost || 'учебной системы') +
          '</b>. Она открыта поверх этого экрана.</p>' +
          '<p>Ответы остаются в учебной системе: эта программа их не видит и ' +
          'не сохраняет. Завершить наблюдение можно кнопкой «Завершить тест» ' +
          'на панели наблюдения справа.</p>' +
        '</div>';
    }
  };

  /** Запуск экзамена: отрисовать первый вопрос и пустить таймер. */
  Exam.prototype.start = function (examTitle) {
    this.external = false;
    this.externalHost = '';
    if (this.dom.screen) this.dom.screen.classList.remove('exam--external');
    this.index = 0;
    this.answers = {};
    this.visited = {};
    this.submitted = {};
    this.remaining = TOTAL_SECONDS;
    this.paused = false;
    this.running = true;
    this.startedAt = Date.now();
    this.pausedMs = 0;
    if (this.dom.name && examTitle) this.dom.name.textContent = examTitle;
    this._buildPalette();
    this._render();
    this._startTimer();
  };

  // ------------------------------------------------------------------ таймер

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

  /** Остаток времени: моноширинный таймкод + словесное дублирование состояния. */
  Exam.prototype._renderTimer = function () {
    var low = this.remaining <= LOW_SECONDS && this.remaining > CRIT_SECONDS;
    var crit = this.remaining <= CRIT_SECONDS;

    if (this.dom.timerVal) this.dom.timerVal.textContent = mmss(this.remaining);
    if (this.dom.timer) {
      this.dom.timer.classList.toggle('is-low', low);
      this.dom.timer.classList.toggle('is-crit', crit);
      this.dom.timer.setAttribute('data-state', crit ? 'crit' : (low ? 'low' : 'normal'));
    }
    // цвет не единственный носитель смысла: подпись меняется вместе с ним
    if (this.dom.timerLabel) {
      var label = 'осталось';
      if (crit) label = 'осталось · меньше минуты';
      else if (low) label = 'осталось · мало времени';
      if (this.dom.timerLabel.textContent !== label) this.dom.timerLabel.textContent = label;
    }
  };

  /**
   * Пауза по вердикту прокторинга: время не идёт И ВВОД НЕДОСТУПЕН.
   *
   * Раньше здесь стоял только флаг, и «оверлей сверху» считался достаточной
   * блокировкой. Он ею не был: оверлей перехватывает МЫШЬ (position:fixed с
   * полупрозрачным фоном), но не клавиатуру. Поле ответа не получало ни
   * `disabled`, ни `readonly`, ловушки фокуса не было, `go()` не проверял
   * `paused`. В сумме это означало, что часы экзамена остановлены, а печатать
   * можно — то есть пауза была для списывающего СТРОГО выгоднее, чем её
   * отсутствие. Поэтому пауза теперь действительно приостанавливает ввод.
   */
  Exam.prototype.pause = function () {
    if (this.paused || !this.running) return;
    this.paused = true;
    this._pauseStartedAt = Date.now();
    this._setSaveState('тест приостановлен, время не идёт, ввод недоступен');
    this._applyPausedInput();
  };

  Exam.prototype.resume = function () {
    if (!this.paused || !this.running) return;
    this.paused = false;
    if (this._pauseStartedAt) this.pausedMs += Date.now() - this._pauseStartedAt;
    this._pauseStartedAt = 0;
    this._setSaveState('ответы сохраняются локально');
    this._applyPausedInput();
    var ta = this.dom.body ? this.dom.body.querySelector('textarea') : null;
    if (ta) { try { ta.focus(); } catch (e) {} }
  };

  /**
   * Привести доступность ввода в соответствие с `paused`.
   *
   * Зовётся и из pause/resume, и из `_render()`: вопрос может перерисоваться
   * во время паузы (например, при возврате оболочки на тот же экран), и
   * свежесозданный textarea иначе пришёл бы разблокированным.
   *
   * Три уровня, потому что ни одного по отдельности не хватает:
   * * `disabled` на полях и кнопках — снимает и ввод, и табуляцию в них;
   * * `inert` на содержимом экзамена — выключает его целиком для фокуса и
   *   указателя там, где он поддерживается (Chromium в Electron — да);
   * * `aria-hidden` — чтобы скринридер не читал приостановленный тест как
   *   доступный. Доступность паузы обеспечивает оверлей, он вне этого узла.
   */
  Exam.prototype._applyPausedInput = function () {
    var paused = !!this.paused;
    var body = this.dom.body;
    if (body) {
      var fields = body.querySelectorAll('textarea, input, button, select');
      for (var i = 0; i < fields.length; i += 1) {
        fields[i].disabled = paused;
      }
      if (paused) {
        body.setAttribute('inert', '');
        body.setAttribute('aria-hidden', 'true');
      } else {
        body.removeAttribute('inert');
        body.removeAttribute('aria-hidden');
      }
    }
    // Навигация по билету — тоже ввод: на паузе вопрос не меняется.
    if (this.dom.prev) this.dom.prev.disabled = paused || this.index === 0;
    if (this.dom.next) this.dom.next.disabled = paused;
    if (this.dom.palette) {
      if (paused) {
        this.dom.palette.setAttribute('inert', '');
      } else {
        this.dom.palette.removeAttribute('inert');
      }
    }
  };

  Exam.prototype.isPaused = function () { return this.paused; };

  Exam.prototype._setSaveState = function (text) {
    if (this.dom.saveState) this.dom.saveState.textContent = text;
  };

  // ------------------------------------------------------------------ навигация

  Exam.prototype.go = function (nextIndex) {
    if (!this.running) return;
    // Локальных вопросов в стороннем тесте нет — навигации тоже.
    if (this.external) return;
    // На паузе вопрос не меняется. Без этой проверки навигация работала прямо
    // поверх оверлея: `_leaveCurrent()` отправлял answer_submit, а счётчик
    // времени стоял — то есть билет можно было пройти целиком на остановленных
    // часах. Решение о продолжении принимает оболочка (а при review_required —
    // проктор), и до него экзамен не двигается.
    if (this.paused) return;
    if (nextIndex < 0) return;
    if (nextIndex >= QUESTIONS.length) { this.finish('student'); return; }
    this._leaveCurrent();
    this.index = nextIndex;
    this._render();
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

  /**
   * answer_submit в телеметрию. Передаётся ТОЛЬКО длина ответа в символах —
   * сам текст не покидает renderer (Р-08).
   */
  Exam.prototype._submitAnswer = function (q) {
    if (!this.telemetry) return;
    var a = this.answers[q.id];
    var len = 0;
    if (a && typeof a.value === 'string') len = a.value.length;
    this.telemetry.answerSubmit(q.id, len);
    this.submitted[q.id] = true;
  };

  /** Есть ли содержательный ответ на вопрос. */
  Exam.prototype._isAnswered = function (q) {
    var a = this.answers[q.id];
    if (!a) return false;
    if (typeof a.choice === 'number') return true;
    return typeof a.value === 'string' && a.value.trim().length > 0;
  };

  /** Состояние вопроса для навигации: answered | skipped | untouched. */
  Exam.prototype._stateOf = function (i) {
    var q = QUESTIONS[i];
    if (this._isAnswered(q)) return 'answered';
    return this.visited[q.id] ? 'skipped' : 'untouched';
  };

  Exam.prototype.answeredCount = function () {
    var n = 0;
    for (var i = 0; i < QUESTIONS.length; i++) if (this._isAnswered(QUESTIONS[i])) n++;
    return n;
  };

  // ------------------------------------------------------------------ палитра вопросов

  /**
   * Кнопки навигации создаются один раз: дальше меняется только состояние.
   * Так не теряется фокус и не пересоздаются слушатели на каждом нажатии клавиши.
   */
  Exam.prototype._buildPalette = function () {
    var box = this.dom.palette;
    if (!box) return;
    var self = this;
    var html = '';
    for (var i = 0; i < QUESTIONS.length; i++) {
      var q = QUESTIONS[i];
      html += '<button type="button" class="qbtn" data-idx="' + i + '" data-state="untouched"' +
              (q.type === 'open' ? ' data-kind="open"' : ' data-kind="choice"') + '>' +
                '<span class="qbtn__num" aria-hidden="true">' + (i + 1) + '</span>' +
                '<span class="qbtn__mark" aria-hidden="true">' + glyph('untouched') + '</span>' +
                '<span class="qbtn__kind" aria-hidden="true"></span>' +
              '</button>';
    }
    box.innerHTML = html;
    box.setAttribute('role', 'list');
    box.setAttribute('aria-label', 'Навигация по вопросам');

    var btns = box.querySelectorAll('.qbtn');
    for (var j = 0; j < btns.length; j++) {
      btns[j].addEventListener('click', function (ev) {
        var node = ev.currentTarget;
        var idx = parseInt(node.getAttribute('data-idx'), 10);
        if (!isNaN(idx) && idx !== self.index) self.go(idx);
      });
    }
    this._paletteBuilt = true;
    this._syncPalette();
  };

  /** Обновление состояний: класс + форма + подпись в aria-label. */
  Exam.prototype._syncPalette = function () {
    var box = this.dom.palette;
    if (!box) return;
    var btns = box.querySelectorAll('.qbtn');
    for (var i = 0; i < btns.length && i < QUESTIONS.length; i++) {
      var btn = btns[i];
      var q = QUESTIONS[i];
      var state = this._stateOf(i);
      var current = i === this.index;

      btn.setAttribute('data-state', state);
      btn.classList.toggle('is-answered', state === 'answered');
      btn.classList.toggle('is-skipped', state === 'skipped');
      btn.classList.toggle('is-untouched', state === 'untouched');
      btn.classList.toggle('is-current', current);
      btn.classList.toggle('is-open', q.type === 'open');
      if (current) btn.setAttribute('aria-current', 'true');
      else btn.removeAttribute('aria-current');

      // форма маркера: текущий вопрос помечен шевроном, остальные — статусом ответа
      var mark = btn.querySelector('.qbtn__mark');
      var wanted = current ? 'current' : state;
      if (mark && mark.getAttribute('data-glyph') !== wanted) {
        mark.setAttribute('data-glyph', wanted);
        mark.innerHTML = glyph(wanted);
      }

      var label = 'Вопрос ' + (i + 1) + ' из ' + QUESTIONS.length + ', ' +
                  TYPE_TAG[q.type] + ', ' + STATE_TEXT[state] + (current ? ', текущий' : '');
      btn.setAttribute('aria-label', label);
      btn.setAttribute('title', label);
    }
    this._renderLegend();
  };

  /** Легенда состояний рядом с навигацией: смысл не в цвете, а в форме и подписи. */
  Exam.prototype._ensureLegend = function () {
    var node = el('exam-legend');
    if (node) return node;
    var box = this.dom.palette;
    if (!box || !box.parentNode) return null;
    try {
      node = document.createElement('div');
      node.id = 'exam-legend';
      node.className = 'qlegend';
      if (box.nextSibling) box.parentNode.insertBefore(node, box.nextSibling);
      else box.parentNode.appendChild(node);
      return node;
    } catch (e) { return null; }
  };

  Exam.prototype._renderLegend = function () {
    var node = this.dom.legend;
    if (!node) return;
    var answered = this.answeredCount();
    var items = [
      { k: 'answered', t: 'отвечен' },
      { k: 'skipped', t: 'пропущен' },
      { k: 'current', t: 'текущий' }
    ];
    var html = '';
    for (var i = 0; i < items.length; i++) {
      html += '<span class="qlegend__item" data-state="' + items[i].k + '">' +
                glyph(items[i].k) + '<span>' + items[i].t + '</span>' +
              '</span>';
    }
    html += '<span class="qlegend__item qlegend__item--count mono">' +
            answered + '/' + QUESTIONS.length + ' отвечено</span>';
    node.innerHTML = html;
  };

  // ------------------------------------------------------------------ отрисовка вопроса

  Exam.prototype._render = function () {
    var q = QUESTIONS[this.index];
    if (!q || !this.dom.body) return;
    this.visited[q.id] = true;

    /*
     * Слушатели телеметрии прошлого вопроса снимаются В САМОМ НАЧАЛЕ отрисовки —
     * до того, как ниже будет создано и привязано новое поле ответа.
     * Telemetry.attach() пушит запись в свой _bound на каждый вызов и держит
     * замыкание на textarea, а защита el.__telemetryBound спасает только от
     * повторного attach к ТОМУ ЖЕ элементу — здесь поле каждый раз новое.
     * Без этой строки каждый заход на открытый вопрос добавлял пять живых
     * слушателей и удерживал отсоединённый узел вместе с текстом ответа,
     * а detachAll() звался один раз, на завершении экзамена.
     * Порядок критичен: ниже по функции _renderOpen() уже вызывает attach()
     * для нового поля, и detachAll() после него снял бы ровно то, что только
     * что привязали. Статистика по вопросам (_stats) при detachAll не теряется —
     * снимаются только слушатели.
     */
    if (this.telemetry && typeof this.telemetry.detachAll === 'function') {
      this.telemetry.detachAll();
    }

    if (this.dom.pos) {
      this.dom.pos.textContent = 'вопрос ' + (this.index + 1) + ' из ' + QUESTIONS.length;
    }
    if (this.dom.prev) this.dom.prev.disabled = this.index === 0;
    if (this.dom.next) this.dom.next.textContent = this.index === QUESTIONS.length - 1 ? 'К завершению' : 'Далее';
    this._setSaveState(this.paused ? 'тест приостановлен, время не идёт, ввод недоступен'
                                   : 'ответы сохраняются локально');

    // .qsheet — отступы и центровка листа (styles.css ждёт этот узел от exam.js)
    var sheet = document.createElement('div');
    sheet.className = 'qsheet';

    var card = document.createElement('article');
    card.className = 'qcard';
    card.setAttribute('data-type', q.type);
    sheet.appendChild(card);

    var textId = 'qtext-' + q.id;
    card.innerHTML =
      '<div class="qcard__top">' +
        '<span class="tag' + (q.type === 'open' ? ' tag--open' : '') + '">' + esc(TYPE_TAG[q.type]) + '</span>' +
        '<span class="tag tag--diff">' + esc(DIFF_TAG[q.difficulty] || 'средний') + '</span>' +
        '<span class="tag mono">' + esc(q.id) + '</span>' +
      '</div>' +
      '<div class="qcard__text" id="' + textId + '" role="heading" aria-level="2">' + esc(q.text) + '</div>';

    if (q.type === 'choice') this._renderChoice(card, q, textId);
    else this._renderOpen(card, q, textId);

    this.dom.body.innerHTML = '';
    this.dom.body.appendChild(sheet);

    this._protect(card);
    this._syncPalette();
    // Разметка вопроса только что создана заново. Если идёт пауза, её поля
    // обязаны приехать уже заблокированными: иначе любая перерисовка во время
    // приостановки возвращала бы студенту рабочее поле ответа.
    this._applyPausedInput();

    // question_shown уходит ПОСЛЕ отрисовки — время до ответа считается от показа
    if (this.telemetry) this.telemetry.questionShown(q.id, q.difficulty);
    if (this.onQuestionShown) {
      try { this.onQuestionShown(q, this.index); } catch (e) {}
    }
  };

  /** Вопрос с выбором варианта. Выбор помечен и формой (галочка), и состоянием radio. */
  Exam.prototype._renderChoice = function (card, q, textId) {
    var self = this;
    var saved = this.answers[q.id];
    var opts = document.createElement('div');
    opts.className = 'opts';
    opts.setAttribute('role', 'radiogroup');
    opts.setAttribute('aria-labelledby', textId);

    var html = '';
    for (var i = 0; i < q.options.length; i++) {
      var checked = !!(saved && saved.choice === i);
      html += '<label class="opt' + (checked ? ' is-checked' : '') + '" data-opt="' + i + '">' +
                '<input type="radio" name="' + esc(q.id) + '" value="' + i + '"' + (checked ? ' checked' : '') + ' />' +
                '<span class="opt__radio" aria-hidden="true"></span>' +
                '<span class="opt__label">' + esc(q.options[i]) + '</span>' +
                '<span class="opt__mark" aria-hidden="true">' + (checked ? glyph('answered') : '') + '</span>' +
              '</label>';
    }
    opts.innerHTML = html;
    card.appendChild(opts);

    var foot = document.createElement('div');
    foot.className = 'qcard__foot';
    foot.innerHTML = '<span class="muted">Вариант можно изменить до перехода к следующему вопросу.</span>';
    card.appendChild(foot);

    var labels = opts.querySelectorAll('.opt');
    for (var k = 0; k < labels.length; k++) {
      labels[k].addEventListener('change', function (ev) {
        var idx = parseInt(ev.currentTarget.getAttribute('data-opt'), 10);
        if (isNaN(idx)) return;
        self.answers[q.id] = { choice: idx };
        var all = opts.querySelectorAll('.opt');
        for (var m = 0; m < all.length; m++) {
          var on = m === idx;
          all[m].classList.toggle('is-checked', on);
          var mk = all[m].querySelector('.opt__mark');
          if (mk) mk.innerHTML = on ? glyph('answered') : '';
        }
        self._syncPalette();
      });
    }
  };

  /** Развёрнутый ответ: крупное поле, видимый счётчик, напоминание о приватности. */
  Exam.prototype._renderOpen = function (card, q, textId) {
    var countId = 'answer-count-' + q.id;
    var privacyId = 'answer-privacy-' + q.id;
    var fieldId = 'answer-' + q.id;

    var wrap = document.createElement('div');
    wrap.className = 'answer__wrap';
    wrap.innerHTML =
      '<label class="answer__label" for="' + fieldId + '">Ваш ответ</label>' +
      '<textarea class="answer" id="' + fieldId + '" rows="12" spellcheck="false" ' +
        'aria-describedby="' + countId + ' ' + privacyId + '" ' +
        'placeholder="' + esc(q.placeholder || 'Ваш ответ…') + '"></textarea>' +
      '<div class="answer__bar">' +
        '<span class="answer__count mono" id="' + countId + '" aria-live="polite">0 символов · 0 слов</span>' +
        '<span class="answer__saved muted">черновик сохраняется локально</span>' +
      '</div>';
    card.appendChild(wrap);

    var foot = document.createElement('div');
    foot.className = 'qcard__foot';
    foot.innerHTML =
      '<span class="qcard__privacy" id="' + privacyId + '">' +
        '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" ' +
          'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
          '<rect x="4" y="10" width="16" height="10" rx="2.5"></rect>' +
          '<path d="M8 10V7.5a4 4 0 0 1 8 0V10"></path>' +
        '</svg>' +
        'Фиксируются интервалы между нажатиями, не символы. Текст ответа остаётся на этом компьютере.' +
      '</span>';
    card.appendChild(foot);

    var ta = wrap.querySelector('textarea');
    var count = wrap.querySelector('#' + countId);
    var self = this;

    // значение подставляется ДО привязки телеметрии, иначе восстановление
    // ответа выглядело бы как программная вставка
    var savedOpen = this.answers[q.id];
    if (savedOpen && typeof savedOpen.value === 'string') ta.value = savedOpen.value;

    function renderCount() {
      if (!count) return;
      var v = ta.value;
      var chars = v.length;
      var trimmed = v.trim();
      var words = trimmed ? trimmed.split(/\s+/).length : 0;
      count.textContent = chars + ' ' + plural(chars, 'символ', 'символа', 'символов') + ' · ' +
                          words + ' ' + plural(words, 'слово', 'слова', 'слов');
    }

    ta.addEventListener('input', function () {
      self.answers[q.id] = { value: ta.value };
      renderCount();
      self._syncPalette();
    });
    renderCount();

    // поле ответа остаётся полностью рабочим: выделение и правка разрешены явно
    try {
      ta.style.userSelect = 'text';
      ta.style.webkitUserSelect = 'text';
    } catch (e) {}

    if (this.telemetry) {
      this.telemetry.attach(ta, function () { return q.id; });
    }
    try { ta.focus(); } catch (e) {}
  };

  // ------------------------------------------------------------------ защита текста

  /**
   * Контекстное меню на экране экзамена заблокировано целиком: через него
   * вопрос уходит в буфер в один клик. Ставится один раз, в capture-фазе,
   * чтобы сработать раньше любых обработчиков внутри экрана.
   */
  Exam.prototype._installProtection = function () {
    var self = this;
    function blockMenu(ev) { ev.preventDefault(); }
    if (this.dom.screen) this.dom.screen.addEventListener('contextmenu', blockMenu, true);
    // оверлеи паузы и блокировки лежат вне #screen-exam — закрываем и их,
    // но только пока тест идёт: на других экранах меню не трогаем
    document.addEventListener('contextmenu', function (ev) {
      if (self.running) ev.preventDefault();
    }, true);
  };

  /**
   * Текст вопроса и варианты не выделяются и не копируются; поле ответа
   * исключено из запрета — студент обязан иметь возможность править свой текст.
   */
  Exam.prototype._protect = function (card) {
    function inAnswer(node) {
      if (!node) return false;
      if (node.closest) return !!node.closest('.answer, textarea, input');
      // текстовый узел: смотрим на родителя
      var p = node.parentNode;
      return !!(p && p.closest && p.closest('.answer, textarea, input'));
    }
    function guard(ev) {
      if (!inAnswer(ev.target)) ev.preventDefault();
    }
    card.addEventListener('selectstart', guard);
    card.addEventListener('copy', guard);
    card.addEventListener('cut', guard);
    card.addEventListener('dragstart', guard);
    // страховка на случай, если лист стилей не задал user-select для текста вопроса
    try {
      var nodes = card.querySelectorAll('.qcard__text, .opts, .qcard__top, .qcard__foot');
      for (var i = 0; i < nodes.length; i++) {
        nodes[i].style.userSelect = 'none';
        nodes[i].style.webkitUserSelect = 'none';
      }
    } catch (e) {}
  };

  // ------------------------------------------------------------------ завершение

  /** Завершение: 'student' | 'timeout' | 'lock'. */
  Exam.prototype.finish = function (reason) {
    /*
     * Тест не запущен. Раньше здесь стоял молчаливый return, и это давало
     * кнопку «Завершить», которая не делает НИЧЕГО: ни перехода, ни сообщения,
     * ни записи в журнал. Попасть в это состояние просто — экран экзамена
     * показан в обход _beginSession() (на живом проходе 08.10 так вышло дважды).
     * Молчащая кнопка — это та же жалоба «нажимаю, ничего не происходит»,
     * поэтому о пустом нажатии теперь сообщаем зовущему.
     */
    if (!this.running) {
      if (this.onIdleFinish) { try { this.onIdleFinish(reason || 'student'); } catch (e) {} }
      return null;
    }
    if (!this.external) {
      var q = QUESTIONS[this.index];
      if (q) { this._captureAnswer(q); this._submitAnswer(q); }

      // добираем всё, что было отвечено, но не зафиксировано (на всякий случай)
      for (var i = 0; i < QUESTIONS.length; i++) {
        var qq = QUESTIONS[i];
        if (!this.submitted[qq.id] && this.answers[qq.id]) this._submitAnswer(qq);
      }
    }

    this.running = false;
    this.paused = true;
    if (this._timer) { clearInterval(this._timer); this._timer = null; }
    if (this.telemetry) this.telemetry.detachAll();

    var answered = this.external ? 0 : this.answeredCount();
    if (!this.external) this._syncPalette();

    var res = {
      reason: reason || 'student',
      // На стороннем тесте ответов у нас НЕТ, и «0 из 6» здесь было бы ложью:
      // ноль означал бы, что студент не ответил ни на что. Поэтому счёт
      // отсутствует (total 0), а отчёт говорит об этом словами.
      external: this.external === true,
      external_host: this.externalHost || '',
      answered: answered,
      total: this.external ? 0 : QUESTIONS.length,
      elapsed_ms: Date.now() - this.startedAt - this.pausedMs,
      paused_ms: this.pausedMs,
      remaining_s: this.external ? null : Math.round(this.remaining)
    };
    if (this.onFinish) { try { this.onFinish(res); } catch (e) {} }
    return res;
  };

  /**
   * Остановить тест БЕЗ отчёта и без onFinish.
   *
   * Нужен ровно для одного случая: оболочка отвергла переход в состояние
   * экзамена, то есть наблюдения нет и теста нет. Это не «завершённый тест»
   * (отчёт о нём означал бы, что он был), а несостоявшийся — поэтому отдельный
   * метод, а не finish().
   */
  Exam.prototype.abort = function () {
    this.running = false;
    this.paused = true;
    if (this._timer) { clearInterval(this._timer); this._timer = null; }
    if (this.telemetry) this.telemetry.detachAll();
    this.external = false;
    this.externalHost = '';
    if (this.dom.screen) this.dom.screen.classList.remove('exam--external');
  };

  Exam.prototype.questions = function () { return QUESTIONS; };

  window.Proctor = window.Proctor || {};
  window.Proctor.exam = new Exam();
})();
