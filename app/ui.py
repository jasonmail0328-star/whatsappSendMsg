# app/ui.py
from typing import List, Tuple
from . import db
import html

def esc(s):
    return html.escape(str(s) if s is not None else "")

def render_main_page(accounts_rows: List[Tuple]) -> str:
    rows_html = ""
    for r in accounts_rows:
        if len(r) >= 7:
            account_id, profile_path, phone, status, today_sent, last_used, in_use = r
        else:
            account_id, profile_path, phone, status, today_sent, last_used = r
            in_use = 0
        disabled_attr = "disabled" if in_use else ""
        busy_label = "（忙）" if in_use else ""
        rows_html += "<tr><td>{0}</td><td>{1}</td><td>{2}</td><td>{3}</td><td>{4}</td><td>{5}</td><td><button class='send-btn' data-acc='{0}' {6}>发送{7}</button> <button class='del-btn' data-acc='{0}'>删除</button></td></tr>".format(
            esc(account_id), esc(phone or ""), esc(status), esc(today_sent or 0), esc(last_used or ""), esc(profile_path), disabled_attr, busy_label
        )

    prefix = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>WhatsApp 多账号 管理器（Web）</title>
  <style>
    body{font-family:Arial, Helvetica, sans-serif; margin:20px}
    button{padding:6px 10px;margin:4px}
    table{border-collapse:collapse;width:100%}
    th,td{border:1px solid #ddd;padding:6px}
    th{background:#f2f2f2}
    textarea{width:100%;height:80px}
    #send_status{white-space:pre-wrap;background:#f9f9f9;padding:8px;border:1px solid #eee;margin-top:8px}
    #bulk_progress{height:18px;background:#eee;border:1px solid #ddd; width:100%; margin-top:8px}
    #bulk_progress_bar{height:100%; width:0%; background:linear-gradient(90deg,#4caf50,#81c784)}
  </style>
</head>
<body>
  <h2>WhatsApp 多账号 管理器（Web）</h2>
  <div>
    <button id="btn-init">初始化 DB</button>
    <button id="btn-add">添加账号（扫码）</button>
    <button id="btn-accounts">查看账号列表</button>
  </div>
  <hr/>
  <h3>消息内容</h3>
  <textarea id="message_text" placeholder="在此输入要发送的消息"></textarea>
  <div><label><input type="checkbox" id="dry_run"> Dry-run（仅模拟）</label></div>
  <div style="margin-top:8px;"><label><input type="checkbox" id="auto_shutdown"> 关闭页面时自动关闭后台服务器</label></div>
  <hr/>
  <h3>单账号发送</h3>
  <table id="accounts_table">
    <thead><tr><th>account_id</th><th>phone</th><th>status</th><th>today_sent</th><th>last_used</th><th>profile_path</th><th>操作</th></tr></thead>
    <tbody>
"""

    bulk_and_suffix = """
    </tbody>
  </table>
  <hr/>
  <h3>批量发送</h3>
  <div>
    <label>发送总数（N）：<input type="number" id="bulk_count" value="5" min="1" style="width:80px"/></label>
    <label style="margin-left:12px"><input type="checkbox" id="per_account"> 每个账号各发送一条（忽略 N）</label>
    <button id="btn-bulk">开始批量发送</button>
  </div>
  <div id="bulk_progress"><div id="bulk_progress_bar"></div></div>
  <div id="bulk_status" style="margin-top:8px;background:#f6f6f6;padding:8px;border:1px solid #eee">无批量任务</div>
  <hr/>
  <div id="add_box" style="margin-top:20px;display:none">
    <h3>添加账号进度</h3>
    <div id="add_status">等待中...</div>
    <div style="margin-top:8px"><button id="btn-stop-add">关闭</button></div>
  </div>

  <div id="send_box" style="margin-top:20px">
    <h3>发送任务状态</h3>
    <div id="send_status">无任务</div>
  </div>

<script>
const API = {
  add: '/add',
  add_status: (sid)=>`/add_status/${sid}`,
  send: '/send',
  send_status: (sid)=>`/send_status/${sid}`,
  bulk: '/bulk_send',
  bulk_status: (sid)=>`/bulk_status/${sid}`,
  delete: '/delete_account',
  accounts: '/accounts'
};

function ajaxJson(url, opts={}) {
  return fetch(url, opts).then(r=>{
    if(!r.ok) throw new Error('HTTP '+r.status);
    return r.json();
  });
}

function showStatus(el, obj){
  document.getElementById(el).innerText = JSON.stringify(obj, null, 2);
}

document.getElementById('btn-init').addEventListener('click', ()=>{ location.href='/init'; });
document.getElementById('btn-accounts').addEventListener('click', ()=>{ location.href='/accounts'; });

// Add-account logic with auto-refresh on registered
document.getElementById('btn-add').addEventListener('click', async ()=>{
  try{
    const res = await ajaxJson(API.add, {method:'POST'});
    const sid = res.session_id;
    document.getElementById('add_box').style.display='block';
    let poll = setInterval(async ()=>{
      try{
        const st = await ajaxJson(API.add_status(sid));
        showStatus('add_status', st);
        if(st.status === 'done' || st.status === 'failed' || st.status === 'error' || (st.status==='done' && st.result && st.result.success)){
          clearInterval(poll);
          setTimeout(()=>{ location.reload(); }, 700);
        }
        if(st.status === 'failed' || st.status==='error'){
          clearInterval(poll);
        }
      }catch(e){
        console.error(e);
        clearInterval(poll);
      }
    }, 1000);
    document.getElementById('btn-stop-add').onclick = ()=>{ clearInterval(poll); document.getElementById('add_box').style.display='none'; };
  }catch(e){
    alert('请求失败: '+e);
  }
});

// Delegate send & delete buttons
document.getElementById('accounts_table').addEventListener('click', async (ev)=>{
  const btn = ev.target;
  if(btn.classList.contains('send-btn')){
    const acc = btn.dataset.acc;
    const msg = document.getElementById('message_text').value;
    if(!msg || !msg.trim()){ alert('请输入要发送的消息'); return; }
    const dry = document.getElementById('dry_run').checked;
    btn.disabled = true;
    try{
      const res = await ajaxJson(API.send, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({account_id: acc, message: msg, dry_run: dry})});
      if(res.session_id){
        document.getElementById('send_status').innerText = '任务已创建: ' + res.session_id;
        let sid = res.session_id;
        let spoll = setInterval(async ()=>{
          try{
            const st = await ajaxJson(API.send_status(sid));
            showStatus('send_status', st);
            if(['done','failed','error','rejected'].includes(st.status)){
              clearInterval(spoll);
              btn.disabled = false;
              location.reload();
            }
          }catch(e){
            console.error(e);
            clearInterval(spoll);
            btn.disabled = false;
          }
        }, 1000);
      } else {
        alert('创建任务失败: '+JSON.stringify(res));
        btn.disabled = false;
      }
    }catch(e){
      alert('请求失败: '+e);
      btn.disabled = false;
    }
  } else if(btn.classList.contains('del-btn')){
    const acc = btn.dataset.acc;
    if(!confirm('确认删除账号 '+acc+' ?')) return;
    const delProfile = confirm('同时删除 profile 目录？');
    const delMsgs = confirm('同时删除发送记录？');
    try{
      const res = await ajaxJson(API.delete, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({account_id: acc, remove_profile: delProfile, remove_messages: delMsgs})});
      if(res.ok){ alert('已删除'); location.reload(); } else { alert('删除失败: '+JSON.stringify(res)); }
    }catch(e){ alert('请求失败: '+e); }
  }
});

// Bulk
document.getElementById('btn-bulk').addEventListener('click', async ()=>{
  const msg = document.getElementById('message_text').value;
  if(!msg || !msg.trim()){ alert('请输入要发送的消息'); return; }
  const dry = document.getElementById('dry_run').checked;
  const per = document.getElementById('per_account').checked;
  const count = parseInt(document.getElementById('bulk_count').value || '0', 10);
  if(!per && (!count || count<=0)){ alert('请输入有效总数'); return; }
  try{
    const res = await ajaxJson(API.bulk, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({count: count, per_account: per, message: msg, dry_run: dry})});
    const sid = res.session_id;
    document.getElementById('bulk_status').innerText = '批量任务已创建: ' + sid;
    let poll = setInterval(async ()=>{
      try{
        const st = await ajaxJson(API.bulk_status(sid));
        showStatus('bulk_status', st);
        if(st.result && st.result.results){
          const total = per ? st.result.results.length : (st.result.requested_count || 0);
          const done = st.result.results.filter(r => r.result && r.result.ok).length;
          const perc = total>0 ? Math.min(100, Math.round((done/Math.max(1,total))*100)) : 0;
          document.getElementById('bulk_progress_bar').style.width = perc + '%';
        }
        if(['done','failed','error','rejected'].includes(st.status)){
          clearInterval(poll);
          setTimeout(()=>{ location.reload(); }, 700);
        }
      }catch(e){
        console.error(e);
        clearInterval(poll);
      }
    }, 1500);
  }catch(e){
    alert('请求失败: '+e);
  }
});

// auto shutdown
window.addEventListener('beforeunload', function (e) {
  try {
    var auto = document.getElementById('auto_shutdown').checked;
    if(auto){
      navigator.sendBeacon('/shutdown', '');
    }
  } catch (err) {}
});
</script>
</body>
</html>
"""

    return prefix + rows_html + bulk_and_suffix

def render_accounts_page(rows: List[Tuple]) -> str:
    rows_html = ""
    for r in rows:
        try:
            if len(r) >= 7:
                account_id, profile_path, phone, status, today_sent, last_used, in_use = r
            else:
                account_id, profile_path, phone, status, today_sent, last_used = r
                in_use = 0
        except Exception:
            account_id = r[0] if len(r) > 0 else ""
            profile_path = r[1] if len(r) > 1 else ""
            phone = r[2] if len(r) > 2 else ""
            status = r[3] if len(r) > 3 else ""
            today_sent = r[4] if len(r) > 4 else 0
            last_used = r[5] if len(r) > 5 else ""
            in_use = r[6] if len(r) > 6 else 0
        rows_html += "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            esc(account_id), esc(phone), esc(status), esc(today_sent), esc(last_used), esc(profile_path)
        )
    html_page = """<!doctype html>
<html>
<head><meta charset="utf-8"/><title>Accounts</title>
<style>body{font-family:Arial}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ddd;padding:6px}th{background:#f2f2f2}</style>
</head><body><h2>Accounts</h2><a href="/">返回</a><table><thead><tr><th>account_id</th><th>phone</th><th>status</th><th>today_sent</th><th>last_used</th><th>profile_path</th></tr></thead><tbody>""" + rows_html + "</tbody></table></body></html>"
    return html_page