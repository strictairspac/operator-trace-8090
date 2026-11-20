import {datasets,plans,traces,backfill} from '../api/business.js';
export const store={datasets:[],plans:[],traces:[],user:null,migrationId:null,migrations:{},migrationSource:null};
export async function refresh(){[store.datasets,store.plans,store.traces]=await Promise.all([datasets.list(),plans.list(),traces.list()]);}
export async function refreshMigration(){
 let id=store.migrationId;if(!id)return null;
 store.migrations[id]=await backfill.progress(id);
 return store.migrations[id];
}
export async function loadSource(id){
 store.migrationSource=await backfill.source(id);
 return store.migrationSource;
}
