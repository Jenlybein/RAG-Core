/**
 * 用量看板 · 共用的图表层
 *
 * 四屏共用：窗口定义、假数据生成、ECharts 主题（颜色从 tokens.css 的 CSS 变量里读，
 * 所以深色/浅色跟着画布那枚三段开关走，不在这里再抄一套颜色）。
 *
 * 为什么读 CSS 变量而不是写死：#4f 页脚的规矩是「颜色只有一个出处」。ECharts 要的是具体
 * 颜色值，所以在挂载时把 `--ink` / `--muted` / `--line-lighter` / `--accent` 读出来，主题一变
 * 再读一遍重画。
 */

(function () {
  const THIRD = '#5e5ce6'; // 第三本账（大模型）的系列色：tokens 里没有对应语义，只在图上用

  function css(name, fallback) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  function palette() {
    return {
      ink: css('--ink', '#1d1d1f'),
      muted: css('--muted', '#5b5b61'),
      line: css('--line-lighter', 'rgba(24,33,52,0.12)'),
      accent: css('--accent', '#0071e3'),
      star: css('--star', '#ff9f0a'),
      danger: css('--danger', '#d70015'),
      third: THIRD,
      font: css('--font-sans', 'system-ui'),
    };
  }

  // ── 窗口：与 DECISIONS 里的口径一字不差 ──────────────────────────────
  const WINDOWS = {
    '1h': { buckets: 6, unit: '分钟', step: 10, label: '近 1 小时 · 10 分钟一桶', tail: 0.55 },
    '1d': { buckets: 24, unit: '小时', step: 60, label: '近 24 小时 · 1 小时一桶', tail: 0.42 },
    '7d': { buckets: 7, unit: '天', step: 1440, label: '近 7 天 · 1 天一桶', tail: 0.63 },
    '30d': { buckets: 30, unit: '天', step: 1440, label: '近 30 天 · 1 天一桶', tail: 0.63 },
  };

  const HOUR_PROFILE = [
    0.12, 0.08, 0.06, 0.05, 0.05, 0.08, 0.18, 0.42, 0.78, 1.05, 1.12, 0.96,
    0.7, 0.86, 1.08, 1.14, 1.02, 0.82, 0.58, 0.44, 0.36, 0.28, 0.2, 0.14,
  ];
  const DOW = [0.62, 1.06, 1.1, 1.08, 1.02, 0.92, 0.6];

  // 固定「现在」：静态稿每次打开都是同一组数，评审时不会被跳动的图干扰
  const NOW = new Date('2026-11-24T04:40:00+08:00');

  function rng(seed) {
    let s = seed >>> 0;
    return () => {
      s = (s * 1664525 + 1013904223) >>> 0;
      return s / 4294967296;
    };
  }

  const pad = (n) => String(n).padStart(2, '0');

  function labels(win) {
    const w = WINDOWS[win];
    const out = [];
    for (let i = w.buckets - 1; i >= 0; i--) {
      const d = new Date(NOW.getTime() - i * w.step * 60000);
      if (w.unit === '分钟') out.push(`${pad(d.getHours())}:${pad(Math.floor(d.getMinutes() / 10) * 10)}`);
      else if (w.unit === '小时') out.push(`${pad(d.getHours())}:00`);
      else out.push(`${pad(d.getMonth() + 1)}-${pad(d.getDate())}`);
    }
    return out;
  }

  /**
   * 一次量的三本账。`baseDay` 是「一天的检索次数」量级，三个屏（租户 / 单 Key / 站点）
   * 各自传自己的数，于是四屏之间的相对大小是对的。
   */
  function series(win, baseDay, seed) {
    const w = WINDOWS[win];
    const r = rng((seed || 7) * 104729 + w.buckets);
    const calls = [];
    for (let i = 0; i < w.buckets; i++) {
      let v;
      if (w.unit === '分钟') {
        v = (baseDay / 144) * HOUR_PROFILE[NOW.getHours()] * (0.8 + r() * 0.5);
      } else if (w.unit === '小时') {
        const d = new Date(NOW.getTime() - (w.buckets - 1 - i) * 3600000);
        v = (baseDay / 24) * HOUR_PROFILE[d.getHours()] * (0.85 + r() * 0.35);
      } else {
        const d = new Date(NOW.getTime() - (w.buckets - 1 - i) * 86400000);
        v = baseDay * DOW[d.getDay()] * (0.9 + r() * 0.24);
      }
      calls.push(Math.max(0, Math.round(v)));
    }
    // 最后一个桶还没走完
    calls[calls.length - 1] = Math.round(calls[calls.length - 1] * w.tail);

    const rr = calls.map((c, i) => Math.round(c * (0.3 + 0.12 * Math.abs(Math.sin(i + 1)))));
    const llm = calls.map((c) => Math.round(c * 0.008));
    const okRate = calls.map(() => Number((0.955 + r() * 0.038).toFixed(3)));
    const p95 = calls.map(() => Math.round(430 + r() * 420));
    return { labels: labels(win), r: calls, rr, llm, okRate, p95 };
  }

  const sum = (a) => a.reduce((x, y) => x + y, 0);
  const fmt = (n) => n.toLocaleString('zh-CN');

  // ── 主题 ────────────────────────────────────────────────────────────
  const instances = [];

  function mount(id, factory) {
    const el = document.getElementById(id);
    if (!el) return null;
    const chart = echarts.init(el);
    const entry = {
      chart,
      apply() {
        chart.setOption(factory(palette()), true);
      },
    };
    instances.push(entry);
    entry.apply();
    return entry;
  }

  function repaint() {
    instances.forEach((entry) => entry.apply());
  }

  function watchTheme() {
    // 画布的主题是纯 CSS（:has() 读 radio），所以这里只能「等它算完再读」
    const after = () => requestAnimationFrame(repaint);
    document.querySelectorAll('.theme-input').forEach((input) => input.addEventListener('change', after));
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    if (mq.addEventListener) mq.addEventListener('change', after);
    window.addEventListener('resize', () => instances.forEach((entry) => entry.chart.resize()));
  }

  // ── 常用图型 ────────────────────────────────────────────────────────

  function axis(p, extra) {
    return Object.assign(
      {
        animation: false,
        textStyle: { fontFamily: p.font, color: p.muted, fontSize: 11 },
        grid: { left: 4, right: 6, top: 16, bottom: 2, containLabel: true },
        tooltip: {
          trigger: 'axis',
          backgroundColor: css('--glass-solid-card', '#fff'),
          borderColor: p.line,
          borderWidth: 1,
          padding: [7, 10],
          textStyle: { color: p.ink, fontSize: 12, fontFamily: p.font },
        },
        axisPointer: { type: 'shadow', shadowStyle: { color: p.line } },
      },
      extra || {},
    );
  }

  /** 三本账堆叠柱（检索 / 重排 / 大模型） */
  function meters(p, data, opts) {
    const o = opts || {};
    return axis(p, {
      legend: {
        show: true,
        right: 0,
        top: 0,
        itemWidth: 8,
        itemHeight: 8,
        itemGap: 14,
        textStyle: { color: p.muted, fontSize: 12, fontFamily: p.font },
        data: ['检索', '重排', '大模型'],
      },
      grid: { left: 4, right: 6, top: 30, bottom: 2, containLabel: true },
      xAxis: {
        type: 'category',
        data: data.labels,
        axisLine: { lineStyle: { color: p.line } },
        axisTick: { show: false },
        axisLabel: { interval: o.interval === undefined ? 'auto' : o.interval, color: p.muted },
      },
      yAxis: { type: 'value', splitLine: { lineStyle: { color: p.line } }, axisLabel: { color: p.muted } },
      series: [
        { name: '检索', type: 'bar', stack: 'm', data: data.r, barMaxWidth: 22, itemStyle: { color: p.accent } },
        { name: '重排', type: 'bar', stack: 'm', data: data.rr, barMaxWidth: 22, itemStyle: { color: p.star } },
        {
          name: '大模型',
          type: 'bar',
          stack: 'm',
          data: data.llm,
          barMaxWidth: 22,
          itemStyle: { color: p.third, borderRadius: [3, 3, 0, 0] },
        },
      ],
    });
  }

  /** 成功率（左轴 %）+ P95（右轴 ms） */
  function quality(p, data) {
    return axis(p, {
      // 图例放到底部：顶部右侧要留给右轴的 1000ms 刻度，放上面会和刻度叠字
      legend: {
        show: true,
        left: 0,
        bottom: 0,
        itemWidth: 8,
        itemHeight: 8,
        itemGap: 14,
        textStyle: { color: p.muted, fontSize: 12, fontFamily: p.font },
      },
      grid: { left: 4, right: 6, top: 16, bottom: 26, containLabel: true },
      xAxis: {
        type: 'category',
        data: data.labels,
        axisLine: { lineStyle: { color: p.line } },
        axisTick: { show: false },
        axisLabel: { color: p.muted },
      },
      yAxis: [
        {
          type: 'value',
          min: 90,
          max: 100,
          splitLine: { lineStyle: { color: p.line } },
          axisLabel: { color: p.muted, formatter: '{value}%' },
        },
        {
          type: 'value',
          min: 300,
          max: 1000,
          splitLine: { show: false },
          axisLabel: { color: p.muted, formatter: '{value}ms' },
        },
      ],
      series: [
        {
          name: '成功率',
          type: 'line',
          data: data.okRate.map((v) => Number((v * 100).toFixed(1))),
          smooth: true,
          symbol: 'none',
          lineStyle: { width: 2, color: p.accent },
        },
        {
          name: 'P95',
          type: 'line',
          yAxisIndex: 1,
          data: data.p95,
          smooth: true,
          symbol: 'none',
          lineStyle: { width: 1.6, color: p.third, type: 'dotted' },
        },
      ],
    });
  }

  /** 横向条形（失败原因、延迟分档都走它） */
  function hbars(p, items, opts) {
    const o = opts || {};
    return axis(p, {
      grid: { left: 4, right: 46, top: 4, bottom: 0, containLabel: true },
      xAxis: { type: 'value', show: false },
      yAxis: {
        type: 'category',
        inverse: true,
        data: items.map((i) => i.name),
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: p.muted, fontSize: 12 },
      },
      series: [
        {
          type: 'bar',
          data: items.map((i) => ({ value: i.value, itemStyle: { color: i.color(p) } })),
          barWidth: 12,
          itemStyle: { borderRadius: 3 },
          label: {
            show: true,
            position: 'right',
            color: p.muted,
            fontSize: 11,
            formatter: o.formatter || '{c}',
          },
        },
      ],
    });
  }

  /** 竖柱（延迟分布） */
  function vbars(p, labelsArr, values) {
    return axis(p, {
      xAxis: {
        type: 'category',
        data: labelsArr,
        axisLine: { lineStyle: { color: p.line } },
        axisTick: { show: false },
        axisLabel: { color: p.muted, fontSize: 11 },
      },
      yAxis: { type: 'value', splitLine: { lineStyle: { color: p.line } }, axisLabel: { color: p.muted } },
      series: [
        {
          type: 'bar',
          data: values,
          barMaxWidth: 28,
          itemStyle: { color: p.accent, borderRadius: [3, 3, 0, 0] },
        },
      ],
    });
  }

  window.UsageCharts = {
    WINDOWS,
    NOW,
    series,
    labels,
    palette,
    axis,
    meters,
    quality,
    hbars,
    vbars,
    mount,
    repaint,
    watchTheme,
    sum,
    fmt,
    rng,
  };
})();
