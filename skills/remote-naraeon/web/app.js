'use strict';
// Remote credentials never enter the browser. The fragment is this local process's capability.
const session = location.hash.slice(1);
history.replaceState(null, '', '/');
const cards = [];
let intervalMs = 2000;
async function api(path, data) {
  const response = await fetch(path, {method: data ? 'POST' : 'GET', cache: 'no-store',
    headers: {'X-Narae-Session': session, ...(data ? {'Content-Type': 'application/json'} : {})},
    ...(data ? {body: JSON.stringify(data)} : {})});
  const value = await response.json();
  if (!response.ok || value.ok === false) throw new Error(value.error || '관제 요청 실패');
  return data ? value.result : value;
}
function makeCard(name) {
  const root = document.querySelector('#device-card').content.firstElementChild.cloneNode(true);
  document.querySelector('#grid').append(root);
  const $ = selector => root.querySelector(selector);
  $('h2').textContent = name;
  const img = $('img'), status = $('.status');
  const state = {name, root, active: false, busy: false, timer: null, frame: null, armed: false, generation: 0};
  const request = (operation, args = {}) => api('/api/device', {device: name, operation, arguments: args});
  function message(text, error = false) { status.textContent = text; status.classList.toggle('error', error); }
  function buttons() {
    $('.start').disabled = state.busy || state.active;
    $('.arm').disabled = state.busy || !state.active;
    for (const b of root.querySelectorAll('.controls button,.typing button,.typing input,.typing select')) b.disabled = state.busy || !state.armed || !state.frame;
    $('.screen').classList.toggle('live', state.armed && !state.busy && !!state.frame);
    $('.control-state').textContent = state.armed ? '이 기기 제어권 확인됨 · 화면을 클릭해 터치' : '관찰 전용 · 제어권 자동 인계 없음';
  }
  function clearFrame() {
    state.frame = null;
    img.hidden = true; img.removeAttribute('src');
    $('.empty').hidden = false;
  }
  function halt(text, error = false) {
    state.active = state.armed = false; state.generation++;
    clearTimeout(state.timer); clearFrame(); message(text, error); buttons();
  }
  async function stop() {
    halt('중지됨 · 기기의 원격 모드는 유지됩니다.');
    try { await request('stop'); } catch (e) { message(e.message, true); }
  }
  function options(select, entries, placeholder) {
    const chosen = select.value;
    select.replaceChildren(new Option(placeholder, ''));
    for (const [id, label] of entries) select.add(new Option(label, String(id)));
    if (chosen !== '' && [...select.options].some(o => o.value === chosen)) select.value = chosen;
    else if (entries.length === 1) select.value = String(entries[0][0]);
  }
  async function display(frame) {
    const generation = state.generation;
    const decoded = new Image(); decoded.src = frame.image;
    await decoded.decode();
    if (!state.active || generation !== state.generation) return;
    state.frame = frame; state.armed = frame.armed;
    img.src = frame.image; img.alt = name + ' · 방금 받은 실제 화면'; img.hidden = false;
    $('.empty').hidden = true;
    options($('.edit-target'), frame.editable.map(n => [n.id, n.label]), '입력란 선택');
    options($('.scroll-target'), frame.scrollable.map(id => [id, '스크롤 영역 ' + id]), '스크롤 영역');
    message('수신 ' + new Date(frame.captured_at * 1000).toLocaleTimeString('ko-KR') + ' · ' + frame.width + '×' + frame.height + ' · 다음 갱신 ' + intervalMs / 1000 + '초 뒤');
  }
  function schedule() {
    clearTimeout(state.timer);
    if (state.active && !document.hidden) state.timer = setTimeout(() => refresh(false), intervalMs);
  }
  async function refresh(start) {
    if (state.busy || document.hidden || (!start && !state.active)) return;
    state.active = true; state.busy = true; buttons();
    const generation = state.generation;
    try {
      const frame = await request('frame', {start});
      if (generation === state.generation && state.active) await display(frame);
    } catch (e) { if (generation === state.generation) halt(e.message + ' · 자동 재연결 없음', true); }
    finally { state.busy = false; buttons(); schedule(); }
  }
  async function arm() {
    if (state.busy || !state.active) return;
    clearTimeout(state.timer); state.busy = true; buttons();
    const generation = state.generation;
    try {
      await request('arm');
      if (generation === state.generation && state.active) {
        clearFrame(); state.armed = true;
        message('제어권 확인됨 · 새 화면 수신 중…');
        const frame = await request('frame');
        if (generation === state.generation && state.active) await display(frame);
      }
    } catch (e) { if (generation === state.generation) halt(e.message, true); }
    finally { state.busy = false; buttons(); schedule(); }
  }
  async function action(kind, extra = {}) {
    if (!state.armed || !state.frame || state.busy || !state.active) return;
    const generation = state.generation;
    clearTimeout(state.timer); state.busy = true; buttons();
    const frameId = state.frame.frame_id;
    // The displayed frame is consumed exactly once, including on timeout.
    state.frame = null;
    try {
      await request('action', {action: kind, frame_id: frameId, ...extra});
      if (generation === state.generation && state.active) {
        message('조작 요청 수락 · 새 화면으로 결과 확인 중…');
        // One observation; no automatic mutation retry.
        const frame = await request('frame');
        if (generation === state.generation && state.active) await display(frame);
      }
    } catch (e) { if (generation === state.generation) halt(e.message + ' · 조작을 재전송하지 않았습니다.', true); }
    finally { state.busy = false; buttons(); schedule(); }
  }
  $('.start').onclick = () => refresh(true);
  $('.stop').onclick = stop;
  $('.arm').onclick = arm;
  $('.expand').onclick = () => { const expanded = root.classList.toggle('expanded'); $('.expand').textContent = expanded ? '격자로 복귀' : '크게 보기'; };
  img.onclick = e => {
    if (!state.frame || !state.frame.tap_supported) return;
    const box = img.getBoundingClientRect();
    // The <img> element keeps its natural aspect ratio; letterbox is outside it.
    const x = (e.clientX - box.left) * state.frame.width / box.width;
    const y = (e.clientY - box.top) * state.frame.height / box.height;
    if (x >= 0 && y >= 0 && x < state.frame.width && y < state.frame.height) action('tap', {x, y});
  };
  for (const b of root.querySelectorAll('[data-action]')) b.onclick = () => {
    const kind = b.dataset.action;
    if (kind.startsWith('scroll_')) {
      const id = $('.scroll-target').value;
      if (!id) { message('스크롤 영역을 선택하세요.', true); return; }
      action(kind, {node_id: Number(id)});
    } else action(kind);
  };
  $('.typing').onsubmit = e => {
    e.preventDefault(); const id = $('.edit-target').value;
    if (!id) { message('입력할 항목을 선택하세요.', true); return; }
    action('type', {node_id: Number(id), text: $('.text').value});
  };
  buttons();
  return {start: () => refresh(true), stop, pause: () => { clearTimeout(state.timer); }, state};
}
async function init() {
  if (!session) throw new Error('인증된 관제 URL로 여세요. 터미널에서 monitor를 다시 실행할 수 있습니다.');
  const config = await api('/api/devices'); intervalMs = config.interval * 1000;
  for (const name of config.devices) cards.push(makeCard(name));
  document.querySelector('#notice').textContent = config.devices.length + '대 등록 · 화면은 시작 버튼을 누른 기기만 조회합니다.';
  document.querySelector('#start-all').onclick = () => cards.forEach(c => { if (!c.state.active) c.start(); });
  document.querySelector('#stop-all').onclick = () => cards.forEach(c => c.stop());
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      // A hidden tab stops viewing and discards cached frames. Resume is explicit.
      cards.forEach(c => c.stop());
      document.querySelector('#notice').textContent = '다른 창으로 이동해 관제를 중지했습니다. 보기 시작을 눌러 다시 연결하세요.';
    }
  });
  window.addEventListener('pagehide', () => {
    for (const c of cards) fetch('/api/device', {method: 'POST', keepalive: true,
      headers: {'X-Narae-Session': session, 'Content-Type': 'application/json'},
      body: JSON.stringify({device: c.state.name, operation: 'stop'})}).catch(() => {});
  });
}
init().catch(e => { document.querySelector('#notice').textContent = e.message; });
