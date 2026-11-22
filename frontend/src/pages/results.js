import {plot} from '../components/common/chart.js';
import {feedback} from '../components/common/fields.js';
import {empty, metrics, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {REVIEW, STATUS, VERDICT} from '../constants/enums.js';
import {handler, poll} from '../hooks/action.js';
import {workspace} from '../stores/workspace.js';
import {button, el, h, link, p} from '../utils/dom.js';
import {date, value} from '../utils/format.js';

const verdictPill = row => {
  const verdict = row.verdict;
  return VERDICT[verdict] ? pill(VERDICT[verdict], verdict) : value(verdict);
};

function decorate(result) {
  // 补全结论列用中文状态胶囊展示，其余列沿用通用渲染。
  return result.tables.map(item => ({
    ...item,
    columns: item.columns.map(column => column.key === 'verdict'
      ? {...column, render: verdictPill} : column),
  }));
}

function progressLine(job) {
  const checkpoint = job.progress;
  if (!checkpoint || !checkpoint.total_groups) return null;
  const done = checkpoint.groups ? checkpoint.groups.length : 0;
  return p(`已核算 ${done} / ${checkpoint.total_groups} 个单元，中断后会从该进度恢复，不重复发布。`, 'subtle');
}

export async function results(route, refresh) {
  const {spec,runs,datasets} = workspace;
  const id = route.id || runs[0]?.id;
  if (!id) return panel(spec.pages[3],empty('还没有可查看的运行结果。','#run','前往运行'));
  const job = await workspace.getRun(id);
  const source = datasets.find(item => item.id === job.dataset_id);
  const message = feedback();
  const finished = item => item.status === 'complete' || item.status === 'failed';
  if (!finished(job)) {
    const original = location.hash;
    const status = el('p',{role:'status'},'后台正在计算，完成后会自动展示。');
    const checkpoint = progressLine(job);
    poll(()=>workspace.getRun(id),finished,()=>location.hash===original)
      .then(result=>{if (result && status.isConnected && location.hash===original) refresh();
        else if(status.isConnected) status.textContent='仍在执行，请点击刷新检查。';})
      .catch(error=>{if(status.isConnected) status.textContent=error.message;});
    return panel(job.name,pill(STATUS[job.status],job.status),status,checkpoint,
      button('刷新',{class:'quiet',onClick:refresh}));
  }
  const download = button('下载结果 CSV',{class:'quiet','data-testid':'export',onClick:event=>
    handler(download,message,()=>workspace.download(`/runs/${job.id}/export`,`${spec.short_name}-${job.id}.csv`))(event)});
  const history = el('label',{class:'field run-picker',for:'run-picker'},el('span',{},'切换运行记录'),
    el('select',{id:'run-picker',onChange:event=>{location.hash=`#results/${event.target.value}`;}},
      (runs.some(item=>item.id===job.id) ? runs : [job,...runs]).map(item=>el('option',{value:item.id,selected:item.id===job.id},item.name))));
  const result = job.result;
  const analysis = result ? el('section',{class:'analysis','data-testid':'analysis-complete'},
    el('div',{class:'result-head'},p(job.name,'eyeline'),h(2,result.headline),p(result.note,'intro-copy'),
      el('div',{class:'actions'},pill(STATUS[job.status],job.status),download,message)),
    metrics(result.metrics),
    result.charts.map(plot),
    (result.sections || []).map(item=>panel(item.title,p(item.text))),
    decorate(result).map(item=>panel(item.title,table(item.columns,item.rows,{caption:item.title}))))
    : panel(job.name,pill(STATUS[job.status],job.status),p(job.error,'feedback'),link('返回调整参数',`#run/${job.dataset_id}`,{class:'quiet'}));
  const inputs = spec.params.map(param=>el('div',{},el('dt',{},param.label),el('dd',{},value(job.params[param.key]))));
  return el('div',{},el('div',{class:'two-col'},el('div',{class:'wide-panel'},analysis),
    el('aside',{class:'sidebar-panel'},panel('运行来源',history,
      source && link(source.name,`#inputs/${source.id}`),p(date(job.finished_at),'subtle'),
      el('dl',{class:'dictionary'},inputs)),
    result && panel(spec.decision, pill(REVIEW[job.review],job.review),
      link('打开复核记录',`#review/${job.id}`,{class:'quiet'})))));
}
