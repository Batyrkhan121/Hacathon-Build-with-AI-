// Designer's state catalogue only. Production web/** is not modified.
import {t,getLang,setLang,applyI18n,onLangChange,formatNumber} from './lib/i18n.js';
import * as view from './lib/render.js';
import * as map from './lib/map.js';

const $=id=>document.getElementById(id),params=new URLSearchParams(location.search);
const [fixtures,landmarks]=await Promise.all([fetch('../fixtures.json').then(r=>r.json()),fetch('../landmarks.json').then(r=>r.json())]);
const state={view:params.get('view')||'home',scenario:'S1',budget:(params.get('view')&&!['home','answer-short'].includes(params.get('view'))?120:60),landmark:'expo',time:'15:00',day:0,flags:{},cell:false,selected:1,variant:null,answer:null};
const copy=x=>JSON.parse(JSON.stringify(x));
const icon=(name)=>{const img=document.createElement('img');img.src=`../assets/icons/${name}.svg`;img.alt='';img.setAttribute('aria-hidden','true');return img;};
const PRESETS=[['S1','walking','mega_silk_way','15:00'],['S2','unknown','expo','11:00'],['S3','child','khan_shatyr','12:00'],['S4','airplane','baiterek','16:00'],['S5','moon','baiterek','23:30']];
const brand=document.querySelector('.brand');document.body.insertBefore(brand,document.querySelector('.app'));
document.querySelectorAll('.lang button').forEach(b=>{b.textContent=({kk:'ҚАЗ',ru:'РУС',en:'ENG'})[b.dataset.lang];b.addEventListener('click',()=>setLang(b.dataset.lang));});
$('mock-pill').hidden=false;
const advanced=document.createElement('details');advanced.className='parameters';
const advancedSummary=document.createElement('summary');advanced.append(advancedSummary);
for(const el of [...$('ask').querySelectorAll('fieldset')])advanced.append(el);
$('ask').append(advanced);
const mapWrap=document.querySelector('.map-wrap'),app=document.querySelector('.app');
const spec=document.createElement('div');spec.id='spec';spec.hidden=true;document.body.append(spec);
const ifSmall=()=>innerWidth<1000;
let timer;

function chip(container,text,active,action){const b=view.h('button',{type:'button',class:'chip','aria-pressed':String(active),text,onclick:action});container.append(b);return b;}
function controls(){
 applyI18n();document.querySelectorAll('.lang button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.lang===getLang())));
 $('presets').replaceChildren();for(const [id,ic] of PRESETS){const b=view.h('button',{class:'preset',type:'button','data-preset':id,'aria-pressed':String(state.scenario===id&&state.view!=='home'),onclick:()=>scenario(id)});b.append(icon(ic),document.createTextNode(t(`preset.${id}.label`)));$('presets').append(b);}
 $('landmark').replaceChildren(...landmarks.map(l=>view.h('option',{value:l.id,text:l.names[getLang()]||l.id})));$('landmark').value=state.landmark;
 $('budget-chips').replaceChildren();for(const n of [30,60,120])chip($('budget-chips'),t('budget.'+n),state.budget===n,()=>{state.budget=n;change();});
 $('mode-chips').replaceChildren();for(const [flag,ic] of [['tourist','airplane'],['night','moon'],['with_kids','child'],['wheelchair','accessibility'],['group','group']]){const key=flag==='group'?'flag.group':'flag.'+flag;const b=chip($('mode-chips'),t(key),!!state.flags[flag],()=>{state.flags[flag]=!state.flags[flag];change();});b.prepend(icon(ic));}
 $('flag-chips').replaceChildren();chip($('flag-chips'),t('flag.indoor_only'),!!state.flags.indoor_only,()=>{state.flags.indoor_only=!state.flags.indoor_only;change();});
 $('departure-chips').replaceChildren();for(const n of [90,120,180,240])chip($('departure-chips'),t('departure.'+n),(state.departure||120)===n,()=>{state.departure=n;change();});$('departure').hidden=!state.flags.tourist;
 $('day-chips').replaceChildren();for(const n of [0,1])chip($('day-chips'),t(n?'when.tomorrow':'when.today'),state.day===n,()=>{state.day=n;change();});
 $('time-chips').replaceChildren();for(const tm of [null,'10:00','19:00','23:30'])chip($('time-chips'),tm||t('when.now'),state.time===tm,()=>{state.time=tm;change();});$('time-input').value=state.time||'';
 const name=landmarks.find(l=>l.id===state.landmark)?.names[getLang()]||'EXPO';
 advancedSummary.textContent=`${state.cell?t('where.cellOption'):name} · ${t('budget.'+state.budget)} · ${state.day?t('when.tomorrow')+' ':''}${state.time||t('when.now')}`;
 $('where-note').textContent=state.cell?t('where.noteCell',{cell:fixtures.localmockAnswer.context.h3_cell,area:formatNumber(.1,1)}):t('where.noteLandmark',{id:state.landmark});
 $('submit').querySelector('span').textContent=t('submit');
}
function request(){return {query:$('query').value||t(`preset.${state.scenario}.query`),location:state.cell?{h3_cell:fixtures.localmockAnswer.context.h3_cell}:{landmark_id:state.landmark},at:state.time?`2026-10-${state.day?'21':'20'}T${state.time}:00+05:00`:null,lang:getLang(),overrides:{time_budget_min:state.budget,...Object.fromEntries(Object.entries(state.flags).filter(([key])=>key!=='group')), ...(state.flags.group?{group_size:4}:{}),...(state.flags.tourist?{departure_in_min:state.departure||120}:{})},want_route:true,use_llm:state.view!=='no-ai'};}
function restoreMap(){if(mapWrap.parentNode!==app)app.append(mapWrap);}
function placeMap(){const data=document.querySelector('.data-used');if(ifSmall()&&data)data.before(mapWrap);else if(!ifSmall())restoreMap();map.invalidate();}
function enrich(){
 const route=document.querySelector('.route');if(route)route.querySelector('.route-note').textContent=t('route.direct');
 for(const el of document.querySelectorAll('.card')){
  const rank=Number(el.dataset.rank),card=state.answer?.cards.find(c=>c.rank===rank);
  const poi=el.querySelector('.badge-poi'),ai=el.querySelector('.badge-ai'),hours=el.querySelector('.badge-hours');
  if(poi)poi.prepend(icon('poi'));if(ai)ai.prepend(icon('ai'));
  if(hours){hours.prepend(icon(hours.classList.contains('is-open')?'open':hours.classList.contains('is-closed')?'closed':'clock'));if(card?.open_status==='closed'&&card.open_confidence!=='high')hours.classList.add('is-medium');if(card?.open_status==='unknown'&&(!card.open_note?.trim()||/дерек жоқ|нет данных|no.*hours/i.test(card.open_note))){hours.textContent=t('hours.noData');hours.prepend(icon('unknown'));}}
  const facts=el.querySelectorAll('.card-facts>span');facts[0]?.prepend(icon('walking'));facts[1]?.prepend(icon('clock'));
  el.querySelectorAll('.tags li').forEach((tag,i)=>{if(i>3)tag.remove();});
  const buttons=el.querySelectorAll('.card-actions .btn');buttons[0]?.prepend(icon('map-pin'));buttons[1]?.classList.add('card-route-extra');
  const site=el.querySelector('a');if(site){site.removeAttribute('href');site.setAttribute('role','button');site.setAttribute('aria-disabled','true');site.addEventListener('click',e=>e.preventDefault());}
  if(state.view==='closing-soon'||state.variant==='closing_soon'){const p=view.h('p',{class:'card-warning',text:card.open_note});el.append(p);}
  if(state.view==='selected'&&rank===state.selected)el.classList.add('is-selected');
 }
 const noai=document.querySelector('.pill-noai');if(noai){noai.prepend(icon('poi'));noai.parentNode.prepend(noai);}
 for(const badge of document.querySelectorAll('.badge-hours.is-open'))badge.childNodes.forEach(node=>{if(node.nodeType===Node.TEXT_NODE)node.textContent=node.textContent.replace(/\s*\(OSM\)/g,'');});
 $('stale').hidden=state.view!=='stale';$('answer').classList.toggle('is-stale',state.view==='stale');
 placeMap();
}
function context(){return {request:request(),originName:state.cell?t('map.myArea'):'EXPO',onMap:rank=>{map.focusStop(rank);document.querySelector('.map-wrap').scrollIntoView({block:'start',behavior:'smooth'});},onRoute:rank=>map.showRoute(rank,t('route.direct')),onHover:rank=>map.highlightStop(rank),onClearFilters:()=>{state.flags={};state.view='answer';render();}};}
function prepareAnswer(){let a=copy(fixtures.localmockAnswer);a.warnings=[];
 if(state.variant){a.cards=[copy(fixtures.cardVariants[state.variant].card)];a.cards[0].rank=1;a.route=null;a.summary='';if(state.variant==='no_ai')a.data_used.generation='template';}
 else if(state.view==='answer-short')a=copy(fixtures.answers.walk_short);
 else if(state.view==='no-ai')a=copy(fixtures.answers.no_ai);
 else if(state.view==='empty')a=copy(fixtures.answers.empty);
 else if(state.view==='departure')a=copy(fixtures.answers.departure_warning);
 else if(state.view==='over-budget')a=copy(fixtures.answers.over_budget);
 else if(state.view==='closed-medium')a=copy(fixtures.answers.closed_medium);
 a.warnings=(a.warnings||[]).filter(w=>!w.includes('Мок-деректер'));
 a.lang=getLang();a.context.local_time=request().at||'2026-10-20T17:30:00+05:00';a.data_used.time=`20.10.2026 · ${state.time||'17:30'} (UTC+5)`;if(state.cell||['privacy','data','pick'].includes(state.view)){a.context.landmark_id=null;a.data_used.location=t('where.noteCell',{cell:a.context.h3_cell,area:formatNumber(.1,1)});}else{a.data_used.location=t('where.noteLandmark',{id:state.landmark});}
 if(state.view==='night'){a.cards=[];a.route=null;a.summary=t('empty.title');a.warnings=[t('hours.noData')];}
 if(state.view==='coffee'){a.cards=[copy(fixtures.cardVariants.likely_open_ai.card)];a.cards[0].rank=1;a.route=null;a.summary=t('preset.S2.query');a.warnings=['Розеткалар туралы дерек жоқ.'];}
 if(state.view==='kids'){a.cards=[copy(fixtures.cardVariants.unnamed_playground.card)];a.cards[0].rank=1;a.route=null;a.summary=t('preset.S3.query');}
 if(state.view==='no-ai'||state.variant==='no_ai')a.warnings=[];
 if(getLang()!=='kk'){
  const en=getLang()==='en';
  a.summary=en?'Three ideas near EXPO: a museum, a warm break and a playground.':'Три идеи у EXPO: музей, перерыв в тепле и детская площадка.';
  for(const c of a.cards){const museum=c.category==='museum',play=c.category==='playground';c.title=museum?(en?'Alem.ai — Museum of Singularity':'Alem.ai — Музей Сингулярности'):play?(en?'Playground · 120 m':'Детская площадка · 120 м'):'Mega Silk Way';c.why=museum?(en?'A nearby immersive museum for a curious afternoon.':'Иммерсивный музей рядом для новых впечатлений.'):play?(en?'A short nearby stop for a walk with children.':'Короткая остановка рядом для прогулки с ребёнком.'):(en?'A warm place to take a break after your walk.':'Можно согреться и сделать перерыв после прогулки.');c.what_to_do=museum?(en?'Explore the exhibits. Check tickets before visiting.':'Посмотрите экспозицию. Уточните билеты перед визитом.'):(en?'Take a short break. Check opening hours beforehand.':'Сделайте короткий перерыв. Проверьте часы перед визитом.');c.open_note=c.open_status==='unknown'?t('hours.noData'):(en?'Mock schedule: Tue–Sun 10:00–18:30.':'Мок-расписание: вт–вс 10:00–18:30.');}
 }
 return a;
}
function render(){
 if(state.view!=='pick')map.stopPick();restoreMap();controls();
 document.body.classList.toggle('card-only',!!state.variant||state.view==='selected');
 document.body.classList.toggle('capture-response',params.has('capture')&&!['home','geo-denied','geo-far','query-empty'].includes(state.view));
 document.body.classList.toggle('route-only',state.view==='route');
 document.body.classList.toggle('data-only',state.view==='data');
 document.body.classList.toggle('spec-mode',state.view==='styles');spec.hidden=state.view!=='styles';
 $('status').textContent='';$('form-error').hidden=true;$('map-fallback').hidden=state.view!=='map-error';$('stale').hidden=true;
 if(state.view==='styles'){spec.innerHTML=styleSheet();return;}
 if(state.view==='home'||state.view==='geo-denied'||state.view==='geo-far'||state.view==='query-empty'){
  view.renderIntro($('answer'),{onExample:()=>scenario('S1')});$('answer').querySelector('.btn')?.classList.replace('btn-primary','btn-link');
  if(state.view==='query-empty'){$('form-error').textContent=t('query.empty');$('form-error').hidden=false;}
  if(state.view==='geo-denied'||state.view==='geo-far'){advanced.open=true;$('where-note').textContent=t(state.view==='geo-denied'?'geo.denied':'geo.far');}
  map.clearAnswer();return;
 }
 if(state.view==='loading'){view.renderLoading($('answer'),{onCancel:()=>{state.view='home';render();}});$('status').textContent=t('status.loading',{where:'EXPO'});return;}
 if(['error','timeout','too-large','unavailable'].includes(state.view)){
  view.renderError($('answer'),{kind:({error:'network',timeout:'timeout','too-large':'too_large',unavailable:'unavailable'})[state.view]},{canRetryNoAi:true,onRetry:()=>submit(),onRetryNoAi:()=>{state.view='no-ai';render();},onShorten:()=>{state.view='home';render();$('query').focus();}});return;
 }
 state.answer=prepareAnswer();
 if(state.view==='selected'){state.answer.cards=[copy(fixtures.cardVariants.open_poi.card)];state.answer.route=null;state.answer.summary='';}
 view.renderAnswer($('answer'),state.answer,context());
 $('status').textContent=t('status.done',{sec:'0,7',n:state.answer.cards.length});enrich();
 if(map.hasMap()){
  map.clearCell();if(state.cell||state.view==='privacy'||state.view==='data')map.showCell(state.answer.context.h3_cell,t('cell.tooltip',{area:formatNumber(.1,1)}));
  map.showAnswer(state.answer,{originLabel:t('map.origin',{name:'EXPO'}),popupFor:view.popupFor,onSelect:rank=>{document.querySelectorAll('.card').forEach(el=>el.classList.toggle('is-selected',Number(el.dataset.rank)===rank));map.highlightStop(rank);view.highlightCard(rank);},routeLabel:t('route.direct'),markerLabel:c=>`${c.rank}. ${c.title}`});
  // Landmark context remains available as actual sourced coordinates; no coordinates enter request().
 }
 if(state.view==='stale'){$('stale').hidden=false;$('answer').classList.add('is-stale');}
 if(state.view==='data'){const d=document.querySelector('.data-used');d.open=true;const sent=request();document.querySelector('.sent-json').textContent=JSON.stringify({location:sent.location,lang:sent.lang,at:sent.at,overrides:sent.overrides,want_route:sent.want_route,use_llm:sent.use_llm,query:sent.query},null,2);}
 if(state.view==='pick')startPick();
 if(state.view==='map-error')$('map-fallback').hidden=false;
}
function change(){controls();if(!['home','loading','geo-denied','geo-far','query-empty'].includes(state.view)){state.view='stale';$('stale').hidden=false;$('answer').classList.add('is-stale');}else if(state.view!=='home'){state.view='home';render();}}
function scenario(id){const p=PRESETS.find(p=>p[0]===id);state.scenario=id;state.landmark=p[2];state.time=p[3];state.flags={};if(id==='S3')state.flags.with_kids=true;if(id==='S4')state.flags.tourist=true;if(id==='S5')state.flags.night=true;$('query').value=t(`preset.${id}.query`);state.budget=id==='S4'?120:60;submit();}
function submit(){clearTimeout(timer);if(!$('query').value.trim()){$('form-error').textContent=t('query.empty');$('form-error').hidden=false;return;}state.variant=null;state.view='loading';render();timer=setTimeout(()=>{state.view=({S1:'answer-short',S2:'coffee',S3:'kids',S4:'departure',S5:'night'})[state.scenario]||'answer';render();if(ifSmall())$('results').scrollIntoView({block:'start',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});else document.querySelector('.panel').scrollTo({top:$('results').offsetTop-88,behavior:'smooth'});},700);}
function startPick(){state.cell=true;placeMap();map.startPick((lat,lon)=>{state.cell=true;state.view='privacy';render();},{label:t('pick.banner'),cancelLabel:t('pick.cancel'),onCancel:()=>{map.stopPick();state.view='answer';render();}});}
function styleSheet(){return `<div class="spec-sheet"><h1>Qadam · UI</h1><p>360 × 800 / 1440 × 900<br>Ә Ғ Қ Ң Ө Ұ Ү Һ І<br>ә ғ қ ң ө ұ ү һ і</p><h2>Түс / color</h2>${[['#245FC5','--accent · Ұсыну / route'],['#17613A','--open · open + high'],['#166474','--maybe · outline'],['#9E3029','--closed · closed'],['#515B68','--unknown · no data'],['#6340A3','--ai · generated'],['#495362','--poi · neutral outline'],['#684D08','--warn · warning']].map(([color,label])=>`<div class="swatch"><i style="background:${color}"></i><span>${label}<br>${color}</span></div>`).join('')}<h2>Типографика / type</h2><p>System UI · 16 / 24 px<br>Title · 20 / 27 px<br>Why / What · 15 / 22 px<br>Facts / badges · 13–14 px</p><h2>Сетка / spacing</h2><p>8 px · card padding 16 px<br>Card radius 12 px · chip 8 px<br>Touch ≥ 44 px · contrast ≥ 4,5:1</p></div>`;}
$('ask').addEventListener('submit',e=>{e.preventDefault();submit();});$('query').addEventListener('input',()=>{if(state.answer)change();});$('query').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();submit();}});$('landmark').addEventListener('change',e=>{state.landmark=e.target.value;state.cell=false;change();});$('time-input').addEventListener('change',e=>{state.time=e.target.value;change();});$('geo-btn').addEventListener('click',()=>{state.view='geo-denied';render();});$('pick-btn').addEventListener('click',()=>{startPick();mapWrap.scrollIntoView({block:'start',behavior:'smooth'});});$('stale-btn').addEventListener('click',submit);onLangChange(render);window.addEventListener('resize',()=>{placeMap();advanced.open=innerWidth>=1000&&state.view==='home';});
const cardViews={ 'card-open':'open_poi','card-maybe':'likely_open_ai','card-unnamed':'unnamed_playground','closing-soon':'closing_soon','card-no-ai':'no_ai'};
state.variant=cardViews[state.view]||null;if(state.variant==='closing_soon')state.time='17:30';state.cell=['privacy','data','pick'].includes(state.view);advanced.open=innerWidth>=1000&&state.view==='home';
if(params.get('lang'))setLang(params.get('lang'));applyI18n();
map.initMap($('map'));map.showLandmarkHints(landmarks);if(!map.hasMap())$('map-fallback').hidden=false;
$('query').value=state.view==='home'?'':fixtures.localmockAnswer.intent.raw_query;render();
window.preview={state,fixtures,show(name){state.view=name;state.variant=cardViews[name]||null;state.cell=['privacy','data','pick'].includes(name);render();},request,ready:true};
