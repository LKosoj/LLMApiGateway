from llm_gateway_core.api.v1.web_evidence import (
    EvidenceMatrixError,
    build_evidence_matrix,
    filter_articles_for_passed_evidence,
    normalize_evidence_extraction,
    normalize_evidence_plan,
    parse_json_object,
)


def test_build_evidence_matrix_passes_candidate_with_required_quote():
    plan = normalize_evidence_plan(
        {
            "mode": "applied",
            "task_type": "vendor_selection",
            "candidate_type": "studio",
            "requirements": [
                {
                    "id": "specialization",
                    "label": "Specialization",
                    "description": "Relevant specialization",
                    "required": True,
                    "min_sources": 1,
                }
            ],
        }
    )
    article = {
        "url": "https://example.com/a",
        "title": "A",
        "content": "Studio A designs offices.",
    }
    extraction = normalize_evidence_extraction(
        {
            "candidates": [
                {
                    "name": "Studio A",
                    "aliases": [],
                    "evidence": [
                        {
                            "criterion_id": "specialization",
                            "status": "supports",
                            "claim": "Studio A designs offices.",
                            "quote": "Studio A designs offices.",
                            "confidence": 0.9,
                        }
                    ],
                }
            ]
        },
        plan=plan,
        article=article,
    )

    matrix = build_evidence_matrix(plan, extraction)

    assert matrix["passed_candidates"] == ["Studio A"]
    assert matrix["candidates"][0]["status"] == "passed"


def test_evidence_extraction_rejects_hallucinated_quote():
    plan = normalize_evidence_plan(
        {
            "mode": "applied",
            "task_type": "vendor_selection",
            "candidate_type": "studio",
            "requirements": [
                {
                    "id": "specialization",
                    "label": "Specialization",
                    "description": "Relevant specialization",
                    "required": True,
                    "min_sources": 1,
                }
            ],
        }
    )
    extraction = normalize_evidence_extraction(
        {
            "candidates": [
                {
                    "name": "Studio A",
                    "aliases": [],
                    "evidence": [
                        {
                            "criterion_id": "specialization",
                            "status": "supports",
                            "claim": "Studio A designs offices.",
                            "quote": "This quote is not in the article.",
                            "confidence": 0.9,
                        }
                    ],
                }
            ]
        },
        plan=plan,
        article={
            "url": "https://example.com/a",
            "title": "A",
            "content": "Studio A is mentioned.",
        },
    )

    assert extraction == []


def test_filter_articles_for_passed_evidence_keeps_only_supporting_urls():
    matrix = {
        "mode": "applied",
        "passed_candidates": ["Studio A"],
        "candidates": [
            {
                "name": "Studio A",
                "status": "passed",
                "evidence": [
                    {
                        "criterion_id": "specialization",
                        "status": "supports",
                        "url": "https://example.com/keep",
                    }
                ],
            },
            {
                "name": "Studio B",
                "status": "rejected",
                "evidence": [
                    {
                        "criterion_id": "specialization",
                        "status": "unclear",
                        "url": "https://example.com/drop",
                    }
                ],
            },
        ],
    }

    filtered = filter_articles_for_passed_evidence(
        [
            {"url": "https://example.com/keep", "title": "Keep", "content": "keep"},
            {"url": "https://example.com/drop", "title": "Drop", "content": "drop"},
        ],
        matrix,
    )

    assert [item["url"] for item in filtered] == ["https://example.com/keep"]


def test_parse_json_object_rejects_markdown_wrapped_json():
    try:
        parse_json_object('```json\n{"mode": "not_applicable"}\n```', "plan")
    except EvidenceMatrixError as exc:
        assert "invalid JSON" in str(exc)
    else:
        raise AssertionError("Expected EvidenceMatrixError")


def test_parse_json_object_rejects_non_standard_json_constants():
    for text in ['{"confidence": NaN}', '{"confidence": Infinity}']:
        try:
            parse_json_object(text, "extraction")
        except EvidenceMatrixError as exc:
            assert "invalid JSON" in str(exc)
        else:
            raise AssertionError(f"Expected EvidenceMatrixError for {text}")


def test_normalize_evidence_plan_rejects_conflicting_mode_and_task_type():
    try:
        normalize_evidence_plan(
            {
                "mode": "applied",
                "task_type": "general_research",
                "candidate_type": "studio",
                "requirements": [
                    {
                        "id": "specialization",
                        "label": "Specialization",
                        "description": "Relevant specialization",
                        "required": True,
                        "min_sources": 1,
                    }
                ],
            }
        )
    except EvidenceMatrixError as exc:
        assert "conflicts" in str(exc)
    else:
        raise AssertionError("Expected EvidenceMatrixError")


def test_normalize_evidence_plan_rejects_unexpected_fields():
    invalid_plans = [
        {
            "mode": "not_applicable",
            "task_type": "general_research",
            "candidate_type": "",
            "requirements": [],
            "extra": True,
        },
        {
            "mode": "applied",
            "task_type": "vendor_selection",
            "candidate_type": "studio",
            "requirements": [
                {
                    "id": "specialization",
                    "label": "Specialization",
                    "description": "Relevant specialization",
                    "required": True,
                    "min_sources": 1,
                    "extra": True,
                }
            ],
        },
    ]

    for plan in invalid_plans:
        try:
            normalize_evidence_plan(plan)
        except EvidenceMatrixError as exc:
            assert "unexpected" in str(exc)
        else:
            raise AssertionError(f"Expected EvidenceMatrixError for {plan!r}")


def test_normalize_evidence_plan_rejects_malformed_not_applicable_plan():
    invalid_plans = [
        {
            "mode": "not_applicable",
            "task_type": "general_research",
            "candidate_type": 123,
            "requirements": [],
        },
        {
            "mode": "not_applicable",
            "task_type": "general_research",
            "candidate_type": "",
            "requirements": "bad",
        },
        {
            "mode": "not_applicable",
            "task_type": "general_research",
            "candidate_type": "studio",
            "requirements": [],
        },
        {
            "mode": "not_applicable",
            "task_type": "general_research",
            "candidate_type": "",
            "requirements": [
                {
                    "id": "specialization",
                    "label": "Specialization",
                    "description": "Relevant specialization",
                    "required": True,
                    "min_sources": 1,
                }
            ],
        },
    ]

    for plan in invalid_plans:
        try:
            normalize_evidence_plan(plan)
        except EvidenceMatrixError:
            pass
        else:
            raise AssertionError(f"Expected EvidenceMatrixError for {plan!r}")


def test_normalize_evidence_plan_rejects_non_string_applied_fields():
    valid_requirement = {
        "id": "specialization",
        "label": "Specialization",
        "description": "Relevant specialization",
        "required": True,
        "min_sources": 1,
    }
    invalid_plans = [
        {
            "mode": "applied",
            "task_type": "vendor_selection",
            "candidate_type": 123,
            "requirements": [valid_requirement],
        },
        {
            "mode": "applied",
            "task_type": "vendor_selection",
            "candidate_type": "studio",
            "requirements": [{**valid_requirement, "label": 123}],
        },
        {
            "mode": "applied",
            "task_type": "vendor_selection",
            "candidate_type": "studio",
            "requirements": [{**valid_requirement, "description": 123}],
        },
    ]

    for plan in invalid_plans:
        try:
            normalize_evidence_plan(plan)
        except EvidenceMatrixError:
            pass
        else:
            raise AssertionError(f"Expected EvidenceMatrixError for {plan!r}")


def test_normalize_evidence_plan_rejects_optional_requirements():
    try:
        normalize_evidence_plan(
            {
                "mode": "applied",
                "task_type": "vendor_selection",
                "candidate_type": "studio",
                "requirements": [
                    {
                        "id": "nice_to_have",
                        "label": "Nice to have",
                        "description": "Optional criterion",
                        "required": False,
                        "min_sources": 1,
                    }
                ],
            }
        )
    except EvidenceMatrixError as exc:
        assert "requirements must be required" in str(exc)
    else:
        raise AssertionError("Expected EvidenceMatrixError")


def test_normalize_evidence_plan_rejects_missing_required_flag():
    try:
        normalize_evidence_plan(
            {
                "mode": "applied",
                "task_type": "vendor_selection",
                "candidate_type": "studio",
                "requirements": [
                    {
                        "id": "specialization",
                        "label": "Specialization",
                        "description": "Relevant specialization",
                        "min_sources": 1,
                    }
                ],
            }
        )
    except EvidenceMatrixError as exc:
        assert "requirements must be required" in str(exc)
    else:
        raise AssertionError("Expected EvidenceMatrixError")


def test_normalize_evidence_plan_rejects_invalid_min_sources():
    invalid_values = [None, "1", 0, 4]
    for invalid_value in invalid_values:
        try:
            normalize_evidence_plan(
                {
                    "mode": "applied",
                    "task_type": "vendor_selection",
                    "candidate_type": "studio",
                    "requirements": [
                        {
                            "id": "specialization",
                            "label": "Specialization",
                            "description": "Relevant specialization",
                            "required": True,
                            "min_sources": invalid_value,
                        }
                    ],
                }
            )
        except EvidenceMatrixError as exc:
            assert "min_sources" in str(exc)
        else:
            raise AssertionError(f"Expected EvidenceMatrixError for {invalid_value!r}")


def test_build_evidence_matrix_requires_distinct_source_urls_for_min_sources():
    plan = normalize_evidence_plan(
        {
            "mode": "applied",
            "task_type": "vendor_selection",
            "candidate_type": "studio",
            "requirements": [
                {
                    "id": "specialization",
                    "label": "Specialization",
                    "description": "Relevant specialization",
                    "required": True,
                    "min_sources": 2,
                }
            ],
        }
    )
    same_url_evidence = {
        "criterion_id": "specialization",
        "status": "supports",
        "claim": "Studio A designs offices.",
        "quote": "Studio A designs offices.",
        "confidence": 0.9,
        "url": "https://example.com/a",
        "title": "A",
        "language": "en",
    }

    same_url_matrix = build_evidence_matrix(
        plan,
        [
            {
                "name": "Studio A",
                "aliases": [],
                "evidence": [same_url_evidence, same_url_evidence],
            }
        ],
    )
    distinct_url_matrix = build_evidence_matrix(
        plan,
        [
            {
                "name": "Studio A",
                "aliases": [],
                "evidence": [
                    same_url_evidence,
                    {**same_url_evidence, "url": "https://example.com/b"},
                ],
            }
        ],
    )

    assert same_url_matrix["candidates"][0]["status"] == "rejected"
    assert same_url_matrix["candidates"][0]["missing_required"] == ["specialization"]
    assert distinct_url_matrix["candidates"][0]["status"] == "passed"


def test_build_evidence_matrix_merges_candidates_by_aliases():
    plan = normalize_evidence_plan(
        {
            "mode": "applied",
            "task_type": "vendor_selection",
            "candidate_type": "studio",
            "requirements": [
                {
                    "id": "specialization",
                    "label": "Specialization",
                    "description": "Relevant specialization",
                    "required": True,
                    "min_sources": 2,
                }
            ],
        }
    )
    evidence = {
        "criterion_id": "specialization",
        "status": "supports",
        "claim": "Studio A designs offices.",
        "quote": "Studio A designs offices.",
        "confidence": 0.9,
        "url": "https://example.com/a",
        "title": "A",
        "language": "en",
    }

    matrix = build_evidence_matrix(
        plan,
        [
            {
                "name": "Studio A LLC",
                "aliases": ["Studio A"],
                "evidence": [evidence],
            },
            {
                "name": "Studio A",
                "aliases": ["Studio A LLC"],
                "evidence": [{**evidence, "url": "https://example.com/b"}],
            },
        ],
    )

    assert len(matrix["candidates"]) == 1
    assert matrix["passed_candidates"] == ["Studio A LLC"]
    assert matrix["candidates"][0]["aliases"] == ["Studio A"]
    assert len(matrix["candidates"][0]["evidence"]) == 2


def test_build_evidence_matrix_supports_policy_with_contradicting_evidence():
    plan = normalize_evidence_plan(
        {
            "mode": "applied",
            "task_type": "vendor_selection",
            "candidate_type": "studio",
            "requirements": [
                {
                    "id": "specialization",
                    "label": "Specialization",
                    "description": "Relevant specialization",
                    "required": True,
                    "min_sources": 1,
                }
            ],
        }
    )
    support = {
        "criterion_id": "specialization",
        "status": "supports",
        "claim": "Studio A designs offices.",
        "quote": "Studio A designs offices.",
        "confidence": 0.9,
        "url": "https://example.com/a",
        "title": "A",
        "language": "en",
    }
    contradiction = {
        **support,
        "status": "contradicts",
        "claim": "Studio A does not design offices.",
        "quote": "Studio A does not design offices.",
        "url": "https://example.com/b",
    }

    matrix = build_evidence_matrix(
        plan,
        [
            {
                "name": "Studio A",
                "aliases": [],
                "evidence": [support, contradiction],
            }
        ],
    )

    assert matrix["candidates"][0]["status"] == "passed"
    assert matrix["candidates"][0]["missing_required"] == []


def test_evidence_extraction_rejects_invalid_structure():
    plan = normalize_evidence_plan(
        {
            "mode": "applied",
            "task_type": "vendor_selection",
            "candidate_type": "studio",
            "requirements": [
                {
                    "id": "specialization",
                    "label": "Specialization",
                    "description": "Relevant specialization",
                    "required": True,
                    "min_sources": 1,
                }
            ],
        }
    )

    try:
        normalize_evidence_extraction(
            {
                "candidates": [
                    {
                        "name": "Studio A",
                        "aliases": [],
                        "evidence": [
                            {
                                "criterion_id": "specialization",
                                "status": "maybe",
                                "claim": "Studio A designs offices.",
                                "quote": "Studio A designs offices.",
                                "confidence": 0.9,
                            }
                        ],
                    }
                ]
            },
            plan=plan,
            article={
                "url": "https://example.com/a",
                "title": "A",
                "content": "Studio A designs offices.",
            },
        )
    except EvidenceMatrixError as exc:
        assert "invalid status" in str(exc)
    else:
        raise AssertionError("Expected EvidenceMatrixError")


def test_evidence_extraction_rejects_unexpected_fields():
    plan = normalize_evidence_plan(
        {
            "mode": "applied",
            "task_type": "vendor_selection",
            "candidate_type": "studio",
            "requirements": [
                {
                    "id": "specialization",
                    "label": "Specialization",
                    "description": "Relevant specialization",
                    "required": True,
                    "min_sources": 1,
                }
            ],
        }
    )
    cases = [
        (
            {
                "candidates": [],
                "extra": True,
            },
            "unexpected",
        ),
        (
            {
                "candidates": [
                    {
                        "name": "Studio A",
                        "aliases": [],
                        "evidence": [],
                        "extra": True,
                    }
                ]
            },
            "unexpected",
        ),
        (
            {
                "candidates": [
                    {
                        "name": "Studio A",
                        "aliases": [],
                        "evidence": [
                            {
                                "criterion_id": "specialization",
                                "status": "supports",
                                "claim": "Studio A designs offices.",
                                "quote": "Studio A designs offices.",
                                "confidence": 0.9,
                                "extra": True,
                            }
                        ],
                    }
                ]
            },
            "unexpected",
        ),
    ]

    for extraction, expected_detail in cases:
        try:
            normalize_evidence_extraction(
                extraction,
                plan=plan,
                article={
                    "url": "https://example.com/a",
                    "title": "A",
                    "content": "Studio A designs offices.",
                },
            )
        except EvidenceMatrixError as exc:
            assert expected_detail in str(exc)
        else:
            raise AssertionError(f"Expected EvidenceMatrixError for {extraction!r}")


def test_evidence_extraction_rejects_normalized_criterion_id():
    plan = normalize_evidence_plan(
        {
            "mode": "applied",
            "task_type": "vendor_selection",
            "candidate_type": "studio",
            "requirements": [
                {
                    "id": "specialization",
                    "label": "Specialization",
                    "description": "Relevant specialization",
                    "required": True,
                    "min_sources": 1,
                }
            ],
        }
    )

    try:
        normalize_evidence_extraction(
            {
                "candidates": [
                    {
                        "name": "Studio A",
                        "aliases": [],
                        "evidence": [
                            {
                                "criterion_id": "Specialization",
                                "status": "supports",
                                "claim": "Studio A designs offices.",
                                "quote": "Studio A designs offices.",
                                "confidence": 0.9,
                            }
                        ],
                    }
                ]
            },
            plan=plan,
            article={
                "url": "https://example.com/a",
                "title": "A",
                "content": "Studio A designs offices.",
            },
        )
    except EvidenceMatrixError as exc:
        assert "unknown criterion_id" in str(exc)
    else:
        raise AssertionError("Expected EvidenceMatrixError")


def test_evidence_extraction_rejects_invalid_confidence():
    plan = normalize_evidence_plan(
        {
            "mode": "applied",
            "task_type": "vendor_selection",
            "candidate_type": "studio",
            "requirements": [
                {
                    "id": "specialization",
                    "label": "Specialization",
                    "description": "Relevant specialization",
                    "required": True,
                    "min_sources": 1,
                }
            ],
        }
    )

    try:
        normalize_evidence_extraction(
            {
                "candidates": [
                    {
                        "name": "Studio A",
                        "aliases": [],
                        "evidence": [
                            {
                                "criterion_id": "specialization",
                                "status": "supports",
                                "claim": "Studio A designs offices.",
                                "quote": "Studio A designs offices.",
                                "confidence": 2,
                            }
                        ],
                    }
                ]
            },
            plan=plan,
            article={
                "url": "https://example.com/a",
                "title": "A",
                "content": "Studio A designs offices.",
            },
        )
    except EvidenceMatrixError as exc:
        assert "confidence" in str(exc)
    else:
        raise AssertionError("Expected EvidenceMatrixError")


def test_evidence_extraction_validates_confidence_before_quote_filter():
    plan = normalize_evidence_plan(
        {
            "mode": "applied",
            "task_type": "vendor_selection",
            "candidate_type": "studio",
            "requirements": [
                {
                    "id": "specialization",
                    "label": "Specialization",
                    "description": "Relevant specialization",
                    "required": True,
                    "min_sources": 1,
                }
            ],
        }
    )

    try:
        normalize_evidence_extraction(
            {
                "candidates": [
                    {
                        "name": "Studio A",
                        "aliases": [],
                        "evidence": [
                            {
                                "criterion_id": "specialization",
                                "status": "supports",
                                "claim": "Studio A designs offices.",
                                "quote": "This quote is not in the article.",
                                "confidence": "high",
                            }
                        ],
                    }
                ]
            },
            plan=plan,
            article={
                "url": "https://example.com/a",
                "title": "A",
                "content": "Studio A is mentioned.",
            },
        )
    except EvidenceMatrixError as exc:
        assert "confidence" in str(exc)
    else:
        raise AssertionError("Expected EvidenceMatrixError")


def test_evidence_extraction_rejects_non_string_schema_fields():
    plan = normalize_evidence_plan(
        {
            "mode": "applied",
            "task_type": "vendor_selection",
            "candidate_type": "studio",
            "requirements": [
                {
                    "id": "specialization",
                    "label": "Specialization",
                    "description": "Relevant specialization",
                    "required": True,
                    "min_sources": 1,
                }
            ],
        }
    )
    article = {
        "url": "https://example.com/a",
        "title": "A",
        "content": "Studio A designs offices.",
    }
    valid_candidate = {
        "name": "Studio A",
        "aliases": [],
        "evidence": [
            {
                "criterion_id": "specialization",
                "status": "supports",
                "claim": "Studio A designs offices.",
                "quote": "Studio A designs offices.",
                "confidence": 0.9,
            }
        ],
    }
    invalid_candidates = [
        ({**valid_candidate, "name": 123}, "candidate name"),
        ({key: value for key, value in valid_candidate.items() if key != "aliases"}, "aliases"),
        ({**valid_candidate, "aliases": "Studio"}, "aliases"),
        ({**valid_candidate, "aliases": [123]}, "aliases"),
        (
            {
                **valid_candidate,
                "evidence": [
                    {
                        **valid_candidate["evidence"][0],
                        "claim": 123,
                    }
                ],
            },
            "claim",
        ),
        (
            {
                **valid_candidate,
                "evidence": [
                    {
                        **valid_candidate["evidence"][0],
                        "quote": 123,
                    }
                ],
            },
            "quote",
        ),
    ]

    for candidate, expected_detail in invalid_candidates:
        try:
            normalize_evidence_extraction({"candidates": [candidate]}, plan=plan, article=article)
        except EvidenceMatrixError as exc:
            assert expected_detail in str(exc)
        else:
            raise AssertionError(f"Expected EvidenceMatrixError for {expected_detail}")
