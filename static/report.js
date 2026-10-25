(function initGatewayReport(global) {
    'use strict';

    // Shared DOM helpers of the report pages (live.js, failures.js, my-key.js).
    // Everything visible is passed in already translated: this file owns no copy.

    const SVG_NS = 'http://www.w3.org/2000/svg';
    const CHART_HEIGHT = 220;
    const CHART_PADDING = { top: 10, right: 8, bottom: 24, left: 44 };
    const MIN_LABEL_SPACING = 72;

    function el(tagName, className, text) {
        const element = document.createElement(tagName);
        if (className) element.className = className;
        if (text !== undefined && text !== null) element.textContent = String(text);
        return element;
    }

    function svgEl(tagName, attrs = {}) {
        const element = document.createElementNS(SVG_NS, tagName);
        Object.entries(attrs).forEach(([key, value]) => element.setAttribute(key, String(value)));
        return element;
    }

    function numberValue(value) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : 0;
    }

    function niceMax(value) {
        if (value <= 0) return 1;
        const step = 10 ** Math.floor(Math.log10(value));
        return Math.ceil(value / step) * step;
    }

    // Draws a bar chart into `container`. `values` and `labels` are parallel
    // arrays; `highlight` is the index of the bar to emphasise (current hour).
    function renderBarChart(container, options) {
        const values = options.values.map(numberValue);
        const width = Math.max(280, Math.floor(container.clientWidth || 600));
        const plotWidth = width - CHART_PADDING.left - CHART_PADDING.right;
        const plotHeight = CHART_HEIGHT - CHART_PADDING.top - CHART_PADDING.bottom;
        const max = niceMax(Math.max(0, ...values));
        const svg = svgEl('svg', {
            class: 'rp-chart',
            viewBox: `0 0 ${width} ${CHART_HEIGHT}`,
            role: 'img',
            'aria-label': options.ariaLabel,
        });
        if (options.tone) svg.setAttribute('data-tone', options.tone);

        [0, max / 2, max].forEach((tick) => {
            const y = CHART_PADDING.top + plotHeight - (tick / max) * plotHeight;
            svg.appendChild(svgEl('line', {
                x1: CHART_PADDING.left, x2: width - CHART_PADDING.right, y1: y, y2: y,
            }));
            const label = svgEl('text', { x: CHART_PADDING.left - 6, y: y + 4, 'text-anchor': 'end' });
            label.textContent = options.formatTick(tick);
            svg.appendChild(label);
        });

        const slot = plotWidth / Math.max(1, values.length);
        const barWidth = Math.max(2, slot * 0.72);
        const labelEvery = Math.max(1, Math.ceil(MIN_LABEL_SPACING / slot));
        values.forEach((value, index) => {
            const height = (value / max) * plotHeight;
            const x = CHART_PADDING.left + index * slot + (slot - barWidth) / 2;
            const rect = svgEl('rect', {
                x, y: CHART_PADDING.top + plotHeight - height,
                width: barWidth, height: Math.max(value > 0 ? 1 : 0, height), rx: 2,
            });
            if (index === options.highlight) rect.setAttribute('data-highlight', '');
            const tooltip = svgEl('title');
            tooltip.textContent = options.formatTooltip(index, value);
            rect.appendChild(tooltip);
            svg.appendChild(rect);
            if (index % labelEvery === 0) {
                const label = svgEl('text', {
                    x: CHART_PADDING.left + index * slot + slot / 2,
                    y: CHART_HEIGHT - 6,
                    'text-anchor': 'middle',
                });
                label.textContent = options.labels[index];
                svg.appendChild(label);
            }
        });
        container.replaceChildren(svg);
    }

    const chartOptions = new WeakMap();
    const chartObserver = typeof global.ResizeObserver === 'function'
        ? new global.ResizeObserver((entries) => {
            entries.forEach((entry) => {
                const options = chartOptions.get(entry.target);
                if (options) renderBarChart(entry.target, options);
            });
        })
        : null;

    function barChart(container, options) {
        if (!chartOptions.has(container)) chartObserver?.observe(container);
        chartOptions.set(container, options);
        renderBarChart(container, options);
    }

    // Replaces a chart with other content (e.g. an empty-state line) so the
    // resize observer no longer redraws the old bars over it.
    function clearChart(container, content) {
        chartOptions.delete(container);
        container.replaceChildren(content);
    }

    function meter(fraction, tone) {
        const element = el('div', 'rp-meter');
        if (tone) element.setAttribute('data-tone', tone);
        const fill = document.createElement('i');
        fill.style.width = `${Math.round(Math.min(1, Math.max(0, fraction)) * 100)}%`;
        element.appendChild(fill);
        return element;
    }

    // rows: [{ label, value, display }] — value drives the bar width.
    function rankList(container, rows) {
        const max = Math.max(1, ...rows.map((row) => numberValue(row.value)));
        const list = el('div', 'rp-rank');
        rows.forEach((row) => {
            const item = el('div', 'rp-rank-row');
            const name = el('b', null, row.label);
            name.setAttribute('title', row.label);
            item.append(name, el('span', null, row.display), meter(numberValue(row.value) / max, row.tone));
            list.appendChild(item);
        });
        container.replaceChildren(list);
    }

    // A table cell that turns into "label: value" on phones (see report.css).
    function cell(label, content, className) {
        const td = el('td', className);
        td.setAttribute('data-label', label);
        if (content instanceof Node) td.appendChild(content);
        else td.textContent = content === undefined || content === null ? '' : String(content);
        return td;
    }

    function headCell(content) {
        const td = el('td');
        td.setAttribute('data-head', '');
        td.appendChild(content);
        return td;
    }

    function target(model, provider) {
        const element = el('span', 'rp-target', model);
        if (provider) element.append(' · ', el('span', null, provider));
        return element;
    }

    // The same "model · provider" label, as a button that opens a per-model view.
    function targetButton(model, provider) {
        const button = el('button', 'rp-target-btn');
        button.type = 'button';
        button.appendChild(target(model, provider));
        return button;
    }

    function badge(text, tone) {
        const element = el('span', 'rp-badge', text);
        if (tone) element.setAttribute('data-tone', tone);
        return element;
    }

    function pad(value) {
        return String(value).padStart(2, '0');
    }

    // Keys match the SQLite strftime buckets of the analytics API (UTC).
    function hourKey(date) {
        return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())} ${pad(date.getUTCHours())}:00:00`;
    }

    function dayKey(date) {
        return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`;
    }

    // Returns the last `count` hourly (or daily) slots, oldest first.
    function timeSlots(count, unit, now = new Date()) {
        const stepMs = unit === 'day' ? 86400000 : 3600000;
        // Slots start at the bucket boundary so labels read "14:00", not "14:37".
        const start = unit === 'day' ? now.getTime() : Math.floor(now.getTime() / stepMs) * stepMs;
        const slots = [];
        for (let offset = count - 1; offset >= 0; offset -= 1) {
            const date = new Date(start - offset * stepMs);
            slots.push({ date, key: unit === 'day' ? dayKey(date) : hourKey(date) });
        }
        return slots;
    }

    function seriesValues(slots, series, field) {
        const byKey = new Map((series || []).map((row) => [row.time_period, numberValue(row[field])]));
        return slots.map((slot) => byKey.get(slot.key) || 0);
    }

    // Change against the previous period: null when both are zero, 'new' when
    // only the previous one is, otherwise the direction and relative size.
    function changeOf(current, previous) {
        const now = numberValue(current);
        const before = numberValue(previous);
        if (now === 0 && before === 0) return null;
        if (before === 0) return { direction: 'new', ratio: null };
        const ratio = (now - before) / before;
        if (Math.abs(ratio) < 0.005) return { direction: 'same', ratio: 0 };
        return { direction: ratio > 0 ? 'up' : 'down', ratio: Math.abs(ratio) };
    }

    // Puts (or removes, when `text` is empty) the "vs previous period" line
    // under a headline number.
    function setKpiDelta(card, text, tone) {
        let line = card.querySelector('[data-kpi-delta]');
        if (!text) {
            line?.remove();
            return;
        }
        if (!line) {
            line = el('div', 'rp-kpi-delta');
            line.setAttribute('data-kpi-delta', '');
            card.appendChild(line);
        }
        line.textContent = text;
        if (tone) line.setAttribute('data-tone', tone);
        else line.removeAttribute('data-tone');
    }

    function setPressed(group, value) {
        group.querySelectorAll('button[data-value]').forEach((button) => {
            button.setAttribute('aria-pressed', String(button.dataset.value === value));
        });
    }

    global.gatewayReport = Object.freeze({
        el,
        barChart,
        clearChart,
        meter,
        rankList,
        cell,
        headCell,
        target,
        targetButton,
        badge,
        numberValue,
        timeSlots,
        seriesValues,
        changeOf,
        setKpiDelta,
        setPressed,
    });
})(window);
