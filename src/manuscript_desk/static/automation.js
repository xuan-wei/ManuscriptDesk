const csrf = document.querySelector('meta[name="automation-csrf"]').content;
const $ = selector => document.querySelector(selector);
let jobs = [];
let runSummaries = [];
let refreshTimer = null;
let loading = false;
const watchedRequests = new Map();

const phaseLabels = {
  queued: '排队中',
  claimed: '准备执行',
  running: '运行中',
  finished: '已完成',
  failed: '失败',
  cancelled: '已取消',
};
const scopeLabels = {
  starred: '标星',
  working: '进行中',
  all: '全部',
};
const resultLabels = {
  success: '成功',
  partial: '部分成功',
  no_changes: '无新变化',
  failed: '失败',
  running: '运行中',
};

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
    for (const [key, value] of Object.entries(smtp)) {
      const element = $(`#smtp-form [name="${key}"]`);
      if (element && value != null) element.value = value;
    }
    $('#smtp-password-state').textContent = smtp.password_configured ? '密码已配置' : '密码未配置';
    $('#smtp-password-state').className = `badge ${smtp.password_configured ? 'success' : 'failed'}`;
    runSummaries = runs.runs.map(run => String(run.summary_json ?? ''));
    $('#run-history').innerHTML = runs.runs.length ? runs.runs.map((run, index) => {
      const summary = runSummaries[index];
      const limit = 80;
      const short = summary.length > limit ? `${summary.slice(0, limit).trimEnd()}…` : summary;
      const detail = summary.length > limit
        ? ` <button class="run-summary-link" type="button" data-run-summary="${index}">查看详情</button>`
        : '';
      const label = resultLabels[run.status] || phaseLabels[run.status] || run.status;
      const statusClass = run.status === 'success' ? 'success' : run.status === 'failed' ? 'failed' : run.status === 'partial' ? 'active' : '';
      return `<tr><td class="run-task">${esc(run.job_name)}</td><td>${esc(run.kind === 'email' ? '邮件' : '更新')}</td><td class="run-start">${esc(run.started_at)}</td><td><span class="badge ${statusClass}">${esc(label)}</span></td><td class="run-summary"><span>${esc(short)}</span>${detail}</td></tr>`;
    }).join('') : '<tr><td colspan="5">暂无记录</td></tr>';
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
  form.timezone.value = job?.timezone || 'Asia/Shanghai';
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
  const summaryButton = event.target.closest('[data-run-summary]');
  if (summaryButton) {
    $('#run-summary-detail').textContent = runSummaries[Number(summaryButton.dataset.runSummary)] ?? '';
    $('#run-summary-dialog').showModal();
    return;
  }
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
  const recipient = prompt('测试邮件收件地址');
  if (!recipient) return;
  try {
    await api('/api/automation/smtp/test', {method: 'POST', body: JSON.stringify({recipient})});
    toast('SMTP 配置已就绪');
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
