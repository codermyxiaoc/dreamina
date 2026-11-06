const byId = id => document.getElementById(id);
async function refresh() {
  const state = await chrome.runtime.sendMessage({type: 'GET_STATUS'});
  if (state?.error) throw new Error(state.error);
  byId('extensionId').value = state.extension_id;
  byId('accountId').value = state.account_id;
  byId('status').textContent = `${state.enabled ? '已启用' : '已暂停'} · ${state.key_configured ? '密钥已配置' : '未配置密钥'}\n${state.status}` +
    (state.last_job ? `\n最近任务：${state.last_job.id}\n${state.last_job.status}` : '');
}
async function command(type) {
  for (const button of document.querySelectorAll('button')) button.disabled = true;
  try {
    const result = await chrome.runtime.sendMessage({type, account_id: byId('accountId').value.trim(), key: byId('key').value.trim()});
    byId('key').value = '';
    if (result?.error) throw new Error(result.error);
    await refresh();
  } catch (error) { byId('status').textContent = error.message; }
  finally { for (const button of document.querySelectorAll('button')) button.disabled = false; }
}
byId('bind').addEventListener('click', () => command('BIND'));
byId('pause').addEventListener('click', () => command('PAUSE'));
byId('sync').addEventListener('click', () => command('SYNC'));
refresh().catch(error => { byId('status').textContent = error.message; });
