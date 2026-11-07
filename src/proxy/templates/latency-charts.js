/* Client-side OHLC charts. No image requests, CDN, or third-party telemetry. */
(() => {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';
  const green = '#089981', red = '#f23645';
  const fmt = (t, seconds = false) => new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', ...(seconds ? {second: '2-digit'} : {}), hourCycle: 'h23'
  }).format(new Date(t * 1000));
  const num = n => Number(n.toFixed(2)).toLocaleString('zh-CN');
  const svg = (tag, attrs, text) => {
    const node = document.createElementNS(NS, tag);
    Object.entries(attrs || {}).forEach(([key, value]) => node.setAttribute(key, value));
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const el = (tag, cls, text) => {
    const node = document.createElement(tag); if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text; return node;
  };
  function ticks(low, high) {
    const span = Math.max(high-low, high*.08, .1), rough = span*1.24/5;
    const power = 10**Math.floor(Math.log10(rough));
    const step = [1,2,2.5,5,10].find(v => v*power >= rough)*power;
    const a = Math.max(0, Math.floor((low-span*.12)/step)*step);
    const b = Math.ceil((high+span*.12)/step)*step;
    return Array.from({length: Math.round((b-a)/step)+1}, (_,i) => a+i*step);
  }

  class CandlePlot {
    constructor(host, mode) {
      this.host = host; this.mode = mode; this.index = null;
      this.heading = el('div', 'lc-plot-heading');
      this.heading.append(el('strong', '', mode === 'queued' ? '含排队 · 总耗时' : '不排队 · 请求耗时'));
      this.latest = el('span', 'lc-latest'); this.heading.append(this.latest);
      this.root = svg('svg', {role: 'img', tabindex: '0', 'aria-label': '延迟 K 线；鼠标悬停、触摸或使用左右方向键查看详情'});
      this.tip = el('div', 'lc-tooltip'); this.tip.hidden = true; this.tip.setAttribute('role','status');
      this.hint = el('div', 'lc-hint', mode === 'queued' ? '虚线空心 · 排队等待 + 请求执行' : '实心蜡烛 · 仅请求执行');
      host.append(this.heading, this.root, this.tip, this.hint);
      this.root.addEventListener('pointermove', e => this.point(e));
      this.root.addEventListener('pointerdown', e => this.point(e));
      this.root.addEventListener('pointerleave', () => this.hide());
      this.root.addEventListener('blur', () => this.hide());
      this.root.addEventListener('keydown', e => {
        if (e.key === 'Escape') return this.hide();
        if (!this.data || !['ArrowLeft','ArrowRight','Home','End'].includes(e.key)) return;
        e.preventDefault();
        const n = this.data.timestamps.length;
        this.index = e.key === 'Home' ? 0 : e.key === 'End' ? n-1 : Math.max(0,Math.min(n-1,(this.index ?? n-1)+(e.key==='ArrowLeft'?-1:1)));
        this.show(this.index);
      });
      this.observer = new ResizeObserver(() => this.draw()); this.observer.observe(host);
    }
    update(data, series) { this.data = data; this.series = series; this.draw(); }
    destroy() { this.observer.disconnect(); }
    draw() {
      if (!this.data || this.host.clientWidth < 100) return;
      const d = this.data, item = this.series;
      const w = this.host.clientWidth, h = 320, left = 58, right = w-16, top = 28, bottom = 252;
      const valid = (item?.ohlc || []).filter(Boolean), last = valid.at(-1);
      this.latest.textContent = last ? `最新 ${num(last.close_ms/1000)} s` : '暂无记录';
      this.latest.style.color = last ? (last.close_ms >= last.open_ms ? green : red) : '#65768b';
      this.root.setAttribute('viewBox', `0 0 ${w} ${h}`);
      this.root.replaceChildren();
      this.root.append(svg('title', {}, `${item?.model || '模型'} · ${this.mode === 'queued' ? '含排队' : '不排队'}，北京时间`));
      const ys = ticks(valid.length ? Math.min(...valid.map(c=>c.low_ms/1000)) : 0, valid.length ? Math.max(...valid.map(c=>c.high_ms/1000)) : 1);
      const x = t => left+(t-d.display_start)/(d.display_end-d.display_start)*(right-left);
      const y = v => bottom-(v-ys[0])/(ys.at(-1)-ys[0])*(bottom-top);
      this.geometry = {w,h,left,right,top,bottom,x,y};
      const add = (tag, attrs, text) => this.root.append(svg(tag,attrs,text));
      add('text',{x:left,y:16,fill:'#65768b','font-size':12}, '延迟 / 秒');
      ys.forEach(v => {
        add('line',{x1:left,y1:y(v),x2:right,y2:y(v),stroke:'#e5ebf2'});
        add('text',{x:left-10,y:y(v)+4,fill:'#586b82','text-anchor':'end','font-size':12}, num(v));
      });
      const desired=(d.display_end-d.display_start)/Math.max(2,Math.floor((right-left)/110));
      const stride=[1,5,10,15,30,60,120,300,600,900,1800,3600,7200,10800,14400,21600,43200,86400,172800,345600].find(v=>v>=desired);
      const times=[d.display_start,d.display_end];
      for(let t=Math.ceil(d.display_start/stride)*stride;t<d.display_end;t+=stride) {
        if(x(t)>left+80 && x(t)<right-80) times.push(t);
      }
      if(times.length===2) times.push(Math.round((d.display_start+d.display_end)/2/d.bucket_seconds)*d.bucket_seconds);
      times.forEach(t=>{
        const px=x(t), label=fmt(t,d.bucket_seconds<60).split(' ');
        const anchor=t===d.display_start?'start':t===d.display_end?'end':'middle';
        add('line',{x1:px,y1:top,x2:px,y2:bottom,stroke:'#f0f3f7'});
        add('text',{x:px,y:bottom+24,fill:'#23364e','text-anchor':anchor,'font-size':12},label.at(-1));
        add('text',{x:px,y:bottom+43,fill:'#65768b','text-anchor':anchor,'font-size':11},label[0].slice(5));
      });
      const slot=(right-left)/d.timestamps.length, bw=Math.max(2,Math.min(16,slot*.68));
      (item?.ohlc || []).forEach((c,i)=>{
        if(!c) return;
        const px=x(d.timestamps[i]+d.bucket_seconds/2), color=c.close_ms>=c.open_ms?green:red;
        const a=y(Math.max(c.open_ms,c.close_ms)/1000), b=y(Math.min(c.open_ms,c.close_ms)/1000);
        const dash=this.mode==='queued'?'3 2':'none';
        add('line',{x1:px,y1:y(c.high_ms/1000),x2:px,y2:y(c.low_ms/1000),stroke:color,'stroke-width':1.7,'stroke-dasharray':dash});
        add('rect',{x:px-bw/2,y:(a+b)/2-Math.max(3,b-a)/2,width:bw,height:Math.max(3,b-a),fill:this.mode==='queued'?'white':color,stroke:color,'stroke-width':1.5,'stroke-dasharray':dash});
      });
      if(!valid.length) add('text',{x:(left+right)/2,y:140,fill:'#65768b','text-anchor':'middle','font-size':14},'所选时段暂无记录');
      this.cross=svg('g',{'pointer-events':'none'}); this.root.append(this.cross);
      if(this.index!==null) this.show(Math.min(this.index,d.timestamps.length-1));
    }
    point(event) {
      if(!this.geometry) return;
      const box=this.root.getBoundingClientRect(), g=this.geometry;
      const px=(event.clientX-box.left)*g.w/box.width, py=(event.clientY-box.top)*g.h/box.height;
      if(px<g.left || px>g.right || py<g.top || py>g.bottom) return this.hide();
      this.index=Math.min(this.data.timestamps.length-1,Math.floor((px-g.left)/(g.right-g.left)*this.data.timestamps.length));
      this.show(this.index);
    }
    hide() { this.index=null; this.tip.hidden=true; this.cross?.replaceChildren(); }
    show(i) {
      const d=this.data, c=this.series?.ohlc[i], g=this.geometry;
      if(!g || !this.cross) return;
      const start=d.timestamps[i], end=Math.min(start+d.bucket_seconds,d.window_end), px=g.x(start+d.bucket_seconds/2);
      this.cross.replaceChildren(svg('line',{x1:px,y1:g.top,x2:px,y2:g.bottom,stroke:'#8c9cb0','stroke-dasharray':'4 3'}));
      this.tip.replaceChildren(el('strong','',this.model || '此模型'));
      this.tip.append(el('div','lc-tip-time', `${fmt(start,true)} — ${fmt(end,true)} 北京时间`));
      if(c) {
        const grid=el('div','lc-tip-grid');
        [['开',c.open_ms],['高',c.high_ms],['低',c.low_ms],['收',c.close_ms]].forEach(([label,value])=>{
          const row=el('div');row.append(el('span','',label),el('b','',`${num(value/1000)} s`));grid.append(row);
        });
        this.tip.append(grid,el('div','lc-tip-note', c.legacy_count ? `${c.count-c.legacy_count} 次请求 + ${c.legacy_count} 个历史分钟均值` : `${c.count} 次请求`));
        if(c.first_timestamp) this.tip.append(el('div','lc-tip-note',`首笔 ${fmt(c.first_timestamp,true)}\n末笔 ${fmt(c.last_timestamp,true)}`));
        if(c.legacy_count) this.tip.append(el('div','lc-tip-note','含历史均值，非完整逐请求高低值'));
      } else this.tip.append(el('div','lc-tip-note','此时段无请求记录'));
      if(start+d.bucket_seconds>d.window_end) this.tip.append(el('div','lc-tip-note','当前周期持续更新中'));
      this.tip.hidden=false;
      const tw=this.tip.offsetWidth;
      this.tip.style.left=`${Math.max(4,Math.min(g.w-tw-4,px>g.w/2?px-tw-12:px+12))}px`;
      this.tip.style.top='48px';
    }
  }

  window.LatencyDashboard = class {
    constructor(root, api) {
      this.root=root; this.api=api; this.plots=[]; this.serial=0;
      root.innerHTML=`<div class="lc-top"><div><h2>模型延迟 K 线</h2><p>北京时间 UTC+8 · 悬停、触摸或方向键查看具体时段</p></div><div class="lc-tools"><label>时间范围<select data-lc="range"><option value="60">最近 1 小时</option><option value="360">最近 6 小时</option><option value="1440" selected>最近 24 小时</option><option value="10080">最近 7 天</option></select></label><label>模型<select data-lc="model"><option value="">全部模型</option></select></label><button class="btn g" data-lc="refresh">刷新</button></div></div><div class="lc-meta"><span data-lc="period"></span><span data-lc="status" role="status">等待数据</span></div><div class="lc-legend"><span><i style="background:${green}"></i>上涨：延迟增加</span><span><i style="background:${red}"></i>下跌：延迟降低</span><span>实体：首末延迟 · 影线：最高 / 最低 · 各模型独立纵轴</span></div><div data-lc="cards"></div>`;
      this.find=key=>root.querySelector(`[data-lc="${key}"]`);
      this.find('range').onchange=()=>this.refresh(true);
      this.find('model').onchange=()=>this.render(true);
      this.find('refresh').onclick=()=>this.refresh(true);
    }
    async refresh(force=false) {
      if(this.busy&&!force) return;
      const seq=++this.serial; this.busy=true;
      this.find('status').textContent='更新中…';
      try {
        const minutes=this.find('range').value;
        const results=await Promise.all(['active','queued'].map(mode=>this.api(`/model-latency?minutes=${minutes}&mode=${mode}`,{cache:'no-store'})));
        if(seq!==this.serial) return;
        this.data=results;
        const d=results[0], step=d.bucket_seconds;
        const interval=step>=3600?`${step/3600} 小时`:step>=60?`${step/60} 分钟`:`${step} 秒`;
        this.find('period').textContent=`${fmt(d.window_start)} — ${fmt(d.window_end)} · 每根 ${interval}`;
        this.find('status').textContent=`${fmt(d.window_end,true).split(' ').at(-1)} 已更新 · 每 15 秒刷新`;
        this.find('status').classList.remove('lc-error');
        this.render();
      } catch(e) {
        if(seq!==this.serial) return;
        this.find('status').textContent=`更新失败${this.data?'，保留上次数据':''} · ${e.message}`;
        this.find('status').classList.add('lc-error');
      } finally { if(seq===this.serial) this.busy=false; }
    }
    render(force=false) {
      if(!this.data) return;
      const models=[...new Set(this.data.flatMap(d=>d.series.map(s=>s.model)))].sort();
      const select=this.find('model'), selected=select.value;
      if(models.join('|')!==this.modelKey) {
        select.replaceChildren(new Option('全部模型',''),...models.map(m=>new Option(m,m)));
        select.value=models.includes(selected)?selected:''; this.modelKey=models.join('|');
      }
      const visible=select.value?[select.value]:models, key=visible.join('|');
      if(force || key!==this.viewKey || !this.plots.length) {
        this.plots.forEach(p=>p.destroy());this.plots=[]; this.find('cards').replaceChildren();
        visible.forEach(model=>{
          const card=el('article','lc-model'), title=el('div','lc-model-title');
          title.append(el('h3','',model)); card.append(title);
          const pair=el('div','lc-pair');card.append(pair);this.find('cards').append(card);
          ['active','queued'].forEach(mode=>{
            const host=el('div','lc-plot');pair.append(host);
            const plot=new CandlePlot(host,mode);plot.model=model;this.plots.push(plot);
          });
        });
        if(!visible.length) this.find('cards').append(el('div','lc-empty','所选时段暂无请求记录；新请求完成后会自动出现。'));
        this.viewKey=key;
      }
      this.plots.forEach(p=>{
        const data=this.data[p.mode==='queued'?1:0]; p.update(data,data.series.find(s=>s.model===p.model));
      });
    }
  };
})();
