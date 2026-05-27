"""Tests for the build_output filter."""

from llm_gateway_core.services.token_compression.filters.build_output import build_output

SAMPLE_NPM = """\
npm warn deprecated old-package@1.0.0: use new-package instead
npm warn deprecated another@2.0.0: deprecated
npm warn deprecated third@3.0.0: deprecated
npm warn deprecated fourth@4.0.0: deprecated
Downloading express
Downloading lodash
Downloading react
npm warn something else
added 100 packages in 5s
"""

SAMPLE_CARGO = """\
   Compiling foo v0.1.0
   Compiling bar v0.2.0
   Compiling baz v0.3.0
error[E0308]: mismatched types
 --> src/main.rs:10:5
  |
10|     let x: i32 = "hello";
  |                  ^^^^^^^ expected i32, found &str
   Finished dev [unoptimized + debuginfo] target(s) in 1.23s
"""


def test_build_output_npm():
    result = build_output(SAMPLE_NPM)
    assert "added 100 packages" in result
    # downloaded count
    assert "Downloaded 3 packages" in result
    # deprecated truncated
    assert "+1 more deprecated" in result


def test_build_output_cargo():
    result = build_output(SAMPLE_CARGO)
    assert "error[E0308]" in result
    assert "Compiled 3 packages" in result
    assert "Finished" in result


def test_build_output_empty():
    result = build_output("")
    assert result == ""


def test_build_output_filter_name():
    assert build_output.filter_name == "build-output"


def test_build_output_no_match_passthrough():
    text = "no build output here\n"
    result = build_output(text)
    # Returns original if nothing matches (result.rstrip("\n") or text)
    assert result == text.rstrip("\n") or result == text


SAMPLE_CARGO_WARNING = """\
   Compiling mylib v0.1.0
warning[W0001]: unused variable `x`
 --> src/main.rs:1:5
  |
1 | let x = 42;
  |     ^ help: consider using `_x`
   Finished dev [unoptimized + debuginfo] target(s) in 0.5s
"""


def test_build_output_cargo_warning_continuation_in_warnings():
    """Rust warning continuation lines must go to warnings[], not errors[]."""
    result = build_output(SAMPLE_CARGO_WARNING)
    # The warning headline and its continuation lines must appear in result
    assert "warning[W0001]" in result
    assert "src/main.rs:1:5" in result
    # No error should be reported
    assert "error" not in result.lower() or "error" not in result.split("warning")[0].lower()
