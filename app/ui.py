# app/ui.py
# Render main HTML page. Use placeholder replacement to avoid Python f-string parsing JS braces.

from html import escape

def _render_accounts_table(rows):
    out = []
    out.append('<table id="accounts_table" border="1" cellpadding="6" style="border-collapse:collapse; width:100%;">')
    out.append('<thead><tr><th>Account ID</th><th>Phone/Info</th><th>Profile Path</th><th>Actions</th></tr></thead>')
    out.append('<tbody>')
    for r in rows or []:
        try:
            acc = escape(str(r[0]))
            prof = escape(str(r[1]) if len(r) > 1 else "")
            phone = escape(str(r[2]) if len(r) > 2 and r[2] is not None else "")
        except Exception:
            acc = escape(str(r))
            prof = ""
            phone = ""
        out.append(
            '<tr>'
            f'<td>{acc}</td>'
            f'<td>{phone}</td>'
            f'<td style="max-width:320px; overflow:hidden; text-overflow:ellipsis;">{prof}</td>'
            '<td>'
            f'<button class="send-btn" data-acc="{acc}">发送</button> '
            f'<button class="del-btn" data-acc="{acc}">删除</button>'
            '</td>'
            '</tr>'
        )
    out.append('</tbody></table>')
    return "\n".join(out)

def render_main_page(rows):
    accounts_table = _render_accounts_table(rows)

    html_template = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>WhatsApp Send Manager</title>
  <style>
    body { font-family: Arial, Helvetica, sans-serif; margin: 16px; }
    textarea { width: 100%; height: 120px; }
    .control-row { margin-bottom: 12px; }
    #add_box, #bulk_status_box { margin-top: 8px; padding: 8px; border: 1px solid #ddd; display:none; background:#f9f9f9; }
    pre.status { white-space: pre-wrap; word-break: break-word; max-height: 240px; overflow:auto; }
    button[disabled] { opacity: 0.6; }
  </style>
</head>
<body>
  <h2>WhatsApp Send Manager</h2>

  <div class="control-row">
    <button id="btn-add">添加账号（扫码）</button>
    <div id="add_box">
      <div>添加状态: <pre id="add_status" class="status"></pre></div>
    </div>
  </div>

  <h3>账户列表</h3>
  <div>
    __ACCOUNTS_TABLE__
  </div>

  <h3>发送消息</h3>
  <div class="control-row">
    <label>消息内容：</label><br/>
    <textarea id="message_text" placeholder="在此输入要发送的消息"></textarea>
  </div>
  <div class="control-row">
    <label><input type="checkbox" id="dry_run"> 仅模拟（Dry Run）</label>
  </div>

  <div class="control-row">
    <button id="btn-bulk-send">开始批量发送</button>
    <button id="btn-bulk-stop" style="display:none;">停止批量发送</button>
  </div>

  <div class="control-row">
    <label>发送总数 / 轮数（当未勾选“每个账号各发送一条”时，count 表示轮数，每轮所有账号各发一条）: 
      <input type="number" id="bulk_count" value="1" min="0" style="width:80px;">
    </label>
    &nbsp;&nbsp;
    <label><input type="checkbox" id="per_account"> 每个账号各发送一条（忽略 count）</label>
  </div>

  <div class="control-row">
    <label>账号间延迟（秒）: <input type="number" id="account_interval" step="0.1" value="1" style="width:80px;"></label>
    &nbsp;&nbsp;
    <label>轮间延迟（秒）: <input type="number" id="round_interval" step="0.1" value="5" style="width:80px;"></label>
  </div>

  <div id="bulk_status_box">
    <div>批量任务状态: <pre id="bulk_status" class="status"></pre></div>
  </div>

  <div class="control-row">
    <div>单账号发送状态: <pre id="send_status" class="status"></pre></div>
  </div>

<script>
(function(){
  async function ajaxJson(url, opts) {
    const resp = await fetch(url, Object.assign({credentials: 'same-origin'}, opts || {}));
    const ct = resp.headers.get('content-type') || '';
    if (!resp.ok) {
      let text = await resp.text().catch(()=> "");
      let body = null;
      try { body = JSON.parse(text); } catch(e) { body = text; }
      const err = body && body.error ? body.error : resp.statusText || 'HTTP error';
      throw new Error(err + (typeof body === 'string' ? (": "+body) : ""));
    }
    if (ct.indexOf('application/json') !== -1) {
      return resp.json();
    } else {
      return resp.text();
    }
  }

  const API = {
    add: '/add',
    add_status: (sid) => '/add_status/' + encodeURIComponent(sid),
    send: '/send',
    send_status: (sid) => '/send_status/' + encodeURIComponent(sid),
    bulk_send: '/bulk_send',
    bulk_status: (sid) => '/bulk_status/' + encodeURIComponent(sid),
    bulk_cancel: '/bulk_cancel',
    delete_account: '/delete_account'
  };

  const btnAdd = document.getElementById('btn-add');
  if (btnAdd) {
    btnAdd.addEventListener('click', async function(){
      const btn = this;
      if (btn.disabled) return;
      btn.disabled = true;
      try {
        const res = await ajaxJson(API.add, { method: 'POST' });
        if (res && res.session_id) {
          document.getElementById('add_box').style.display = 'block';
          document.getElementById('add_status').innerText = '排队中... session=' + res.session_id;
          const poll = setInterval(async function(){
            try {
              const s = await ajaxJson(API.add_status(res.session_id));
              document.getElementById('add_status').innerText = JSON.stringify(s, null, 2);
              if (s && (s.status === 'done' || s.status === 'failed' || s.status === 'error')) {
                clearInterval(poll);
                document.getElementById('add_box').style.display = 'none';
                location.reload();
              }
            } catch(e) {
              console.error('add poll fail', e);
            }
          }, 1000);
        } else {
          alert('启动添加账号失败');
          btn.disabled = false;
        }
      } catch(e) {
        console.error('add failed', e);
        alert('启动添加失败: ' + e);
        btn.disabled = false;
      }
    });
  }

  let BULK_POLL_TIMER = null;
  let CURRENT_BULK_SID = null;

  function startBulkPolling(sid) {
    CURRENT_BULK_SID = sid;
    document.getElementById('bulk_status_box').style.display = 'block';
    document.getElementById('btn-bulk-stop').style.display = 'inline';
    const interval = 1500;
    BULK_POLL_TIMER = setInterval(async function(){
      try {
        const s = await ajaxJson(API.bulk_status(sid));
        document.getElementById('bulk_status').innerText = JSON.stringify(s, null, 2);
        if (s && (s.status === 'done' || s.status === 'failed' || s.status === 'partial' || s.status === 'cancelled' || s.status === 'error')) {
          stopBulkPolling();
          document.getElementById('btn-bulk-send').disabled = false;
        }
      } catch(e) {
        console.error('bulk status poll failed', e);
      }
    }, interval);
  }

  function stopBulkPolling() {
    if (BULK_POLL_TIMER) {
      clearInterval(BULK_POLL_TIMER);
      BULK_POLL_TIMER = null;
    }
    CURRENT_BULK_SID = null;
    document.getElementById('btn-bulk-stop').style.display = 'none';
    const b = document.getElementById('btn-bulk-send');
    if (b) b.disabled = false;
  }

  const btnBulk = document.getElementById('btn-bulk-send');
  if (btnBulk) {
    btnBulk.addEventListener('click', async function(){
      const btn = this;
      if (btn.disabled) return;
      const count = parseInt(document.getElementById('bulk_count').value || '0', 10);
      const per_account = document.getElementById('per_account').checked;
      const message = document.getElementById('message_text').value;
      const dry = document.getElementById('dry_run').checked;
      const account_interval = parseFloat(document.getElementById('account_interval').value || '1');
      const round_interval = parseFloat(document.getElementById('round_interval').value || '5');
      if (!message || !message.trim()) { return alert('请输入要发送的消息'); }
      btn.disabled = true;
      try {
        const resp = await ajaxJson(API.bulk_send, { method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ count: count, per_account: per_account, message: message, dry_run: dry, account_delay: account_interval, round_delay: round_interval })
        });
        if (resp && resp.session_id) {
          document.getElementById('bulk_status_box').style.display = 'block';
          document.getElementById('bulk_status').innerText = '已排队, session=' + resp.session_id;
          startBulkPolling(resp.session_id);
        } else {
          alert('无法启动批量发送任务');
          btn.disabled = false;
        }
      } catch(e) {
        console.error('bulk_send failed', e);
        alert('批量发送请求失败: ' + e);
        btn.disabled = false;
      }
    });
  }

  const btnBulkStop = document.getElementById('btn-bulk-stop');
  if (btnBulkStop) {
    btnBulkStop.addEventListener('click', async function(){
      const sid = CURRENT_BULK_SID;
      if (!sid) return;
      try {
        await ajaxJson(API.bulk_cancel, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ session_id: sid }) });
        stopBulkPolling();
      } catch(e) {
        console.error('bulk_cancel failed', e);
        alert('取消失败: ' + e);
      }
    });
  }

  const accountsTable = document.getElementById('accounts_table');
  if (accountsTable) {
    accountsTable.addEventListener('click', async function(ev){
      const btn = ev.target;
      if (btn.classList.contains('send-btn')) {
        const acc = btn.getAttribute('data-acc');
        const msg = document.getElementById('message_text').value;
        if (!msg || !msg.trim()) { return alert('请输入要发送的消息'); }
        const dry = document.getElementById('dry_run').checked;
        try {
          const resp = await ajaxJson(API.send, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ account_id: acc, message: msg, dry_run: dry }) });
          if (resp && resp.session_id) {
            document.getElementById('send_status').innerText = '已排队, session=' + resp.session_id;
            const poll = setInterval(async function(){
              try {
                const s = await ajaxJson(API.send_status(resp.session_id));
                document.getElementById('send_status').innerText = JSON.stringify(s, null, 2);
                if (s && (s.status === 'done' || s.status === 'failed' || s.status === 'error')) {
                  clearInterval(poll);
                  location.reload();
                }
              } catch(e) {
                console.error('send poll fail', e);
              }
            }, 1000);
          } else {
            alert('无法启动发送任务');
          }
        } catch(e) {
          console.error('send request failed', e);
          alert('发送请求失败: ' + e);
        }
      } else if (btn.classList.contains('del-btn')) {
        const acc = btn.getAttribute('data-acc');
        if (!confirm('确认删除账号 ' + acc + ' ?')) return;
        const remove_profile_checkbox = document.getElementById('del_profile');
        const remove_messages_checkbox = document.getElementById('del_messages');
        const remove_profile = remove_profile_checkbox ? remove_profile_checkbox.checked : false;
        const remove_messages = remove_messages_checkbox ? remove_messages_checkbox.checked : false;
        try {
          const resp = await fetch(API.delete_account, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ account_id: acc, remove_profile: remove_profile, remove_messages: remove_messages })});
          if (resp.ok) {
            location.reload();
          } else {
            const txt = await resp.text().catch(()=>'');
            alert('删除失败: ' + (txt || resp.statusText));
          }
        } catch(e) {
          console.error('delete failed', e);
          alert('删除失败: ' + e);
        }
      }
    });
  }

})();
</script>

</body>
</html>
"""
    return html_template.replace("__ACCOUNTS_TABLE__", accounts_table)