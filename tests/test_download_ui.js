const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
assert(!html.includes('generate-now')&&!html.includes('<canvas'));
assert(html.includes('Download GeoPackage'));
const elements={};
for(const id of ['island','region','province','municipality','barangay','status','download-frame'])elements[id]={value:'',dataset:{},addEventListener(type,fn){this[type]=fn},replaceChildren(...items){this.value=items[0]?.value||''}};
const buttons=['jpeg','zip','gpkg'].map(format=>({dataset:{format},addEventListener(type,fn){this[type]=fn}}));
let posted=[],mode='success',downloaded=false;
const context={document:{getElementById:id=>elements[id],querySelectorAll:()=>buttons,body:{appendChild(){}},createElement:()=>({click(){downloaded=true},remove(){}})},Option:function(text,value){this.value=value},console,URLSearchParams,URL:{createObjectURL:()=> 'blob:map',revokeObjectURL(){}},setTimeout:(fn,delay)=>{if(delay===3000)fn();return 1},fetch:async(url,options)=>{
 if(url.startsWith('/api/slope-boundaries'))return {ok:true,json:async()=>[{code:'region:140000000',name:'Cordillera Administrative Region',island:'Luzon'}]};
 if(url==='/api/prepare-download'){posted.push(JSON.parse(options.body));return {ok:true,json:async()=>({job_id:'job',status:'processing'})};}
 if(url.startsWith('/api/download-status/'))return {ok:true,json:async()=>mode==='failure'?{status:'failed',error:'No slope source coverage for this area'}:{status:'ready',download_url:'/api/download-file/job'}};
 return {ok:true,blob:async()=>({}),headers:{get:()=> 'attachment; filename="slope.jpg"'}};
}};
vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],context);
setImmediate(async()=>{
 elements.region.value='region:010000000';
 for(const [province,municipality,barangay,level,fid] of [['','','','region','010000000'],['province:66','','','province','66'],['province:66','municipality:1','','municipality','1'],['province:66','municipality:1','barangay:3','barangay','3']]){
  elements.province.value=province;elements.municipality.value=municipality;elements.barangay.value=barangay;
  await buttons[2].click();const data=posted.at(-1);assert.equal(data.level,level);assert.equal(data.fid,fid);assert.equal(data.format,'gpkg');
 }
 await buttons[0].click();assert(downloaded);
 mode='failure';await buttons[0].click();assert.equal(elements.status.textContent,'No slope source coverage for this area');
 console.log('PASS: one-click exports retain every scope and leading-zero region codes, download JPEG, and report source errors');
});
