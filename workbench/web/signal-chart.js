'use strict';
/* Geometry only: consumes audited annotations, never derives a trading signal. */
(function(root){
  const NS='http://www.w3.org/2000/svg';
  const valid=(...v)=>v.every(n=>n!==null&&Number.isFinite(n));
  const intersects=(a,b)=>a.x<b.x+b.width+5&&a.x+a.width+5>b.x&&a.y<b.y+b.height+4&&a.y+a.height+4>b.y;
  function presentation(items,options={}){
    const {period='all',rank=1,showInvalid=true}=options;
    const relevant=items.filter(p=>period==='all'?['day','30m','5m'].includes(p.period):p.period===period);
    const atRank=relevant.filter(p=>p.rank===rank);
    const visible=atRank.filter(p=>showInvalid||!['invalidated','withdrawn'].includes(p.state));
    const historical=atRank.filter(p=>['invalidated','withdrawn'].includes(p.state)).length;
    const sorted=visible.slice().sort((a,b)=>b.anchor-b.anchor||b.available_at-a.available_at||a.id.localeCompare(b.id));
    return {items:sorted,total:atRank.length,historical,active:atRank.length-historical,
      buys:atRank.filter(p=>p.side==='buy').length,sells:atRank.filter(p=>p.side==='sell').length,
      ranks:[...new Set(relevant.map(p=>p.rank))].sort((a,b)=>a-b),latest:sorted[0]||null};
  }
  function markerRange(point,barCount){
    const center=point.anchor,reference=Math.max(center,point.available_at,point.confirmed_index??0);
    return {from:Math.max(-2,center-65),to:Math.min(barCount+8,Math.max(center+65,reference+15))};
  }
  function layoutMarkers(items,width,height){
    const occupied=[],result=[];
    for(const item of items.slice().sort((a,b)=>a.x-b.x||a.id.localeCompare(b.id))){
      const boxWidth=Math.min(Math.max(64,item.label.length*11+14),Math.max(64,width-12));
      let chosen=null;
      for(let lane=0;lane<10&&!chosen;lane++)for(const direction of [item.side==='buy'?1:-1,item.side==='buy'?-1:1]){
        const x=Math.max(5,Math.min(item.x+8,width-boxWidth-5));
        const y=Math.max(7,Math.min(item.y+direction*(22+lane*23)-9,height-24));
        const box={x,y,width:boxWidth,height:19};
        if(!occupied.some(old=>intersects(box,old))){chosen=box;occupied.push(box);break;}
      }
      result.push({...item,box:chosen,label_visible:chosen!==null});
    }
    return result;
  }
  function draw(options){
    const {group,items,x,y,width,height,selected,showTimes,onSelect}=options;
    const node=(tag,attrs={},text)=>{const el=document.createElementNS(NS,tag);for(const[k,v]of Object.entries(attrs))el.setAttribute(k,String(v));if(text!==undefined)el.textContent=text;return el;};
    const append=(tag,attrs,text)=>{const el=node(tag,attrs,text);group.appendChild(el);return el;};
    const label=(px,py,text,color)=>append('text',{x:px,y:py,fill:color,'font-size':10,'font-family':'Microsoft YaHei, sans-serif',stroke:'#fff','stroke-width':3,'paint-order':'stroke'},text);
    const visible=items.map(p=>({id:p.id,point:p,x:x(p.anchor),y:y(p.price),side:p.side,
      label:p.label+(p.state==='triggered'?'·触发':p.state==='invalidated'?'·失效':p.state==='withdrawn'?'·撤回':p.state==='structure_complete'?'·本级完成':p.state==='needs_review'?'·待复核':'·候选')}))
      .filter(p=>valid(p.x,p.y)&&p.x>=0&&p.x<=width&&p.y>=0&&p.y<height);
    const chosen=items.find(p=>p.id===selected);
    if(chosen){
      if(chosen.center){const c=chosen.center,a=x(c.start),b=x(c.end),top=y(c.zg),bottom=y(c.zd);if(valid(a,b,top,bottom))append('rect',{x:a,y:top,width:Math.max(1,b-a),height:Math.max(1,bottom-top),fill:'#b88b33','fill-opacity':.12,stroke:'#a77a25','stroke-width':1.8});}
      chosen.references.forEach((span,i)=>{const a=x(span.start),b=x(span.end),py=height-22-(i%4)*18;if(!valid(a,b))return;append('path',{d:`M ${a} ${py-4} V ${py} H ${b} V ${py-4}`,stroke:'#5c7894','stroke-width':1.4,fill:'none'});label(Math.max(5,Math.min((a+b)/2,width-100)),py-5,span.label,'#57748e');});
      const boundary=y(chosen.invalidation),start=x(chosen.anchor);
      if(valid(boundary,start)&&boundary>7&&boundary<height-10){append('line',{x1:Math.max(0,start),y1:boundary,x2:width,y2:boundary,stroke:'#947a61','stroke-width':1.2,'stroke-dasharray':'5 4'});label(Math.max(5,width-168),boundary-5,'失效 '+chosen.invalidation_operator+' '+chosen.invalidation.toFixed(2),'#82674e');}
      if(chosen.first_context){const c=chosen.first_context,a=x(c.anchor),b=x(chosen.anchor),ya=y(c.price),yb=y(chosen.price);if(valid(a,b,ya,yb))append('line',{x1:a,y1:ya,x2:b,y2:yb,stroke:'#a77a25','stroke-dasharray':'4 4','stroke-width':1.5});}
    }
    const positions=layoutMarkers(visible,width,height);
    let labelCount=0,timeCount=0;
    for(const item of positions){
      const p=item.point,color=p.state==='needs_review'?'#bc791e':p.state==='invalidated'||p.state==='withdrawn'?'#8b97a3':p.side==='buy'?'#c34a51':'#178878';
      const selectedPoint=p.id===selected;
      const control=node('g',{'class':'signal-marker','data-signal-id':p.id,tabindex:0,role:'button','aria-label':p.label+' '+p.state_label+' '+p.anchor_time});
      const title=node('title',{},`${p.label} · ${p.state_label}\n端点 ${p.anchor_time} / ${p.price}\n依据 ${p.available_time} / 延后 ${p.delay_bars} 根\n失效 ${p.invalidation_operator} ${p.invalidation}`);control.appendChild(title);
      const dy=p.side==='buy'?1:-1;
      control.appendChild(node('path',{d:`M ${item.x} ${item.y} L ${item.x-6} ${item.y+dy*11} L ${item.x+6} ${item.y+dy*11} Z`,fill:p.state==='triggered'?color:'#fff',stroke:color,'stroke-width':selectedPoint?2.6:1.8}));
      control.appendChild(node('circle',{cx:item.x,cy:item.y+dy*6,r:12,fill:'transparent',stroke:'none'}));
      if(p.state==='invalidated'||p.state==='withdrawn')control.appendChild(node('path',{d:`M ${item.x-5} ${item.y-5} l 10 10 m 0 -10 l -10 10`,stroke:color,'stroke-width':1.5}));
      if(item.box){const b=item.box;labelCount++;control.appendChild(node('line',{x1:item.x,y1:item.y+dy*12,x2:b.x+5,y2:b.y+9,stroke:color,'stroke-opacity':.55,'stroke-width':.8}));control.appendChild(node('rect',{...b,rx:3,fill:'#fff','fill-opacity':.95,stroke:color,'stroke-opacity':selectedPoint?1:.4}));control.appendChild(node('text',{x:b.x+6,y:b.y+13,fill:color,'font-size':10,'font-family':'Microsoft YaHei, sans-serif'},item.label));}
      control.addEventListener('click',event=>{event.stopPropagation();onSelect(p);});
      control.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();onSelect(p);}});
      group.appendChild(control);
      if(showTimes||selectedPoint){
        for(const milestone of [{index:p.available_at,price:p.available_price,filled:false,label:'依据'},{index:p.confirmed_index,price:p.confirmed_price,filled:true,label:'触发'}]){
          if(milestone.index===null||milestone.index===undefined)continue;
          const xx=x(milestone.index),yy=y(milestone.price);if(!valid(xx,yy)||xx<0||xx>width||yy<0||yy>height)continue;
          const diamond=append('path',{d:`M ${xx} ${yy-5} l 5 5 l -5 5 l -5 -5 Z`,fill:milestone.filled?'#316ba3':'#fff',stroke:'#316ba3','stroke-width':1.5});diamond.appendChild(node('title',{},milestone.label+' '+(milestone.filled?p.confirmed_time:p.available_time)));timeCount++;
          if(selectedPoint){append('line',{x1:item.x,y1:item.y,x2:xx,y2:yy,stroke:'#6b91af','stroke-width':1,'stroke-dasharray':'3 4'});label(Math.max(5,Math.min(xx+7,width-92)),Math.max(14,yy-9),milestone.label,'#316ba3');}
        }
      }
    }
    return {visible_markers:positions.length,visible_labels:labelCount,hidden_dense_labels:positions.length-labelCount,milestones:timeCount,
      buy_markers:positions.filter(p=>p.side==='buy').length,sell_markers:positions.filter(p=>p.side==='sell').length};
  }
  root.SignalChart={layoutMarkers,draw,presentation,markerRange};
})(globalThis);
