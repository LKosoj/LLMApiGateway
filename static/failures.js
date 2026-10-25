document.addEventListener('DOMContentLoaded', () => {
    'use strict';

    const STATUS_ENDPOINT = '/v1/api/upstream-status';
    const PAUSES_ENDPOINT = '/v1/admin/upstream-pauses';
    const RESUME_ENDPOINT = '/v1/admin/upstream-pauses/resume';
    const HEALTH_RUN_ENDPOINT = '/v1/api/upstream-health/run';
    const CHAINS_ENDPOINT = '/v1/api/fallback-records?limit=10';
    const RANGE_SLOTS = { '24h': { count: 24, unit: 'hour' }, '7d': { count: 7, unit: 'day' } };
    const RELIABILITY_LIMIT = 15;
    const WHY_LIMIT = 8;
    // Error codes with their own wording in failures:reasons; other http_5xx
    // codes share one label, everything else falls back to "unknown".
    const KNOWN_REASONS = new Set([
        'http_429', 'http_400', 'http_401', 'http_403', 'http_404',
        'read_timeout', 'connect_timeout', 'connect_error', 'context_overflow',
        'empty_completion', 'format_ignored', 'unparsed_tool_call_dialect', 'upstream_provider_error',
    ]);
    const { apiFetch, bootstrapRoleUI } = window.gatewayAuth;
    const i18n = window.gatewayI18n;
    const report = window.gatewayReport;
    const { el, cell, headCell, target, badge, numberValue } = report;

    Theme.attachToggle('darkModeToggle');

    const els = {
        updatedAt: document.getElementById('updatedAt'),
        range: document.getElementById('rangeSelect'),
        checkAll: document.getElementById('checkAllBtn'),
        kpiUnavailable: document.getElementById('kpiUnavailable'),
        kpiNotRescued: document.getElementById('kpiNotRescued'),
        kpiRescued: document.getElementById('kpiRescued'),
        kpiFailedAttempts: document.getElementById('kpiFailedAttempts'),
        downList: document.getElementById('downList'),
        downEmpty: document.getElementById('downEmpty'),
        errorsHint: document.getElementById('errorsHint'),
        errorsChart: document.getElementById('errorsChart'),
        whyList: document.getElementById('whyList'),
        reliabilityBody: document.getElementById('reliabilityBody'),
        reliabilityEmpty: document.getElementById('reliabilityEmpty'),
        chainsBody: document.getElementById('chainsBody'),
        chainsEmpty: document.getElementById('chainsEmpty'),
        pauseDetails: document.getElementById('pauseDetails'),
        pauseForm: document.getElementById('pauseForm'),
        pauseKey: document.getElementById('pauseKey'),
        pauseModel: document.getElementById('pauseModel'),
        pauseModels: document.getElementById('pauseModels'),
        pauseMinutes: document.getElementById('pauseMinutes'),
        pauseReason: document.getElementById('pauseReason'),
        pauseSubmit: document.getElementById('pauseSubmit'),
        modelDetail: document.getElementById('modelDetail'),
        modelDetailTitle: document.getElementById('modelDetailTitle'),
        modelDetailHint: document.getElementById('modelDetailHint'),
        modelDetailChart: document.getElementById('modelDetailChart'),
        modelDetailWhy: document.getElementById('modelDetailWhy'),
        modelDetailClose: document.getElementById('modelDetailClose'),
        messageArea: document.getElementById('messageArea'),
        messageDetail: document.getElementById('messageDetail'),
    };
    const pageStatus = window.gatewayUi.createStatus(els.messageArea, {
        rawDetailElement: els.messageDetail,
        renderMessage: (message) => i18n.t(message.key, message.values || {}),
    });

    let lastData = null;
    let checking = false;
    let checkedCount = null;
    let detailTarget = null;
    let detailData = null;
    let detailGeneration = 0;
    let requestGeneration = 0;
    let unsubscribeLocale = null;
    let stopped = false;

    function formatNumber(value) {
        return i18n.formatNumber(numberValue(value));
    }

    function formatPercent(value) {
        return i18n.t('failures:values.percent', {
            value: i18n.formatNumber(numberValue(value), { maximumFractionDigits: 1 }),
        });
    }

    function formatSeconds(ms) {
        return i18n.t('failures:values.seconds', {
            value: i18n.formatNumber(numberValue(ms) / 1000, { maximumFractionDigits: 1 }),
        });
    }

    function formatCountdown(seconds) {
        const total = Math.max(0, Math.round(numberValue(seconds)));
        return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
    }

    function formatRelative(iso) {
        const date = iso ? new Date(iso) : null;
        if (!date || Number.isNaN(date.getTime())) return i18n.t('failures:values.never');
        const minutes = Math.round((Date.now() - date.getTime()) / 60000);
        if (minutes < 60) return i18n.formatRelativeTime(-Math.max(0, minutes), 'minute');
        if (minutes < 48 * 60) return i18n.formatRelativeTime(-Math.round(minutes / 60), 'hour');
        return i18n.formatRelativeTime(-Math.round(minutes / 1440), 'day');
    }

    function reasonLabel(code) {
        if (KNOWN_REASONS.has(code)) return i18n.t(`failures:reasons.${code}`);
        if (/^http_5\d\d$/.test(code || '')) return i18n.t('failures:reasons.http_5xx');
        return i18n.t('failures:reasons.unknown');
    }

    function fingerprintLabel(fingerprint) {
        return i18n.t('failures:down.keyFingerprint', { fingerprint: String(fingerprint || '').slice(-4) });
    }

    function setKpi(card, value, note, tone) {
        card.querySelector('[data-kpi-value]').textContent = value;
        card.querySelector('[data-kpi-note]').textContent = note;
        if (tone) card.setAttribute('data-tone', tone);
        else card.removeAttribute('data-tone');
    }

    // "▲ 12% vs the previous period"; `upIsBad` colours growth red and a drop green.
    // `previous` is null when the dashboard came without a comparison.
    function setDelta(card, current, previous, upIsBad) {
        const change = previous === null ? null : report.changeOf(current, previous);
        if (!change) {
            report.setKpiDelta(card, '');
            return;
        }
        const percent = i18n.formatNumber(change.ratio || 0, { style: 'percent', maximumFractionDigits: 0 });
        let tone = null;
        if (upIsBad && (change.direction === 'up' || change.direction === 'new')) tone = 'bad';
        if (upIsBad && change.direction === 'down') tone = 'ok';
        report.setKpiDelta(card, i18n.t(`failures:delta.${change.direction}`, { percent }), tone);
    }

    // One card per target the router skips right now: cooling down, failing,
    // key rejected, or paused manually (a pause may cover all models of a key).
    function downCards(data) {
        const cards = [];
        data.status.forEach((row) => {
            let kind = null;
            if (numberValue(row.cooldown_remaining_seconds) > 0) kind = 'cooldown';
            else if (row.health_status === 'invalid') kind = 'invalid';
            else if (row.health_status === 'error') kind = 'error';
            if (kind) cards.push({ kind, row });
        });
        cards.sort((a, b) => String(b.row.last_checked_at || '').localeCompare(String(a.row.last_checked_at || '')));
        data.pauses.forEach((pause) => cards.push({ kind: 'paused', row: pause }));
        return cards;
    }

    // Targets known to the router plus manual pauses on targets it has not
    // seen yet, so a paused model is never missing from the "of N".
    function targetCount(data) {
        const known = new Set(data.status.map((row) => `${row.provider}|${row.upstream_key_fingerprint}|${row.model}`));
        const extra = data.pauses.filter((pause) => (
            !pause.model || !known.has(`${pause.provider}|${pause.upstream_key_fingerprint}|${pause.model}`)
        ));
        return known.size + extra.length;
    }

    function renderKpis(data, cards) {
        const counts = { cooldown: 0, error: 0, paused: 0 };
        cards.forEach((card) => {
            counts[card.kind === 'invalid' ? 'error' : card.kind] += 1;
        });
        setKpi(
            els.kpiUnavailable,
            i18n.t('failures:kpis.unavailableValue', {
                count: formatNumber(cards.length),
                total: formatNumber(targetCount(data)),
            }),
            i18n.t('failures:kpis.unavailableNote', {
                cooldown: formatNumber(counts.cooldown),
                error: formatNumber(counts.error),
                paused: formatNumber(counts.paused),
            }),
            cards.length > 0 ? 'bad' : null,
        );

        const fallback = data.dashboard.reliability?.fallback || {};
        const requests = fallback.requests || {};
        const summary = fallback.summary || {};
        setKpi(
            els.kpiNotRescued,
            formatNumber(requests.failed),
            i18n.t('failures:kpis.notRescuedNote'),
            numberValue(requests.failed) > 0 ? 'bad' : null,
        );
        setKpi(
            els.kpiRescued,
            formatNumber(requests.rescued),
            i18n.t('failures:kpis.rescuedNote', {
                total: numberValue(requests.rescued) + numberValue(requests.failed),
            }),
        );
        const attempts = numberValue(summary.attempts);
        setKpi(
            els.kpiFailedAttempts,
            formatNumber(summary.errors),
            i18n.t('failures:kpis.failedAttemptsNote', {
                share: formatPercent(attempts ? (100 * numberValue(summary.errors)) / attempts : 0),
            }),
            numberValue(summary.errors) > 0 ? 'warn' : null,
        );

        const previous = data.dashboard.previous?.fallback;
        const previousRequests = previous?.requests || {};
        const previousSummary = previous?.summary || {};
        setDelta(els.kpiNotRescued, requests.failed, previous ? numberValue(previousRequests.failed) : null, true);
        setDelta(els.kpiRescued, requests.rescued, previous ? numberValue(previousRequests.rescued) : null, false);
        setDelta(els.kpiFailedAttempts, summary.errors, previous ? numberValue(previousSummary.errors) : null, true);
    }

    function resumeButton(pause) {
        const button = el('button', 'rp-btn rp-btn-sm', i18n.t('failures:actions.resume'));
        button.type = 'button';
        button.addEventListener('click', () => void resumePause(pause, button));
        return button;
    }

    function cardButton(label, onClick) {
        const button = el('button', 'rp-btn rp-btn-sm', label);
        button.type = 'button';
        button.addEventListener('click', () => onClick(button));
        return button;
    }

    function renderDown(cards) {
        els.downEmpty.hidden = cards.length > 0;
        els.downList.hidden = cards.length === 0;
        els.downList.replaceChildren(...cards.map(({ kind, row }) => {
            const card = el('article', 'rp-down-card');
            if (kind === 'cooldown') card.setAttribute('data-tone', 'warn');
            if (kind === 'paused') card.setAttribute('data-tone', 'paused');

            const head = el('div', 'rp-down-card-head');
            const time = formatCountdown(kind === 'paused' ? row.remaining_seconds : row.cooldown_remaining_seconds);
            head.append(
                row.model ? target(row.model, row.provider) : target(i18n.t('failures:down.allModels'), row.provider),
                badge(
                    i18n.t(`failures:down.badge.${kind}`, { time }),
                    kind === 'paused' ? null : kind === 'cooldown' ? 'warn' : 'bad',
                ),
            );
            card.append(head, el('div', 'rp-down-why', i18n.t(`failures:down.why.${kind}`, { reason: row.reason || '' })));
            if (kind !== 'paused' && row.last_error) {
                const raw = el('div', 'rp-down-raw rp-mono', row.last_error);
                raw.setAttribute('title', row.last_error);
                card.appendChild(raw);
            }

            const meta = el('div', 'rp-down-meta');
            meta.appendChild(el('span', null, fingerprintLabel(row.upstream_key_fingerprint)));
            if (kind !== 'paused') {
                if (numberValue(row.cooldown_escalation_level) > 0) {
                    meta.appendChild(el('span', null, i18n.t('failures:down.escalation', {
                        level: formatNumber(row.cooldown_escalation_level),
                    })));
                }
                meta.appendChild(el('span', null, i18n.t('failures:down.checked', {
                    time: formatRelative(row.last_checked_at),
                })));
            }
            card.appendChild(meta);
            const actions = card.appendChild(el('div', 'rp-down-actions'));
            if (kind === 'paused') {
                actions.appendChild(resumeButton(row));
            } else {
                actions.append(
                    cardButton(i18n.t('failures:actions.checkNow'), (button) => void checkTarget(row, button)),
                    cardButton(i18n.t('failures:actions.pause'), () => openPauseForm(row)),
                );
            }
            return card;
        }));
    }

    function errorsHint(range) {
        return i18n.t(RANGE_SLOTS[range].unit === 'hour' ? 'failures:errors.hintHour' : 'failures:errors.hintDay');
    }

    function errorsChart(container, range, dashboard, ariaLabel) {
        const slotsConfig = RANGE_SLOTS[range];
        const slots = report.timeSlots(slotsConfig.count, slotsConfig.unit);
        const values = report.seriesValues(slots, dashboard.reliability?.fallback?.series, 'errors');
        const dateOptions = slotsConfig.unit === 'hour'
            ? { hour: '2-digit', minute: '2-digit' }
            : { day: 'numeric', month: 'short' };
        const labels = slots.map((slot) => i18n.formatDate(slot.date, dateOptions));
        if (values.every((value) => value === 0)) {
            report.clearChart(container, el('div', 'rp-muted', i18n.t('failures:why.empty')));
            return;
        }
        report.barChart(container, {
            values,
            labels,
            highlight: values.length - 1,
            tone: 'bad',
            ariaLabel,
            formatTick: formatNumber,
            formatTooltip: (index, value) => i18n.t('failures:errors.tooltip', {
                period: labels[index],
                value: formatNumber(value),
            }),
        });
    }

    function renderErrors(data) {
        els.errorsHint.textContent = errorsHint(data.range);
        errorsChart(els.errorsChart, data.range, data.dashboard, i18n.t('failures:errors.aria'));
    }

    function whyList(container, dashboard) {
        const rows = (dashboard.reliability?.fallback?.error_types || []).slice(0, WHY_LIMIT);
        if (rows.length === 0) {
            container.replaceChildren(el('div', 'rp-muted', i18n.t('failures:why.empty')));
            return;
        }
        report.rankList(container, rows.map((row) => ({
            label: `${reasonLabel(row.label)} · ${row.label}`,
            value: row.errors,
            display: formatNumber(row.errors),
            tone: 'bad',
        })));
    }

    function renderWhy(data) {
        whyList(els.whyList, data.dashboard);
    }

    function renderDetail() {
        if (!detailTarget) return;
        els.modelDetailTitle.textContent = i18n.t('failures:detail.heading', {
            model: detailTarget.model,
            provider: detailTarget.provider,
            fingerprint: String(detailTarget.upstream_key_fingerprint || '').slice(-4),
        });
        if (!detailData) return;
        els.modelDetailHint.textContent = errorsHint(detailData.range);
        errorsChart(
            els.modelDetailChart,
            detailData.range,
            detailData.dashboard,
            i18n.t('failures:detail.aria', { model: detailTarget.model }),
        );
        whyList(els.modelDetailWhy, detailData.dashboard);
    }

    // Pause form: key choices come from the configured provider keys, model
    // suggestions from what the router has seen on the chosen key.
    function pauseTargetValue(item) {
        return `${item.provider}|${item.upstream_key_fingerprint}`;
    }

    function renderPauseModels() {
        const chosen = els.pauseKey.value;
        const models = new Set((lastData?.status || [])
            .filter((row) => pauseTargetValue(row) === chosen && row.model)
            .map((row) => row.model));
        els.pauseModels.replaceChildren(...[...models].sort().map((model) => {
            const option = document.createElement('option');
            option.value = model;
            return option;
        }));
    }

    function renderPauseForm(data) {
        const chosen = els.pauseKey.value;
        els.pauseKey.replaceChildren(...data.pauseTargets.map((item) => {
            const option = document.createElement('option');
            option.value = pauseTargetValue(item);
            option.textContent = `${item.provider} · ${fingerprintLabel(item.upstream_key_fingerprint)}`;
            return option;
        }));
        if (data.pauseTargets.some((item) => pauseTargetValue(item) === chosen)) els.pauseKey.value = chosen;
        renderPauseModels();
    }

    function renderReliability(data) {
        const rows = [...(data.dashboard.reliability?.fallback?.upstream || [])]
            .sort((a, b) => numberValue(a.success_rate) - numberValue(b.success_rate)
                || numberValue(b.errors) - numberValue(a.errors))
            .slice(0, RELIABILITY_LIMIT);
        els.reliabilityEmpty.hidden = rows.length > 0;
        els.reliabilityBody.replaceChildren(...rows.map((row) => {
            const rate = numberValue(row.success_rate);
            const tone = rate < 90 ? 'bad' : rate < 99 ? 'warn' : 'ok';
            const success = el('div', 'rp-meter-cell');
            success.append(report.meter(rate / 100, tone), el('span', null, formatPercent(rate)));
            const open = report.targetButton(row.model, row.provider);
            open.addEventListener('click', () => openDetail(row));
            const tr = document.createElement('tr');
            tr.append(
                headCell(open),
                cell(
                    i18n.t('failures:reliability.columns.key'),
                    el('span', 'rp-mono', fingerprintLabel(row.upstream_key_fingerprint)),
                ),
                cell(i18n.t('failures:reliability.columns.success'), success),
                cell(i18n.t('failures:reliability.columns.errors'), formatNumber(row.errors), 'rp-num'),
                cell(i18n.t('failures:reliability.columns.attempts'), formatNumber(row.attempts), 'rp-num'),
                cell(
                    i18n.t('failures:reliability.columns.topError'),
                    row.top_error_type ? reasonLabel(row.top_error_type) : i18n.t('failures:values.notAvailable'),
                ),
                cell(
                    i18n.t('failures:reliability.columns.duration'),
                    row.avg_duration_ms === null || row.avg_duration_ms === undefined
                        ? i18n.t('failures:values.notAvailable')
                        : formatSeconds(row.avg_duration_ms),
                    'rp-num',
                ),
            );
            return tr;
        }));
    }

    function chainHops(attempts) {
        const chain = el('div', 'rp-chain');
        attempts.forEach((attempt, index) => {
            if (index > 0) chain.appendChild(el('span', 'rp-arrow', '→'));
            const hop = el('span', 'rp-hop', `${attempt.success ? '✓' : '✕'} ${attempt.model || attempt.provider || ''}`);
            if (attempt.success) hop.setAttribute('data-tone', 'ok');
            const detail = attempt.success
                ? formatSeconds(attempt.duration_ms)
                : `${reasonLabel(attempt.error_type)} · ${formatSeconds(attempt.duration_ms)}`;
            hop.appendChild(el('em', null, detail));
            if (attempt.error_message) hop.setAttribute('title', attempt.error_message);
            chain.appendChild(hop);
        });
        return chain;
    }

    function renderChains(data) {
        els.chainsEmpty.hidden = data.chains.length > 0;
        els.chainsBody.replaceChildren(...data.chains.map((record) => {
            const time = el('span', null, i18n.formatDate(new Date(record.timestamp), {
                day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
            }));
            const timeCell = el('span');
            timeCell.append(
                time,
                ' ',
                badge(
                    i18n.t(record.success ? 'failures:chains.rescuedBadge' : 'failures:chains.notRescuedBadge'),
                    record.success ? 'ok' : 'bad',
                ),
            );
            const requested = el('div', null, record.gateway_model || i18n.t('failures:values.notAvailable'));
            requested.appendChild(el('div', 'rp-muted', i18n.t('failures:chains.total', {
                client: record.x_title || i18n.t('failures:values.notAvailable'),
                duration: formatSeconds(record.total_duration_ms),
            })));
            const tr = document.createElement('tr');
            tr.append(
                headCell(timeCell),
                cell(i18n.t('failures:chains.columns.requested'), requested),
                cell(i18n.t('failures:chains.columns.chain'), chainHops(record.attempts || [])),
            );
            return tr;
        }));
    }

    function renderUpdated() {
        if (!lastData) return;
        const time = i18n.formatDate(lastData.loadedAt, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        const updated = i18n.t('failures:updated', { time });
        els.updatedAt.textContent = checkedCount === null
            ? updated
            : `${updated} · ${i18n.t('failures:checkDone', { count: checkedCount })}`;
    }

    function renderCheckButton() {
        els.checkAll.disabled = checking;
        els.checkAll.textContent = i18n.t(checking ? 'failures:actions.checking' : 'failures:actions.checkAll');
    }

    function render() {
        renderCheckButton();
        if (!lastData) return;
        const cards = downCards(lastData);
        renderKpis(lastData, cards);
        renderDown(cards);
        renderErrors(lastData);
        renderWhy(lastData);
        renderReliability(lastData);
        renderChains(lastData);
        renderPauseForm(lastData);
        renderDetail();
        renderUpdated();
    }

    function showError(message, rawDetail = null) {
        els.messageArea.hidden = false;
        els.messageDetail.hidden = rawDetail === null;
        pageStatus.error(message, { rawDetail });
    }

    function clearError() {
        pageStatus.clear();
        els.messageArea.hidden = true;
        els.messageDetail.hidden = true;
    }

    function reportFailure(error) {
        if (error.message === 'Authentication required') return;
        const descriptor = error.descriptor || window.gatewayUi.describeApiError(null);
        showError({ key: descriptor.summaryKey, values: descriptor.summaryValues }, descriptor.rawDetail || error.message);
    }

    async function fetchJson(url, options) {
        const response = await apiFetch(url, options);
        const body = await response.json().catch(() => null);
        if (!response.ok) {
            const descriptor = window.gatewayUi.describeApiError(body, { status: response.status });
            const error = new Error(descriptor.rawDetail || `HTTP ${response.status}`);
            error.descriptor = descriptor;
            throw error;
        }
        return body;
    }

    function postJson(url, payload) {
        return fetchJson(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
    }

    async function load() {
        const generation = ++requestGeneration;
        const range = els.range.value;
        void loadDetail();
        try {
            const [status, pauses, dashboard, chains] = await Promise.all([
                fetchJson(STATUS_ENDPOINT),
                fetchJson(PAUSES_ENDPOINT),
                fetchJson(`/v1/api/analytics-dashboard?range=${encodeURIComponent(range)}&compare=previous`),
                fetchJson(CHAINS_ENDPOINT),
            ]);
            if (stopped || generation !== requestGeneration) return;
            lastData = {
                range,
                status: status.rows || [],
                pauses: pauses.pauses || [],
                pauseTargets: pauses.targets || [],
                dashboard,
                chains: chains.records || [],
                loadedAt: new Date(),
            };
            clearError();
            render();
        } catch (error) {
            if (stopped || generation !== requestGeneration) return;
            reportFailure(error);
        }
    }

    async function checkAll() {
        if (checking) return;
        checking = true;
        renderCheckButton();
        try {
            const result = await postJson(HEALTH_RUN_ENDPOINT, {});
            checkedCount = numberValue(result.checked);
            await load();
        } catch (error) {
            reportFailure(error);
        } finally {
            checking = false;
            renderCheckButton();
        }
    }

    async function checkTarget(row, button) {
        button.disabled = true;
        button.textContent = i18n.t('failures:actions.checking');
        try {
            const result = await postJson(HEALTH_RUN_ENDPOINT, {
                provider: row.provider,
                model: row.model,
                upstream_key_fingerprint: row.upstream_key_fingerprint,
            });
            checkedCount = numberValue(result.checked);
            await load();
        } catch (error) {
            button.disabled = false;
            button.textContent = i18n.t('failures:actions.checkNow');
            reportFailure(error);
        }
    }

    function openPauseForm(row) {
        els.pauseDetails.open = true;
        els.pauseKey.value = pauseTargetValue(row);
        renderPauseModels();
        els.pauseModel.value = row.model || '';
        els.pauseReason.focus();
    }

    async function submitPause(event) {
        event.preventDefault();
        const chosen = lastData?.pauseTargets.find((item) => pauseTargetValue(item) === els.pauseKey.value);
        if (!chosen) return;
        els.pauseSubmit.disabled = true;
        try {
            await postJson(PAUSES_ENDPOINT, {
                provider: chosen.provider,
                upstream_key_fingerprint: chosen.upstream_key_fingerprint,
                model: els.pauseModel.value.trim() || null,
                duration_seconds: Math.round(Number(els.pauseMinutes.value) * 60),
                reason: els.pauseReason.value.trim(),
            });
            els.pauseReason.value = '';
            els.pauseDetails.open = false;
            await load();
        } catch (error) {
            reportFailure(error);
        } finally {
            els.pauseSubmit.disabled = false;
        }
    }

    async function loadDetail() {
        if (!detailTarget) return;
        const generation = ++detailGeneration;
        const range = els.range.value;
        const params = new URLSearchParams({
            range,
            provider: detailTarget.provider || '',
            model: detailTarget.model || '',
            upstream_key_fingerprint: detailTarget.upstream_key_fingerprint || '',
        });
        try {
            const dashboard = await fetchJson(`/v1/api/analytics-dashboard?${params}`);
            if (stopped || generation !== detailGeneration) return;
            detailData = { range, dashboard };
            renderDetail();
        } catch (error) {
            if (stopped || generation !== detailGeneration) return;
            reportFailure(error);
        }
    }

    function openDetail(row) {
        detailTarget = {
            provider: row.provider,
            model: row.model,
            upstream_key_fingerprint: row.upstream_key_fingerprint,
        };
        detailData = null;
        report.clearChart(els.modelDetailChart, el('div', 'rp-muted', i18n.t('failures:detail.loading')));
        els.modelDetailWhy.replaceChildren();
        els.modelDetailHint.textContent = '';
        els.modelDetail.hidden = false;
        renderDetail();
        els.modelDetailTitle.focus();
        void loadDetail();
    }

    function closeDetail() {
        detailTarget = null;
        detailData = null;
        detailGeneration += 1;
        els.modelDetail.hidden = true;
    }

    async function resumePause(pause, button) {
        button.disabled = true;
        try {
            await postJson(RESUME_ENDPOINT, {
                provider: pause.provider,
                upstream_key_fingerprint: pause.upstream_key_fingerprint,
                model: pause.model,
            });
            await load();
        } catch (error) {
            button.disabled = false;
            reportFailure(error);
        }
    }

    els.range.addEventListener('change', () => void load());
    els.checkAll.addEventListener('click', () => void checkAll());
    els.pauseKey.addEventListener('change', renderPauseModels);
    els.pauseForm.addEventListener('submit', (event) => void submitPause(event));
    els.modelDetailClose.addEventListener('click', closeDetail);

    window.addEventListener('beforeunload', () => {
        stopped = true;
        requestGeneration += 1;
        unsubscribeLocale?.();
    });

    Promise.all([bootstrapRoleUI(), i18n.ready]).then(() => {
        if (stopped) return;
        unsubscribeLocale = i18n.subscribe(() => {
            pageStatus.rerender();
            render();
        });
        renderCheckButton();
        void load();
    }).catch(() => undefined);
});
