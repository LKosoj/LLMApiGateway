import assert from 'node:assert/strict';
import test from 'node:test';

import { registerFallback } from '../src/fallback.mjs';

// buildEvalTaskSummary агрегирует evalSummary.tasks поперёк моделей и не
// трогает DOM, поэтому его можно проверять без браузера. Рендер сводки
// (renderEvalTaskSummary) покрыт браузерным тестом
// tests/test_rules_editor_eval_task_summary_browser.py.
function buildSummary(models) {
    const ctx = {};
    registerFallback(ctx);
    return ctx.buildEvalTaskSummary(models);
}

function modelWithTasks(tasks) {
    return {id: 'provider:model', evalSummary: {tasks}};
}

test('aggregates pass counts and points across models', () => {
    const rows = buildSummary([
        modelWithTasks([
            {id: 'tool_call_lite', points: 200, maxPoints: 200, status: 'passed', details: {jsonObject: true}},
            {id: 'grounded_qa_lite', points: 25, maxPoints: 50, status: 'failed', details: {refusedUnknown: false}},
        ]),
        modelWithTasks([
            {id: 'tool_call_lite', points: 100, maxPoints: 200, status: 'failed', details: {jsonObject: false}},
            {id: 'grounded_qa_lite', points: 50, maxPoints: 50, status: 'passed', details: {refusedUnknown: true}},
        ]),
    ]);

    const byId = new Map(rows.map(row => [row.id, row]));
    assert.equal(byId.get('tool_call_lite').evaluated, 2);
    assert.equal(byId.get('tool_call_lite').passed, 1);
    assert.equal(byId.get('tool_call_lite').points, 300);
    assert.equal(byId.get('tool_call_lite').maxPoints, 400);
    assert.equal(byId.get('grounded_qa_lite').passed, 1);
});

test('counts only failed boolean checks', () => {
    const rows = buildSummary([
        modelWithTasks([
            {
                id: 'instruction_following_lite',
                points: 80,
                maxPoints: 200,
                status: 'failed',
                details: {
                    exactlyFourLines: true,
                    jsonLineValid: false,
                    markerRepeats: false,
                    marker: 'ROUTER',
                    expectedRepeats: 2,
                    rawOutput: 'STATUS: READY',
                },
            },
        ]),
        modelWithTasks([
            {
                id: 'instruction_following_lite',
                points: 160,
                maxPoints: 200,
                status: 'failed',
                details: {exactlyFourLines: true, jsonLineValid: false, markerRepeats: true, rawOutput: 'x'},
            },
        ]),
    ]);

    const checks = rows[0].failedChecks;
    assert.equal(checks.get('jsonLineValid'), 2);
    assert.equal(checks.get('markerRepeats'), 1);
    assert.equal(checks.has('marker'), false);
    assert.equal(checks.has('rawOutput'), false);
    assert.equal(checks.has('expectedRepeats'), false);
});

test('ignores models without an eval summary', () => {
    const rows = buildSummary([
        {id: 'provider:missing'},
        {id: 'provider:skipped', evalSummary: {status: 'not_evaluated', tasks: []}},
        modelWithTasks([{id: 'code_unit_lite', points: 200, maxPoints: 200, status: 'passed', details: {}}]),
    ]);

    assert.equal(rows.length, 1);
    assert.equal(rows[0].id, 'code_unit_lite');
    assert.equal(rows[0].evaluated, 1);
});
