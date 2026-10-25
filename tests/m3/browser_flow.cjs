// Optional browser behavior test. Start tests.m3.browser_server first; requires Playwright.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const base = 'http://127.0.0.1:8913';
(async()=>{
  const browser=await chromium.launch({headless:true,channel:process.env.M3_TEST_BROWSER_CHANNEL || 'chrome'});
  const page=await browser.newPage({viewport:{width:1280,height:900}});
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  const signin=async account=>{console.log('Role:',account);await page.goto(base+'/__test__/signin/'+account);await page.locator('#identity').filter({hasText:account}).waitFor();};
  const tab=async text=>{await page.getByRole('tab',{name:text,exact:true}).click();};
  page.on('response',response=>{if(response.status()>=400)console.log('HTTP',response.status(),new URL(response.url()).pathname);});
  const notice=async text=>{await page.locator('#notice').filter({hasText:text}).waitFor();};
  const action=async (label, field, value)=>{
    await page.getByRole('button',{name:label,exact:true}).click();
    if(field)await page.getByRole('dialog').getByLabel(field).fill(value);
    await page.getByRole('dialog').getByRole('button',{name:'确认'+label,exact:true}).click();
    await page.getByRole('dialog').waitFor({state:'hidden'});
  };
  const report=async location=>{
    await tab('提交报修');await page.getByLabel('报修教室').selectOption('sp-school');
    await page.getByLabel('具体位置').fill(location);await page.getByLabel('故障描述').fill('浏览器测试：灯具无法正常使用');
    await page.getByRole('button',{name:'提交报修',exact:true}).click();
  };
  try{
    await signin('student1');await report('走廊灯具');await notice('报修已提交');
    assert.equal(await page.getByRole('tab',{name:'派单管理'}).count(),0);
    assert.equal(await page.getByRole('tab',{name:'派单管理'}).isVisible(),false);
    await signin('dispatcher');
    await page.getByRole('button',{name:'选择承接人'}).click();
    await page.getByLabel('承接人',{exact:true}).selectOption('u-technician');
    await page.getByRole('button',{name:'确认派单'}).click();await notice('已派单');
    await signin('technician');await action('开始维修');await notice('维修中');
    await action('提交完工','完工说明','更换灯具，待验收');await notice('待验收');
    await signin('inspector');await action('退回返修','退回原因（选填）','亮度仍不足');await notice('维修中');
    await signin('technician');await action('提交完工','完工说明','调整灯具亮度');await notice('待验收');
    await signin('inspector');await action('验收通过','验收说明（选填）','照明恢复');await notice('已验收');
    await signin('student1');await tab('处理任务');await action('提交评价','维修评价','维修后正常使用');await notice('已验收');
    await report('重复报修');await notice('报修已提交');await tab('工单查询');
    const duplicate=page.locator('#orders-list .card').filter({has:page.getByRole('heading',{name:'重复报修',exact:true})});
    await duplicate.getByRole('button',{name:'取消工单'}).click();
    await page.getByLabel('取消原因（选填）').fill('重复提交');await page.getByRole('button',{name:'确认取消工单'}).click();await notice('已取消');
    await page.request.post(base+'/__test__/lose-next-report');
    await report('回执丢失测试');await notice('服务暂时无法确认结果');
    await page.reload();await page.getByRole('button',{name:'核实并重试原请求'}).waitFor();
    await page.getByRole('button',{name:'核实并重试原请求'}).click();await notice('待派单');
    await page.locator('#pending').waitFor({state:'hidden'});
    await tab('工单查询');await page.locator('#orders-list .card').nth(2).waitFor();assert.equal(await page.locator('#orders-list .card').count(),3);
    await signin('admin');await tab('维修统计');
    await page.getByLabel('开始日期',{exact:true}).filter({visible:true}).fill('2026-10-14');
    await page.getByLabel('结束日期（不含）',{exact:true}).filter({visible:true}).fill('2026-10-17');
    await page.getByRole('button',{name:'查看统计'}).click();
    await page.locator('#stats-result .stat strong').first().filter({hasText:'3'}).waitFor();
    assert.equal(await page.locator('#stats-result').getByText('已验收：1',{exact:true}).count(),1);
    assert.equal(await page.locator('#stats-result').getByText('已取消：1',{exact:true}).count(),1);
    const dir=path.resolve('tmp/m3-browser');fs.mkdirSync(dir,{recursive:true});
    await page.screenshot({path:path.join(dir,'statistics.png'),fullPage:true});
    await tab('工单查询');await page.locator('#view').selectOption('MANAGED');await page.getByRole('button',{name:'查询',exact:true}).click();
    await page.locator('#orders-list .card').filter({has:page.getByRole('heading',{name:'走廊灯具',exact:true})}).getByRole('button',{name:'查看进度'}).click();
    await page.getByRole('dialog').getByText('验收退回返修',{exact:false}).waitFor();
    await page.screenshot({path:path.join(dir,'progress.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(dir,'mobile.png'),fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    assert.deepEqual(errors,[]);
    console.log('Browser flow passed: report, dispatch, repair, return, acceptance, evaluation, cancellation, lost-response replay, statistics, desktop/mobile layout. D/M1 simulated.');
  }catch(error){
    const dir=path.resolve('tmp/m3-browser');fs.mkdirSync(dir,{recursive:true});
    await page.screenshot({path:path.join(dir,'failure.png'),fullPage:true});
    console.error('Last notice:',await page.locator('#notice').textContent());
    console.error('Page errors:',errors);
    throw error;
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
