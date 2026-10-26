/* One selected-id set drives both the preview and candidate checkboxes. */
(() => {
  'use strict';
  const data = JSON.parse(document.getElementById('journey-data').textContent);
  const byId = new Map(data.candidates.map(item => [item.original_id, item]));
  data.journeys.forEach(journey => journey.members.forEach(item => byId.set(item.original_id, item)));
  const el = id => document.getElementById(`journey-${id}`);
  const node = (tag, text, className) => {
    const result = document.createElement(tag);
    if (text !== undefined) result.textContent = text;
    if (className) result.className = className;
    return result;
  };
  const button = (label, action) => {
    const result = node('button', label, 'app-button app-button-secondary');
    result.type = 'button'; result.onclick = action;
    return result;
  };
  const searchText = item => [item.title, item.journal, item.manuscript_id, item.original_id, item.status].join(' ').toLocaleLowerCase();
  const compare = (a, b) => {
    // Server supplies fixed-width UTC microsecond keys: no browser date guessing.
    const left = a.submission_sort_date || '', right = b.submission_sort_date || '';
    const dateOrder = left < right ? -1 : left > right ? 1 : 0;
    const aId = Array.from(a.original_id, char => char.codePointAt(0));
    const bId = Array.from(b.original_id, char => char.codePointAt(0));
    let idOrder = 0;
    for (let index = 0; index < Math.min(aId.length, bId.length); index++) {
      if (aId[index] !== bId[index]) { idOrder = aId[index] - bId[index]; break; }
    }
    return dateOrder || idOrder || aId.length - bId.length;
  };
  const describe = item => `${item.journal || '未知期刊'} · ${item.manuscript_id || item.original_id} · ${item.status || ''}${item.is_hidden ? ' · 已隐藏' : ''}`;
  const dateLabel = item => item.submission_sort_date ? `首次提交：${item.submitted_date}` : '日期未知';
  let current = null, selected = new Set(), baseline = '', busy = false, active = false;
  const fingerprint = () => JSON.stringify([...selected].sort());
  const dirty = () => active && fingerprint() !== baseline;
  const mayLeave = () => !busy && (!dirty() || window.confirm('有未保存改动，放弃这些改动？'));
  const ordered = () => [...selected].map(id => byId.get(id)).sort(compare);
  function feedback(text) { el('feedback').textContent = text; }
  function renderList() {
    const query = el('search').value.trim().toLocaleLowerCase();
    const visibility = el('visibility').value;
    el('list').replaceChildren();
    const matches = data.journeys.filter(journey => {
      const hidden = journey.hidden || journey.members.some(item => item.is_hidden);
      return (visibility === 'all' || (visibility === 'hidden' ? hidden : !hidden)) && journey.members.some(item => searchText(item).includes(query));
    });
    matches.forEach(journey => {
      const li = node('li'); li.setAttribute('aria-current', String(current?.id === journey.id));
      li.append(node('strong', journey.representative.title || journey.representative.manuscript_id), node('small', `${journey.member_count} 次投稿 · ${describe(journey.representative)}${journey.hidden ? ' · 整个历程已隐藏' : ''}`));
      const actions = node('div', undefined, 'journey-actions');
      actions.append(button('添加投稿', () => open(journey, true)), button('编辑历程', () => open(journey)));
      li.append(actions); el('list').append(li);
    });
    if (!matches.length) el('list').append(node('li', data.journeys.length ? '没有匹配的历程' : '尚无投稿历程，可新建历程。'));
  }
  function renderCandidates() {
    const query = el('candidate-search').value.trim().toLocaleLowerCase();
    el('candidates').replaceChildren();
    // Include removed existing members so they can be reselected without losing state.
    const candidates = new Map(data.candidates.map(item => [item.original_id, item]));
    current?.members.forEach(item => candidates.set(item.original_id, item));
    [...candidates.values()].filter(item => searchText(item).includes(query)).sort(compare).forEach(item => {
      const label = node('label'), input = node('input'), copy = node('span');
      input.type = 'checkbox'; input.value = item.original_id; input.checked = selected.has(item.original_id); input.disabled = busy;
      input.onchange = () => { input.checked ? selected.add(item.original_id) : selected.delete(item.original_id); renderEditor(); };
      copy.append(node('strong', item.title || item.manuscript_id), node('small', `${describe(item)} · ${dateLabel(item)}`));
      label.append(input, copy); el('candidates').append(label);
    });
    if (!el('candidates').children.length) el('candidates').append(node('p', '没有匹配的可添加投稿（已归组投稿不在候选中）。'));
  }
  function renderEditor() {
    const items = ordered();
    el('editor').hidden = !active;
    el('editor-heading').textContent = !active ? '选择已有历程，或新建历程' : current ? '编辑投稿历程' : '新建投稿历程';
    el('count').textContent = `原有 ${current?.member_count || 0} 次，保存后 ${items.length} 次`;
    el('members').replaceChildren();
    items.forEach((item, index) => {
      const li = node('li');
      const remove = button('移除', () => { selected.delete(item.original_id); renderEditor(); });
      remove.setAttribute('aria-label', `移除 ${item.title || item.manuscript_id}`); remove.disabled = busy;
      li.append(node('strong', `${index + 1}. ${item.title || item.manuscript_id}`), node('small', `${dateLabel(item)} · ${describe(item)}`), remove);
      el('members').append(li);
    });
    const representative = items.at(-1);
    el('preview').textContent = representative ? `${representative.title || representative.manuscript_id} · ${describe(representative)}` : '至少选择两次投稿。';
    el('save').disabled = busy || items.length < 2;
    el('cancel').disabled = busy; el('new').disabled = busy;
    el('dissolve').hidden = !current; el('dissolve').disabled = busy;
    el('reload').disabled = busy;
    renderCandidates();
  }
  function open(journey, focusCandidates = false) {
    if (!mayLeave()) return;
    current = journey; active = true;
    selected = new Set(journey?.members.map(item => item.original_id) || []); baseline = fingerprint();
    el('candidate-search').value = ''; el('reload').hidden = true; feedback('');
    renderList(); renderEditor();
    if (focusCandidates) el('candidate-search').focus();
    else { el('editor-heading').tabIndex = -1; el('editor-heading').focus(); }
  }
  async function mutate(dissolve = false) {
    if (busy || !active) return;
    if (dissolve && (!current || !window.confirm('解散历程？所有投稿恢复独立展示，不删除投稿及历史，原隐藏/标星状态保留。'))) return;
    busy = true; renderEditor(); feedback('正在保存…');
    try {
      const url = current ? `/api/journeys/${current.id}${dissolve ? '' : '/order'}` : '/api/journeys';
      const ids = ordered().map(item => item.original_id);
      const response = await fetch(url, {
        method: dissolve ? 'DELETE' : current ? 'PUT' : 'POST',
        headers: {'Content-Type': 'application/json'},
        ...(dissolve ? {} : {body: JSON.stringify(current ? {ordered_original_ids: ids, expected_revision: current.revision} : {member_original_ids: ids})})
      });
      const result = await response.json();
      if (!response.ok) {
        if (response.status === 409) { el('reload').hidden = false; throw new Error('历程已变更或投稿已归组。选择仍保留，请重新加载最新历程后重试。'); }
        throw new Error(typeof result.detail === 'string' ? result.detail : '保存失败，请重试。');
      }
      baseline = fingerprint(); window.location.assign(dissolve ? '/journeys' : `/journeys?edit=${result.journey.id}`);
    } catch (error) { feedback(error.message || '网络错误，选择已保留，请重试。'); }
    finally { busy = false; renderEditor(); }
  }
  el('search').oninput = renderList; el('visibility').onchange = renderList;
  el('candidate-search').oninput = renderCandidates;
  el('new').onclick = () => open(null);
  el('cancel').onclick = () => {
    if (!mayLeave()) return;
    current = null; active = false; selected.clear(); baseline = fingerprint(); feedback(''); el('reload').hidden = true;
    renderEditor(); renderList();
  };
  el('save').onclick = () => mutate(); el('dissolve').onclick = () => mutate(true);
  el('reload').onclick = () => { if (mayLeave()) { baseline = fingerprint(); window.location.assign(current ? `/journeys?edit=${current.id}` : '/journeys'); } };
  window.addEventListener('beforeunload', event => { if (dirty()) { event.preventDefault(); event.returnValue = ''; } });
  renderList(); renderEditor();
  if (data.selected_id) open(data.journeys.find(journey => journey.id === data.selected_id));
})();
