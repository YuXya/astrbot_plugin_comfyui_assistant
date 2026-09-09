const fs=require('node:fs'); const path=require('node:path'); const assert=require('node:assert/strict'); const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
 const root=path.resolve(__dirname,'../pages/assistant'); const out=path.resolve(__dirname,'../validation/ui');fs.mkdirSync(out,{recursive:true});
 try{
  const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('http://assistant.test/**',r=>{const file=new URL(r.request().url()).pathname.slice(1)||'index.html';if(!['index.html','style.css','app.js'].includes(file))return r.abort();return r.fulfill({body:fs.readFileSync(path.join(root,file)),contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'});});
  await page.addInitScript(()=>{
   const graph={'51':{class_type:'Simple String',inputs:{string:'test'}},'52':{class_type:'Simple String',inputs:{string:'second text'}},'82':{class_type:'EmptyLatentImage',inputs:{width:1024,height:1024,batch_size:1}},'29':{class_type:'SaveImage',inputs:{images:['82',0]}}};
   const bindings={texts:[{node:'51',input:'string',label:'绘画描述'},{node:'52',input:'string',label:'第二段描述'}],images:[],width:[{node:'82',input:'width'}],height:[{node:'82',input:'height'}],outputs:['29']};
   window.fixture={version:'0.1.2',settings:{server_url:'http://127.0.0.1:8188',min_edge:256,max_edge:2048,multiple:8,max_pixels:2097152,record_days:30,image_days:7,poll_seconds:3,tracking_minutes:30},workflows:['文生图','图生图','多图生图'].map((name,i)=>({id:String(i),name:'Krea2'+name,enabled:true,short:'根据描述与参考图完成绘制。',detailed:'测试说明',graph,bindings})),tasks:[{id:'test-task',workflow_name:'Krea2文生图',generation:'completed',delivery:'failed',start:'sent',error:'平台未确认发送成功',created:Date.now()/1000,width:768,height:1024,scope:'fixture:GroupMessage:100',owner:'fixture',attempts:[],files_expired:false}]};
   window.actions=[];window.AstrBotPluginPage={ready:async()=>{},apiGet:async()=>structuredClone(window.fixture),apiPost:async(route,data)=>{window.actions.push(data);if(data.action==='save_workflow'){const i=window.fixture.workflows.findIndex(w=>w.id===data.workflow.id);if(i>=0)window.fixture.workflows[i]=data.workflow;else window.fixture.workflows.push({...data.workflow,id:'new'});}if(data.action==='delete_workflow')window.fixture.workflows=window.fixture.workflows.filter(w=>w.id!==data.id);if(data.action==='settings')window.fixture.settings={...window.fixture.settings,...data.settings};if(data.action==='resend')window.fixture.tasks[0].delivery='sent';return {ok:true};}};
  });
  for(const width of [1500,390]){
   await page.setViewportSize({width,height:1050});await page.goto('http://assistant.test');await page.getByRole('button',{name:'编辑',exact:true}).first().waitFor();
   assert.equal(await page.locator('.card').count(),3);assert.equal(await page.locator('img').count(),0);
   await page.screenshot({path:path.join(out,`workflows-${width}.png`),fullPage:true});
   const openEditor=()=>page.getByRole('button',{name:'编辑',exact:true}).first().click();
   const saveEditor=async()=>{await page.getByRole('button',{name:'保存工作流',exact:true}).click();await page.locator('dialog').waitFor({state:'hidden'});};
   const prefix=page.getByLabel('默认提示词前缀',{exact:true}),target=page.getByLabel('前缀应用位置',{exact:true});
   const configured='masterpiece, best quality,\n高质量，source_anime,';
   const first=JSON.stringify(['51','string']),second=JSON.stringify(['52','string']);
   await openEditor();assert.equal(await prefix.inputValue(),'');assert.equal(await target.inputValue(),first);
   const scale=page.getByLabel('图片大小写入缩放',{exact:true});assert.equal(await scale.inputValue(),'1');await scale.fill('0.5');
   await page.getByLabel('工作流名称',{exact:true}).fill('Krea2文生图-测试');await page.getByLabel('详细介绍',{exact:true}).fill('新的详细介绍');
   await prefix.fill(configured);await target.selectOption(second);
   const textSection=page.locator('.binding-section').filter({has:page.getByRole('heading',{name:'文字入口',exact:true})});
   await textSection.getByRole('button',{name:'＋ 入口',exact:true}).click();
   assert.equal(await prefix.inputValue(),configured);assert.equal(await target.inputValue(),second);assert.equal(await scale.inputValue(),'0.5');
   await textSection.getByRole('button',{name:'×',exact:true}).last().click();
   await saveEditor();await openEditor();
   assert.equal(await prefix.inputValue(),configured);assert.equal(await target.inputValue(),second);assert.equal(await scale.inputValue(),'0.5');
   await page.screenshot({path:path.join(out,`prefix-${width}.png`),fullPage:true});
   assert.equal(await page.evaluate(()=>document.querySelector('dialog').scrollWidth>document.querySelector('dialog').clientWidth),false);
   // Removing an earlier slot must not change the configured node/field target.
   await textSection.getByRole('button',{name:'×',exact:true}).first().click();
   assert.equal(await target.inputValue(),second);assert.equal(await prefix.inputValue(),configured);
   await page.getByRole('button',{name:'关闭',exact:true}).click();await openEditor();
   // Removing the selected target requires an explicit replacement.
   await textSection.getByRole('button',{name:'×',exact:true}).last().click();
   assert.equal(await target.inputValue(),'');assert.equal(await target.evaluate(e=>e.validity.valueMissing),true);
   const before=await page.evaluate(()=>window.actions.length);
   await page.getByRole('button',{name:'保存工作流',exact:true}).click();
   assert.equal(await page.evaluate(()=>window.actions.length),before);
   await target.selectOption(first);await saveEditor();await openEditor();
   assert.equal(await prefix.inputValue(),configured);assert.equal(await target.inputValue(),first);
   await prefix.fill('');await saveEditor();await openEditor();assert.equal(await prefix.inputValue(),'');
   await prefix.fill(configured);await saveEditor();
   await page.locator('[data-tab=tasks]').click();await page.screenshot({path:path.join(out,`tasks-${width}.png`),fullPage:true});
   page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'重发原图',exact:true}).click();await page.getByText('调用成功',{exact:true}).waitFor();
   await page.locator('[data-tab=settings]').click();await page.getByLabel('最大边长',{exact:true}).fill('1920');await page.getByLabel('AI 回复编辑（开始提示）',{exact:true}).fill('在画了老大，大小：{size}');await page.getByRole('button',{name:'保存设置',exact:true}).click();
   assert.equal(await page.evaluate(()=>window.fixture.settings.max_edge),1920);
   assert.equal(await page.evaluate(()=>window.fixture.settings.start_message_template),'在画了老大，大小：{size}');
   await page.locator('[data-tab=workflows]').click();await page.locator('[data-tab=settings]').click();assert.equal(await page.getByLabel('AI 回复编辑（开始提示）',{exact:true}).inputValue(),'在画了老大，大小：{size}');
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
   await page.screenshot({path:path.join(out,`settings-${width}.png`),fullPage:true});
   await page.locator('[data-tab=workflows]').click();await page.getByRole('button',{name:'编辑',exact:true}).first().click();await page.screenshot({path:path.join(out,`editor-${width}.png`),fullPage:true});assert.equal(await page.evaluate(()=>document.querySelector('dialog').scrollWidth>document.querySelector('dialog').clientWidth),false);
   await page.getByRole('button',{name:'关闭',exact:true}).click();
   const graph=await page.evaluate(()=>window.fixture.workflows[0].graph);
   await page.locator('#upload').setInputFiles({name:'upload-check.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(graph))});
   await page.locator('dialog').waitFor({state:'visible'});
   assert.equal(await prefix.inputValue(),'');assert.equal(await scale.inputValue(),'1');
   await page.getByLabel('工作流名称',{exact:true}).fill('上传检查');
   await page.getByRole('button',{name:'保存工作流',exact:true}).click();await page.locator('dialog').waitFor({state:'hidden'});
   assert.equal(await page.locator('.card').count(),4);
   const card=page.getByRole('article').filter({hasText:'上传检查'});page.once('dialog',d=>d.accept());await card.getByRole('button',{name:'删除',exact:true}).click();await page.waitForFunction(()=>window.fixture.workflows.length===3);
  }
  assert.deepEqual(errors,[]);console.log('Desktop/mobile UI passed: size scale and start-template save/reopen, prefix save/clear, stable target, deleted-target validation, upload, delete, edit, settings, resend, no image previews, no overflow.');
 }finally{await browser.close();}
})();
