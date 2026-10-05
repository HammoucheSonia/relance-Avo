function toast(msg, type) {
  const wrap = document.getElementById('toasts') || (() => {
    const w = document.createElement('div'); w.id = 'toasts';
    document.body.appendChild(w); return w;
  })();
  const el = document.createElement('div');
  el.className = 'toast' + (type ? ' ' + type : '');
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => {
    el.style.animation = 'slideOut .3s ease-in forwards';
    setTimeout(() => el.remove(), 300);
  }, 3200);
}

function animateCounters() {
  document.querySelectorAll('.kpi .val[data-target]').forEach(el => {
    const target = parseFloat(el.dataset.target) || 0;
    const prefix = el.dataset.prefix || '';
    const suffix = el.dataset.suffix || '';
    const decimals = parseInt(el.dataset.decimals || '0');
    let start = null;
    const dur = 900;
    const fmt = v => v.toLocaleString('fr-FR', {minimumFractionDigits: decimals,
                                                  maximumFractionDigits: decimals});
    requestAnimationFrame(function step(ts){
      if(!start) start = ts;
      const p = Math.min((ts-start)/dur, 1);
      const eased = 1 - Math.pow(1-p, 3);
      el.textContent = prefix + fmt(target*eased) + suffix;
      if(p<1) requestAnimationFrame(step);
    });
  });
}

const VERT = '#1e5c3a', VERTC = '#2a7a4a', OR = '#c08a1f', ROUGE = '#b3453b';

function drawDonut(canvas, data) {
  if(!canvas || !window.Chart || !data.length) return;
  new Chart(canvas, {
    type: 'doughnut',
    data: {
      labels: data.map(d => d.label),
      datasets: [{
        data: data.map(d => d.montant),
        backgroundColor: [VERT, VERTC, OR, '#d1782a', ROUGE, '#7a1f1f'],
        borderWidth: 0, hoverOffset: 8
      }]
    },
    options: {
      cutout: '72%', responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'bottom', labels: { padding: 14, font: { size: 12 },
                  usePointStyle: true, pointStyle: 'circle' } },
        tooltip: { callbacks: { label: c =>
          ` ${c.label} : ${c.raw.toLocaleString('fr-FR',{maximumFractionDigits:0})} €` } }
      },
      animation: { animateRotate: true, duration: 900, easing: 'easeOutQuart' }
    }
  });
}

function drawLine(canvas, labels, values) {
  if(!canvas || !window.Chart) return;
  new Chart(canvas, {
    type: 'line',
    data: { labels, datasets: [{
      data: values, fill: true, tension: .35, borderWidth: 2,
      borderColor: VERTC,
      backgroundColor: (c) => {
        const g = c.chart.ctx.createLinearGradient(0,0,0,260);
        g.addColorStop(0, 'rgba(42,122,74,.35)');
        g.addColorStop(1, 'rgba(42,122,74,0)');
        return g;
      },
      pointRadius: 0, pointHoverRadius: 6,
      pointHoverBackgroundColor: VERTC, pointHoverBorderColor: '#fff',
      pointHoverBorderWidth: 3
    }]},
    options: {
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false }, ticks: { color: '#6b7280' } },
        y: { border: { display: false }, grid: { color: 'rgba(0,0,0,.05)' },
             ticks: { color: '#6b7280', callback: v => v.toLocaleString('fr-FR') + ' €' } }
      },
      animation: { duration: 1200, easing: 'easeOutQuart' }
    }
  });
}

function sparkline(canvas, values) {
  if(!canvas || !window.Chart || !values.length) return;
  new Chart(canvas, {
    type: 'line',
    data: { labels: values.map((_,i)=>i), datasets: [{
      data: values, borderColor: VERTC, borderWidth: 2, tension: .4,
      pointRadius: 0, fill: false
    }]},
    options: { plugins: { legend: {display:false}, tooltip: {enabled:false} },
               scales: { x: {display:false}, y: {display:false} },
               animation: {duration: 600} }
  });
}

document.addEventListener('DOMContentLoaded', () => {
  animateCounters();
  document.querySelectorAll('canvas.spark').forEach(c =>
    sparkline(c, c.dataset.spark.split(',').map(Number)));
});

async function uploadPj(code, input) {
  const files = input.files;
  if(!files.length) return;
  const fd = new FormData();
  for(const f of files) fd.append('factures', f);
  const r = await fetch(`/factures/${code}`, {method:'POST', body:fd});
  const j = await r.json();
  if(j.ok){
    const zone = document.getElementById('pj-'+code);
    zone.innerHTML = '';
    j.liste.forEach(f => {
      const d = document.createElement('div');
      d.className = 'pj';
      d.innerHTML = `<span>📄</span>
        <a href="/factures/${code}/${f}" target="_blank">${f}</a>
        <button class="del" type="button" onclick="delPj('${code}','${f}')">×</button>`;
      zone.appendChild(d);
    });
    toast(`${j.ajoutes} facture(s) ajoutée(s)`, 'gold');
  }
  input.value = '';
}

async function delPj(code, fname) {
  if(!confirm(`Supprimer ${fname} ?`)) return;
  const r = await fetch(`/factures/${code}/${fname}`, {method:'DELETE'});
  if(r.ok){
    document.querySelector(`#pj-${code} a[href$="${fname}"]`)?.closest('.pj')?.remove();
    toast('Fichier supprimé');
  }
}