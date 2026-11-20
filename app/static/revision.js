import {escape, heading, interval, table, time} from './tickets.js';

const CHANGE_LABEL = {KEPT: '沿用旧时段', MOVED: '改期', NEW: '新增工序', DEFERRED: '暂缓落位', RECOVERED: '恢复落位'};
const CHANGE_CLASS = {KEPT: 'kept', MOVED: 'moved', NEW: 'new', DEFERRED: 'deferred', RECOVERED: 'new'};
const STAGE_LABEL = {DRAFT: '草稿', PASSED: '核对通过', PUBLISHED: '已发布', FAILED: '核对失败', PREVIEW: '试排预览'};

function reasonText(code) {
  if (code.startsWith('BLOCKED:')) return `避让占用 ${code.slice(8).split(',').join('、')}`;
  if (code.startsWith('CHAIN:')) return `前序链连带（${code.slice(6)}）`;
  if (code.startsWith('DEPENDENCY:')) return `前置未落位（${code.slice(11)}）`;
  const map = {
    SELF_REVISED: '本工序工单已修订',
    CHAIN_SHIFT: '前置工序改期连带推移',
    TECH_SWITCH: '原技师时段被占用，改派技师',
    RESOURCE_CONTENTION: '共享舞台或技师时段重排',
    LOCK_INVALID: '旧时段在修订后日历/预约下失效',
    UNCHANGED_LOCKED_SLOT: '工单未修订，锁定沿用旧时段',
    SELF_CHANGED_SLOT_STABLE: '字段虽修订，原时段仍可落位',
    NEW_ORDER: '修订新增工序',
    REPLAN: '受影响面内统一重排',
    SEED: '直接修订点',
  };
  if (code === 'STAGE') return '共享舞台时段相交连带';
  if (code === 'TECH') return '共享技师时段相交连带';
  return map[code] || code;
}

export function describeChange(change) {
  return CHANGE_LABEL[change] || change;
}

export function describeReason(code) {
  if (code === 'STAGE' || code === 'TECH') return reasonText(code);
  const prefix = code.split(':')[0];
  if (prefix === 'STAGE') return `共享舞台连带（${code.slice(6)}）`;
  if (prefix === 'TECH') return `共享技师连带（${code.slice(5)}）`;
  return reasonText(code);
}

function slotText(slot) {
  if (!slot) return '—';
  return `${interval([slot.start, slot.end])} · ${slot.tech}`;
}

function badge(change) {
  return `<span class="badge ${CHANGE_CLASS[change] || ''}">${escape(describeChange(change))}</span>`;
}

function verifyBanner(rev) {
  const verify = rev.verify;
  const passed = verify.status === 'PASSED';
  const total = verify.checks.length;
  const failed = verify.checks.filter(check => !check.passed).length;
  return `<div class="rev-banner ${passed ? 'ok' : 'bad'}"><strong>${escape(STAGE_LABEL[rev.status] || rev.status)}</strong>
    <span>${passed ? `${total} 项核对全部通过，可发布切换` : `${failed} 项核对未通过，旧版联单继续生效`}</span>
    <small>${escape(verify.late_minutes.before)} → ${escape(verify.late_minutes.after)} 超时分钟（修订前 → 修订后）</small></div>`;
}

function checksSection(rev) {
  const rows = rev.verify.checks.map(check => [
    check.name,
    `<span class="badge ${check.passed ? 'pass' : 'fail'}">${check.passed ? '通过' : '未过'}</span>`,
    JSON.stringify(check.detail),
  ]);
  return heading('发布前核对清单') + table(['核对项', '结论', '依据明细'], rows);
}

function diffSection(rev) {
  const changed = rev.diff.changed.map(item => [
    item.id,
    Object.values(item.fields).map(f => `${escape(f.label)}：${escape(JSON.stringify(f.before))} → ${escape(JSON.stringify(f.after))}`).join('<br>'),
  ]);
  return heading('工单字段差异')
    + table(['工序', '字段变更（旧 → 新）'], changed)
    + `<h3>新增工序</h3><p>${rev.diff.added.map(escape).join('、') || '无'}</p>`
    + `<h3>删除工序</h3><p>${rev.diff.removed.map(escape).join('、') || '无'}</p>`;
}

function comparisonSection(rev, result, activeJob) {
  const rows = result.schedule.jobs.map(job => {
    const ann = rev.annotations[job.id] || {change: 'KEPT', reasons: ['UNCHANGED_LOCKED_SLOT'], old: null, new: null};
    const oldSlot = ann.old ? slotText(ann.old) : (ann.change === 'NEW' ? '无（新增）' : '—');
    const newSlot = job.status === 'scheduled'
      ? slotText({start: job.start, end: job.end, tech: job.tech})
      : escape(job.code);
    const reasons = (ann.reasons || []).map(describeReason).join('、') || '—';
    return [
      `<button class="link-job" data-job="${escape(job.id)}" aria-pressed="${job.id === activeJob}">${escape(job.id)}</button>`,
      badge(ann.change),
      oldSlot,
      newSlot,
      reasons,
    ];
  });
  return heading('逐项联单对照（旧时段 → 新时段）')
    + table(['工序', '结论', '旧版时段/技师', '修订后时段/技师', '改期或沿用依据'], rows);
}

function impactSection(rev) {
  if (rev.impact_meta.calendar) {
    return heading('受影响面') + '<p>剧场日历本身已修订，全部已落位工序的资源依据失效，整批纳入重排。</p>';
  }
  const traces = rev.impact_meta.traces || {};
  const rows = rev.impacted.map(id => [
    id,
    (traces[id] || []).map(describeReason).join('、'),
  ]);
  return heading('受影响面与连带证据') + table(['工序', '纳入原因'], rows);
}

function selectedTicket(rev, result, jobId) {
  const job = result.schedule.jobs.find(row => row.id === jobId);
  if (!job) return heading('工序票签') + '<p>选择工序查看旧时段与改期依据</p>';
  const ann = rev.annotations[job.id] || {change: 'KEPT', reasons: [], old: null, new: null};
  const techName = result.calendars.technicians[job.tech]?.name;
  return heading('工序修订票签')
    + `<div class="ticket ${ann.change === 'KEPT' ? '' : 'bad'}">
        <small>${escape(job.id)} · 工单第 ${job.line} 行</small>
        <strong>${escape(job.title)}</strong>
        <p>${badge(ann.change)}</p>
        <small>旧版时段</small><p><time>${ann.old ? interval([ann.old.start, ann.old.end]) : '—'}</time> · ${escape(ann.old?.tech || '—')}</p>
        <small>修订后时段</small><p><time>${job.start == null ? '未落位' : interval([job.start, job.end])}</time> · ${escape(techName || job.role)}</p>
        <small>放行 / 截止</small><p><time>${time(job.ready ?? job.release)}</time> / <time>${time(job.deadline)}</time></p>
        <small>依据</small><p>${(ann.reasons || []).map(describeReason).join('、') || '—'}</p>
      </div>`;
}

function actionsSection(rev, state) {
  const baselineId = state.archive?.id;
  if (!baselineId) {
    return heading('修订操作') + '<p class="bad-text">当前没有已归档联单，请先在「转场排程」确认归档基线。</p>';
  }
  const id = rev?.id;
  const isPreview = rev?.status === 'PREVIEW';
  const canDraft = true;
  const canVerify = id && !isPreview && rev.status !== 'PUBLISHED';
  const canPublish = id && !isPreview && (rev.status === 'PASSED' || rev.status === 'PUBLISHED');
  return heading('修订操作')
    + `<div class="archived">
        <small>基线联单</small><code>${escape(baselineId)}</code>
        ${id ? `<small>修订联单（${escape(STAGE_LABEL[rev.status] || rev.status)}）</small><code>${escape(id)}</code>` : '<p>先试排预览，再生成草稿联单。</p>'}
        <div class="rev-actions">
          <button id="rev-preview" class="primary" type="button">试排预览（不落库）</button>
          <button id="rev-draft" type="button" ${canDraft ? '' : 'disabled'}>生成修订草稿</button>
          <button id="rev-verify" type="button" ${canVerify ? '' : 'disabled'}>重新核对</button>
          <button id="rev-publish" type="button" ${canPublish ? '' : 'disabled'}>${rev?.status === 'PUBLISHED' ? '已发布（幂等）' : '发布切换'}</button>
          ${id && !isPreview ? `<a class="file" href="/api/revisions/${encodeURIComponent(id)}/export">下载修订联单</a>` : ''}
        </div>
      </div>`;
}

export function revisionView(data, state) {
  const rev = state.rev?.revision
    ? {id: state.rev.id, status: state.rev.status, ...state.rev.revision}
    : state.rev;
  if (!rev) {
    return [
      heading('修订联单') + '<p>选择修订批次后试排预览。系统只重排受影响工序及其共享舞台、技师与前序链连带工序，其余班组锁定旧时段。</p>',
      actionsSection(null, state),
    ];
  }
  const result = state.rev.result || data;
  const board = verifyBanner(rev) + checksSection(rev) + diffSection(rev)
    + comparisonSection(rev, result, state.job);
  const evidence = impactSection(rev) + selectedTicket(rev, result, state.job) + actionsSection(rev, state);
  return [board, evidence];
}
