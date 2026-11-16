/* Reload regression with shared browser storage and a fake API. Run after
   npm install --no-save jsdom@24 fake-indexeddb. */
const { JSDOM, VirtualConsole } = require('jsdom');
const { indexedDB } = require('fake-indexeddb');
const fs = require('fs');
const assert = require('assert');
const html = fs.readFileSync('web/index.html', 'utf8').replace('<script src="/static/app.js"></script>', '');
const js = fs.readFileSync('web/static/app.js', 'utf8');
const storage = new Map();
let account = 'a@example.com', parts = [], posts = 0, uploads = 0, failUpload = false, storageFull = false, signedOut = false;
let identifies = 0, hangIdentify = false;
// A completed identification: matched_part points at a record that no longer
// exists, which is what a stale restored match looks like.
const scanResult = { report: { overall_confidence: 'high', provider: 'gemini', grounded: false, part_name: 'PCV valve', oem_part_number: 'PCV-1', alternate_numbers: [], superseded_numbers: [], category: 'engine', technical_notes: [], fitment: [{ make: 'Toyota', model: 'Vitz', engine: '1NZ', years: '2006-2012' }], sources: [], search_queries: [] }, report_markdown: '**PCV valve** report', matched_part: { id: 999, part_name: 'Deleted record', verified: false, quantity: 2, unit_price: 500, currency: 'KES', image_path: '', bin_location: '' }, candidates: [], image_path: 'uploads/scan.jpg', lens_url: '' };
const wait = async (fn) => { for (let i=0;i<150;i++) { if (fn()) return; await new Promise(r=>setTimeout(r,10)); } throw Error('timed out'); };
async function page() {
  const console = new VirtualConsole();
  console.on('jsdomError', error => { if (!error.message.includes('Not implemented: navigation')) throw error; });
  const dom = new JSDOM(html, {url:'http://localhost/', runScripts:'outside-only', pretendToBeVisual:true, virtualConsole:console});
  const w = dom.window;
  Object.defineProperty(w, 'localStorage', {value:{getItem:k=>storage.get(k) ?? null, setItem:(k,v)=>{ if (storageFull) throw Error("quota"); storage.set(k,String(v)); }, removeItem:k=>storage.delete(k)}});
  w.indexedDB = indexedDB; w.Blob = Blob; w.File = File;
  w.URL.createObjectURL=()=> 'blob:test'; w.scrollTo=()=>{}; w.confirm=()=>true;
  w.fetch = async (path, options={}) => {
    let data, status = 200;
    if (path === '/api/auth/session') data = {authenticated:true,email:account};
    else if (path === '/api/config') data = {shop_name:'Test',tagline:'Test',max_upload_mb:10,currency:'KES'};
    else if (path === '/api/health') data = {vision_provider:'offline'};
    else if (path === '/api/stats') data = {parts:parts.length,units:0,inventory_value:0,out_of_stock:0};
    else if (path === '/api/demo-images') data = [];
    else if (path.startsWith('/api/parts?q=')) data = parts;
    else if (path === '/api/parts' && options.method === 'POST') { posts++; data = { ...JSON.parse(options.body), id: posts }; parts.push(data); }
    else if (/^\/api\/parts\/\d+\/image$/.test(path)) { uploads++; if (failUpload) {status=500;data={detail:'upload failed'};} else data=parts.find(p=>p.id===Number(path.split('/')[3])); }
    else if (/^\/api\/parts\/\d+$/.test(path)) { const id=Number(path.split('/')[3]); data=parts.find(p=>p.id===id); if (!data) {status=404;data={detail:'Not found'};} else if (options.method==='PATCH') Object.assign(data,JSON.parse(options.body)); }
    else if (path === '/api/identify') { identifies++; if (hangIdentify) return new Promise(()=>{}); data = scanResult; }
    else if (path === '/api/auth/signout') { signedOut = true; data={}; }
    else throw Error('Unexpected API '+path);
    return {status, ok:status<400, text:async()=>JSON.stringify(data)};
  };
  w.eval(js);
  await wait(()=>w.document.querySelector('#auth-email').textContent === account);
  await new Promise(r=>setTimeout(r,30));
  return dom;
}
const fire = (w, el, event='input') => el.dispatchEvent(new w.Event(event,{bubbles:true}));
const key = (email=account) => `verifix:${encodeURIComponent(email)}:draft:v1`;
const scanKey = (email=account) => `verifix:${encodeURIComponent(email)}:scan:v1`;
const deleteStoredPhoto = (photoKey) => new Promise((resolve, reject) => {
  const request = indexedDB.open('verifix-draft-photos', 1);
  request.onsuccess = () => { const db = request.result; const tx = db.transaction('photos','readwrite'); tx.objectStore('photos').delete(photoKey); tx.oncomplete = () => { db.close(); resolve(); }; tx.onerror = tx.onabort = () => { db.close(); reject(tx.error); }; };
  request.onerror = () => reject(request.error);
});
const pickIdentifyPhoto = (w, d, name) => { const file = new File(['identify bytes'], name, {type:'image/jpeg'}); Object.defineProperty(d.querySelector('#file-input'), 'files', {value:[file], configurable:true}); d.querySelector('#file-input').dispatchEvent(new w.Event('change', {bubbles:true})); };
(async()=>{
 let dom=await page(), w=dom.window, d=w.document;
 d.querySelector('#tab-inventory').click();
 d.querySelector('#search').value='probe'; fire(w,d.querySelector('#search')); d.querySelector('[data-stock-filter=out]').click(); d.querySelector('#sort-received').click();
 d.querySelector('#btn-new-part').click();
 let form=d.querySelector('#part-form');
 for (const [name,value] of Object.entries({part_name:'Recovery test',oem_part_number:'ABC',quantity:'7',unit_price:'123.5',image_path:'uploads/old.jpg'})) { form.elements[name].value=value; fire(w,form.elements[name]); }
 form.elements.verified.checked=true; fire(w,form.elements.verified,'change');
 const photo=new File(['photo bytes'],'part.jpg',{type:'image/jpeg'});
 Object.defineProperty(form.elements.part_image,'files',{value:[photo],configurable:true}); fire(w,form.elements.part_image,'change');
 await new Promise(r=>setTimeout(r,100));
 dom.window.dispatchEvent(new w.Event('pagehide')); dom.window.close();
 dom=await page(); w=dom.window; d=w.document; form=d.querySelector('#part-form');
 await wait(()=>d.querySelector('#part-photo-preview').textContent.includes('part.jpg'));
 assert(!d.querySelector('#drawer').hidden && d.querySelector('#tab-inventory').classList.contains('active'));
 assert.equal(d.querySelector('#search').value,'probe'); assert.equal(d.querySelector('[data-stock-filter=out]').getAttribute('aria-pressed'),'true'); assert(d.querySelector('#sort-received').textContent.includes('↓'));
 d.querySelector('#search').value=''; fire(w,d.querySelector('#search')); d.querySelector('[data-stock-filter=all]').click();
 assert.equal(form.elements.part_name.value,'Recovery test'); assert.equal(form.elements.quantity.value,'7'); assert.equal(form.elements.unit_price.value,'123.5'); assert(form.elements.verified.checked); assert.equal(form.elements.image_path.value,'uploads/old.jpg');
 failUpload=true; fire(w,form,'submit'); await wait(()=>uploads===1); await new Promise(r=>setTimeout(r,30));
 assert.equal(posts,1); assert.equal(JSON.parse(storage.get(key())).fields.id,'1'); assert(storage.has(key()));
 dom.window.close(); failUpload=false; dom=await page(); w=dom.window; d=w.document; form=d.querySelector('#part-form');
 await wait(()=>d.querySelector('#part-photo-preview').textContent.includes('part.jpg'));
 assert.equal(form.elements.id.value,'1'); fire(w,form,'submit'); await wait(()=>!storage.has(key()));
 assert.equal(posts,1); assert.equal(uploads,2); assert(d.querySelector('#drawer').hidden);
 // Edit survives reload, and a failed PATCH retains it.
 d.querySelector('[data-edit="1"]').click(); form=d.querySelector('#part-form'); form.elements.part_name.value='Edited locally'; fire(w,form.elements.part_name);
 dom.window.close(); dom=await page(); w=dom.window; d=w.document; form=d.querySelector('#part-form');
 assert.equal(form.elements.id.value,'1'); assert.equal(form.elements.part_name.value,'Edited locally');
 parts=[]; fire(w,form,'submit'); await new Promise(r=>setTimeout(r,40)); assert(storage.has(key()));
 d.querySelector('[data-close]').click(); assert(storage.has(key()));
 // Another account cannot see the first account's draft.
 dom.window.close(); account='b@example.com'; dom=await page(); w=dom.window; d=w.document;
 assert(d.querySelector('#drawer').hidden); d.querySelector('#btn-new-part').click();
 d.querySelector('#part-form').elements.part_name.value='Other account'; fire(w,d.querySelector('#part-form').elements.part_name);
 dom.window.close(); account='a@example.com'; dom=await page(); w=dom.window; d=w.document;
 assert.equal(d.querySelector('#part-form').elements.part_name.value,'Edited locally');
 w.confirm=()=>false; d.querySelector('#discard-draft').click(); assert(storage.has(key()));
 w.confirm=()=>true; d.querySelector('#discard-draft').click(); assert(!storage.has(key())); assert(storage.has(key('b@example.com')));
 // A missing photo is explicitly reported and cannot be silently saved.
 d.querySelector('#btn-new-part').click(); form=d.querySelector('#part-form'); form.elements.part_name.value='Missing photo'; fire(w,form.elements.part_name);
 let draft=JSON.parse(storage.get(key())); draft.photoKey='missing-photo'; storage.set(key(),JSON.stringify(draft));
 dom.window.close(); dom=await page(); w=dom.window; d=w.document; form=d.querySelector('#part-form');
 await wait(()=>d.querySelector('#draft-status').textContent.includes('could not be recovered'));
 fire(w,form,'submit'); await new Promise(r=>setTimeout(r,30)); assert.equal(posts,1);
 // An uncertain interrupted creation is never retried automatically.
 draft=JSON.parse(storage.get(key())); draft.photoKey=null; draft.uncertainCreate=true; storage.set(key(),JSON.stringify(draft));
 dom.window.close(); dom=await page(); w=dom.window; d=w.document; form=d.querySelector('#part-form');
 assert(d.querySelector('#draft-status').textContent.includes('may have completed')); fire(w,form,'submit'); await new Promise(r=>setTimeout(r,30)); assert.equal(posts,1);
 d.querySelector('#discard-draft').click();
 // Corrupt data is retained rather than submitted or silently erased.
 storage.set(key(),'{bad'); dom.window.close(); dom=await page(); w=dom.window; d=w.document;
 assert.equal(storage.get(key()),'{bad'); assert(d.querySelector('#drawer').hidden); assert.equal(posts,1);
 // Quota errors must be visible; they must not claim the draft was saved.
 storage.delete(key()); storageFull=true; d.querySelector('#btn-new-part').click();
 assert(d.querySelector('#draft-status').textContent.includes('could not be saved')); storageFull=false;
 d.querySelector('#discard-draft').click();
 // Explicit logout clears the current account, not another account's draft.
 d.querySelector('#btn-new-part').click(); assert(storage.has(key())); d.querySelector('#btn-signout').click();
 await wait(()=>signedOut); assert(!storage.has(key())); assert(storage.has(key('b@example.com')));
 // A finished identification is restored after a reload and never re-run.
 signedOut = false; account = 'a@example.com'; dom = await page(); w = dom.window; d = w.document;
 pickIdentifyPhoto(w, d, 'scan.jpg');
 await wait(()=>identifies===1);
 await wait(()=>d.querySelector('#report').textContent.includes('PCV valve'));
 assert(JSON.parse(storage.get(scanKey())).version === 1);
 dom.window.close(); identifies = 0;
 dom = await page(); w = dom.window; d = w.document;
 await wait(()=>d.querySelector('#report').textContent.includes('PCV valve'));
 await wait(()=>d.querySelector('#identify-status').textContent.includes('not re-run'));
 await new Promise(r=>setTimeout(r,60));
 assert.equal(identifies, 0);                                              // no automatic re-scan
 assert(d.querySelector('#match-body').textContent.includes('Deleted record'));
 assert(d.querySelector('#identify-status').textContent.includes('#999'));  // stale match is disclosed
 assert(!d.querySelector('#btn-identify').disabled && !d.querySelector('#preview').hidden); // photo came back
 // Losing only the photo is reported honestly while the report stays usable.
 await deleteStoredPhoto(`${account}:identify`);
 dom.window.close(); identifies = 0; dom = await page(); w = dom.window; d = w.document;
 await wait(()=>d.querySelector('#report').textContent.includes('PCV valve'));
 await wait(()=>d.querySelector('#identify-status').textContent.includes('photo is no longer on this device'));
 assert.equal(identifies, 0);
 assert(d.querySelector('#btn-identify').disabled);                         // nothing to re-identify with
 // An identification interrupted by the tab switch is not restarted, and the
 // completed report from before it is still there.
 hangIdentify = true; pickIdentifyPhoto(w, d, 'interrupted.jpg');
 await wait(()=>identifies===1);
 dom.window.close(); hangIdentify = false; identifies = 0;
 dom = await page(); w = dom.window; d = w.document;
 await wait(()=>d.querySelector('#report').textContent.includes('PCV valve'));
 await new Promise(r=>setTimeout(r,60));
 assert.equal(identifies, 0);
 // A corrupt report is cleared, not re-run and not shown as if it were valid.
 storage.set(scanKey(), '{bad'); dom.window.close(); identifies = 0;
 dom = await page(); w = dom.window; d = w.document;
 await wait(()=>d.querySelector('#identify-status').textContent.includes('could not be read'));
 assert.equal(storage.get(scanKey()), null);
 assert.equal(identifies, 0);
 assert(!d.querySelector('#report-empty').hidden);
 // Signing out clears this account's saved identification, not another's.
 pickIdentifyPhoto(w, d, 'scan.jpg');
 await wait(()=>identifies===1);
 await wait(()=>!!storage.get(scanKey()));
 account = 'b@example.com'; storage.set(scanKey(), JSON.stringify({version:1, savedAt:Date.now(), result:scanResult}));
 account = 'a@example.com'; signedOut = false;
 d.querySelector('#btn-signout').click();
 await wait(()=>signedOut);
 assert.equal(storage.get(scanKey()), null);
 assert(!!storage.get(scanKey('b@example.com')));
 console.log('PASS: reload, new/edit, photo, partial failure and retry, cleanup, discard, account isolation, identification recovery'); dom.window.close();
})().catch(e=>{console.error(e);process.exitCode=1;});
