"""Delete/restore controls shared by receipt lists and details."""
import json
from .render import esc


def control(sha, deleted):
    action = 'restore' if deleted else 'delete'
    label = 'Restore receipt' if deleted else 'Delete receipt'
    return f'<button type="button" class="receipt-delete" data-sha="{esc(sha)}" data-action="{action}">{label}</button>'


def script(base):
    return '''<p id="receipt-deletion-message" role="status"></p><script>
(()=>{
 const base=''' + json.dumps(base) + ''';
 const message=document.getElementById('receipt-deletion-message');
 async function change(sha,action,button){
  button.disabled=true;
  try{
   const response=await fetch(base+'/api/receipts/'+encodeURIComponent(sha)+(action==='restore'?'/restore':''),{
    method:action==='restore'?'POST':'DELETE',headers:{'X-Ledger-Action':'receipt-'+action}});
   const result=await response.json();
   if(!response.ok)throw Error(result.detail||'Receipt could not be updated.');
   sessionStorage.setItem('receipt-deletion-result',JSON.stringify({sha,deleted:result.deleted}));
   window.location.reload();
  }catch(error){message.textContent=error.message;button.disabled=false;}
 }
 for(const button of document.querySelectorAll('.receipt-delete')){
  button.addEventListener('click',()=>change(button.dataset.sha,button.dataset.action,button));
 }
 const saved=sessionStorage.getItem('receipt-deletion-result');
 if(saved){
  sessionStorage.removeItem('receipt-deletion-result');const result=JSON.parse(saved);
  message.textContent=result.deleted?'Receipt deleted from the active ledger. ':'Receipt restored.';
  if(result.deleted){const undo=document.createElement('button');undo.type='button';undo.textContent='Undo';
   undo.addEventListener('click',()=>change(result.sha,'restore',undo));message.append(undo);}
 }
})();
</script>'''
