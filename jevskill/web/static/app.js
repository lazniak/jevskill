/* jev · console — vanilla ES2020, no framework, no build step.
 *
 * Three rules this file keeps, because they are what the page is *for*:
 *
 *  1. Never invent an answer. With no key the Run button is disabled and the
 *     page says which variable to set; it does not draw a plausible bar.
 *  2. Never round a number up. Latency, tokens and cost are printed as the
 *     server measured them, with the cost source named, because this project's
 *     whole claim is that its figures are reproducible.
 *  3. Say what a number means. `noul` is drawn as a probability and labelled as
 *     one — a bar at 0.62 is not a "yes", and the label says so.
 */
'use strict';

const $ = (sel) => document.querySelector(sel);
const PRICE_PER_MTOK = 0.042;

/* Small DOM builder. Text always goes in through `textContent`, so an option
 * description or a question name can never become markup. */
function el(tag, attrs, children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, value);
  }
  for (const child of children || []) if (child) node.appendChild(child);
  return node;
}

function slug(text) {
  const base = String(text || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 40);
  if (!base) return 'q';
  return /^[0-9]/.test(base) ? 'q_' + base : base;
}

function money(value) {
  const n = Number(value || 0);
  return '$' + n.toFixed(8);
}

async function api(path, options) {
  const response = await fetch(path, options);
  let payload = null;
  try { payload = await response.json(); } catch (_) { payload = null; }
  if (!response.ok) {
    const error = new Error((payload && payload.error) || (response.status + ' ' + response.statusText));
    error.payload = payload || {};
    error.status = response.status;
    throw error;
  }
  return payload;
}

const postJSON = (path, body) => api(path, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

/* ------------------------------------------------------------------ state */

const state = {
  doctor: null,
  templates: {},
  cards: [],
  jsonMode: false,
  jsonValid: true,
  running: false,
  lastRequest: null,
  lastResponse: null,
  totals: { calls: 0, tokens: 0, cost: 0, latency: null },
  estimate: { tokens: 0, chars: 0, over: false },
  runCost: null,
};

let nextId = 1;

function newCard(type) {
  return {
    id: nextId++,
    type: type || 'noul',
    name: '',
    instructions: '',
    trueText: '',
    falseText: '',
    options: [
      { key: 'yes', desc: '' },
      { key: 'no', desc: '' },
    ],
    levels: ['Low', 'Medium', 'High'],
  };
}

function cardName(card, index) {
  return card.name.trim() || slug(card.instructions) || 'q' + index;
}

/* The bundle exactly as it will be sent. `validate_questions` on the server is
 * the authority; this only has to produce the same shape the CLI does. */
function bundleFromCards() {
  const out = {};
  state.cards.forEach((card, index) => {
    const question = { type: card.type, instructions: card.instructions.trim() };
    if (card.type === 'noul') {
      const criteria = {};
      if (card.trueText.trim()) criteria.true = card.trueText.trim();
      if (card.falseText.trim()) criteria.false = card.falseText.trim();
      if (Object.keys(criteria).length) question.criteria = criteria;
    } else if (card.type === 'choice') {
      const criteria = {};
      for (const opt of card.options) {
        const key = opt.key.trim();
        if (key) criteria[key] = opt.desc.trim() || key;
      }
      question.criteria = criteria;
    } else {
      question.criteria = card.levels.map((l) => l.trim()).filter(Boolean);
    }
    out[cardName(card, index)] = question;
  });
  return out;
}

function cardsFromBundle(bundle) {
  const cards = [];
  for (const [name, question] of Object.entries(bundle || {})) {
    const card = newCard(question.type);
    card.name = name;
    card.instructions = String(question.instructions || '');
    if (question.type === 'noul') {
      const criteria = question.criteria || {};
      card.trueText = criteria.true ? String(criteria.true) : '';
      card.falseText = criteria.false ? String(criteria.false) : '';
    } else if (question.type === 'choice') {
      card.options = Object.entries(question.criteria || {}).map(([key, desc]) => ({
        key: String(key), desc: desc === key ? '' : String(desc),
      }));
      if (!card.options.length) card.options = newCard('choice').options;
    } else if (question.type === 'score') {
      const levels = (question.criteria || []).map(String);
      card.levels = levels.length >= 2 ? levels : newCard('score').levels;
    }
    cards.push(card);
  }
  return cards;
}

function currentBundle() {
  if (!state.jsonMode) return bundleFromCards();
  try {
    const parsed = JSON.parse($('#bundle-json').value);
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch (_) {
    return {};
  }
}

/* ------------------------------------------------------------- rendering */

function renderCards() {
  const host = $('#cards');
  host.textContent = '';
  state.cards.forEach((card, index) => host.appendChild(renderCard(card, index)));
  if (!state.cards.length) {
    host.appendChild(el('p', {
      class: 'note',
      text: 'No questions yet. Use the quick gate above, + question, or a template.',
    }));
  }
}

function renderCard(card, index) {
  const typeSelect = el('select', {
    'aria-label': 'question type',
    onchange: (event) => { card.type = event.target.value; refresh(); },
  }, ['noul', 'choice', 'score'].map((kind) => el('option', {
    value: kind, text: kind, selected: kind === card.type,
  })));

  const nameInput = el('input', {
    class: 'name', type: 'text', value: card.name,
    placeholder: cardName(card, index),
    'aria-label': 'question name',
    oninput: (event) => { card.name = event.target.value; syncJson(); },
  });

  const head = el('div', { class: 'card-head' }, [
    typeSelect,
    nameInput,
    el('button', { type: 'button', class: 'tiny', text: 'duplicate', onclick: () => {
      const copy = JSON.parse(JSON.stringify(card));
      copy.id = nextId++;
      copy.name = cardName(card, index) + '_2';
      state.cards.splice(index + 1, 0, copy);
      refresh();
    } }),
    el('button', { type: 'button', class: 'tiny', text: 'delete', onclick: () => {
      state.cards.splice(index, 1);
      refresh();
    } }),
  ]);

  const body = el('div', { class: 'card-body' }, [
    el('textarea', {
      spellcheck: 'false',
      'aria-label': 'instructions',
      placeholder: 'Name the target value with backticks, e.g. `state.diff`. Say what counts, not just what to judge.',
      oninput: (event) => { card.instructions = event.target.value; syncJson(); scheduleEstimate(); },
    }),
  ]);
  body.firstChild.value = card.instructions;

  if (card.type === 'noul') {
    body.appendChild(fieldRow('true', card.trueText, 'What counts as true.', (value) => {
      card.trueText = value; syncJson();
    }));
    body.appendChild(fieldRow('false', card.falseText, 'What counts as false.', (value) => {
      card.falseText = value; syncJson();
    }));
    body.appendChild(el('p', {
      class: 'note',
      text: 'Returns P(true), a probability. Your code applies the threshold — the model never does.',
    }));
  } else if (card.type === 'choice') {
    const list = el('div', {});
    card.options.forEach((opt, optIndex) => list.appendChild(renderOption(card, opt, optIndex)));
    body.appendChild(list);
    body.appendChild(el('div', { class: 'btn-row', style: 'margin-top:8px' }, [
      el('button', { type: 'button', class: 'tiny', text: '+ option', onclick: () => {
        card.options.push({ key: '', desc: '' }); refresh();
      } }),
      el('button', { type: 'button', class: 'tiny', text: '+ unclear', onclick: () => {
        addEscape(card, 'unclear', 'Not enough information in the state to decide.');
      } }),
      el('button', { type: 'button', class: 'tiny', text: '+ none', onclick: () => {
        addEscape(card, 'none', 'None of the options above applies.');
      } }),
    ]));
    const hasEscape = card.options.some((o) => ['unclear', 'none', 'other'].includes(o.key.trim()));
    body.appendChild(el('p', {
      class: hasEscape ? 'note' : 'note is-warn',
      text: hasEscape
        ? 'Option keys are what your code branches on — name them for the code, not for a reader.'
        : 'No escape hatch. Without one, an item that fits nothing forces a wrong label and you cannot tell it happened.',
    }));
  } else {
    const list = el('div', {});
    card.levels.forEach((level, levelIndex) => {
      list.appendChild(el('div', { class: 'opt' }, [
        el('span', { class: 'grip mono', text: String(levelIndex), title: 'level index' }),
        el('input', {
          class: 'desc', type: 'text', value: level,
          'aria-label': 'level ' + levelIndex,
          oninput: (event) => { card.levels[levelIndex] = event.target.value; syncJson(); },
        }),
        el('button', {
          type: 'button', class: 'tiny', text: '×', 'aria-label': 'remove level',
          disabled: card.levels.length <= 2,
          onclick: () => { card.levels.splice(levelIndex, 1); refresh(); },
        }),
      ]));
    });
    body.appendChild(list);
    body.appendChild(el('div', { class: 'btn-row', style: 'margin-top:8px' }, [
      el('button', {
        type: 'button', class: 'tiny', text: '+ level',
        disabled: card.levels.length >= 10,
        onclick: () => { card.levels.push(''); refresh(); },
      }),
    ]));
    body.appendChild(el('p', {
      class: card.levels.length > 7 ? 'note is-warn' : 'note',
      text: card.levels.length > 7
        ? 'Over 7 levels: coarser axes are better calibrated, and jevskill.primitives.score() refuses more than 7.'
        : 'Lowest level first — the order is the scale. The answer is a weighted mean, so 1.4 is a real value.',
    }));
  }

  return el('div', { class: 'card' }, [head, body]);
}

function fieldRow(label, value, placeholder, onInput) {
  const input = el('input', {
    class: 'desc', type: 'text', value: value, placeholder: placeholder,
    'aria-label': label,
    oninput: (event) => onInput(event.target.value),
  });
  return el('div', { class: 'opt' }, [
    el('span', { class: 'key mono', text: label }),
    input,
  ]);
}

function addEscape(card, key, description) {
  if (card.options.some((o) => o.key.trim() === key)) return;
  const empty = card.options.findIndex((o) => !o.key.trim());
  const option = { key: key, desc: description };
  if (empty >= 0) card.options[empty] = option;
  else card.options.push(option);
  refresh();
}

/* Drag to reorder. `draggable` is switched on only while the grip is held, so
 * the text inputs inside the row stay selectable. */
let dragFrom = null;

function renderOption(card, opt, index) {
  const row = el('div', { class: 'opt' }, [
    el('span', {
      class: 'grip', text: '⠿', title: 'drag to reorder',
      onmousedown: () => { row.setAttribute('draggable', 'true'); },
    }),
    el('input', {
      class: 'key mono', type: 'text', value: opt.key, placeholder: 'key',
      'aria-label': 'option key',
      oninput: (event) => { opt.key = event.target.value; syncJson(); },
    }),
    el('input', {
      class: 'desc', type: 'text', value: opt.desc, placeholder: 'what this option means',
      'aria-label': 'option description',
      oninput: (event) => { opt.desc = event.target.value; syncJson(); },
    }),
    el('button', {
      type: 'button', class: 'tiny', text: '×', 'aria-label': 'remove option',
      disabled: card.options.length <= 2,
      onclick: () => { card.options.splice(index, 1); refresh(); },
    }),
  ]);
  row.addEventListener('dragstart', () => { dragFrom = { card: card, index: index }; row.classList.add('is-dragging'); });
  row.addEventListener('dragend', () => { row.removeAttribute('draggable'); row.classList.remove('is-dragging'); });
  row.addEventListener('dragover', (event) => {
    if (dragFrom && dragFrom.card === card) { event.preventDefault(); row.classList.add('is-target'); }
  });
  row.addEventListener('dragleave', () => row.classList.remove('is-target'));
  row.addEventListener('drop', (event) => {
    event.preventDefault();
    row.classList.remove('is-target');
    if (!dragFrom || dragFrom.card !== card) return;
    const [moved] = card.options.splice(dragFrom.index, 1);
    card.options.splice(index, 0, moved);
    dragFrom = null;
    refresh();
  });
  return row;
}

/* ------------------------------------------------------- json two-way sync */

function syncJson() {
  if (!state.jsonMode) $('#bundle-json').value = JSON.stringify(bundleFromCards(), null, 2);
  updateRunState();
  scheduleEstimate();
}

function onJsonInput() {
  const raw = $('#bundle-json').value;
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('the bundle must be an object of {name: question}');
    }
    state.jsonValid = true;
    $('#json-error').classList.add('hidden');
    state.cards = cardsFromBundle(parsed);
  } catch (err) {
    state.jsonValid = false;
    const box = $('#json-error');
    box.textContent = 'invalid JSON — ' + err.message;
    box.classList.remove('hidden');
  }
  updateRunState();
  scheduleEstimate();
}

function setJsonMode(on) {
  state.jsonMode = on;
  if (on) $('#bundle-json').value = JSON.stringify(bundleFromCards(), null, 2);
  else { state.jsonValid = true; $('#json-error').classList.add('hidden'); renderCards(); }
  $('#json-editor').classList.toggle('hidden', !on);
  $('#cards').classList.toggle('hidden', on);
  $('#toggle-json').setAttribute('aria-pressed', String(on));
  updateRunState();
}

function refresh() {
  renderCards();
  syncJson();
}

/* ------------------------------------------------------------- estimating */

let estimateTimer = null;

function scheduleEstimate() {
  clearTimeout(estimateTimer);
  estimateTimer = setTimeout(runEstimate, 220);
}

async function runEstimate() {
  const text = $('#state').value;
  try {
    const sized = await postJSON('/api/estimate', { state: text });
    state.estimate = { tokens: sized.tokens, chars: sized.chars, over: sized.over_budget };
    $('#estimate').textContent =
      sized.tokens.toLocaleString() + ' tokens · ' + sized.chars.toLocaleString() +
      ' chars · budget ' + sized.budget.toLocaleString();
    const share = Math.min(1, sized.tokens / sized.budget);
    const fill = $('#budget-fill');
    fill.style.width = (share * 100).toFixed(1) + '%';
    fill.classList.toggle('is-warn', share >= 0.7 && !sized.over_budget);
    fill.classList.toggle('is-over', sized.over_budget);
    $('#over-budget').classList.toggle('hidden', !sized.over_budget);

    /* The run cost is the whole payload, not just the state: question text is
     * real input and is billed. Sized by the same server-side constant so the
     * page cannot drift from the ledger's idea of how big a request is. */
    const whole = await postJSON('/api/estimate', {
      state: text + '\n' + JSON.stringify(currentBundle()),
    });
    state.runCost = whole;
    $('#run-cost').textContent =
      '~' + money(whole.tokens / 1e6 * PRICE_PER_MTOK) + '  ·  ' +
      whole.tokens.toLocaleString() + ' tok × $' + PRICE_PER_MTOK + '/Mtok';
  } catch (_) {
    $('#estimate').textContent = 'estimate unavailable';
  }
}

/* ------------------------------------------------------------------- run */

function questionCount() {
  return Object.keys(currentBundle()).length;
}

/* The first card the server would reject, named so the hint can say which one.
 * `validate_questions` on the server stays the authority; this only spares a
 * round trip for the gaps a person can see: an empty instruction, a choice with
 * one option, a score with one level. JSON mode is checked by the server. */
function firstIncomplete() {
  if (state.jsonMode) return null;
  for (let i = 0; i < state.cards.length; i += 1) {
    const card = state.cards[i];
    const label = 'question ' + (i + 1);
    if (!card.instructions.trim()) return label + ' needs instructions';
    if (card.type === 'choice' && card.options.filter((o) => o.key.trim()).length < 2) {
      return label + ' needs two options';
    }
    if (card.type === 'score' && card.levels.filter((l) => l.trim()).length < 2) {
      return label + ' needs two levels';
    }
  }
  return null;
}

function updateRunState() {
  const hasKey = state.doctor && state.doctor.key_found;
  const incomplete = firstIncomplete();
  const ok = hasKey && !state.running && state.jsonValid && questionCount() > 0 && !incomplete;
  $('#run').disabled = !ok;
  let hint = 'ctrl + enter';
  if (!hasKey) hint = 'no key — run disabled';
  else if (!state.jsonValid) hint = 'fix the JSON to run';
  else if (!questionCount()) hint = 'add a question';
  else if (incomplete) hint = incomplete;
  $('#run-hint').textContent = hint;
}

function showError(where, payload) {
  const box = $(where);
  box.textContent = '';
  if (!payload) return;
  const node = el('div', { class: 'error' }, [
    el('span', { text: payload.error || 'request failed' }),
  ]);
  if (payload.hint) node.appendChild(el('span', { class: 'hint', text: payload.hint }));
  if (payload.status === 503 && payload.doctor) {
    node.appendChild(el('span', {
      class: 'hint',
      text: 'Set ' + (payload.doctor.key_env_names || []).join(' or ') + ' and reload.',
    }));
  }
  box.appendChild(node);
}

async function run() {
  if ($('#run').disabled) return;
  const bundle = currentBundle();
  if (!Object.keys(bundle).length) return;
  state.running = true;
  updateRunState();
  const button = $('#run');
  button.textContent = '';
  button.appendChild(el('span', { class: 'spinner' }));
  button.appendChild(document.createTextNode('deciding'));
  $('#run-error').textContent = '';

  const request = {
    state: $('#state').value,
    questions: bundle,
    redact: $('#redact').checked,
    intent: $('#quick').value.trim() || '',
  };
  try {
    const started = performance.now();
    const response = await postJSON('/api/decide', request);
    state.lastRequest = request;
    state.lastResponse = response;
    state.totals.calls += 1;
    state.totals.tokens += Number((response.usage && response.usage.input_tokens) || 0);
    state.totals.cost += Number(response.cost_usd || 0);
    state.totals.latency = Number(
      (response.timing_ms && response.timing_ms.total_ms) || (performance.now() - started)
    );
    renderTotals();
    renderResult(response);
    loadHistory();
  } catch (err) {
    showError('#run-error', err.payload || { error: err.message });
  } finally {
    state.running = false;
    button.textContent = 'run';
    updateRunState();
  }
}

function renderTotals() {
  $('#t-calls').textContent = String(state.totals.calls);
  $('#t-tokens').textContent = state.totals.tokens.toLocaleString();
  $('#t-cost').textContent = money(state.totals.cost);
  $('#t-latency').textContent =
    state.totals.latency === null ? '— ms' : state.totals.latency.toFixed(0) + ' ms';
}

/* ---------------------------------------------------------------- result */

function bar(label, value, winner) {
  const row = el('div', { class: 'row' + (winner ? ' is-winner' : '') }, [
    el('span', { class: 'label', text: label, title: label }),
    el('span', { class: 'bar' }, [el('span', {})]),
    el('span', { class: 'num', text: value.toFixed(3) }),
  ]);
  row.querySelector('.bar span').style.width = (Math.max(0, Math.min(1, value)) * 100).toFixed(2) + '%';
  return row;
}

function renderResult(response) {
  const host = $('#answers');
  host.textContent = '';
  const answers = (response.decisions && response.decisions.answers) || {};
  const review = response.review || {};

  for (const [name, answer] of Object.entries(answers)) {
    const flagged = Object.prototype.hasOwnProperty.call(review, name);
    const head = el('div', { class: 'answer-head' }, [
      el('span', { class: 'answer-name', text: name }),
      el('span', { class: 'answer-kind', text: answer.kind || answer.type || '' }),
      flagged ? el('span', { class: 'badge', text: 'needs review' }) : null,
    ]);
    const block = el('div', { class: 'answer' }, [head]);

    if (answer.type === 'noul') {
      const probability = Number(answer.noul);
      block.appendChild(bar('P(true)', probability, false));
      block.appendChild(el('p', {
        class: 'caption',
        text: 'probability, not a boolean — ' + probability.toFixed(3) +
              '. Your code picks the threshold.',
      }));
    } else if (answer.type === 'choice') {
      const probabilities = Object.entries(answer.probabilities || {})
        .sort((a, b) => b[1] - a[1]);
      probabilities.forEach(([key, value], index) => {
        block.appendChild(bar(key, Number(value), index === 0));
      });
      const top = probabilities[0] ? Number(probabilities[0][1]) : 0;
      const second = probabilities[1] ? Number(probabilities[1][1]) : 0;
      block.appendChild(el('p', {
        class: 'caption',
        text: 'winner ' + String(answer.choice) +
              ' · confidence ' + (answer.confidence !== undefined && answer.confidence !== null
                ? Number(answer.confidence).toFixed(3) : 'n/a') +
              ' · margin ' + (top - second).toFixed(3) + ' (top-1 − top-2)',
      }));
    } else if (answer.type === 'score') {
      const legend = answer.legend || {};
      const probabilities = Object.entries(answer.probabilities || {})
        .sort((a, b) => Number(a[0]) - Number(b[0]));
      const best = probabilities.reduce((acc, kv) => (Number(kv[1]) > acc ? Number(kv[1]) : acc), 0);
      probabilities.forEach(([key, value]) => {
        const label = legend[key] !== undefined ? key + ' · ' + legend[key] : key;
        block.appendChild(bar(label, Number(value), Number(value) === best));
      });
      block.appendChild(el('p', {
        class: 'caption',
        text: 'mean ' + Number(answer.score).toFixed(3) +
              ' · confidence ' + (answer.confidence !== undefined && answer.confidence !== null
                ? Number(answer.confidence).toFixed(3) : 'n/a') +
              ' — a weighted mean over the levels, so a fractional value is a real answer.',
      }));
    } else {
      block.appendChild(el('pre', { class: 'raw', text: JSON.stringify(answer, null, 2) }));
    }

    if (flagged) {
      block.appendChild(el('p', { class: 'caption', text: 'needs review: ' + review[name] }));
    }
    host.appendChild(block);
  }

  const timing = response.timing_ms || {};
  const parts = [
    'serialize ' + Number(timing.serialize_ms || 0).toFixed(1),
    'http ' + Number(timing.http_ms || 0).toFixed(1),
    'parse ' + Number(timing.parse_ms || 0).toFixed(1),
    'total ' + Number(timing.total_ms || 0).toFixed(1) + ' ms',
  ];
  const footer = $('#result-footer');
  footer.textContent = '';
  footer.appendChild(el('span', { text: parts.join(' / ') }));
  footer.appendChild(document.createTextNode(
    '   ·   tokens in ' + Number((response.usage || {}).input_tokens || 0).toLocaleString() +
    '   ·   ' + money(response.cost_usd) + ' (' + (response.cost_source || 'unknown') + ')' +
    '   ·   ' + (response.model || '') +
    '   ·   ' + (response.provider || '') +
    '   ·   req ' + (response.request_id || '—') +
    '   ·   ' + (response.decision_id || '—')
  ));

  const redactions = response.redactions || { count: 0, kinds: [] };
  $('#redaction-report').textContent = redactions.count
    ? redactions.count + ' replaced (' + redactions.kinds.join(', ') + ') before sending'
    : ($('#redact').checked ? 'nothing matched the credential patterns' : 'redaction off — state sent as typed');

  $('#raw-json').textContent = JSON.stringify(
    { request: state.lastRequest, response: response }, null, 2);
  $('#result-panel').classList.remove('hidden');
}

/* --------------------------------------------------------------- copying */

async function copyText(text, button) {
  const original = button.textContent;
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = 'copied';
  } catch (_) {
    button.textContent = 'copy failed';
  }
  setTimeout(() => { button.textContent = original; }, 1200);
}

function asCli() {
  const bundle = JSON.stringify(currentBundle());
  const lines = [
    '# save the state to state.txt first, then:',
    'jevskill ask --state-file state.txt \\',
    "  --questions '" + bundle.replace(/'/g, "'\\''") + "' \\",
    '  --intent ' + JSON.stringify($('#quick').value.trim() || 'console') + ' --json',
  ];
  if (!$('#redact').checked) lines.splice(3, 0, '  --no-redact \\');
  lines.push('# or with nothing installed:');
  lines.push("# python \"$SKILL_DIR/scripts/jev_query.py\" --state-file state.txt --questions '<the same json>'");
  return lines.join('\n');
}

function asCurl() {
  const body = JSON.stringify({
    state: $('#state').value,
    questions: currentBundle(),
    redact: $('#redact').checked,
    intent: $('#quick').value.trim() || '',
  });
  return [
    "curl -s " + window.location.origin + "/api/decide \\",
    "  -H 'Content-Type: application/json' \\",
    "  -d '" + body.replace(/'/g, "'\\''") + "'",
  ].join('\n');
}

/* --------------------------------------------------------------- history */

async function loadHistory() {
  try {
    const payload = await api('/api/history?limit=20');
    const host = $('#history');
    host.textContent = '';
    for (const row of payload.decisions) {
      const button = el('button', { type: 'button', class: 'hist-row', onclick: () => pickHistory(row) }, [
        el('span', { class: 'ts', text: row.ts }),
        el('span', { class: 'intent', text: row.intent || '(no intent)' }),
        el('span', { class: 'num', text: row.questions + 'q' }),
        el('span', { class: 'num', text: Number(row.tokens_in || 0).toLocaleString() + ' tok' }),
        el('span', { class: 'num lat', text: Number(row.latency_ms || 0).toFixed(0) + ' ms' }),
      ]);
      host.appendChild(button);
    }
    $('#history-note').textContent = payload.decisions.length
      ? payload.count + ' console decision(s) in ' + payload.ledger
      : 'no console decisions yet in ' + payload.ledger;
  } catch (err) {
    $('#history-note').textContent = 'history unavailable: ' + err.message;
  }
}

function pickHistory(row) {
  /* The ledger stores measurements, not payloads — deliberately, so it never
   * becomes a copy of the user's data. So a row can be summarised and must not
   * be "reloaded": inventing a state that matches the numbers would be a lie
   * about what was asked. */
  $('#history-note').textContent = row.has_request
    ? ''
    : row.decision_id + ' · ' + row.ts + ' · ' + row.questions + ' question(s) · ' +
      Number(row.tokens_in || 0).toLocaleString() + ' tokens · ' + money(row.cost_usd) +
      ' · ' + Number(row.latency_ms || 0).toFixed(0) + ' ms' +
      (row.outcome ? ' · outcome ' + row.outcome : '') +
      ' — the ledger records the measurement, not the request, so there is nothing to reload.';
}

/* --------------------------------------------------------------- doctor */

function renderDoctor(report) {
  state.doctor = report;
  const pill = $('#doctor-pill');
  if (report.key_found) {
    pill.classList.remove('is-bad');
    pill.textContent = [
      report.provider, report.model,
      report.key_name + ' (' + report.key_source + ')',
      report.key_fingerprint,
    ].join(' · ');
    pill.title = report.endpoint;
  } else {
    pill.classList.add('is-bad');
    pill.textContent = 'no key · ' + report.provider;
    const banner = $('#banner');
    banner.textContent = '';
    banner.appendChild(el('div', { class: 'error' }, [
      el('span', { text: 'No API key found, so nothing can be decided — and this console will never simulate an answer.' }),
      el('span', { class: 'hint', text: 'Set JEV_API_KEY (the vendor endpoint) or OPENROUTER_API_KEY in your environment, then reload this page.' }),
      el('span', { class: 'hint', text: 'Provider ' + report.provider + ' · model ' + report.model + ' · ' + report.endpoint }),
    ]));
  }
  updateRunState();
}

/* ------------------------------------------------------------- templates */

function renderTemplates(templates) {
  state.templates = templates;
  const menu = $('#templates-menu');
  menu.textContent = '';
  for (const [key, template] of Object.entries(templates)) {
    menu.appendChild(el('button', {
      type: 'button', role: 'menuitem',
      onclick: () => { applyTemplate(key); closeMenus(); },
    }, [
      el('span', { text: template.title || key }),
      el('span', { class: 'sub', text: template.source || '' }),
    ]));
  }
}

function applyTemplate(key) {
  const template = state.templates[key];
  if (!template) return;
  state.cards = cardsFromBundle(template.questions);
  if (state.jsonMode) $('#bundle-json').value = JSON.stringify(template.questions, null, 2);
  refresh();
  if (template.hint) {
    const box = $('#run-error');
    box.textContent = '';
    box.appendChild(el('p', { class: 'note', text: template.hint }));
  }
}

function closeMenus() {
  $('#templates-menu').classList.add('hidden');
  $('#templates-btn').setAttribute('aria-expanded', 'false');
}

/* ------------------------------------------------------------------ wire */

function loadFile(file) {
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    $('#state').value = String(reader.result || '');
    scheduleEstimate();
  };
  reader.readAsText(file);
}

function wire() {
  $('#state').addEventListener('input', scheduleEstimate);
  $('#clear-state').addEventListener('click', () => { $('#state').value = ''; scheduleEstimate(); });
  $('#pick-file').addEventListener('click', () => $('#file-input').click());
  $('#file-input').addEventListener('change', (event) => loadFile(event.target.files[0]));

  const zone = $('#state-panel');
  ['dragenter', 'dragover'].forEach((name) => zone.addEventListener(name, (event) => {
    event.preventDefault();
    zone.classList.add('is-over');
  }));
  ['dragleave', 'drop'].forEach((name) => zone.addEventListener(name, (event) => {
    event.preventDefault();
    zone.classList.remove('is-over');
  }));
  zone.addEventListener('drop', (event) => loadFile(event.dataTransfer.files[0]));

  $('#redact').addEventListener('change', () => {
    $('#redaction-report').textContent = $('#redact').checked
      ? '' : 'redaction off — the state will be sent exactly as typed';
  });

  $('#quick').addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    const text = $('#quick').value.trim();
    if (!text) return;
    const card = newCard('noul');
    card.name = slug(text);
    card.instructions = text;
    state.cards = [card];
    if (state.jsonMode) setJsonMode(false);
    refresh();
    run();
  });

  $('#add-question').addEventListener('click', () => { state.cards.push(newCard('noul')); refresh(); });
  $('#toggle-json').addEventListener('click', () => setJsonMode(!state.jsonMode));
  $('#bundle-json').addEventListener('input', onJsonInput);
  $('#run').addEventListener('click', run);

  $('#templates-btn').addEventListener('click', (event) => {
    event.stopPropagation();
    const menu = $('#templates-menu');
    const open = menu.classList.toggle('hidden');
    $('#templates-btn').setAttribute('aria-expanded', String(!open));
  });
  document.addEventListener('click', closeMenus);

  $('#toggle-raw').addEventListener('click', () => {
    const raw = $('#raw-json');
    const shown = raw.classList.toggle('hidden');
    $('#toggle-raw').setAttribute('aria-pressed', String(!shown));
  });
  $('#copy-cli').addEventListener('click', (event) => copyText(asCli(), event.target));
  $('#copy-curl').addEventListener('click', (event) => copyText(asCurl(), event.target));
  $('#refresh-history').addEventListener('click', loadHistory);

  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      // Only the decide view answers to this. A computer-use run takes the
      // mouse and the keyboard of this machine; it is asked for with a click
      // or not at all.
      if ($('#view-decide').classList.contains('hidden')) return;
      event.preventDefault();
      run();
    }
    if (event.key === 'Escape') {
      closeMenus();
      $('#raw-json').classList.add('hidden');
      $('#toggle-raw').setAttribute('aria-pressed', 'false');
    }
  });
}

/* ======================================================== computer use ====
 *
 * A run in this view does not draw a chart: it takes the mouse and the
 * keyboard of the machine the page is open on. So the panel is built around
 * the way *out*, not the way in — STOP is the tallest control in the command
 * panel, the ways to stop are printed under it in both languages and are
 * always visible, and **nothing here starts a run from the keyboard**: a run
 * that takes the desktop has to be asked for with a click.
 */

const CU_POLL_BUSY_MS = 400;
const CU_POLL_IDLE_MS = 3000;
const CU_ACTIVE = ['planning', 'waiting_window', 'running', 'waiting_confirm'];
const CU_GLYPH = {
  pending: '○', running: '◐', done: '●', failed: '✕', stopped: '■', skipped: '–',
};
const CU_STATE_TEXT = {
  idle: 'idle', planning: 'planning', waiting_window: 'waiting for window',
  running: 'running', waiting_confirm: 'waiting for confirm',
  done: 'done', stopped: 'stopped', failed: 'failed',
};
/* Kinds that are the machinery talking, not the run making progress. */
const CU_DIM_KINDS = ['llm', 'verify', 'escalate', 'replan', 'text'];

const cu = {
  since: 0, runId: null, busy: false, lost: false, sending: false,
  timer: null, immediate: false,
  models: null, preview: null, previewFor: '', note: '',
  lines: [], stick: true, confirmKey: '', killKey: '', last: null,
};

/* localStorage throws outright in some privacy modes; a console that cannot
 * remember a preference must still run. */
function lsGet(key, fallback) {
  try {
    const value = localStorage.getItem(key);
    return value === null ? fallback : value;
  } catch (_) { return fallback; }
}

function lsSet(key, value) {
  try { localStorage.setItem(key, value); } catch (_) { /* not important enough to fail on */ }
}

/* Six decimals, because a planning call costs a fraction of a cent and a run
 * that spent nothing should say so rather than print eight zeroes. */
function cuMoney(value) {
  const n = Number(value || 0);
  return n === 0 ? '$0' : '$' + n.toFixed(6);
}

/* OpenRouter quotes per token; people price models per million. */
function perMtok(price) {
  const n = Number(price || 0) * 1e6;
  if (!isFinite(n)) return '$?';
  return '$' + String(Number(n.toFixed(2)));
}

/* ------------------------------------------------------------ the views */

function setView(name) {
  const wantCu = name === 'cu';
  $('#view-decide').classList.toggle('hidden', wantCu);
  $('#view-cu').classList.toggle('hidden', !wantCu);
  $('#tab-decide').setAttribute('aria-pressed', String(!wantCu));
  $('#tab-cu').setAttribute('aria-pressed', String(wantCu));
  lsSet('jev.view', wantCu ? 'cu' : 'decide');
  try {
    history.replaceState(null, '', wantCu ? '#cu' : location.pathname + location.search);
  } catch (_) { /* file:// and old engines */ }
  // A hidden element has no scroll height, so the log has to be pinned again
  // on the way in.
  if (wantCu && cu.stick) { const log = $('#cu-log'); log.scrollTop = log.scrollHeight; }
}

/* ----------------------------------------------------------- the picker */

function cuModel() {
  const select = $('#cu-model');
  if (select.value === '__custom') return $('#cu-model-custom').value.trim() || null;
  return select.value || null;
}

function cuModelNotes(lines) {
  const host = $('#cu-model-note');
  host.textContent = '';
  for (const line of lines) host.appendChild(el('p', { class: 'note', text: line }));
}

function cuSyncModelUi() {
  $('#cu-model-custom').classList.toggle('hidden', $('#cu-model').value !== '__custom');
  cuUpdateButtons();
}

function cuRestoreModel(payload) {
  const select = $('#cu-model');
  const stored = lsGet('jev.cu.model', null);
  let wanted = stored !== null ? stored : ((payload && payload.default) || '');
  // No key means no planning model can run, so the page says so and picks the
  // one option that still works rather than pre-selecting a failure.
  if (payload && !payload.key_found) wanted = '';
  if (!wanted) select.value = '';
  else if (Array.prototype.some.call(select.options, (o) => o.value === wanted)) select.value = wanted;
  else { select.value = '__custom'; $('#cu-model-custom').value = wanted; }
  cuSyncModelUi();
}

async function cuLoadModels() {
  const select = $('#cu-model');
  const custom = select.querySelector('option[value="__custom"]');
  let payload;
  try {
    payload = await api('/api/cu/models');
  } catch (_) {
    cuModelNotes(['the model list could not be read — an id can still be typed under custom id…']);
    cuRestoreModel(null);
    return;
  }
  cu.models = payload;
  const group = el('optgroup', { label: 'recommended' });
  for (const model of payload.recommended || []) {
    group.appendChild(el('option', {
      value: model.id,
      text: (model.name || model.id) + ' · ' + perMtok(model.prompt) +
            ' / ' + perMtok(model.completion) + ' per Mtok',
    }));
  }
  select.insertBefore(group, custom);

  const list = $('#cu-models-all');
  list.textContent = '';
  for (const id of payload.all || []) list.appendChild(el('option', { value: id }));

  const notes = [];
  if (!payload.key_found) {
    notes.push('planning model needs ' + (payload.key_hint || 'OPENROUTER_API_KEY') +
               ' — Jev-only still works');
  }
  if (payload.source === 'fallback') {
    notes.push('model list is the built-in fallback (OpenRouter unreachable); ' +
               'any id can still be typed');
  }
  cuModelNotes(notes);
  cuRestoreModel(payload);
}

/* --------------------------------------------------------- the settings */

function cuNumber(selector, fallback) {
  const value = Number($(selector).value);
  return isFinite(value) && value > 0 ? value : fallback;
}

/* The cap is the one field where a blank box must not mean zero: the server
 * reads a cap of 0 as "no cap at all", so an emptied field falls back to the
 * default rather than quietly uncapping the run. A typed 0 is still honoured. */
function cuCap() {
  const raw = $('#cu-cap').value.trim();
  const value = Number(raw);
  return raw === '' || !isFinite(value) || value < 0 ? 0.5 : value;
}

function cuRestorePrefs() {
  for (const [selector, key] of [['#cu-max-steps', 'jev.cu.max_steps'],
                                 ['#cu-budget', 'jev.cu.budget_s'],
                                 ['#cu-cap', 'jev.cu.usd_cap']]) {
    const stored = lsGet(key, null);
    if (stored !== null && stored !== '' && isFinite(Number(stored))) $(selector).value = stored;
  }
  // Dry run is the default, and stays the default until it is switched off by
  // hand: the cheap mistake is a simulation nobody wanted.
  $('#cu-dry').checked = lsGet('jev.cu.dry', '1') !== '0';
}

function cuSavePrefs() {
  lsSet('jev.cu.max_steps', $('#cu-max-steps').value);
  lsSet('jev.cu.budget_s', $('#cu-budget').value);
  lsSet('jev.cu.usd_cap', $('#cu-cap').value);
  lsSet('jev.cu.dry', $('#cu-dry').checked ? '1' : '0');
}

function cuUpdateButtons() {
  const command = $('#cu-command').value.trim();
  const model = cuModel();
  const dry = $('#cu-dry').checked;
  const start = $('#cu-start');
  start.textContent = dry ? 'simulate' : 'start';
  start.disabled = cu.busy || cu.sending || !command;
  $('#cu-plan-btn').disabled = cu.busy || cu.sending || !command || !model;
  const stop = $('#cu-stop');
  stop.disabled = !cu.busy;
  stop.classList.toggle('is-armed', cu.busy);

  let hint = cu.note || '';
  if (!command) hint = 'write a command first';
  else if (cu.busy) hint = 'a run is active — STOP takes the desktop back';
  else if (!model) hint = 'plan needs a planning model';
  else if (!hint && cu.preview) hint = 'plan ready — start runs exactly these steps';
  $('#cu-hint').textContent = hint;
}

/* ----------------------------------------------------------- the writes */

function cuShowError(err) {
  const payload = (err && err.payload) || { error: (err && err.message) || 'request failed' };
  const status = (err && err.status) || payload.status;
  const box = $('#cu-error');
  box.textContent = '';
  // Asking to start while a run is active is a fact about the console, not a
  // failure — it gets a line, not a boxed alarm.
  if (status === 409) {
    box.appendChild(el('p', {
      class: 'note',
      text: (payload.error || 'busy') + (payload.hint ? ' — ' + payload.hint : ''),
    }));
    return;
  }
  showError('#cu-error', payload);
}

async function cuStart() {
  if ($('#cu-start').disabled) return;
  const command = $('#cu-command').value.trim();
  if (!command) return;
  cu.sending = true;
  cuUpdateButtons();
  $('#cu-error').textContent = '';
  const body = {
    command: command,
    model: cuModel(),
    dry_run: $('#cu-dry').checked,
    max_steps: cuNumber('#cu-max-steps', 25),
    budget_s: cuNumber('#cu-budget', 90),
    total_budget_s: 600,
    usd_cap: cuCap(),
  };
  // What the user read is what runs: a plan shown in the list is sent back
  // verbatim rather than planned again behind their back.
  if (cu.preview && cu.previewFor === command) body.plan = cu.preview;
  try {
    await postJSON('/api/cu/start', body);
    cu.note = '';
  } catch (err) {
    cuShowError(err);
  } finally {
    cu.sending = false;
    cuUpdateButtons();
    cuPollSoon();
  }
}

async function cuPlanNow() {
  if ($('#cu-plan-btn').disabled) return;
  const command = $('#cu-command').value.trim();
  const button = $('#cu-plan-btn');
  cu.sending = true;
  cuUpdateButtons();
  button.textContent = 'planning…';
  $('#cu-error').textContent = '';
  try {
    const payload = await postJSON('/api/cu/plan', { command: command, model: cuModel() });
    cu.preview = (payload.steps || []).map((step) => Object.assign({}, step, { status: 'pending' }));
    cu.previewFor = command;
    cu.note = payload.note || '';
    cuRenderPlanSteps(cu.preview);
    cuAppendEvents(payload.events || []);
  } catch (err) {
    cuShowError(err);
  } finally {
    button.textContent = 'plan';
    cu.sending = false;
    cuUpdateButtons();
  }
}

async function cuStop() {
  if ($('#cu-stop').disabled) return;
  try {
    await postJSON('/api/cu/stop', { reason: 'STOP button' });
  } catch (err) {
    cuShowError(err);
  }
  cuPollSoon();
}

async function cuAnswer(allow) {
  try {
    await postJSON('/api/cu/confirm', { allow: !!allow });
  } catch (err) {
    cuShowError(err);
  }
  cuPollSoon();
}

/* --------------------------------------------------------- the polling */

function cuPollSoon() {
  cu.immediate = true;
  clearTimeout(cu.timer);
  cu.timer = setTimeout(cuPoll, 60);
}

async function cuPoll() {
  const sent = cu.since;
  try {
    const payload = await api('/api/cu/status?since=' + sent);
    cu.lost = false;
    cuApply(payload, sent);
  } catch (_) {
    // Keep the last state on screen; only the pill admits the gap.
    cu.lost = true;
    cuRenderStatus(null);
  }
  clearTimeout(cu.timer);
  const wait = cu.immediate ? 0 : (cu.busy ? CU_POLL_BUSY_MS : CU_POLL_IDLE_MS);
  cu.immediate = false;
  cu.timer = setTimeout(cuPoll, wait);
}

function cuApply(payload, sent) {
  cu.last = payload;
  cu.busy = !!payload.busy;
  const runId = payload.run || null;
  let skipEvents = false;
  if (runId !== cu.runId) {
    // A different run (this page started it, or `jevskill cu` did): the log on
    // screen belongs to the previous one, and `since` is counted per run.
    cu.runId = runId;
    cu.since = 0;
    cu.preview = null;
    cu.previewFor = '';
    cuClearLog();
    if (sent !== 0) { skipEvents = true; cu.immediate = true; }
  }
  if (!skipEvents) {
    cuAppendEvents(payload.events || []);
    cu.since = Number(payload.next || 0);
  }
  cuRenderStatus(payload);
  cuRenderPlan(payload);
  cuRenderConfirm(payload);
  cuRenderHowToStop(payload.kill_switch);
  cuUpdateButtons();
}

/* -------------------------------------------------------- the rendering */

function cuRenderStatus(payload) {
  const pill = $('#cu-state');
  if (cu.lost) {
    pill.textContent = 'connection lost';
    pill.classList.add('is-lost');
    return;
  }
  pill.classList.remove('is-lost');
  if (!payload) return;
  const state = String(payload.state || 'idle');
  let label = CU_STATE_TEXT[state] || state;
  const plan = payload.plan || [];
  if (state === 'running' && plan.length && Number(payload.current) >= 0) {
    label += ' ' + (Number(payload.current) + 1) + '/' + plan.length;
  }
  pill.textContent = label;
  pill.classList.toggle('is-live', CU_ACTIVE.includes(state));

  $('#cu-sim').classList.toggle('hidden', !(payload.run && payload.dry_run));
  $('#cu-elapsed').textContent = payload.run ? Number(payload.elapsed_s || 0).toFixed(1) + ' s' : '';

  const spend = payload.spend || {};
  const calls = Number(spend.jev_calls || 0) + Number(spend.llm_calls || 0);
  $('#cu-spend').textContent = payload.run
    ? 'jev ' + cuMoney(spend.jev_usd) + ' · llm ' + cuMoney(spend.llm_usd) +
      ' · ' + calls + (calls === 1 ? ' call' : ' calls')
    : '';

  const platform = payload.platform || { ok: true, reason: '' };
  const note = $('#cu-platform');
  note.classList.toggle('hidden', !!platform.ok);
  if (!platform.ok) note.textContent = (platform.reason || 'live runs are unavailable here') +
    ' — a dry run still works';
}

function cuStepRow(step, index) {
  const status = String(step.status || 'pending');
  const main = el('div', {}, [
    el('span', { class: 'cu-goal', text: String(step.goal || 'goal ' + (index + 1)) }),
    step.launch ? el('span', { class: 'cu-launch', text: 'launch ' + step.launch }) : null,
    step.done_when ? el('span', { class: 'cu-when', text: String(step.done_when) }) : null,
  ]);
  const meta = [];
  const steps = Number(step.steps || 0);
  if (steps) meta.push(steps + (steps === 1 ? ' step' : ' steps'));
  if (step.stop_reason) meta.push(String(step.stop_reason));
  return el('div', { class: 'cu-step is-' + status }, [
    el('span', {
      class: 'cu-glyph', role: 'img', title: status, 'aria-label': status,
      text: CU_GLYPH[status] || '·',
    }),
    main,
    el('span', { class: 'cu-step-meta', text: meta.join(' · ') }),
  ]);
}

function cuRenderPlanSteps(steps) {
  const host = $('#cu-plan');
  host.textContent = '';
  if (!steps || !steps.length) {
    host.appendChild(el('p', {
      class: 'cu-empty', text: 'no plan yet — write a command, then plan or start',
    }));
    return;
  }
  steps.forEach((step, index) => host.appendChild(cuStepRow(step, index)));
}

function cuRenderPlan(payload) {
  cuRenderPlanSteps(cu.preview || (payload && payload.plan) || []);
}

function cuRenderConfirm(payload) {
  const box = $('#cu-confirm');
  const pending = payload && payload.pending_confirm;
  if (!pending) {
    if (cu.confirmKey) {
      box.textContent = '';
      box.classList.add('hidden');
      cu.confirmKey = '';
    }
    return;
  }
  const key = String(pending.asked_at) + '|' + String(pending.prompt);
  if (key !== cu.confirmKey) {
    cu.confirmKey = key;
    box.textContent = '';
    box.appendChild(el('p', { class: 'ask' }, [
      document.createTextNode('The agent wants to: '),
      el('b', { text: String(pending.prompt || 'act on this desktop') }),
    ]));
    const facts = [];
    if (pending.name) facts.push('name ' + pending.name);
    if (pending.target) facts.push('target ' + pending.target);
    if (pending.key) facts.push('key ' + pending.key);
    if (facts.length) box.appendChild(el('p', { class: 'facts', text: facts.join('  ·  ') }));
    // Deny takes the focus, not allow: the default answer to "may I touch the
    // desktop" should never be one stray Enter away.
    const deny = el('button', { type: 'button', text: 'deny', onclick: () => cuAnswer(false) });
    box.appendChild(el('div', { class: 'btn-row' }, [
      el('button', { type: 'button', class: 'btn-primary', text: 'allow', onclick: () => cuAnswer(true) }),
      deny,
    ]));
    box.appendChild(el('p', { class: 'countdown' }));
    box.appendChild(el('p', { class: 'facts', text: 'STOP denies and ends the run' }));
    box.classList.remove('hidden');
    deny.focus();
  }
  // The server denies on timeout; this only shows the clock it is running.
  const left = Math.max(0, Math.round(
    Number(pending.asked_at || 0) + Number(pending.timeout_s || 0) - Date.now() / 1000));
  const countdown = box.querySelector('.countdown');
  countdown.textContent = '';
  countdown.appendChild(el('span', {
    class: left <= 15 ? 'warn' : null,
    text: left ? 'answer within ' + left + ' s' : 'time is up — the agent denied it',
  }));
}

function cuRenderHowToStop(kill) {
  const info = kill || { button: true, hotkey: null, corner: null, cli: 'jevskill cu stop' };
  const key = JSON.stringify([info.hotkey, info.corner, info.cli]);
  if (key === cu.killKey) return;
  cu.killKey = key;
  const cli = info.cli || 'jevskill cu stop';

  const line = (lead, ways) => {
    const node = el('p', {}, [el('span', { text: lead })]);
    ways.forEach((way, index) => {
      if (index) node.appendChild(document.createTextNode(' · '));
      for (const part of way) {
        node.appendChild(part.k
          ? el('span', { class: 'k', text: part.k })
          : document.createTextNode(part.t));
      }
    });
    return node;
  };

  const pl = [[{ t: 'przycisk STOP' }]];
  const en = [[{ t: 'the STOP button' }]];
  if (info.hotkey) {
    pl.push([{ k: info.hotkey }, { t: ' (gdziekolwiek)' }]);
    en.push([{ k: info.hotkey }, { t: ' anywhere' }]);
  }
  if (info.corner) {
    pl.push([{ t: 'mysz w lewy górny róg ekranu' }]);
    en.push([{ t: 'mouse to the top-left corner' }]);
  }
  pl.push([{ k: cli }, { t: ' w terminalu' }]);
  en.push([{ k: cli }, { t: ' in a terminal' }]);

  const host = $('#cu-howstop');
  host.textContent = '';
  host.appendChild(line('Jak przerwać: ', pl));
  host.appendChild(line('To stop: ', en));
  host.appendChild(el('p', {
    class: 'after',
    text: 'Po starcie kliknij okno, w którym agent ma pracować — konsola nie może ' +
          'zostać na wierzchu. / After start, click the window the agent should work ' +
          'in; the console must not stay in front.',
  }));
}

/* -------------------------------------------------------------- the log */

function cuLineClass(event) {
  const kind = String(event.kind || '');
  const text = String(event.text || '');
  if (kind === 'llm_error' || kind === 'goal_stalled') return 'is-stop';
  if (kind === 'end') return text.indexOf('failed') === 0 ? 'is-stop' : 'is-end';
  if (kind === 'confirm' || kind === 'confirm_result') return 'is-gold';
  if (CU_DIM_KINDS.indexOf(kind) >= 0) return 'is-dim';
  return '';
}

function cuLogLine(event) {
  return el('div', { class: ('cu-line ' + cuLineClass(event)).trim() }, [
    el('span', { class: 't', text: Number(event.t || 0).toFixed(2) }),
    el('span', { class: 'kind', text: String(event.kind || '') }),
    el('span', { class: 'what', text: String(event.text || '') }),
  ]);
}

function cuAppendEvents(events) {
  if (!events || !events.length) return;
  const log = $('#cu-log');
  for (const event of events) {
    cu.lines.push(event);
    log.appendChild(cuLogLine(event));
  }
  // Follow the tail, unless the reader has scrolled back to look at something.
  if (cu.stick) log.scrollTop = log.scrollHeight;
}

function cuClearLog() {
  $('#cu-log').textContent = '';
  cu.lines = [];
  cu.stick = true;
}

function cuLogText() {
  return cu.lines.map((event) =>
    Number(event.t || 0).toFixed(2).padStart(7) + '  ' +
    String(event.kind || '').padEnd(14) + ' ' + String(event.text || '')).join('\n');
}

/* ------------------------------------------------------------- the wire */

function cuWire() {
  $('#tab-decide').addEventListener('click', () => setView('decide'));
  $('#tab-cu').addEventListener('click', () => setView('cu'));
  window.addEventListener('hashchange', () => {
    if (location.hash === '#cu') setView('cu');
    else if (!location.hash || location.hash === '#') setView('decide');
  });

  $('#cu-command').addEventListener('input', () => {
    // The plan on screen described the old command; it is no longer what would
    // run, so it stops being what start sends.
    if (cu.previewFor && cu.previewFor !== $('#cu-command').value.trim()) {
      cu.preview = null;
      cu.previewFor = '';
      cu.note = '';
      cuRenderPlan(cu.last);
    }
    cuUpdateButtons();
  });

  $('#cu-model').addEventListener('change', () => {
    cuSyncModelUi();
    lsSet('jev.cu.model', cuModel() || '');
  });
  $('#cu-model-custom').addEventListener('input', () => {
    lsSet('jev.cu.model', cuModel() || '');
    cuUpdateButtons();
  });

  for (const selector of ['#cu-max-steps', '#cu-budget', '#cu-cap']) {
    $(selector).addEventListener('change', cuSavePrefs);
  }
  $('#cu-dry').addEventListener('change', () => { cuSavePrefs(); cuUpdateButtons(); });

  $('#cu-plan-btn').addEventListener('click', cuPlanNow);
  $('#cu-start').addEventListener('click', cuStart);
  $('#cu-stop').addEventListener('click', cuStop);

  const log = $('#cu-log');
  log.addEventListener('scroll', () => {
    cu.stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
  });
  $('#cu-copy-log').addEventListener('click', (event) => copyText(cuLogText(), event.target));
  $('#cu-clear-log').addEventListener('click', cuClearLog);
}

function cuBoot() {
  cuWire();
  cuRestorePrefs();
  cuRenderPlanSteps([]);
  cuRenderHowToStop(null);
  cuUpdateButtons();
  setView(location.hash === '#cu' ? 'cu' : (lsGet('jev.view', 'decide') === 'cu' ? 'cu' : 'decide'));
  cuLoadModels();
  // Poll from boot, not from the first click: a run started by `jevskill cu`
  // in a terminal is this page's business too.
  cuPoll();
}

async function boot() {
  wire();
  cuBoot();
  // Start empty: the quick gate is the fast path, a template the second, and an
  // empty card only produced a 400 from the server on the first click.
  state.cards = [];
  refresh();
  renderTotals();
  try {
    renderDoctor(await api('/api/doctor'));
  } catch (err) {
    $('#doctor-pill').textContent = 'doctor unavailable';
  }
  try {
    const payload = await api('/api/templates');
    renderTemplates(payload.templates || {});
  } catch (_) { /* the console still works without the template menu */ }
  loadHistory();
  scheduleEstimate();
}

boot();
