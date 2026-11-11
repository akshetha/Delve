const form = document.querySelector('#form');
const tickerInput = document.querySelector('#ticker');
const periodInput = document.querySelector('#period');
const errorBox = document.querySelector('#error');
const results = document.querySelector('#results');
const loading = document.querySelector('#loading');
const headlineGroups = ['#headlines', '#marketwatch', '#stocktwits'].map((selector) => document.querySelector(selector));
let chart;

const money = (value) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value);
const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[character]));

function renderLinks(container, items) {
  container.innerHTML = items.map((item) => {
    const href = /^https?:\/\//.test(item.link) ? item.link : '#';
    return `<a class="headline" href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title)}<small>${escapeHtml(item.source)}</small></a>`;
  }).join('');
}

function limitHeadlines(group) {
  const items = [...group.querySelectorAll(':scope > .headline, :scope > .post')];
  let button = group.querySelector(':scope > .show-more');

  if (items.length <= 5) {
    button?.remove();
    return;
  }

  const expanded = button?.dataset.expanded === 'true';
  items.forEach((item, index) => { item.hidden = !expanded && index >= 5; });

  if (!button) {
    button = document.createElement('button');
    button.type = 'button';
    button.className = 'show-more';
    button.addEventListener('click', () => {
      button.dataset.expanded = button.dataset.expanded === 'true' ? 'false' : 'true';
      limitHeadlines(group);
    });
    group.append(button);
  }

  button.textContent = expanded ? 'Show less' : `Show more (${items.length - 5})`;
}

function renderChart(data) {
  const labels = [...data.history.map((item) => item.date), ...data.forecast.slice(1).map((item) => item.date)];
  const history = data.history.map((item) => item.close);
  const padding = Array(data.history.length - 1).fill(null);
  const forecast = [...padding, data.last_close, ...data.forecast.slice(1).map((item) => item.price)];
  const low = [...padding, data.last_close, ...data.forecast.slice(1).map((item) => item.low)];
  const high = [...padding, data.last_close, ...data.forecast.slice(1).map((item) => item.high)];

  chart?.destroy();
  chart = new Chart(document.querySelector('#chart'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Historical close', data: [...history, ...Array(labels.length - history.length).fill(null)], borderColor: '#6da9ff', borderWidth: 2, pointRadius: 0, tension: .18 },
        { label: 'Expected price', data: forecast, borderColor: '#76d8b4', borderWidth: 2, borderDash: [7, 5], pointRadius: 0, tension: .18 },
        { label: 'Upper 90% bound', data: high, borderColor: 'rgba(118,216,180,.35)', borderWidth: 1, pointRadius: 0, tension: .18, fill: false },
        { label: 'Lower 90% bound', data: low, borderColor: 'rgba(118,216,180,.35)', borderWidth: 1, pointRadius: 0, tension: .18, fill: '-1', backgroundColor: 'rgba(118,216,180,.10)' },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { color: '#c7d3e6', usePointStyle: true } },
        title: { display: true, text: `${data.name} (${data.ticker}) · 30 trading days`, color: '#e7edf7', font: { size: 17, weight: '600' } },
        tooltip: { callbacks: { label: (context) => `${context.dataset.label}: ${money(context.parsed.y)}` } },
      },
      scales: {
        x: { ticks: { color: '#91a4c1', maxTicksLimit: 9 }, grid: { color: 'rgba(145,164,193,.08)' } },
        y: { ticks: { color: '#91a4c1', callback: (value) => money(value) }, grid: { color: 'rgba(145,164,193,.10)' } },
      },
    },
  });
}

function render(data) {
  results.hidden = false;
  document.querySelector('#close').textContent = money(data.last_close);
  document.querySelector('#asof').textContent = `Close on ${data.as_of}`;
  document.querySelector('#return').textContent = `${data.projected_return >= 0 ? '+' : ''}${data.projected_return}%`;
  document.querySelector('#drift').textContent = `Shrunk annual drift: ${data.shrunken_annualized_drift >= 0 ? '+' : ''}${data.shrunken_annualized_drift}%`;
  document.querySelector('#volatility').textContent = `${data.annualized_volatility}%`;
  document.querySelector('#source').textContent = `${data.volatility_observations} daily returns · ${data.ticker}`;
  document.querySelector('#sentiment').textContent = data.news.label;

  const formatSourcePrice = (value) => typeof value === 'number' ? money(value) : 'Unavailable';
  document.querySelector('#yahoo-price').textContent = formatSourcePrice(data.price_sources.yahoo.market_price);
  document.querySelector('#google-price').textContent = formatSourcePrice(data.price_sources.google.market_price);
  document.querySelector('#price-difference').textContent = data.price_sources.difference_percent === null
    ? data.price_sources.google.source
    : `Google is ${data.price_sources.difference_percent >= 0 ? '+' : ''}${data.price_sources.difference_percent}% vs. Yahoo`;

  const validation = document.querySelector('#quote-validation');
  validation.textContent = data.price_sources.validation.message;
  validation.className = data.price_sources.validation.status === 'alert' ? 'quote-alert' : data.price_sources.validation.status === 'ok' ? 'quote-ok muted' : 'muted';

  renderLinks(document.querySelector('#headlines'), data.news.items);
  document.querySelector('#news-empty').hidden = data.news.items.length > 0;
  document.querySelector('#analysis').innerHTML = data.analysis.map((item) => `<li>${escapeHtml(item)}</li>`).join('');
  renderLinks(document.querySelector('#marketwatch'), data.marketwatch.items);
  document.querySelector('#marketwatch-empty').hidden = data.marketwatch.items.length > 0;

  const commentary = document.querySelector('#stocktwits');
  commentary.innerHTML = data.stocktwits.items.map((item) => {
    const [title, url] = item.body.split('\u001f');
    const href = /^https?:\/\//.test(url || '') ? url : '#';
    return `<a class="headline" href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(title)}<small>${escapeHtml(item.user)}</small></a>`;
  }).join('');
  document.querySelector('#stocktwits-sentiment').textContent = data.stocktwits.label;
  document.querySelector('#stocktwits-empty').hidden = data.stocktwits.items.length > 0;

  headlineGroups.forEach(limitHeadlines);
  renderChart(data);
}

async function load() {
  const ticker = tickerInput.value.trim().toUpperCase();
  const period = periodInput.value;
  errorBox.style.display = 'none';
  results.hidden = true;
  loading.textContent = `Loading ${ticker || 'selected ticker'} market data…`;
  loading.style.display = 'block';

  try {
    const response = await fetch(`/api/stock?ticker=${encodeURIComponent(ticker)}&period=${period}&days=30`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Unable to load data.');
    render(data);
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.style.display = 'block';
  } finally {
    loading.style.display = 'none';
  }
}

form.addEventListener('submit', (event) => {
  event.preventDefault();
  load();
});

load();
