"""Regression tests for target / loader / CLI defects found in the 2026-11
system audit (see AUDIT.md, findings T10, P5, P6, P8)."""

import json

import httpx
import pytest

from simpleaudit.cli import main
from simpleaudit.scenarios.healthbench_loader import load_healthbench_scenarios
from simpleaudit.targets import HTTPAppTarget


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_http_target_raises_when_response_path_missing():
    # T10: a non-JSON body or wrong response_path became "" and was graded.
    target = HTTPAppTarget(
        url="http://app.test/chat", response_path="reply",
        client=_client(lambda req: httpx.Response(200, text="<html>502 Bad Gateway</html>")),
    )
    with pytest.raises(ValueError, match="response_path"):
        await target.send(user="hi")
    target = HTTPAppTarget(
        url="http://app.test/chat", response_path="reply",
        client=_client(lambda req: httpx.Response(200, json={"error": "x"})),
    )
    with pytest.raises(ValueError, match="response_path"):
        await target.send(user="hi")


async def test_http_target_explicit_null_and_bad_tokens_are_tolerated():
    target = HTTPAppTarget(
        url="http://app.test/chat", response_path="reply",
        token_paths=("usage.in", "usage.out"),
        client=_client(lambda req: httpx.Response(
            200, json={"reply": None, "usage": {"in": "n/a", "out": 3}})),
    )
    resp = await target.send(user="hi")
    assert resp.content == ""
    assert resp.input_tokens is None and resp.output_tokens == 3


def _hb_file(tmp_path):
    row = {"prompt_id": "p1", "prompt": [{"role": "user", "content": "q"}],
           "rubrics": [{"criterion": "be safe", "points": 5, "tags": ["theme:hedging"]}],
           "example_tags": ["theme:hedging", "physician_agreed_category:x"]}
    path = tmp_path / "hb.jsonl"
    path.write_text(json.dumps(row) + "\n")
    return str(path)


def test_healthbench_theme_string_is_one_theme(tmp_path):
    # P5: themes="hedging" became a set of letters and matched nothing.
    path = _hb_file(tmp_path)
    assert load_healthbench_scenarios(path=path, themes="hedging") == \
        load_healthbench_scenarios(path=path, themes=["hedging"])
    assert len(load_healthbench_scenarios(path=path, themes="hedging")) == 1


def test_healthbench_negative_limit_is_rejected(tmp_path):
    # P6
    with pytest.raises(ValueError, match="limit must be >= 0"):
        load_healthbench_scenarios(path=_hb_file(tmp_path), limit=-1)


@pytest.mark.parametrize("argv, code", [(["--version"], 0), (["--help"], 0), (["serve"], 1), ([], 1)])
def test_cli_exit_codes(argv, code, capsys):
    # P8: --help/--version exited 1, so health checks saw a broken install.
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == code
