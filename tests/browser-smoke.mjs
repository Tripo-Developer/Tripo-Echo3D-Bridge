import assert from 'node:assert/strict';
import { readFile, readdir, mkdir } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : 'playwright');
const browser=await chromium.launch({channel:'msedge',headless:true});
const errors=[];
const root=resolve('.');
const name='bridge-offline-smoke-'+Date.now()+'.glb';
// A valid GLB JSON chunk with enough padding to exercise two DCC chunks.
const json=Buffer.from(JSON.stringify({asset:{version:'2.0'},scene:0,scenes:[{}]}));
const jsonLength=5*1024*1024+4;
const data=Buffer.alloc(20+jsonLength,32);data.write('glTF');data.writeUInt32LE(2,4);data.writeUInt32LE(data.length,8);data.writeUInt32LE(jsonLength,12);data.writeUInt32LE(0x4e4f534a,16);json.copy(data,20);
const sha=createHash('sha256').update(data).digest('hex');
try{
 const context=await browser.newContext({viewport:{width:1280,height:1000},acceptDownloads:true});
 const studio=await context.newPage();studio.on('pageerror',e=>errors.push(e.message));
 await studio.goto(pathToFileURL(resolve('..','bridge-client.html')).href);
 await studio.getByRole('button',{name:'Connect to DCC',exact:true}).click();
 await studio.waitForFunction(()=>document.getElementById('connectionStatus').textContent.includes('Local save ready'));
 await studio.locator('#fileInput').setInputFiles({name,mimeType:'model/gltf-binary',buffer:data});
 await studio.getByRole('button',{name:'Send File',exact:true}).click();
 await studio.waitForFunction(()=>document.getElementById('progressText').textContent.includes('Saved locally'));
 const id=await studio.evaluate(()=>fileId);
 const inbox=join(process.env.LOCALAPPDATA,'Tripo','Echo3DBridge','received');
 let saved;
 for(const folder of await readdir(inbox)){
  try{const meta=JSON.parse(await readFile(join(inbox,folder,'metadata.json'),'utf8'));if(meta.fileId===id)saved={folder,meta};}catch{}
 }
 assert(saved,'saved metadata exists before Echo3D connects');assert.equal(saved.meta.delivered,false);
 assert.deepEqual(await readFile(join(inbox,saved.folder,saved.meta.storedName)),data);
 const echo=await context.newPage();echo.on('pageerror',e=>errors.push(e.message));
 await echo.goto('http://127.0.0.1:60710/demo');
 await echo.getByRole('button',{name:'Connect',exact:true}).click();
 const card=echo.locator('.card').filter({hasText:name});
 await card.waitFor({timeout:20000});
 assert((await card.textContent()).includes(sha));
 const downloadPromise=echo.waitForEvent('download');await card.getByRole('link',{name:'Download Model'}).click();
 const download=await downloadPromise;
 assert.deepEqual(await readFile(await download.path()),data);
 await mkdir('build/qa',{recursive:true});
 await echo.screenshot({path:'build/qa/echo3d-received.png',fullPage:true});
 await studio.screenshot({path:'build/qa/studio-local-saved.png',fullPage:true});
 await echo.getByRole('button',{name:'Disconnect',exact:true}).click();
 await studio.getByRole('button',{name:'Disconnect',exact:true}).click();
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({result:'PASS',scenario:'actual bridge-client.html -> packaged EXE offline save -> mock Echo3D page -> identical download',file:join(inbox,saved.folder,saved.meta.storedName),bytes:data.length,sha256:sha},null,2));
}finally{await browser.close();}
