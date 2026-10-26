(() => {
  const monthIndex = {jan:1,feb:2,mar:3,apr:4,may:5,jun:6,jul:7,aug:8,sep:9,oct:10,nov:11,dec:12};
  const $ = id => document.getElementById(id);
  const cards = () => [...document.querySelectorAll('.manuscript-card')];
  let integration = null;

  function validDateKey(year, month, day) {
    const d = new Date(Date.UTC(year, month - 1, day));
    if (d.getUTCFullYear() !== year || d.getUTCMonth() !== month - 1 || d.getUTCDate() !== day) return null;
    return year * 10000 + month * 100 + day;
  }

  function parseDateKey(value) {
    value = String(value || '').trim();
    if (!value) return null;
    let match = value.match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T].*)?$/);
    if (match) return validDateKey(Number(match[1]), Number(match[2]), Number(match[3]));
    match = value.match(/^(\d{1,2})-([A-Za-z]{3})-(\d{2}|\d{4})$/);
    if (match) {
      let year = Number(match[3]);
      if (year < 100) year += year >= 70 ? 1900 : 2000;
      return validDateKey(year, monthIndex[match[2].toLowerCase()] || 0, Number(match[1]));
    }
    match = value.match(/^([A-Za-z]{3})\s+(\d{1,2}),\s*(\d{4})$/);
    if (match) return validDateKey(Number(match[3]), monthIndex[match[1].toLowerCase()] || 0, Number(match[2]));
    return null;
  }

  function normalizeProvider(value) {
    value = String(value || '').trim().toLowerCase();
    return value || '__unknown__';
  }

  function normalizeEditor(value) {
    value = String(value || '').trim();
    if (!value || /^not assigned$/i.test(value)) return '__unassigned__';
    return value;
  }

  function recentActivity(card) {
    return parseDateKey(card.dataset.decisionDate) || parseDateKey(card.dataset.statusDate) || parseDateKey(card.dataset.recordedAt);
  }

  function memberData(member) {
    const data = {
      journal: member.journal || '',
      provider: normalizeProvider(member.provider),
      status: member.status || '',
      detail: member.detailed_status || '',
      title: `${member.title || ''} ${member.manuscript_id || ''} ${member.original_id || ''}`.toLowerCase(),
      changed: !!member.unread && !member.is_hidden,
      starred: !!member.starred,
      round: String(Math.max(0, Number(member.version_number) - 1 || 0)),
      editor: normalizeEditor(member.SE_or_DE || member.editor),
      submitted: parseDateKey(member.submitted_date),
      decision: parseDateKey(member.decision_date),
      statusDate: parseDateKey(member.status_date),
      recorded: parseDateKey(member.recorded_at)
    };
    data.activity = data.decision || data.statusDate || data.recorded;
    return data;
  }

  function cardData(card) {
    let members = [];
    try { members = JSON.parse(card.dataset.journeyMembers || '[]').map(memberData); } catch (_) { members = []; }
    return {
      card,
      journal: card.dataset.journal || '',
      provider: normalizeProvider(card.dataset.provider),
      status: card.dataset.status || '',
      detail: card.dataset.detailedStatus || '',
      title: `${card.dataset.title || ''} ${card.dataset.manuscriptId || ''} ${card.dataset.originalId || ''}`.toLowerCase(),
      changed: card.dataset.statusChanged === 'true',
      starred: card.dataset.starred === 'true',
      round: String(Math.max(0, Number(card.dataset.round) || 0)),
      editor: normalizeEditor(card.dataset.editor),
      submitted: parseDateKey(card.dataset.submittedDate),
      decision: parseDateKey(card.dataset.decisionDate),
      statusDate: parseDateKey(card.dataset.statusDate),
      recorded: parseDateKey(card.dataset.recordedAt),
      activity: recentActivity(card),
      members
    };
  }

  function statusMatches(data, filter) {
    if (!filter) return true;
    const detail = data.detail.toLowerCase();
    const status = data.status;
    const checks = {
      accept: () => status === 'Accept', revision: () => status === 'Revision',
      minor: () => detail.includes('minor revision'), major: () => detail.includes('major revision'),
      reject_and_resubmit: () => status === 'Reject and Resubmit', reject: () => status === 'Reject',
      'reject-only': () => status === 'Reject' && !detail.includes('resubmit'), desk: () => detail.includes('desk reject'),
      draft: () => status === 'Draft', under_review: () => status === 'Under Review',
      awaiting: () => detail.includes('awaiting'), review: () => detail.includes('under review'),
      other: () => status === 'Under Review' && !detail.includes('awaiting') && !detail.includes('under review')
    };
    return checks[filter] ? checks[filter]() : detail.includes(filter.toLowerCase());
  }

  function stageMatches(data, stage) {
    if (!stage) return true;
    if (stage === 'ongoing') return data.status === 'Under Review' || data.status === 'Revision' || data.changed;
    if (stage === 'review') return data.status === 'Under Review';
    if (stage === 'revision') return data.status === 'Revision';
    if (stage === 'accept') return data.status === 'Accept';
    if (stage === 'decision') return data.status === 'Reject' || data.status === 'Reject and Resubmit';
    return true;
  }

  function value(id) { return $(id)?.value || ''; }
  function selectedDate(data, type) {
    return {submitted:data.submitted,activity:data.activity,decision:data.decision,status:data.statusDate,recorded:data.recorded}[type] || null;
  }

  function currentState(options) {
    return {
      journal:value('journalFilter'), provider:value('providerFilter'), status:value('statusFilter'),
      title:value('titleFilter').trim().toLowerCase(), change:value('changeFilter'), detail:value('detailedStatusFilter'),
      editor:value('editorFilter'), round:value('roundFilter'), submittedYear:value('submittedYearFilter'),
      activityYear:value('activityYearFilter'), dateType:value('dateTypeFilter') || 'submitted',
      dateFrom:parseDateKey(value('dateFromFilter')), dateTo:parseDateKey(value('dateToFilter')),
      starredOnly:!!options.starredOnly, stage:options.stage || '', filterScope:options.filterScope || 'latest'
    };
  }

  function matches(data, state) {
    const submittedYear = data.submitted ? String(Math.floor(data.submitted / 10000)) : '__unknown__';
    const activityYear = data.activity ? String(Math.floor(data.activity / 10000)) : '__unknown__';
    const date = selectedDate(data, state.dateType);
    return (!state.journal || data.journal === state.journal)
      && (!state.provider || data.provider === state.provider)
      && statusMatches(data, state.status)
      && (!state.title || data.title.includes(state.title))
      && (!state.change || (state.change === 'changed' ? data.changed : !data.changed))
      && (!state.detail || data.detail === state.detail)
      && (!state.editor || data.editor === state.editor)
      && (!state.round || data.round === state.round)
      && (!state.submittedYear || submittedYear === state.submittedYear)
      && (!state.activityYear || activityYear === state.activityYear)
      && (!state.dateFrom || (date !== null && date >= state.dateFrom))
      && (!state.dateTo || (date !== null && date <= state.dateTo))
      && (!state.starredOnly || data.starred)
      && stageMatches(data, state.stage);
  }

  function updateStats(visible) {
    $('totalCount').textContent = visible.length;
    $('acceptCount').textContent = visible.filter(x => x.status === 'Accept').length;
    $('reviewCount').textContent = visible.filter(x => x.status === 'Under Review').length;
    $('revisionCount').textContent = visible.filter(x => x.status === 'Revision').length;
    $('newCount').textContent = visible.filter(x => x.changed).length;
  }

  function advancedCount(state) {
    return ['change','detail','editor','round','submittedYear','activityYear','dateFrom','dateTo'].filter(k => state[k]).length + (state.dateType !== 'submitted' ? 1 : 0);
  }

  function applyFilters(options = {}) {
    const state = currentState(options);
    const visible = [];
    cards().map(cardData).forEach(data => {
      const candidates = state.filterScope === 'any' && data.members.length > 1 ? data.members : [data];
      const show = candidates.some(candidate => matches(candidate, state));
      data.card.hidden = !show;
      data.card.style.display = show ? 'block' : 'none';
      if (show) visible.push(data);
    });
    updateStats(visible);
    const emptyState = $('manuscriptEmptyState');
    if (emptyState) emptyState.hidden = visible.length !== 0;
    const count = advancedCount(state), badge = $('advancedFilterCount');
    if (badge) { badge.textContent = count; badge.hidden = count === 0; }
  }

  function appendOptions(select, values, label, unknownLabel) {
    values.sort((a,b) => String(a).localeCompare(String(b), 'zh-CN', {numeric:true,sensitivity:'base'}));
    values.forEach(v => { const option=document.createElement('option');option.value=v;option.textContent=v==='__unknown__'?unknownLabel:(label?label(v):v);select.appendChild(option); });
  }

  function populate() {
    const data = cards().map(cardData);
    const unique = fn => [...new Set(data.map(fn))];
    appendOptions($('detailedStatusFilter'), unique(x=>x.detail).filter(Boolean));
    appendOptions($('editorFilter'), unique(x=>x.editor), null, '未提供 / 未分配');
    appendOptions($('roundFilter'), unique(x=>x.round), x=>`V${x}`);
    const sy=unique(x=>x.submitted?String(Math.floor(x.submitted/10000)):'__unknown__');
    const ay=unique(x=>x.activity?String(Math.floor(x.activity/10000)):'__unknown__');
    appendOptions($('submittedYearFilter'),sy,null,'日期未知');
    appendOptions($('activityYearFilter'),ay,null,'日期未知');
    if (data.some(x=>x.provider==='__unknown__')) { const o=document.createElement('option');o.value='__unknown__';o.textContent='未知投稿系统';$('providerFilter').appendChild(o); }
  }

  function reset() {
    ['journalFilter','providerFilter','statusFilter','titleFilter','changeFilter','detailedStatusFilter','editorFilter','roundFilter','submittedYearFilter','activityYearFilter','dateFromFilter','dateToFilter'].forEach(id => { if ($(id)) $(id).value=''; });
    $('dateTypeFilter').value='submitted';
    integration.setStage(''); integration.setStarredOnly(false); integration.setFilterScope('latest');
    document.querySelectorAll('.stage-filter').forEach(b=>b.classList.toggle('is-active',!b.dataset.stage));
    document.querySelectorAll('.filter-scope-option').forEach(b=>{const active=b.dataset.filterScope==='latest';b.classList.toggle('is-active',active);b.setAttribute('aria-pressed',String(active));});
    const url=new URL(location.href);url.searchParams.delete('view');history.replaceState({starredView:false},'',url);
    window.applyFilters();
  }

  function initialize(api) {
    integration=api;
    populate();
    document.querySelectorAll('.filter-section select,.filter-section input').forEach(el=>el.addEventListener(el.id==='titleFilter'?'input':'change',()=>window.applyFilters()));
    $('starToggleBtn').addEventListener('click',()=>window.toggleStarFilter());
    $('resetFiltersBtn').addEventListener('click',reset);
    $('emptyResetFiltersBtn')?.addEventListener('click',reset);
    $('advancedFilterToggle').addEventListener('click',()=>{const panel=$('advancedFilters'),open=panel.hidden;panel.hidden=!open;$('advancedFilterToggle').setAttribute('aria-expanded',String(open));});
    document.querySelectorAll('.stage-filter').forEach(button=>button.addEventListener('click',()=>{integration.setStage(button.dataset.stage);document.querySelectorAll('.stage-filter').forEach(b=>b.classList.toggle('is-active',b===button));window.applyFilters();}));
    document.querySelectorAll('.filter-scope-option').forEach(button=>button.addEventListener('click',()=>{integration.setFilterScope(button.dataset.filterScope);document.querySelectorAll('.filter-scope-option').forEach(b=>{const active=b===button;b.classList.toggle('is-active',active);b.setAttribute('aria-pressed',String(active));});window.applyFilters();}));
    window.addEventListener('popstate',()=>{integration.setStarredOnly(new URL(location.href).searchParams.get('view')==='starred');window.applyFilters();});
  }

  window.ManuscriptFilters={initialize,applyFilters,parseDateKey,normalizeProvider,normalizeEditor,matches,cardData};
})();
