// Context help is presentation-only. Original nodes/IDs remain available to renderers.
export function installHelp(root) {
  const doc=root.ownerDocument, groups=new Map();
  const panel=doc.createElement('div');
  // Progressive enhancement: keep all original help visible unless both the
  // methods and selector are supported. Never hide information before this gate.
  const supported=typeof panel.showPopover==='function' && typeof panel.hidePopover==='function'
    && doc.defaultView.CSS?.supports?.('selector(:popover-open)');
  if(!supported)return {refresh() {}, close() {}};
  panel.id='pe-help-popover'; panel.className='pe-help-popover'; panel.setAttribute('popover','manual');
  panel.setAttribute('role','region'); panel.setAttribute('aria-label','Подробности подсказки');
  root.append(panel);
  let active=null, pinned=false, timer, restoring=false;
  function close(restore=false) {
    clearTimeout(timer); const previous=active; active=null; pinned=false;
    previous?.button.setAttribute('aria-expanded','false');
    if(panel.matches(':popover-open'))panel.hidePopover();
    if(restore && previous?.button.isConnected) { restoring=true; previous.button.focus({preventScroll:true}); restoring=false; }
  }
  function position() {
    if(!active)return;
    const r=active.button.getBoundingClientRect(), margin=8, win=doc.defaultView;
    // innerWidth includes classic scrollbars. Use the visible intersection of
    // the layout and visual viewports (also handles zoom / mobile keyboards).
    const layoutWidth=Math.min(win.innerWidth,doc.documentElement.clientWidth||win.innerWidth);
    const layoutHeight=Math.min(win.innerHeight,doc.documentElement.clientHeight||win.innerHeight);
    const visual=win.visualViewport;
    const left=Math.max(0,visual?.offsetLeft||0), top=Math.max(0,visual?.offsetTop||0);
    const right=Math.min(layoutWidth,visual?visual.offsetLeft+visual.width:layoutWidth);
    const bottom=Math.min(layoutHeight,visual?visual.offsetTop+visual.height:layoutHeight);
    panel.style.width=Math.max(0,Math.min(360,right-left-2*margin))+'px';
    panel.style.maxHeight=Math.max(0,bottom-top-2*margin)+'px';
    const bounds=panel.getBoundingClientRect();
    panel.style.left=Math.max(left+margin,Math.min(r.left,right-bounds.width-margin))+'px';
    panel.style.top=Math.max(top+margin,Math.min(r.bottom+6,bottom-bounds.height-margin))+'px';
  }
  function content(group) {
    group.signature=group.sources.map(source=>source.innerHTML).join("\n");
    panel.replaceChildren();
    const dismiss=doc.createElement('button');dismiss.type='button';dismiss.className='pe-help-dismiss';
    dismiss.textContent='×';dismiss.setAttribute('aria-label','Закрыть подсказку');
    dismiss.addEventListener('click',()=>close(true));panel.append(dismiss);
    for(const source of group.sources) {
      const section=doc.createElement('div');section.className='pe-help-text';
      for(const node of source.childNodes)section.append(node.cloneNode(true));
      section.querySelectorAll('[id]').forEach(el=>el.removeAttribute('id'));
      panel.append(section, doc.createTextNode(' '));
    }
  }
  function open(group, pin=false) {
    clearTimeout(timer);
    if(active!==group)close();
    active=group;pinned=pinned||pin;
    // A popover belonging to a modal must be inside that dialog's focus scope.
    const host=group.button.closest('dialog[open]') || root;
    if(panel.parentElement!==host)host.append(panel);
    content(group);group.button.setAttribute('aria-expanded','true');
    if(!panel.matches(':popover-open'))panel.showPopover();
    position();
  }
  function laterClose() {
    clearTimeout(timer);timer=setTimeout(()=>{
      if(!pinned && !panel.matches(':hover') && !panel.contains(doc.activeElement) && doc.activeElement!==active?.button)close();
    },180);
  }
  panel.addEventListener('pointerenter',()=>clearTimeout(timer));
  panel.addEventListener('pointerleave',laterClose);
  panel.addEventListener('focusout',laterClose);
  panel.addEventListener('keydown',event=>{
    if(event.key!=='Tab'||!active)return;
    const items=[...panel.querySelectorAll('button,a[href]')];
    if(event.shiftKey && doc.activeElement===items[0]) {
      event.preventDefault();active.button.focus();
    } else if(!event.shiftKey && doc.activeElement===items.at(-1)) {
      event.preventDefault();
      const controls=[...root.querySelectorAll('button,input,select,textarea,a[href]')].filter(el=>!panel.contains(el)&&!el.disabled&&el.getClientRects().length);
      const next=controls[controls.indexOf(active.button)+1];close();next?.focus();
    }
  });
  doc.addEventListener('pointerdown',event=>{if(active&&!panel.contains(event.target)&&!active.button.contains(event.target))close();},true);
  doc.addEventListener('keydown',event=>{if(event.key==='Escape'&&active){event.preventDefault();event.stopPropagation();close(true);}},true);
  doc.addEventListener('scroll',()=>{if(active)position();},true);
  window.addEventListener('resize',position);
  window.visualViewport?.addEventListener('resize',position);
  window.visualViewport?.addEventListener('scroll',position);
  root.addEventListener('close',()=>close(),true);
  function anchorFor(source) {
    if(source.matches('[data-row-check], [data-help-source], [data-help-warning]'))return source;
    const cell=source.closest('td');if(cell)return cell;
    if(source.closest('label'))return source.closest('label');
    const previous=source.previousElementSibling;
    if(previous?.matches('label,summary,legend,h2,h3'))return previous;
    if(previous?.matches('.pe-grid2'))return previous.querySelector('label:last-of-type') || source;
    return source;
  }
  function refresh() {
    const next=new Map();
    const selector='p.pe-help:not(#pe-catalog-target):not(#pe-price-matches), #pe-row-form small, #pe-blocks td small, #pe-blocks [data-row-check], [data-help-source], #pe-catalog-status';
    for(const source of root.querySelectorAll(selector)) {
      if(panel.contains(source))continue;
      source.classList.add('pe-help-source');
      if(!source.textContent.trim() || source.hidden)continue;
      const anchor=anchorFor(source);
      if(!next.has(anchor))next.set(anchor,[]);
      next.get(anchor).push(source);
    }
    for(const [anchor,group] of groups)if(!next.has(anchor)||!anchor.isConnected){if(active===group)close();group.button.remove();groups.delete(anchor);}
    for(const [anchor,sources] of next) {
      let group=groups.get(anchor);
      if(!group) {
        const button=doc.createElement('button');button.type='button';button.className='pe-help-button';
        button.setAttribute('aria-controls',panel.id);button.setAttribute('aria-expanded','false');
        group={button,sources};groups.set(anchor,group);
        const warning=anchor.matches('[data-row-check], [data-help-source], [data-help-warning]');
        button.classList.toggle('pe-help-warning',warning);
        const name=anchor.closest('tr')?.querySelector('.pe-row-name')?.textContent || anchor.textContent.split('\n')[0].slice(0,70) || 'параметры расчёта';
        group.label=name.trim();
        button.setAttribute('aria-label',(warning?'Проверить: ':'Подсказка: ')+group.label);
        button.textContent=warning?(anchor.dataset.helpLabel || '! Проверить'):'?';
        if(anchor.matches('td'))(anchor.querySelector('.pe-row-name') || anchor).append(button);
        else if(anchor===sources[0])anchor.before(button);
        else anchor.append(button);
        button.addEventListener('pointerenter',event=>{if(event.pointerType==='mouse')open(group);});
        button.addEventListener('pointerleave',laterClose);
        button.addEventListener('focus',()=>{if(!restoring)open(group);});
        button.addEventListener('blur',laterClose);
        button.addEventListener('click',event=>{event.preventDefault();event.stopPropagation();if(active===group&&pinned)close();else open(group,true);});
        button.addEventListener('keydown',event=>{if(event.key==='Tab'&&!event.shiftKey&&active===group){event.preventDefault();panel.querySelector('button,a')?.focus();}});
      }
      group.sources=sources;
      const warning=anchor.matches('[data-row-check], [data-help-source], [data-help-warning]');
      group.button.classList.toggle('pe-help-warning',warning);
      group.button.setAttribute('aria-label',(warning?'Проверить: ':'Подсказка: ')+group.label);
      group.button.textContent=warning?(anchor.dataset.helpLabel || '! Проверить'):'?';
      if(active===group){if(group.signature!==sources.map(source=>source.innerHTML).join("\n"))content(group);position();}
    }
  }
  refresh();return {refresh,close};
}
