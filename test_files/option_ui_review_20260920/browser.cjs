// Invoke with bundled Node from the repository root. All browser HTTP is mocked.
const fs = require('fs');
const path = require('path');
const os = require('os');
const root = path.resolve(__dirname, '../..');
const bundled = path.join(os.homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules');
const {chromium} = require(path.join(bundled, 'playwright'));
const esbuild = require(path.join(root, 'testplatform/frontend/node_modules/esbuild'));
const out = path.join(os.tmpdir(), 'ba2-option-ui-review-20260920');
fs.mkdirSync(out, {recursive:true});

const ref = (price) => ({price, eventAt:null, observedAt:'2026-09-07', availableAt:null,
  quality:'last_known_bar', source:'fixture', reason:'Fixture only'});
const leg = {id:1,symbol:'XYZ',underlyingSymbol:'XYZ',contractSymbol:'XYZ260918C00095000',
  optionType:'call',strike:95,expiry:'2026-09-18',multiplier:100,multiplierRecorded:true,
  direction:'long',size:1,entryAt:'2026-09-08T13:30:00Z',exitAt:'2026-09-11T19:45:00Z',
  entryPrice:8,exitPrice:13.3,pnl:530,pnlPercent:5.3,exitReason:'exit',transactionId:'7',
  rowBasis:'aggregate_round_trip',positionStatus:'closed',entryUnderlying:ref(100),
  exitUnderlying:ref(108),unavailableFields:[],
  entryContract:{iv:.40,delta:.65,gamma:.03,theta:-.04,vega:.20,open_interest:1234,volume:78,quality:'cache_bar',asOf:'2026-09-07'},
  exitContract:{iv:.38,delta:.75,gamma:.02,theta:-.03,vega:.18,open_interest:1250,volume:82,quality:'cache_bar',asOf:'2026-09-11'}};
const context={schemaVersion:1,backtestId:77,resultDigest:'fixture',selectedTradeId:1,transactionId:'7',
 legs:[leg,{...leg,id:2,contractSymbol:'XYZ260918C00105000',direction:'short',strike:105,
  entryPrice:2,exitPrice:3.6,pnl:-160,pnlPercent:-1.6}],
 underlying:{symbol:'XYZ',provider:'fixture',interval:'1d',cacheStatus:'complete',
  provenance:'current_historical_cache',bars:[
  ['2026-09-04',99,102,97,101],['2026-09-08',100,103,99,102],
  ['2026-09-09',102,106,101,105],['2026-09-10',105,109,104,108],
  ['2026-09-11',108,110,107,109],['2026-09-14',109,111,107,110]]
  .map(([date,open,high,low,close])=>({date,open,high,low,close}))},notices:[]};

(async()=>{
 const result=await esbuild.build({entryPoints:[path.join(__dirname,'harness.tsx')],bundle:true,
  write:false,format:'iife',platform:'browser',define:{'import.meta.env.VITE_API_BASE':'"http://review.invalid/api"'},
  jsx:'automatic',nodePaths:[path.join(root,'testplatform/frontend/node_modules')]});
 const cssDir=path.join(root,'testplatform/frontend/dist/assets');
 const cssName=fs.readdirSync(cssDir).find(n=>n.endsWith('.css'));
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:1250}});
  const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  let requests=0;
  await page.route('**/*',r=>{
   if(r.request().url().includes('/backtests/77/trade-chart')){requests++; return r.fulfill({json:context});}
   return r.abort();
  });
  await page.setContent('<html><head><style>'+fs.readFileSync(path.join(cssDir,cssName),'utf8')+'</style></head><body><div id="root"></div></body></html>');
  await page.evaluate(ctx=>{window.reviewContext=ctx;window.reviewCanvasTexts=[];
   const old=CanvasRenderingContext2D.prototype.fillText;
   CanvasRenderingContext2D.prototype.fillText=function(text,...args){window.reviewCanvasTexts.push(String(text));return old.call(this,text,...args);};
  },context);
  await page.addScriptTag({content:result.outputFiles[0].text});
  await page.getByText('Recorded P&L',{exact:true}).waitFor();
  await page.waitForTimeout(700);
  await page.screenshot({path:path.join(out,'popup-before.png'),fullPage:true});
  const before=await page.locator('svg.absolute path[fill="none"]').getAttribute('d');
  const canvasBefore=await page.locator('canvas').first().evaluate(el=>el.toDataURL());
  const main=await page.locator('.tv-lightweight-charts').boundingBox();
  if(main){
   await page.mouse.move(main.x+main.width-16,main.y+170);await page.mouse.down();
   await page.mouse.move(main.x+main.width-16,main.y+280,{steps:10});await page.mouse.up();
   await page.waitForTimeout(350);
  }
  const after=await page.locator('svg.absolute path[fill="none"]').getAttribute('d');
  const canvasAfter=await page.locator('canvas').first().evaluate(el=>el.toDataURL());
  await page.screenshot({path:path.join(out,'popup-after-axis-drag.png'),fullPage:true});
  const body=await page.locator('body').innerText();
  const markers=await page.evaluate(()=>window.reviewMarkers);
  const canvasTexts=await page.evaluate(()=>[...new Set(window.reviewCanvasTexts)]);
  const evidence={requests,errors,markerDates:markers.map(x=>x.time),
   markersSorted:markers.every((m,i)=>i===0||m.time>=markers[i-1].time),
   priceAxisDragChangedCanvas:canvasBefore!==canvasAfter,
   priceAxisDragUpdatedOverlay:before!==after,
   rendersGreeks:/Delta|Gamma|Theta|Vega|Open interest/.test(body),
   rendersPnlScale:canvasTexts.filter(t=>/P&L|expiration/i.test(t)),
   screenshotDirectory:out,canvasTexts};
  fs.writeFileSync(path.join(out,'evidence.json'),JSON.stringify(evidence,null,2));
  console.log(JSON.stringify(evidence,null,2));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
