import {store} from '../stores/business.js';
import {esc,json} from '../utils/html.js';
import {table} from '../components/common/table.js';

const example={sensors:[{sensor_id:1,zone:'北场',device_type:'pump'},{sensor_id:2,zone:'南场',device_type:'motor'}],readings:[{sensor_id:1,value:80,recorded_at:'2026-11-17T09:00:00'},{sensor_id:2,value:40,recorded_at:'2026-11-17T09:05:00'}]};

const MODES={json:'JSON 快照',rows:'逐行存储'};
const BATCH_STATUSES={not_started:'未开始',pending:'待推进',running:'回填中',paused:'已暂停',failed:'批次失败',completed:'已核齐',rolled_back:'已回退'};

function modeBadge(mode){return `<span class="status ${mode==='rows'?'completed':''}">${MODES[mode]||mode}</span>`;}

function progressBar(backfill){
 let pct=Number(backfill.progress||0);
 return `<div class="progress"><span style="width:${Math.min(100,Math.max(0,pct))}%"></span></div><small class="mono">${backfill.cursor}/${backfill.source_total} 行 · ${esc(BATCH_STATUSES[backfill.status]||backfill.status)} · 批次 ${backfill.batches_run||0}</small>`;
}

function sourceView(source){
 if(!source)return '<p class="muted">点「读源」逐行对照 JSON 源与逐行表。</p>';
 let rows=source.rows.readings;
 return `<div class="source-compare"><div><small>JSON 源</small><b class="mono">${source.source.total} 行</b><code class="checksum">${esc(source.source.checksum)}</code></div><div><small>逐行表</small><b class="mono">${source.rows.total} 行</b><code class="checksum">${esc(source.rows.checksum)}</code></div><div class="${source.source.checksum===source.rows.checksum?'match':'mismatch'}">${source.source.checksum===source.rows.checksum?'校验一致':'校验不一致'}</div></div>${table(['#','sensor_id','value','recorded_at'],rows.map(r=>[`<span class="mono">${r.position??''}</span>`,esc(r.sensor_id),esc(r.value),`<small class="mono">${esc(r.recorded_at)}</small>`]))}`;
}

export function migrationPanel(){
 let id=store.migrationId;
 if(!id)return '<aside class="note">选择一条数据集右侧的「逐行存储」，即可分批回填、逐条核对、切换与回退。迁移期间照常增删改。回填按 position 水位推进，中断后从上一批恢复，重试同一批不重复补行。</aside>';
 let d=store.datasets.find(x=>x.id===Number(id));
 let b=store.migrations[id];
 if(!d)return '';
 let source=store.migrationSource&&store.migrationSource.dataset_id===Number(id)?store.migrationSource:null;
 let canSwitch=b&&b.status==='completed'&&d.storage_mode!=='rows';
 return `<div class="migration" id="migration-live" data-dataset="${d.id}">
 <div class="migration-head"><div><span class="eyebrow">ROW-PER-READING MIGRATION / #${d.id}</span><h2>${esc(d.name)} ${modeBadge(d.storage_mode)}</h2><small>revision ${d.revision} · ${d.readings.length} 采样 · 同值采样各算一行</small></div>
 <button class="quiet" data-close-migration="1">收起</button></div>
 ${b?`<div class="migration-progress"><div class="metrics slim"><div><small>回填状态</small><strong class="mono">${b.status}</strong></div><div><small>已复制 / 源</small><strong class="mono">${b.copied_rows}/${b.source_total}</strong></div><div><small>逐行表行数</small><strong class="mono">${b.row_count}</strong></div></div>${progressBar(b)}${b.error?`<div class="feedback error">${esc(b.error)}</div>`:''}${b.checksum?`<small class="mono muted">核齐 SHA-256 ${esc(b.checksum)}</small>`:''}</div>`:''}
 <div class="migration-actions">
  <label>每批行数<input name="batch_size" type="number" min="1" max="2000" value="${b?.batch_size||200}" ${d.storage_mode==='rows'?'disabled':''}></label>
  <button type="button" data-backfill-start="${d.id}" ${d.storage_mode==='rows'?'disabled':''}>发起回填</button>
  <button type="button" class="quiet" data-backfill-run="${d.id}" data-batches="1" ${d.storage_mode==='rows'?'disabled':''}>推进一批</button>
  <button type="button" class="quiet" data-backfill-run="${d.id}" data-batches="all" ${d.storage_mode==='rows'?'disabled':''}>回填到完成</button>
  <button type="button" class="quiet" data-backfill-retry="${d.id}" ${!b||['completed','rolled_back','not_started'].includes(b.status)?'disabled':''}>重试上一批</button>
  <button type="button" class="quiet" data-read-source="${d.id}">读源</button>
  <button type="button" data-switch-storage="${d.id}" ${canSwitch?'':'disabled'}>切换为逐行存储</button>
  <button type="button" class="quiet" data-rollback-storage="${d.id}" ${d.storage_mode==='rows'?'':'disabled'}>回退到 JSON</button>
 </div>
 <div class="source-view">${sourceView(source)}</div>
 </div>`;
}

export function datasetsPage(){
 return `<div class="split"><section><h2>给算子一份确定的输入</h2><p>输入传感器与采样两张逻辑表，允许空表及重复键。每次执行复制一份输入快照。</p><form id="dataset-form"><input name="id" type="hidden"><label>数据集名称<input name="name" required placeholder="shift-20261117"></label><label>业务说明<input name="description" placeholder="日班采样文件与校验意图"></label><label>表数据 JSON<textarea name="data" class="code-input" rows="12" required>${json(example)}</textarea></label><small>上限：100传感器、2000采样。字段不可扩展；value必须是有限数值。</small><button type="submit">保存数据集</button><button type="button" data-reset="dataset-form" class="quiet">清空</button></form></section><section><h2>输入样本册 <em>${store.datasets.length}</em></h2>${table(['数据集','表行数','存储','动作'],store.datasets.map(d=>[`<b>${esc(d.name)}</b><small>${esc(d.description)}</small>`,`sensors ${d.sensors.length}<small>readings ${d.readings.length}</small>`,modeBadge(d.storage_mode),`<button class="quiet" data-edit-dataset="${d.id}">编辑 / 查看</button> <button class="quiet" data-migrate-dataset="${d.id}">逐行存储</button>`]))}<aside class="note">这里的逻辑数据集和执行轨迹由 PostgreSQL 保存。算子在 Python 中运行，用来观察算法行为；页面指标不代表 PostgreSQL 内核的执行剖面。</aside></section></div><section class="migration-section"><h2>逐行存储迁移</h2>${migrationPanel()}</section>`;
}
