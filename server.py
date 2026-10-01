async function admBanUser(){
  const nick=($('#banNick').value||'').trim();
  if(!nick)return toast('Ошибка','Укажи ник','err');
  const dur=parseInt($('#banDur').value)||0;
  const reason=($('#banReason').value||'').trim();
  const by=($('#banBy').value||State.name||'админ').trim();
  try{
    const r=await api('/api/admin/ban',{nick,duration_ms:dur,reason,by});
    toast('🚫 Забанен',r.nick+' · '+(dur>0?cdStr(dur):'навсегда'),'ok');
    Sound.lose();
    $('#banNick').value='';$('#banReason').value='';
    admLoadBans();
  }catch(e){toast('Ошибка',e.message,'err')}
}
async function admLoadBans(){
  const el=$('#banList');if(!el)return;
  try{
    const list=await api('/api/admin/bans');
    if(!list.length){el.innerHTML='<div style="color:var(--mut2);text-align:center;padding:14px">Активных банов нет</div>';return}
    el.innerHTML=list.map(b=>{
      const left=b.until?cdStr(b.left):'навсегда';
      return `<div class="qrow" style="margin:0;border-left:3px solid var(--red)">
        <div class="qi" style="background:rgba(255,77,94,.15);color:#ff6b78">🚫</div>
        <div class="qt"><b>${b.nick}</b>
          <span>${(b.reason||'без причины').replace(/[<>]/g,'')} · осталось: ${left} · банил: ${b.by||'админ'}</span></div>
        <button class="btn btn-gh sm" data-unban="${b.nick}">Разбанить</button>
      </div>`;
    }).join('');
    $$('[data-unban]').forEach(x=>x.onclick=async()=>{
      try{await api('/api/admin/ban/'+encodeURIComponent(x.dataset.unban),null,'DELETE');
        toast('✅ Разбанен',x.dataset.unban,'ok');admLoadBans();
      }catch(e){toast('Ошибка',e.message,'err')}
    });
  }catch(e){el.innerHTML='<div style="color:var(--red);padding:14px">'+e.message+'</div>'}
}
