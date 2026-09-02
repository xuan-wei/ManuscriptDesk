const csrf = document.querySelector('meta[name="automation-csrf"]').content;
const $ = selector => document.querySelector(selector);
let jobs = [];
let refreshTimer = null;
let loading = false;
let smtpState = null;
const watchedRequests = new Map();
const tabs = [...document.querySelectorAll('[role="tab"]')];

function activateTab(tab, {focus = false} = {}) {
  if (!tab) return;
  tabs.forEach(item => {
    const selected = item === tab;
    item.classList.toggle('is-active', selected);
    item.setAttribute('aria-selected', selected ? 'true' : 'false');
    item.tabIndex = selected ? 0 : -1;
    const panel = document.getElementById(item.getAttribute('aria-controls'));
    if (panel) panel.hidden = !selected;
  });
  if (focus) tab.focus();
}

tabs.forEach((tab, index) => {
  tab.addEventListener('click', () => activateTab(tab));
  tab.addEventListener('keydown', event => {
    let target = null;
    if (event.key === 'ArrowRight') target = tabs[(index + 1) % tabs.length];
    if (event.key === 'ArrowLeft') target = tabs[(index - 1 + tabs.length) % tabs.length];
    if (event.key === 'Home') target = tabs[0];
    if (event.key === 'End') target = tabs[tabs.length - 1];
    if (target) {
      event.preventDefault();
      activateTab(target, {focus: true});
    }
  });
});

const phaseLabels = {
  queued: '排队中',
  claimed: '准备执行',
  running: '运行中',
  finished: '已完成',
  failed: '失败',
  cancelled: '已取消',
};
const scopeLabels = {
  journal: '单个期刊',
  starred: '标星',
  working: '进行中',
  all: '全部启用期刊',
};
const sourceLabels = {
  manual: '手动运行',
  schedule: '定时任务',
  retry: '重试运行',
};
const errorLabels = {
  browser_start_failed: '自动化浏览器无法启动',
  browser_tab_crashed: '浏览器会话崩溃',
  browser_timeout: '期刊页面响应超时',
  credentials_unavailable: '缺少期刊登录凭据',
  empty_result_anomaly: '期刊返回空结果，已保留上次数据',
  provider_login_failed: '期刊系统登录失败',
  provider_unavailable: '期刊网站暂时不可用',
  unsafe_provider_redirect: '检测到异常跳转，已中止',
  tracking_failed: '期刊页面处理失败',
  automation_failed: '任务执行失败',
  email_delivery_failed: '邮件发送失败',
  personal_smtp_unavailable: '个人 SMTP 不可用',
  system_smtp_unavailable: '系统发送通道不可用',
  no_enabled_journals: '没有启用的期刊',
};
const diagnosticLabels = {
  provider_login_rejected: '期刊系统拒绝登录，请检查账号密码',
  provider_login_response_timeout: '登录后页面响应超时，已重试一次',
  provider_login_page_unavailable: '登录页面暂时不可用，已重试一次',
  provider_author_dashboard_unavailable: '作者工作台暂时不可用，已重试一次',
  provider_author_menu_changed: '作者菜单结构发生变化',
  provider_folder_structure_changed: '稿件文件夹结构发生变化',
  provider_table_headers_changed: '稿件列表字段发生变化',
  provider_row_data_incomplete: '稿件列表缺少必要字段',
};
const resultLabels = {
  success: '成功',
  partial: '部分成功',
  no_changes: '无新变化',
  failed: '失败',
  running: '运行中',
};

function failureReason(failure) {
  const diagnostic = failure && diagnosticLabels[failure.diagnostic_code];
  if (diagnostic) return diagnostic;
  const code = failure && errorLabels[failure.error_code || failure.code];
  return code || '更新失败';
}

function failureLines(summary) {
  const failures = Array.isArray(summary.failures) ? summary.failures : [];
  return failures.map(item => {
    const name = item.journal_name || '未命名期刊';
    return `${name} — ${failureReason(item)}`;
  });
}

function formatClock(value) {
  if (!value) return '';
  const date = new Date(String(value).replace(' ', 'T') + 'Z');
  if (Number.isNaN(date.getTime())) return String(value);
  const pad = number => String(number).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function formatDuration(startedAt, finishedAt) {
  if (!startedAt) return '';
  const start = new Date(String(startedAt).replace(' ', 'T') + 'Z').getTime();
  const end = finishedAt ? new Date(String(finishedAt).replace(' ', 'T') + 'Z').getTime() : Date.now();
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return '';
  const seconds = Math.round((end - start) / 1000);
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}


function toast(message) {
  const element = $('#toast');
  element.textContent = message;
  element.hidden = false;
  clearTimeout(element._timer);
  element._timer = setTimeout(() => { element.hidden = true; }, 4500);
}

async function api(url, options = {}) {
  options.headers = {
    Accept: 'application/json',
    ...(options.body ? {'Content-Type': 'application/json', 'X-Automation-CSRF': csrf} : {}),
    ...(options.headers || {}),
  };
  const response = await fetch(url, options);
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`);
  return data;
}

function esc(value) {
  const element = document.createElement('div');
  element.textContent = value ?? '';
  return element.innerHTML;
}

function activeLabel(job) {
  const request = job.active_request;
  if (!request) return '';
  if (request.status === 'queued') return '排队中';
  if (request.status === 'claimed') return '准备执行';
  return job.kind === 'email' ? '发送中' : '更新中';
}

function jobCard(job) {
  const recipients = job.kind === 'email'
    ? `<div class="job-meta">${esc(scopeLabels[job.manuscript_scope] || job.manuscript_scope || '标星')} · ${job.delivery_policy === 'always' ? '每次发送' : '仅新变化'}<br>${(job.recipients || []).map(esc).join('<br>')}</div>`
    : '<div class="job-meta">全部启用期刊</div>';
  const active = Boolean(job.active_request);
  const currentStatus = active ? activeLabel(job) : resultLabels[job.last_status] || job.last_status || '尚未运行';
  const statusClass = active ? 'active' : job.last_status === 'success' ? 'success' : job.last_status === 'failed' ? 'failed' : '';
  return `<article class="job-card"${active ? ' aria-busy="true"' : ''}>
    <div><div class="job-title">${esc(job.name)}</div><div class="job-meta"><code>${esc(job.cron_expression)}</code> · ${esc(job.timezone)}</div></div>
    ${recipients}
    <div><span class="badge ${statusClass}">${esc(currentStatus)}</span><div class="job-meta">下次：${esc(job.next_run_time || '未安排')}</div></div>
    <div class="job-actions">
      <button class="button button-quiet" data-action="run" data-id="${job.id}" ${active ? 'disabled aria-busy="true"' : ''}>${active ? esc(activeLabel(job)) : '立即运行'}</button>
      <button class="button button-quiet" data-action="edit" data-id="${job.id}">编辑</button>
      <button class="button button-quiet" data-action="toggle" data-id="${job.id}">${job.enabled ? '停用' : '启用'}</button>
      <button class="button button-quiet" data-action="delete" data-id="${job.id}">删除</button>
    </div>
  </article>`;
}

function summaryText(run) {
  const summary = run.summary || {};
  if (summary.message_code === 'historical_summary_unavailable') return '历史记录仅保留通用结果';
  if (run.kind === 'email') {
    if (summary.result === 'no_changes') return '没有需要发送的新变化';
    if (summary.result === 'success') return `已发送 ${Number(summary.item_count || 0)} 项，${Number(summary.new_status_count || 0)} 项为新状态`;
    return summary.error_code ? '邮件任务失败，请检查发送通道' : '邮件任务已结束';
  }
  const attempted = Number(summary.attempted_journals || 0);
  const successful = Number(summary.successful_journals || 0);
  const failed = Number(summary.failed_journals || 0);
  if (summary.reason === 'no_enabled_journal') return '该期刊当前未启用';
  if (summary.reason === 'no_enabled_starred_journals') return '标星稿件所在期刊未启用';
  if (summary.reason === 'no_enabled_working_journals') return '进行中稿件所在期刊未启用';
  if (summary.result === 'success') return `更新完成：${successful}/${attempted} 个期刊成功`;
  if (summary.result === 'partial') return `部分完成：${successful}/${attempted} 个期刊成功，${failed} 个失败`;
  if (summary.result === 'no_changes') return attempted ? `已检查 ${attempted} 个期刊，没有发现变化` : '没有发现变化';
  const failures = Array.isArray(summary.failures) ? summary.failures : [];
  if (failures.length) {
    return `更新失败（${failures.length} 项）`;
  }
  return summary.error_code ? '更新失败，请稍后重试' : '更新任务已结束';
}

function terminalMessage(request, kind) {
  if (request.result === 'partial') return '部分期刊更新成功，请查看运行详情';
  if (request.result === 'no_changes') return kind === 'email' ? '没有需要发送的新变化' : '没有发现变化';
  if (request.result === 'success') return kind === 'email' ? '邮件已发送' : '更新完成';
  if (request.status === 'cancelled') return '任务已取消';
  return kind === 'email' ? '邮件发送失败，请查看运行详情' : '更新失败，请查看运行详情';
}

async function watchRequest(requestId, kind) {
  if (!requestId || watchedRequests.has(requestId)) return;
  watchedRequests.set(requestId, kind);
  try {
    while (watchedRequests.has(requestId)) {
      const data = await api(`/api/automation/requests/${requestId}`);
      if (!data.request.active) {
        watchedRequests.delete(requestId);
        toast(terminalMessage(data.request, kind));
        await load();
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 3000));
    }
  } catch (error) {
    if (watchedRequests.has(requestId)) {
      setTimeout(() => watchRequest(requestId, kind), 3000);
      watchedRequests.delete(requestId);
    }
  }
}

function scheduleRefresh(active) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => load().catch(error => toast(error.message)), active ? 3000 : 15000);
}

function renderSmtpState(smtp) {
  smtpState = smtp;
  const personal = smtp.personal || {};
  for (const [key, value] of Object.entries(personal)) {
    const element = $(`#smtp-form [name="${key}"]`);
    if (element && value != null) element.value = value;
  }
  $('#smtp-password-state').textContent = personal.password_configured ? '个人密码已配置' : '个人密码未配置';
  $('#smtp-password-state').className = `badge ${personal.password_configured ? 'success' : 'failed'}`;
  const sourceInput = document.querySelector(`input[name="smtp_source"][value="${smtp.source}"]`);
  if (sourceInput) sourceInput.checked = true;
  const system = smtp.system || {};
  $('#system-smtp-status').textContent = system.message || '系统发送通道状态未知';
  $('#system-smtp-status').className = `smtp-source-status ${system.health || ''}`;
  $('#system-smtp-tested-at').textContent = system.tested_at ? `最近测试：${system.tested_at}` : '';
  const systemRadio = document.querySelector('input[name="smtp_source"][value="system"]');
  systemRadio.disabled = !system.selectable && smtp.source !== 'system';
  $('#system-smtp-card').classList.toggle('is-disabled', systemRadio.disabled);
  $('#personal-smtp-panel').hidden = smtp.source === 'system';
  const warning = $('#smtp-source-warning');
  warning.hidden = !(smtp.source === 'system' && !system.selectable);
  warning.textContent = warning.hidden ? '' : '系统发送通道当前不可用，邮件任务会被阻止。请等待管理员重新测试，或切换回个人 SMTP。';
}

function runRow(run) {
  const summary = run.summary || {};
  const label = resultLabels[run.status] || phaseLabels[run.status] || run.status;
  const statusClass = run.status === 'success' ? 'success' : run.status === 'failed' ? 'failed' : run.status === 'partial' ? 'active' : '';
  const metaParts = [];
  if (run.source) metaParts.push(sourceLabels[run.source] || run.source);
  if (run.kind !== 'email' && summary.update_scope) metaParts.push(scopeLabels[summary.update_scope] || summary.update_scope);
  const startText = formatClock(run.started_at);
  if (startText) metaParts.push(`开始 ${startText}`);
  if (run.finished_at) {
    const endText = formatClock(run.finished_at);
    if (endText) metaParts.push(`结束 ${endText}`);
  } else if (run.status === 'running') {
    metaParts.push('运行中');
  }
  const duration = formatDuration(run.started_at, run.finished_at);
  if (duration) metaParts.push(`耗时 ${duration}`);
  const metaHtml = metaParts.length ? `<div class="run-meta">${esc(metaParts.join(' · '))}</div>` : '';
  const lines = failureLines(summary);
  const detailHtml = lines.length
    ? `<ul class="run-failures">${lines.map(line => `<li>${esc(line)}</li>`).join('')}</ul>`
    : '';
  return `<tr><td class="run-task">${esc(run.job_name)}</td><td>${esc(run.kind === 'email' ? '邮件' : '更新')}</td><td class="run-start">${esc(formatClock(run.started_at) || run.started_at)}</td><td><span class="badge ${statusClass}">${esc(label)}</span></td><td class="run-summary"><span>${esc(summaryText(run))}</span>${metaHtml}${detailHtml}</td></tr>`;
}

async function load() {
  if (loading) return;
  loading = true;
  try {
    const [data, smtp, runs] = await Promise.all([
      api('/api/automation/jobs'),
      api('/api/automation/smtp'),
      api('/api/automation/runs'),
    ]);
    jobs = data.jobs;
    const updates = jobs.filter(job => job.kind === 'update');
    const emails = jobs.filter(job => job.kind === 'email');
    const activeJobs = jobs.filter(job => job.active_request);
    $('#scheduler-state').textContent = data.scheduler.is_running ? '运行中' : '已停止';
    $('#update-count').textContent = updates.length;
    $('#email-count').textContent = emails.length;
    $('#update-state').textContent = data.active_update ? activeLabel({kind: 'update', active_request: data.active_update}) : '空闲';
    $('#update-jobs').innerHTML = updates.length ? updates.map(jobCard).join('') : '尚无更新任务';
    $('#email-jobs').innerHTML = emails.length ? emails.map(jobCard).join('') : '尚无邮件任务';
    renderSmtpState(smtp);
    const latestRun = runs.runs[0] || null;
    const history = runs.runs.slice(1);
    $('#latest-run').innerHTML = latestRun ? runRow(latestRun) : '<tr><td colspan="5">尚无运行记录</td></tr>';
    $('#run-history').innerHTML = history.length ? history.map(runRow).join('') : '<tr><td colspan="5">暂无历史记录</td></tr>';
    for (const job of activeJobs) watchRequest(job.active_request.id, job.kind);
    scheduleRefresh(activeJobs.length > 0);
  } finally {
    loading = false;
  }
}

function openJob(kind, job = null) {
  const form = $('#job-form');
  form.reset();
  form.id.value = job?.id || '';
  form.kind.value = kind;
  form.name.value = job?.name || '';
  form.cron_expression.value = job?.cron_expression || (kind === 'update' ? '0 7 * * *' : '30 8 * * 1-5');
  const timezone = job?.timezone || 'Asia/Shanghai';
  form.timezone.querySelectorAll('[data-saved-timezone]').forEach(option => option.remove());
  if (![...form.timezone.options].some(option => option.value === timezone)) {
    const option = new Option(`${timezone}（当前已保存）`, timezone);
    option.dataset.savedTimezone = 'true';
    form.timezone.add(option);
  }
  form.timezone.value = timezone;
  form.enabled.checked = job ? Boolean(job.enabled) : true;
  $('#email-fields').hidden = kind !== 'email';
  if (kind === 'email') {
    form.manuscript_scope.value = job?.manuscript_scope || 'starred';
    form.delivery_policy.value = job?.delivery_policy || 'changes_only';
    form.recipients.value = (job?.recipients || []).join('\n');
  }
  $('#job-dialog-title').textContent = `${job ? '编辑' : '新增'}${kind === 'email' ? '邮件' : '更新'}任务`;
  $('#cron-preview').textContent = '';
  $('#job-dialog').showModal();
}

document.addEventListener('click', async event => {
  const close = event.target.closest('[data-close-dialog]');
  if (close) {
    document.getElementById(close.dataset.closeDialog)?.close();
    return;
  }
  const newJob = event.target.closest('[data-new-job]');
  if (newJob) return openJob(newJob.dataset.newJob);
  const button = event.target.closest('[data-action]');
  if (!button || button.disabled) return;
  const job = jobs.find(item => item.id === Number(button.dataset.id));
  try {
    if (button.dataset.action === 'edit') return openJob(job.kind, job);
    if (button.dataset.action === 'delete') {
      if (!confirm(`删除任务“${job.name}”？`)) return;
      await api(`/api/automation/jobs/${job.id}`, {method: 'DELETE', body: '{}'});
      toast('任务已删除');
    }
    if (button.dataset.action === 'toggle') {
      await api(`/api/automation/jobs/${job.id}/enabled`, {method: 'PATCH', body: JSON.stringify({enabled: !job.enabled})});
      toast(job.enabled ? '任务已停用' : '任务已启用');
    }
    if (button.dataset.action === 'run') {
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      button.textContent = '正在加入队列…';
      const result = await api(`/api/automation/jobs/${job.id}/run`, {method: 'POST', body: '{}'});
      toast(result.status === 'already_active' ? '该任务已在进行中' : '任务已加入队列');
      watchRequest(result.request_id, job.kind);
    }
    await load();
  } catch (error) {
    button.disabled = false;
    button.removeAttribute('aria-busy');
    toast(error.message);
  }
});

$('#preview-cron').addEventListener('click', async () => {
  const form = $('#job-form');
  try {
    const data = await api('/api/automation/cron-preview', {method: 'POST', body: JSON.stringify({cron_expression: form.cron_expression.value, timezone: form.timezone.value})});
    $('#cron-preview').innerHTML = `<strong>接下来五次：</strong><br>${data.next_runs.map(esc).join('<br>')}`;
  } catch (error) { toast(error.message); }
});

$('#job-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const body = {kind: form.kind.value, name: form.name.value, cron_expression: form.cron_expression.value, timezone: form.timezone.value, enabled: form.enabled.checked};
  if (body.kind === 'email') {
    body.manuscript_scope = form.manuscript_scope.value;
    body.delivery_policy = form.delivery_policy.value;
    body.recipients = form.recipients.value.split(/\n|,/).map(value => value.trim()).filter(Boolean);
  }
  try {
    const id = form.id.value;
    await api(id ? `/api/automation/jobs/${id}` : '/api/automation/jobs', {method: id ? 'PUT' : 'POST', body: JSON.stringify(body)});
    $('#job-dialog').close();
    toast('任务已保存');
    await load();
  } catch (error) { toast(error.message); }
});

document.querySelectorAll('input[name="smtp_source"]').forEach(input => {
  input.addEventListener('change', async event => {
    if (!event.target.checked || !smtpState || event.target.value === smtpState.source) return;
    try {
      await api('/api/automation/smtp/source', {method: 'PUT', body: JSON.stringify({source: event.target.value})});
      toast(event.target.value === 'system' ? '已切换到系统发送通道' : '已切换到个人 SMTP');
      await load();
    } catch (error) {
      renderSmtpState(smtpState);
      toast(error.message);
    }
  });
});

$('#smtp-form').addEventListener('submit', async event => {
  event.preventDefault();
  const body = Object.fromEntries(new FormData(event.currentTarget).entries());
  body.port = Number(body.port);
  body.connect_timeout_seconds = Number(body.connect_timeout_seconds);
  try {
    await api('/api/automation/smtp', {method: 'PUT', body: JSON.stringify(body)});
    toast('SMTP 设置已保存');
    await load();
  } catch (error) { toast(error.message); }
});

$('#replace-password').onclick = () => $('#password-dialog').showModal();
$('#password-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  if (form.password.value !== form.confirm_password.value) return toast('两次密码不一致');
  try {
    await api('/api/automation/smtp/password', {method: 'PUT', body: JSON.stringify({password: form.password.value})});
    form.reset();
    $('#password-dialog').close();
    toast('SMTP 密码已安全替换');
    await load();
  } catch (error) { toast(error.message); }
});

$('#test-smtp').onclick = async () => {
  try {
    const result = await api('/api/automation/smtp/test', {method: 'POST', body: '{}'});
    toast(result.message || '个人 SMTP 配置已就绪');
  } catch (error) { toast(error.message); }
};
$('#reload-jobs').onclick = async () => {
  try {
    await api('/api/automation/reload', {method: 'POST', body: '{}'});
    toast('任务已重新加载');
    await load();
  } catch (error) { toast(error.message); }
};
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) load().catch(error => toast(error.message));
});
load().catch(error => toast(error.message));
