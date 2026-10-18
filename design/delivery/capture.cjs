const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs=require('fs');
const path=require('path');
const root=__dirname;
const shots=[
 ['01-home','home',360,800],['02-answer','answer',360,800],
 ['03-card-open-poi','card-open',360,800],['04-card-likely-ai','card-maybe',360,800],['05-card-unnamed','card-unnamed',360,800],['06-card-closing','closing-soon',360,800],['07-card-no-ai','card-no-ai',360,800],['08-card-selected','selected',360,800],
 ['09-loading','loading',360,800],['10-error','error',360,800],['11-empty','empty',360,800],['12-conditions-changed','stale',360,800],['13-answer-no-ai','no-ai',360,800],
 ['14-departure','departure',360,800],['15-night','night',360,800],['16-coffee','coffee',360,800],['17-with-child','kids',360,800],
 ['18-route','route',360,800],['19-privacy-cell','privacy',360,800,'map'],['20-data-used','data',360,800],['21-pick-map','pick',360,800,'map'],['22-map-unavailable','map-error',360,800,'map'],
 ['23-geolocation-denied','geo-denied',360,800],['24-outside-astana','geo-far',360,800],['25-query-too-large','too-large',360,800],['26-server-loading','unavailable',360,800],['27-timeout','timeout',360,800],['28-query-empty','query-empty',360,800],['29-route-over-budget','over-budget',360,800,'route'],
 ['30-styles','styles',360,800],['31-desktop-1440','answer',1440,900],['32-desktop-1280','answer',1280,720],['33-video-1920','privacy',1920,1080],['34-phone-390','answer',390,844],['35-tablet-768','answer',768,1000],['36-response-ru','answer',360,800,null,'ru'],['37-response-en','answer',360,800,null,'en'],['38-closed-medium','closed-medium',360,800],['39-desktop-form','home',1440,900],['40-sent-payload','data',360,800,'payload'],['41-mega-scenario','answer-short',360,800]
];
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'chrome'});
 const manifest=[],errors=[];
 // Shared browser cache retains unmodified standard OpenStreetMap tiles for the review session.
 const context=await browser.newContext({deviceScaleFactor:1,reducedMotion:'reduce'});
 for(const [name,view,w,h,section,lang='kk'] of shots){
  const page=await context.newPage({viewport:{width:w,height:h}});await page.setViewportSize({width:w,height:h});
  page.on('pageerror',e=>errors.push({name,error:e.message}));
  await page.goto(`http://127.0.0.1:5512/demo/?view=${view}&lang=${lang}`);
  await page.waitForFunction(()=>window.preview?.ready);
  await page.waitForTimeout(w>=1000||section==='map'||view==='route'||view==='home'?800:100);
  if(w>=1000||section==='map'||view==='route'||view==='home')await page.waitForFunction(()=>{const tiles=[...document.querySelectorAll('.leaflet-tile')];return tiles.length>0&&tiles.every(e=>e.complete&&e.naturalWidth>0);},{},{timeout:10000}).catch(()=>{});
  if(section==='map')await page.locator('.map-wrap').scrollIntoViewIfNeeded();
  if(section==='route')await page.locator('.route').scrollIntoViewIfNeeded();
  if(section==='payload')await page.locator('.sent').scrollIntoViewIfNeeded();
  if(view==='map-error')await page.evaluate(()=>{document.querySelector('#map').style.opacity='.22';document.querySelector('#map-fallback').hidden=false;});
  const layout=await page.evaluate(()=>({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,lang:document.documentElement.lang,bodyHeight:document.documentElement.scrollHeight,firstCard:(()=>{const r=document.querySelector('.card')?.getBoundingClientRect();return r?{top:r.top,bottom:r.bottom}:null;})()}));
  if(layout.scrollWidth>w)errors.push({name,error:'horizontal-overflow',...layout});
  await page.screenshot({path:path.join(root,'screens',name+'.png')});
  manifest.push({file:'screens/'+name+'.png',view,width:w,height:h,lang,section:section||'top',url:`demo/?view=${view}&lang=${lang}`,layout});
  await page.close();
 }
 await browser.close();
 fs.writeFileSync(path.join(root,'manifest.json'),JSON.stringify({generatedAt:new Date().toISOString(),frames:manifest,errors},null,2));
 console.log(JSON.stringify({frames:manifest.length,errors,desktop:manifest.filter(x=>x.width>=1000).map(x=>({file:x.file,firstCard:x.layout.firstCard}))},null,2));
 if(errors.length)process.exitCode=1;
})().catch(e=>{console.error(e);process.exitCode=1});
