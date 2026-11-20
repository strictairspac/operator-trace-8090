import {api,post} from './client.js';
export const datasets={list:()=>api('/datasets'),save:(d,id)=>post('/datasets'+(id?'/'+id:''),d,id?'PUT':'POST')};
export const backfill={
  start:(id,batch_size)=>post('/datasets/'+id+'/backfill',{batch_size}),
  progress:id=>api('/datasets/'+id+'/backfill'),
  run:(id,batches)=>post('/datasets/'+id+'/backfill/run',{batches}),
  retry:id=>post('/datasets/'+id+'/backfill/retry'),
  source:(id,limit=2000)=>api('/datasets/'+id+'/storage/source?limit='+limit),
  switch:id=>post('/datasets/'+id+'/storage/switch'),
  rollback:id=>post('/datasets/'+id+'/storage/rollback'),
};
export const plans={list:()=>api('/plans'),save:(d,id)=>post('/plans'+(id?'/'+id:''),d,id?'PUT':'POST')};
export const traces={list:()=>api('/traces'),run:d=>post('/traces',d),detail:id=>api('/traces/'+id)};
